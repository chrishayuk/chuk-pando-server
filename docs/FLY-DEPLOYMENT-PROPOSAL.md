# Fly.io deployment: proposal for approval (2026-11-07). Nothing is created until it is approved.

This turns `docs/FLY-DEPLOYMENT-PLAN.md` (background, Tigris verification, pre-deployment gate) into a concrete,
priced configuration. Region: **London (`lhr`)**, on the owner's Fly account.

## 1. Topology and exact configuration
| App | Machines | Size | Storage | Network exposure |
|---|---|---|---|---|
| `pando-meta` | 1 | shared-cpu-1x, 512 MB | Fly Volume `pando_meta`, 1 GB, at `/data` (WAL and snapshots) | **none public**: no `[http_service]`, no public IP; binds only to its 6PN address, port 7000 |
| `pando-git` | 2 (frontend A and frontend B) | shared-cpu-2x, 1 GB (demo); **performance-1x, 2 GB during measurement windows** | Machine root disk only (a private, disposable cache, wiped at start) | public HTTPS at `pando-git.fly.dev` (TLS at the Fly edge, shared IPv4 plus IPv6), port 8080 inside |
| `pando-load` | 2, only during measurement windows, destroyed afterwards | performance-2x, 4 GB | none | none public; reaches the frontends' 6PN addresses directly (no edge) |
| Tigris bucket `pando-packs` | — | — | object store, authoritative for packs | HTTPS with SigV4, from the frontends only |

- **Host separation.** Frontends A and B are created with separate `fly machine run` placements. Separation is
  recorded from whatever host or zone evidence Fly exposes. If it can't be confirmed, results say **"two Machines"**.
- **Why performance cores during measurement.** Shared-CPU Machines are throttled once their burst budget is spent,
  which would make a "1 vs 2 frontends" comparison measure throttling. Sizes are switched with `fly machine update`
  outside the windows.
- **The metadata service remains a single point of failure.** It runs as one process on one volume, and a volume
  lives on one host. Fly volume snapshots are daily (the default); losing the host can lose up to a day of
  metadata. **No HA or durability claim is made for this deployment.**

## 2. Secrets and authentication boundaries
| Secret | Set on | Purpose |
|---|---|---|
| `PANDO_META_SECRET` (32 random bytes) | `pando-meta`, `pando-git`, `pando-load` | HMAC challenge-response before any metadata request. The service refuses to start without it. |
| `PANDO_AUTH` (`user:password`, generated) | `pando-git` | HTTP Basic on every Git, admin and stats endpoint. One shared credential for the owner and the test clients. |
| `PANDO_URI_SECRET` (32 random bytes) | `pando-git` | Signs packfile URIs (`/_cache/...?exp=&sig=`); a URI is valid for 1 hour and only for its path. |
| `PANDO_S3_KEY`, `PANDO_S3_SECRET` | `pando-git` | Tigris access key, scoped to `pando-packs` only. |

**Boundaries:**
- **Internet to the frontends:** HTTPS at the edge, plus Basic auth or a signed URI.
- **Frontends to the metadata service:** 6PN (WireGuard between hosts), plus the shared-secret handshake. It is not
  mutually authenticated beyond that secret, and is documented as such.
- **Frontends to Tigris:** HTTPS, with certificates verified against the webpki roots, plus SigV4.
- All secrets are generated locally, set with `fly secrets set` (never in files or command history) and listed only
  by name. **Not provided:** per-user accounts, per-repository authorisation, rate limiting.

## 3. Storage assumptions (what the server relies on)
- **Tigris conditional create.** `If-None-Match: *` gives 412 for an existing key, and is exclusive under concurrency.
  *Verified* against Tigris on 2026-11-07: 1 × 200 and 19 × 412 out of 20 concurrent creates. After the F2
  remediation, a 412 leads to a GET of the stored object and a content check before any acknowledgement.
- **Tigris read-after-write.** Strong within a region, per Tigris's documentation. Verified from one client; to be
  re-checked from both frontends (acceptance test T3).
