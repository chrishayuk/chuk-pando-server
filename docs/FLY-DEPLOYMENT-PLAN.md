# Two-Machine Fly.io deployment: plan (DRAFT, 2026-11-07; not deployed)

**Goal (owner):** two Git frontend Machines on separate hosts, a network-reachable metadata service, and S3-compatible
object storage. This is the first environment in which multi-host capacity can be measured.

**Nothing is deployed until:**
1. the independent re-audit (2026-11-07; `verify/audit/archive/net/AUDIT.md`) has reported, and its blockers are resolved;
2. the storage and security checks below have been run against the real services;
3. the owner provides the Fly organisation and credentials, and approves the cost.

The local deployments stay as the correctness reference.

## Pre-deployment gate (owner direction, 2026-11-07, after the re-audit)
**No paid Fly resources are created until every item below holds, and the owner approves the cost.**

1. **Remediation complete.**
   - Audit findings F1–F10 and F12 are fixed, each with a test that fails on the audited code and passes now, the
     auditor's own reproducers included.
   - The full regression passes the repaired gate (`verify/run_suites.sh`, with `verify/gate/`).
   - See `docs/AUDIT-NET-REMEDIATION.md`.
2. **Git clients reach the packfile URI endpoint.**
   - Clients use `https://<app>.fly.dev/_cache/...`: signed, expiring, and served through the same Fly edge and port
     as Git.
   - Before any measurement, from the owner's machine with stock Git: a URI clone and an incremental URI fetch, both
     fsck-clean, with `fetch_served.uri_packs` non-empty.
   - Also check that an expired or tampered URI is refused (403) through the edge, not only locally.
3. **The metadata service is reachable only on the private network.**
   - The `pando-meta` app has **no** `[http_service]` or `[[services]]` section, so Fly allocates no public address.
   - Independently of that, it binds only to its 6PN address (`fly-local-6pn`; `deploy/fly/entrypoint.sh` refuses to
     start if that does not resolve).
   - `PANDO_META_SECRET` is set on both apps, as a Fly secret; the service refuses to start without it.
   - Verified before load: from outside, the app has no public IP (`fly ips list -a pando-meta` is empty); from a
     6PN Machine, a connection without the secret is refused at the handshake.
   - This reduces the risk; it is not a substitute for mutual authentication, and it remains a single point of
     failure (G5).
4. **The object store does what the server depends on.**
   - Conditional create (412) and concurrent-create exclusivity: verified against Tigris (below).
   - Since F2, a 412 downloads the stored object to verify it. On Tigris that is one extra GET, only when the same
     object is published twice (retries, duplicate background work).
   - Durability is as provided by Tigris and is not claimed as measured.
5. **Cross-host fault tests on the deployment** (registered with the measurements; clients on separate Machines):
   - **cache miss on the other Machine:** push through A; clone and fetch through B with a cold cache; the objects come
     from Tigris, verified;
   - **frontend restart:** restart B (its cache is emptied) during a push stream through A; no acknowledged push is
     lost; B serves everything after restart;
   - **metadata service restart:** restart `pando-meta`; pushes fail without acknowledgement while it is down; both
     frontends recover; background maintenance resumes without a further push (F1);
   - **object-store outage:** pushes fail within the bounded wait (F6), are not acknowledged, and recover. Tigris
     cannot be stopped, so this needs either a pausable TCP pass-through on the Machine (proposed small change: a
     `PANDO_S3_CONNECT` address override, so TLS still terminates at Tigris) or revoking and restoring the access
     key, which tests the 403 path, not a stall.

## Proposed topology
```
            WireGuard (fly proxy / wireguard peer) — operator and test clients only, no public Git
                         │
   ┌─────────── Fly private network (6PN, org-scoped) ───────────┐
   │  frontend-a (Machine, region R)   frontend-b (Machine, region R, different host)
   │      Git HTTP :8080, URI cache        Git HTTP :8080, URI cache
   │             │  metadata RPC (tcp, 6PN)       │
   │             └──────────► meta (Machine + Fly Volume) ◄──────┘
   │                               single process, WAL on the volume
   └───────────────────────────────────────────────────────────────┘
                         │ HTTPS (SigV4)
                    Tigris bucket (S3-compatible)
```

