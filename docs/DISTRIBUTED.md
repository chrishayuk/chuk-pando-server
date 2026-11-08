# Distributed operation, milestone 1: specification (FROZEN before implementation, 2026-11-07)

**Target (owner):** two independent server processes, one shared durable metadata system, one shared immutable pack
store, and concurrent Git pushes to different branches. Local, multi-process. No cloud, Kubernetes or regional caches.
The Git protocol frontend and the pack format are unchanged.

The single-process server stays a supported mode and the verified baseline: no `PANDO_META` means today's behaviour.

## Architecture
```
 git clients ──► frontend A (pando serve)        frontend B (pando serve)
                 own: port, events, pid, lock     own: port, events, pid, lock
                 in-memory caches + object index  in-memory caches + object index
                      │  metadata RPC                  │
                      ▼                                ▼
                 metadata service (pando meta-serve): the existing Store
                 (ordered KV, per-key versions, OCC multi-key transactions, per-key commit
                  locks, WAL with group commit, checkpoints) behind a Unix-socket protocol
                      │
 shared pack store: <shared>/blobs (immutable, write-once install via link(2); per-instance tmp)
```
- **Metadata service.** The existing `meta::Store`, unchanged in semantics, served over a Unix domain socket.
  - Operations: `get(k) → (version, value)`, `scan(prefix)`, and `commit(read_set[(k, version)], writes)`.
  - Commit validates every read version under the touched keys' stripe locks, appends to the WAL, fsyncs (group
    commit) and only then replies. A frontend's transaction is therefore exactly today's OCC transaction, with reads
    done remotely.
  - Per-ref compare-and-swap (A4) is preserved because the ref-pointer key is in the read set.
  - Unrelated branches touch disjoint keys. They contend only on a stripe-hash collision and on the shared WAL fsync,
    which is group commit, not a lock held across a push.
  - **There is no repository lock anywhere.**
- **Pack store.** A shared directory.
  - `install` is already write-once across processes (`link` with no replace, then fsync of file and directories).
  - Change: each instance gets its own tmp area `blobs/tmp/<instance-id>/`, holding an exclusive `flock`. The startup
    sweep removes only tmp areas whose lock is free (dead instances), never a live instance's in-flight files.
    An area is created and locked under a staging name (`n-…`) and only then renamed to `i-…`, so it is never
    visible to the sweep without its lock. (Independent audit F3 showed the original create-then-lock order let a
    concurrently starting frontend delete a live frontend's area: 4 of 100 concurrent starts. Fixed; reproducer
    R06 and unit test `concurrent_shared_starts_never_sweep_a_live_area`.) Staging areas left by a process that
    died during creation are removed by age (over an hour). `flock` across hosts on a network filesystem is not
    verified (NFS may make it host-local).
- **Frontend state is a cache of shared state:**
  - ref versions, covers and copies are immutable or write-once, and only positive results are cached, so they never
    go stale;
  - ref pointers are always read from the service;
  - the object→pack index is rebuilt from shared pack records and refreshed on a miss (`locate` already does this).
- **Background work** (compaction, copies) may run on both instances. Every publication is a conditional transaction
  (the key must be absent), so duplicates are idempotent. A losing duplicate can leave an unreferenced immutable blob;
  this is counted and reported.

## Correctness requirements → tests (`verify/dist/`)
| # | Requirement | Test |
|---|---|---|
| D1 | Per-ref atomic CAS across processes | K writers on 2 frontends race to update **one** branch, each with `old = current tip` (raw receive-pack). Per round exactly one succeeds, the others get a lock error, and the ref's version history is a single chain with no lost or duplicated version. |
| D2 | No global lock: independent branches progress concurrently | 2 × 8 writers, each pushing its own branch through alternating frontends. Throughput and latency are compared with the same load on one frontend. The test **fails** if concurrent pushes to distinct branches serialise: all commits of the run fall in strictly non-overlapping push windows, checked from `push_received`/`push_reported` event timestamps. |
| D3 | Acknowledged pushes visible through either server | after every acknowledged push on A, an immediate `ls-remote` and a clone through B see the tip (read-your-writes across frontends), stock and OpenAI-fork clients, with and without packfile URIs |
| D4 | Pack and dependencies complete before ref publication | fetch through B immediately after each acknowledgment on A: fsck-clean, tip reachable; and the holder-only-1-ref dependency case from the hidden suite run through B |
| D5 | Frontend crash during a push | SIGKILL A during pushes (in ingest, after install before commit, around commit). B keeps serving; every acknowledged push is fetchable through B; no ref points at a missing pack; A restarts and recovers without assuming ownership. |
| D6 | Other frontend continues when one dies | A killed: B serves existing refs, accepts pushes; A's dead tmp area is swept by A's restart or by B's next start, never while A is live |
| D7 | Index and cache invalidation | a thin push to B whose base was pushed through A (B has never seen that pack); a fetch through A of packs committed by B; covers and copies built by one instance served by the other |
| D8 | Restart without ownership | restart frontends in either order, and the metadata service; frontends reconnect; all refs and packs intact; maintain completes from either frontend |
| D9 | Existing behaviour | the unchanged hidden suites, CG5/CG6, parallel download, P0 and P1 on the single-process mode; P0/P1-equivalent black-box tests through two frontends |