- **Tigris durability:** as provided. Tigris documents redundant storage on FoundationDB but gives no figure. **Not
  measured, not claimed.**
- **Volume durability:** a single host-attached NVMe volume plus daily snapshots. The WAL is fsynced, which protects
  against process crashes, not against losing the host.

## 4. Cost (Fly's published prices fetched 2026-11-07, for the `iad`/`ewr` reference regions; London's published regional markup is **×1.135** (Fly pricing page data, 2026-11-07). Tigris's higher of its two published tables is used.)
**Always-on demo (per month):**

| Item | Reference price | London (×1.135) |
|---|---|---|
| `pando-meta`, shared-cpu-1x 512 MB | $3.69 | $4.19 |
| Volume, 1 GB (+ snapshots, first 10 GB free) | $0.15 | $0.15 |
| 2 × `pando-git`, shared-cpu-2x 1 GB (derived from Fly's per-CPU and per-GB components) | $14.76 | $16.75 |
| Tigris: under 5 GB stored; requests in the thousands | free tier | ~$0 |
| Egress at $0.02/GB (only the owner's clones) | <$0.10 | <$0.10 |
| **Total** | **≈ $19/month** | **≈ $21/month** |

**Measurement windows (estimate: 10 hours in total over the experiment):**
- 2 frontends upgraded to performance-1x: about $1.
- 2 load Machines, performance-2x: about $2.
- Tigris requests (about 50k PUT, 200k GET, at $0.01 per 1k Class A): about $0.60.

**Experiment total, if everything is destroyed after two weeks:** about **$15–20**. **Upper bound** if it's left
running for a month with measurements: **$30**.

**Teardown:** `fly apps destroy` for the three apps, `fly storage destroy` for the bucket, and the volume and its
snapshots are deleted with the app. This is scripted as `deploy/fly/teardown.sh`, to be written before creation.

## 5. Cross-host acceptance tests (registered; all must pass before any measurement)
| # | Test | Pass condition |
|---|---|---|
| T1 | From the owner's machine, stock Git through `https://pando-git.fly.dev`: push, clone, incremental fetch, **with packfile URIs**, plus a fork-client URI clone | fsck-clean at the tip; `fetch_served.uri_packs` non-empty; URI downloads succeed through the edge; an expired or tampered URI gets 403 |
| T2 | Metadata service private | `fly ips list -a pando-meta` is empty; a 6PN connection without the secret is refused at the handshake; with the secret it works |
| T3 | Read-after-write across Machines | push through A (direct 6PN address), then immediately clone through B with a cold cache, 50 times: every clone at the acknowledged tip, fsck-clean, objects downloaded and verified from Tigris |
| T4 | Same-ref compare-and-swap across Machines | the D1 workload from `pando-load`: exactly one winner per round, no lost update |
| T5 | Frontend restart | restart B during a push stream through A: no acknowledged push lost; B serves everything after restart |
| T6 | Metadata service restart | pushes fail without acknowledgement while it is down; both frontends recover; background maintenance resumes without a further push |
| T7 | Object-store faults (test relay, `verify/fault/s3relay.py`, on frontend B only, through `PANDO_S3_CONNECT`) | stall: B's pushes fail within the bound, nothing acknowledged, recovery when resumed; refuse: fails fast; delay 50–200 ms: pushes succeed. Frontend A, connected directly, is unaffected throughout |
| T8 | Causal timeline | `verify/trace` timeline built from both frontends' events for one push on A and a fetch on B; joins on push id, pack id and LSN with no gaps |

The measurements themselves (1 vs 2 frontends, 8/16/32 writers, at least 3 repetitions, clients not saturated) are
registered separately, before the first run, as the plan requires. A scaling claim needs 2 frontends to exceed 1
beyond run-to-run variance.

## 6. Approval requested
1. The configuration in §1, in `lhr`.
2. A cost ceiling of **$30**, with teardown after the measurements (or an always-on demo at about $23 a month, if
   you want it kept for the video).
3. Permission to create the three apps, the volume and the Tigris bucket on your account. Run order:
   create → T1–T8 → registered measurements → teardown.
