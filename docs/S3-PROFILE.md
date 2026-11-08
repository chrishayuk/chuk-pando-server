# Profiling the MinIO push path (REGISTERED before measuring, 2026-11-07)

**Baseline kept as measured** (`verify/results/net-e575c25.json`): with 16 writers, the reference deployment does
48.0 pushes/s at a median of 191 ms; tcp + MinIO does 16.9 pushes/s at 727 ms (p95 1,551 ms).
**Owner direction:** capture a per-push breakdown first; do not optimise blindly.

## Instrumentation (read-only counters and timers, reported in existing events)
Per object-store request, accumulated per thread and attributed to the push that made it:
- count and bytes;
- time to connect;
- time to hash and sign (SHA-256 of the body, SigV4);
- time from the first byte sent to the full response (transfer plus server);
- per operation kind (PUT, GET, HEAD).

Reported on the existing `push_reported` event, with the server-side push time (receipt to report).
`push_timing`, `pack_ingested.ingest_ms` and `pack_stored.install_ms` already give the other phases.
Background object-store work (copies, covers) is counted separately (`/_stats object_store.*.background`).

## Measurement (`verify/net/profile.py`)
The registered net workload: 2 frontends, 8 and 16 writers × 10 pushes, stock git; tcp-s3 mode and the tcp-fs control.
Per push: server time, ingest, install, transaction, and the object-store breakdown above. Medians and p95 per mode.
Also, per run: background object-store requests and time, and MinIO's own per-PUT latency (curl probe, as before).

## Decision rule (fixed now)
A component is optimised only if, in tcp-s3 mode with 16 writers, it accounts for **≥ 15 % of the median server-side
push time**. The whole registered net measurement is then repeated unchanged, and both results are kept. Components
below the threshold are reported, not changed.

## Profile (before any change; `verify/results/s3prof-before-*.json` plus amendment runs)
| 16 writers, tcp-s3 | median server time | ingest | install | object-store requests (time) |
|---|---|---|---|---|
| first profile | 798 ms | 626 ms | 136 ms | 13 per push, ≈ 61 ms (connect 9, hash/sign 0.3, wire 52) |
| tcp-fs control | 146 ms | 20 ms | 85 ms | — |

**The object-store requests themselves were not the bottleneck** (≈ 8 % of server time). Ingest was.

**Amendment, registered as a measurement change, not a code change:** cache-fill timers were added, because the first
breakdown could not separate the download from the local write. The rerun at 16 writers:
- cache fills: 7 per push (thin bases fetched from the other frontend's packs), 326 ms;
- of which local write + **fsync**: **318 ms = 62 % of the 515 ms median server time**;
- verification: < 0.1 ms.

## Changes made (each above the 15 % threshold)
1. **Cache fills without fsync, and the cache emptied at every start** (`Blobs::new_s3`, `put_local`).
   - In S3 mode the local tree is a disposable cache. Every object in it was either verified from the authoritative
     store or uploaded to it before any metadata referenced it.
   - A process crash cannot tear a cached file, which is complete before it is linked. A power loss is covered by the
     startup wipe.
   - Rerun: median server time 515 → 246 ms, ingest 330 → 85 ms.
2. **No local fsync in `install` / `install_alt` in S3 mode.**
   - Next profile: install was 111 ms (uploads 47 ms, local fsyncs ≈ 64 ms = 33 % of 196 ms).
   - Durability comes from the store, whose PUT returns before the metadata commit.
   - The single-process and shared-directory modes keep every fsync, unchanged.

**Not changed (below threshold, or not separately measured):**
- the per-request connection (connect ≈ 5–12 ms per push in total);
- the SHA-256 and signing (< 0.4 ms);
- ingest's fsync of the incoming pack (part of ingest's remaining 38–85 ms; not isolated).

## Result: the registered net measurement, unchanged (`net-after-dbaa1f9.json`, `-rep2.json`; load below 16 at start)
| 16 writers | before (`net-e575c25`) | after, run 1 | after, run 2 |
|---|---|---|---|
| unix + shared dir (reference) | 48.0 p/s, 191 ms | 45.3, 200 ms | 49.0, 196 ms |
| tcp + shared dir | 47.7, 207 ms | 49.8, 193 ms | 49.1, 189 ms |
| **tcp + MinIO** | **16.9, 727 / p95 1,551 ms** | **53.8, 165 / 304 ms** | **55.2, 169 / 244 ms** |

With 8 writers: MinIO mode 21.4 → 42.4 / 45.6 p/s. A cold-cache clone of 50 versions: 0.18 s.

An earlier "after" run at load 53–87 (another project's build) is kept as
`net-after-dbaa1f9-INVALID-load87.*` and not used.

**Caveat on comparing modes.** MinIO mode is now slightly *faster* than the shared-directory modes. Those still pay a
full `F_FULLFSYNC` per pack locally; MinIO mode delegates durability to MinIO. **MinIO's own fsync behaviour on this
platform was not verified**, so the two modes do not have demonstrably equal durability, and this is not a like-for-like
speed comparison. What the result shows: the MinIO overhead came from redundant local cache durability, not from the
object store or the network path.

## Defect found while profiling (fixed; reported)
Commit `4e2fda0` (network storage) accidentally **deleted the three metadata-store unit tests** (I1 held commit,
I1 randomised, I1′ reopen). An edit replaced everything from `meta::serve` to the end of `meta.rs`, and the test module
followed it. `run_suites.sh` did not run unit tests, so the regression that preceded merge `c82acbc` did not notice.
- They are restored from `684cca1` and pass.
- `run_suites.sh all` now runs the unit tests and checks a **floor on their number** (`verify/unit_min.txt` = 11).
- Every other deletion in that commit was reviewed and is an intended edit.

## Regression (`verify/results/20261107T142452Z-s3prof-p9/`)
hidden 33/1 (known CG3 race); CG5/CG6 15/2 (known two); parallel at 4 jobs passes; **unit 11/11 (floor 11)**; P0 4/4; P1 5/5; dist 11/11; dist (tcp-s3) 10 + 1 skipped by design; net 6/6 (including N5b restart-empties-cache). No new failure. **Merged.**