**Metadata-service failure:** frontends fail requests (no acknowledgment without a durable commit) and reconnect when it
returns. A **single metadata process is not replicated.** It is a single point of failure: a stand-in for OpenAI's
production metadata store, whose design is unpublished. Replication is out of scope for this milestone and stated as a
difference.

## Measurements (registered; reported, not pass/fail except D2's serialisation check)
- push latency and throughput: 1 frontend vs 2 frontends, with writers on distinct branches (8 and 16 writers);
- same-branch contention: CAS failure rate;
- per-push metadata RPCs, store reads/writes (expected unchanged: 3 reads, 4 writes), RPC round-trip time;
- object-index refresh count and cost on the remote-commit miss path;
- duplicate background work: covers and copies built twice, and orphan blobs.

## Not claimed
- equivalence with OpenAI's metadata store or its scale;
- multi-region operation or replicated metadata;
- performance beyond this host.

## Results (2026-11-07, implementation b4d9a67, measured at 75f962c; `verify/results/dist-75f962c.json`)
Host load average ≈ 12 during the run (other projects' processes, not ours; 16 cores). The three configurations ran back
to back in one run, so comparisons between them are fair; absolute numbers are this host's.

**Tests:** `verify/dist/test_dist.py`, 10 tests, all pass.
- **D1:** 15 rounds × 8 writers on one branch through both frontends. One winner per round; versions 1–16, no gap or
  duplicate.
- **D2:** 16 writers on distinct branches; 14 pushes in flight at the peak, split 65/64 between frontends.
- **D2 stalled commit:** a commit stalled 3 s inside the service while holding its stripe locks. Pushes to other
  branches finished meanwhile in 57 ms (A) and 39 ms (B). The rival creation of the stalled branch was ordered after it
  and refused ("reference already exists").
  - **Mutant check:** with a test-only frontend-wide push lock (`PANDO_TEST_MUTANT_GLOBAL_LOCK=1`) the unrelated pushes
    waited 2.5–3.0 s and the test **fails**, so it detects a global lock.
- **D3/D4:** cross-frontend visibility, stock and fork clients, URIs on and off; two-ref dependency case.
- **D5/D6:** 5 SIGKILLs of A during pushes; 13 unacknowledged pushes were interrupted and no acknowledged push was lost.
  B kept serving. A's tmp area survived B's restart and was swept by A's.
- **D7:** a thin push to B against A's pack; A's covers served by URI through B.
- **D8:** service SIGKILL and restart; frontends restart in any order; no acknowledgment while the service is down.

**Measurements** (writers push real stock-git commits to their own branch, 10 each):
| | single process | service + 1 frontend | service + 2 frontends |
|---|---|---|---|
| 8 writers: pushes/s, median / p95 latency | 35.4, 146 / 170 ms | 34.8, 143 / 165 ms | 28.9, 150 / 188 ms |
| 16 writers: pushes/s, median / p95 latency | 45.1, 224 / 267 ms | 45.5, 234 / 271 ms | 41.7, 237 / 295 ms |
| metadata writes per push | 4.0 | 4.0 | 4.0 |
| logical metadata reads per push (16 writers) | 23.9 | 23.9 | 37.4 |
| same branch, 80 attempts: successes / CAS failures | 10 / 70 | 10 / 70 | 10 / 70 |
| metadata RPC round trip (ping) | — | 14.8 µs | 15.1 µs |

**Reading:**
- **Moving the store behind a process boundary costs nothing measurable here** (single ≈ service + 1 frontend).
- **A second frontend on the same host adds no throughput** (−8 % to −18 %). The bottleneck on one machine is the
  clients and the shared CPU, not the server. This milestone demonstrates **correct** horizontal operation (shared
  state, no ownership, no global lock), **not** capacity scaling. That needs separate hosts and is not claimed.
- **Two frontends read more metadata per push** (37 vs 24 logical reads): a frontend's in-memory object index misses
  objects committed through the other frontend, and refreshes. Counted: 36 refreshes, 5.1 ms in total, in the
  background-duplicates run.

  The refresh rescans all pack records of the repository, **O(packs) per miss**. That is the main scalability debt this
  milestone leaves, and the first thing to fix next: an incremental refresh from a pack-record sequence key.
- **Background duplicates:**
  - covers: compaction triggered on both frontends attempted 12 builds for 10 covers. The 2 duplicates produced
    byte-identical packs, so there are **0 orphan pack files**.
  - copies: 31 committed for 32 pushes (the first push is self-contained).

## Differences from OpenAI's published design (recorded)
- one unreplicated metadata process, a single point of failure (theirs is a production metadata store, unpublished);
- a shared local filesystem as the "blob store", where theirs is object storage;
- one host, two frontends.

## Regression and merge (`verify/results/20261107T111632Z-dist-p6/`, single-process mode unless noted)
- hidden: 33 passed, 1 failed (the known CG3 race);
- CG5/CG6: 15 passed, 2 failed (the known two);
- parallel download at 4 jobs: passes;
- P0: 4/4;
- P1: 4/5 in the run. `test_clone_from_empty_uses_top_cover` failed **client-side, before any server request**:
  `git add` in the test's own working repository reported "unable to create temporary file: Invalid argument". It then
  passed 3/3 alone and 5/5 in a full P1 rerun (`p1-rerun.txt`). Recorded as a host flake (loaded machine), not a server
  failure. The failing run is kept.
- dist: 10/10.

**Merged.**
