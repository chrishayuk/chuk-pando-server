# Network-capable deployment: TCP metadata transport and S3-compatible pack storage (REGISTERED before implementation, 2026-11-07)

Owner direction (2026-11-07):
- build and validate locally first; no second host is required yet;
- MinIO as the development object store, after verifying conditional create-only publication;
- no claim of horizontal capacity scaling until tested on independent hosts;
- the metadata service remains a single point of failure and is documented as one.

The local two-frontend deployment (Unix socket, shared directory) is kept unchanged as the correctness reference.

## Object store verified before design
MinIO `RELEASE.2025-10-15T17-29-55Z` (Homebrew bottle, AGPLv3, run as a separate process and never linked; noted for the
licence review), single node, single drive, local only. Checked with `curl --aws-sigv4`:
- `PUT` with `If-None-Match: *` returns 200, then **412** on the second attempt, and the original content is kept;
- **20 concurrent** conditional `PUT`s of one key with different bodies: exactly **1 × 200, 19 × 412**.

Durability of a single-drive MinIO is MinIO's, not ours. That is an assumption of this deployment, not something tested.

## 1. TCP metadata transport
- `pando meta-serve --listen tcp:HOST:PORT` (or `--sock PATH`).
- `PANDO_META=tcp:HOST:PORT` or a socket path.
- Same frames and same semantics: OCC commit replied only after WAL durability, the change log, the stable LSN.
- **No authentication or TLS.** It is for a trusted local network only, and this is stated.
- Connections are pooled. Reads are retried for up to 10 s; a commit whose reply is lost is reported as an unknown
  outcome and never acknowledged.

## 2. S3-compatible pack storage (`PANDO_S3=http://HOST:PORT/BUCKET`, `PANDO_S3_KEY`, `PANDO_S3_SECRET`)
- **The object store is authoritative.** Each frontend keeps a private local cache in today's layout under its own data
  directory. It is never shared, so no cross-process locks or links are involved.
- **Publication** (`install`, before the metadata transaction):
  - order: pack, then index (`.pidx`), each `PUT` with `If-None-Match: *`; `install_alt` likewise.
  - **412** means already published. The stored object is downloaded and must be equivalent to ours before
    publication continues (and so before any metadata commit). Nothing is ever overwritten.
    - pack: byte-identical. Its name is its SHA-1, so any other bytes are wrong.
    - index: semantically equivalent (`PackIndex::equivalent_to`). Everything derived from the pack bytes must be
      identical: entries, sizes, delta bases, prerequisites and tags. The one legitimate difference is the pack an
      external delta base was found in; that pack's index must then contain the base.
    - substitution map (`.alt`): identical, or every entry valid. Two frontends may pick different L0 copies of a
      duplicated object.

    A mismatch fails the push and nothing is acknowledged. *Corrected 2026-11-07.* The independent re-audit (F2)
    showed that the earlier check (size, plus the trailer for packs) let a push be acknowledged against different
    stored bytes, which another frontend then could not serve. See docs/AUDIT-NET-REMEDIATION.md.
  - A crash between the pack and index PUTs leaves an unreferenced pack. A retried push re-verifies it and completes.
    Metadata is never committed before both objects exist.
- **Reads:** a cache miss GETs the object into local tmp, **verifies it**, and moves it into the cache
  (create-only link). Verification:
  - pack: SHA-1 of the content equals its trailer equals the id, and the size matches the index (the size
    comparison was missing until 2026-11-07, audit F9);
  - index: parses (internal checksum), and its id matches;
  - substitution map: parses, and every entry names an object of the cover and an L0 entry that stores that object
    as a delta. An invalid map is never used (the object index is used instead). An unreachable store is not
    remembered as "no map". Until 2026-11-07, maps were not verified (audit F9).

  A failed verification is an error. The object is not cached and not served.
- **Request signing:** AWS SigV4 with the payload's SHA-256, so the store checks the body. It is implemented in-tree:
  SHA-256 and HMAC, tested against FIPS 180-4 / RFC 4231 vectors and the AWS SigV4 example. The offline build has no
  crypto crates. Plain HTTP/1.1 over TCP.

## Required properties → tests (`verify/net/`)
| # | Property | Test |
|---|---|---|
| N1 | Same semantics over TCP | the dist suite D1–D8, I3 rerun with `tcp:` transport + S3 storage (S3 mode skips the 2 tests that inspect a shared tmp directory, which no longer exists) |
| N2 | Create-only publication | two frontends push the same pack at the same moment (identical content to two branches): both succeed, the store holds one copy, and the second publisher logged `already_published`. Direct check: a `PUT` without the condition by the test (simulating a faulty writer) **after** publication is detected on a cold read (verification failure; not served) |
| N3 | Completeness and crash recovery | test hook `PANDO_TEST_ABORT_AFTER_PACK_PUT=1`: the frontend aborts between pack and index PUT. No ref is published and the push is not acknowledged. After restart, the same push succeeds (verified 412 path), and clones through both frontends are fsck-clean |
| N4 | Store unavailable | MinIO stopped: pushes fail and are not acknowledged. Fetches of already-cached packs still work. After restart, pushes succeed |
| N5 | Cold cache | a third frontend with an empty cache serves a clone of history pushed through A and B (all packs fetched from S3 and verified) |
| N6 | No full rescans | I2 measurement repeated in TCP+S3 mode: refresh keys read constant in pack count |
| N7 | Existing behaviour | full regression (single-process mode) and the dist suite in the reference mode |