## Object storage: Tigris (Fly's integrated S3-compatible store)
What the provider documents (checked 2026-11-07; **to be verified by experiment, not assumed**):
- **Conditional create:** `If-None-Match: "*"` writes only if the object does not exist; otherwise
  `412 Precondition Failed`. Conditional operations "always evaluate against the latest state".
- **Consistency:**
  - strong read-after-write within a region, for every bucket type;
  - across regions, strong only for **Single-region** and **Multi-region** buckets; Global and Dual-region buckets are
    eventual (typically sub-second).

  Plan: a **Single-region bucket** in the frontends' region.
- **Durability:** Tigris is described as built on FoundationDB, with data stored redundantly. No formal durability
  figure was found. The deployment's durability claims will therefore be "as provided by Tigris", not measured.
- Endpoint `https://t3.storage.dev` (the legacy `fly.storage.tigris.dev` also resolves), region `auto`.

**Experiments to run before deployment** (scripted from `verify/net`, against a throw-away bucket):
- a second create of one key returns 412 with the original content intact;
- 20 concurrent creates of one key: exactly one 200;
- read-after-write from both frontend Machines;
- the behaviour of a large-body create-only PUT on an existing key (MinIO answers before reading the body; the client
  handles this, but Tigris's behaviour must be observed);
- error codes for a wrong signature, region and time skew.

## Code gaps that must be closed first (found while planning)
| # | Gap | Why it matters on Fly | Proposed change |
|---|---|---|---|
| G1 | **No TLS in the S3 client** (`server/src/s3.rs` is plain HTTP) | Tigris requires HTTPS | Needs TLS: either a vetted crate (`rustls` is not in the offline crate cache, so this needs the owner's approval for new dependencies), or a TLS-terminating sidecar on each Machine with the client talking HTTP to localhost. Recommendation: `rustls`, since a sidecar adds a process to every failure analysis. |
| G2 | **Packfile URIs advertise `127.0.0.1:<port>`** cache nodes | Remote clients cannot reach them, so URI fetches would fail | A configured externally reachable base URL per frontend. On Fly, the URI cache can be served through the frontend's own HTTP port. |
| G3 | **The metadata service has no authentication** (documented) | 6PN is private to the organisation, but any Machine in it could commit | A shared-secret handshake (HMAC challenge) before any request; the secret from Fly secrets. 6PN traffic is already encrypted between hosts. |
| G4 | **No authentication on Git push or `/_admin/*`; `/_stats` is open** | Must not be public | *Revised 2026-11-07:* public HTTPS (Fly edge) with HTTP Basic on all Git/admin/stats endpoints and signed packfile URIs; see below |
| G5 | Single metadata process on one Fly Volume | A volume lives on one host; host loss means downtime and possibly data loss up to the last snapshot | Accepted for the demo and documented as the single point of failure. No HA claim. |

## Placement
- Two frontend Machines in **one region** (latency to the metadata service and Tigris), on different hosts.
- Fly does not expose host identity to the Machine. Separation is requested through placement, and recorded with
  whatever evidence the platform gives (Machine host or zone metadata, if any).
- **If separation cannot be confirmed, results are reported as "two Machines", not "two hosts".**
- Load generators run on separate Machines in the same region, so clients do not compete with the frontends for CPU
  (the bottleneck on the local host).

## Registered measurements (to be frozen in a separate registration before running)
- push throughput and latency with 1 vs 2 frontends at 8, 16 and 32 writers on distinct branches, clients on separate
  Machines;
- the D1/D2/D5/I3 correctness tests re-run against the Fly deployment (same scripts, remote endpoints);
- per-push breakdown (`verify/net/profile.py`), so that object-store latency is not mistaken for a scaling effect.

**A scaling claim requires:** 2 frontends exceeding 1 frontend's throughput beyond run-to-run variance (≥ 3
repetitions), with clients not saturated.

## Owner decisions (2026-11-07)
1. The owner's Fly account (organisation `personal`), region **London (`lhr`)**.
2. G1: **`rustls` approved.**
3. Load-generating clients run as **separate Fly Machines**.
4. **New requirement:** for the video, the owner must connect from their own machine with a Git client. G4 is therefore
   revised: the frontends are exposed on Fly's public HTTPS edge (`https://<app>.fly.dev`, TLS terminated by Fly), with
   access control, instead of private-only.

## Implementation of G1–G4 (branch `p10-fly`; tested locally in the planned configuration, `verify/fly/test_access.py`)
- **G1 TLS:** `rustls` 0.23.46 (ring 0.17.14) with the webpki-roots 1.0.11 Mozilla root store; `PANDO_S3=https://...`.
  `PANDO_S3_CA` adds a CA, which the local tests use with a throw-away CA for MinIO. Certificates are always verified;
  `rustls` correctly rejected a self-signed CA used as an end entity.
- **G2 public packfile URIs:** `PANDO_PUBLIC_URL`. URIs are `<base>/_cache/packs/<repo>/<id>.pack`, served on the
  frontend's own port.
- **Signed URIs:** `PANDO_URI_SECRET` adds `exp` and `sig` = HMAC-SHA256(secret, path ‖ exp); `PANDO_URI_TTL` sets the
  lifetime (default 3600 s). A valid unexpired signature is required and sufficient for `/_cache/`, so Git clients
  fetch URIs without credentials, but URIs cannot be forged, retargeted to another pack, or reused after expiry.
- **G3 metadata handshake:** `PANDO_META_SECRET` on the service and the frontends. The service sends a 32-byte nonce
  per connection; the client must answer with HMAC(secret, nonce) before any request is served.

  **Defect found by the test:** the frontend's startup ping accepted any reply, so a frontend with no secret
  "started" and failed later. The ping now requires the exact reply.
- **G4 frontend access:** `PANDO_AUTH=user:password` requires HTTP Basic (constant-time compare) on every Git
  endpoint, on `/_admin/*` and on `/_stats`. `PANDO_BIND` sets the listen address (default loopback).
- **Tests** (`verify/fly/test_access.py`, 5/5), in tcp + HTTPS-MinIO mode with all secrets set:
  - unauthenticated and wrong-password requests get 401; admin without credentials gets 401;
  - authenticated push and clone through both frontends, stock and OpenAI-fork clients, with and without URIs;
  - URIs used are signed; tampered, unsigned and retargeted URIs get 403; a real URI with a 1 s lifetime is accepted
    at once and refused after expiry;
  - a frontend with a wrong or missing metadata secret cannot start.

  Unit tests: base64 (RFC 4648 vectors) and signing.
- **Not done:** per-user accounts, authorisation per repository, rate limiting. One shared credential for the demo,
  stated as such.

Sources consulted: Tigris docs (conditional operations, consistency), Fly.io docs (private networking, Flycast).

## Tigris verification (2026-11-07; throw-away bucket on the owner's account, destroyed afterwards)
Endpoint `https://fly.storage.tigris.dev`, region `auto`, default (Global) bucket, requests from the local host.
**curl probes:**
- create-only `PUT` → 200, then **412** on a second create; the original content was kept;
- **20 concurrent creates** of one key: **1 × 200, 19 × 412**;
- 8 MB create-only `PUT` on an existing key: **412**, original content intact. Unlike MinIO, Tigris read the whole body
  first (~10 s from here); the client handles both behaviours;
- wrong secret → 403; missing key → 404.

**The server against Tigris** (two local frontends, TCP metadata, HTTPS via `rustls`):
- 4 pushes alternating between frontends;
- the same pack published concurrently by both frontends: one created the 2 objects, the other got 2 × 412
  (verified); both pushes were acknowledged;
- frontend B restarted (empty cache): inline and URI clones fsck-clean at the tip, 9 verified downloads, 0 retries.

**Not verified:** Tigris's durability, which is as provided; behaviour from inside Fly (expected to be same-region);
Single-region bucket creation (`fly storage create` offers no location type; the default Global bucket is strongly
consistent within its region).

**Incident:** during the wrong-secret probe, curl's password prompt printed the probe bucket's access-key *id*, not the
secret. The bucket and its key were destroyed immediately afterwards.