## Measurements (registered; reported, not pass/fail)
- push latency, 8 and 16 writers, in three modes: reference (unix + shared directory), tcp + shared directory, tcp + S3;
- metadata RPC round trip over TCP;
- cold-cache clone time and bytes fetched from S3.

All on one host: the absolute numbers are only for comparison, with no scaling claim.

## Implementation notes (2026-11-07)
- `meta-serve --listen tcp:HOST:PORT`; `PANDO_META=tcp:...`. The same frames as the Unix socket, with Nagle disabled.
- The S3 client is `server/src/s3.rs`, with SHA-256, HMAC-SHA256 and SigV4 in-tree. Unit tests pass: FIPS 180-4 vectors
  (including 1,000,000 × "a"), RFC 4231 case 2, the AWS documentation's GET-object signature example, and calendar
  conversion.
- **Defect found and fixed during N1:** MinIO answers a create-only `PUT` of an existing key with 412 *before reading
  the body* and closes the connection. The client failed while still writing the body, never read the 412, and retried
  16 times. It now reads the response even after a failed body write. The fault was visible only when two frontends'
  maintenance published the same covers.
- All object-store operations retry for up to ~10 s on transport errors or 5xx. That is safe because a create-only PUT
  whose success was lost is resolved by the 412-and-verify path. Retries and give-ups are counted in `/_stats`
  (`object_store`).

## Results (2026-11-07)
**Tests:**
- **N1:** the dist suite in tcp-s3 mode: 10 pass, 1 skipped by design (no shared tmp directory exists). The reference
  mode is unchanged.
- **N2, concurrent publication:** the same pack published by both frontends at once. Exactly 2 objects created in total
  (`.pack` and `.pidx`, by one frontend); the other frontend found both present (2 × 412, verified).
- **N2, overwrite behind our back:** a faulty unconditional PUT replaced a published pack. A cold frontend **refused**
  it ("failed verification"), clones through it failed, and nothing was cached. The warm frontend kept serving its
  verified copy.
- **N3, abort between pack and index upload:** the push was not acknowledged and the ref did not move; the store held
  `.pack` (200) and no `.pidx` (404). The retried push through B created the index (`put_exists` 1, `put_created` 1).
  Clones through both frontends are fsck-clean at the new tip.
- **N4, store down:** the push failed after 10.5 s (the retry window) and was not acknowledged. A clone of
  already-cached history through A worked. After the store restarted, pushes succeed.
- **N5, cold cache:** a new frontend served inline and URI clones of 12 versions, with 12 verified downloads.
- **N6, refresh work in tcp-s3 mode** (`incr-tcp-s3-e575c25.json`): 6, 7 and 7 keys scanned at 64, 256 and 1024 packs;
  constant.

**Measurements** (`net-e575c25.json`; host load 5–10 from other projects; 2 frontends):
| | unix + shared dir (reference) | tcp + shared dir | tcp + MinIO |
|---|---|---|---|
| 8 writers: pushes/s, median / p95 | 34.6, 140 / 161 ms | 34.4, 144 / 174 ms | 21.4, 319 / 352 ms |
| 16 writers: pushes/s, median / p95 | 48.0, 191 / 222 ms | 47.7, 207 / 238 ms | **16.9, 727 / 1,551 ms** |
| metadata RPC round trip | (Unix 15 µs earlier) | 38.8 µs | 29.0 µs |

- **TCP costs little:** throughput is within 1 %.
- **MinIO mode is much slower, and degrades with concurrency.** Measured separately on this host, MinIO takes 9.0 ms
  per sequential 4 KB create-only PUT; with 16 concurrent clients, 16.6 ms median and 875 PUT/s.

  A push does at least 2 synchronous PUTs, plus the background copy's 2 PUTs, plus GETs whenever the receiving frontend
  lacks the base pack (writers alternate frontends). Those account for part of the gap, but **the 16-writer
  degradation is not yet profiled.**

  Candidates, not tested:
  - one TCP connection per request (no keep-alive);
  - per-request SHA-256 of the body in the in-tree implementation;
  - MinIO's own sync per object.
- **Cold-cache clone** (50 versions): 0.283 s cold against 0.150 s warm, 12 object downloads.

**The metadata service is still a single point of failure**, and MinIO here is single-node and single-drive. No
fault-tolerance claim is made for either.

## Regression (`verify/results/20261107T133437Z-net-p8/`, e2beb5a)
hidden 33/1 (known CG3 race); CG5/CG6 15/2 (known two); parallel at 4 jobs passes; P0 4/4; P1 5/5; dist (reference mode) 11/11; dist (tcp-s3) 10 pass + 1 skipped by design; net N2-N5 5/5. No new failure. **Merged.**
