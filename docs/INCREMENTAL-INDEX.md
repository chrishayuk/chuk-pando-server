# Incremental object-index refresh (REGISTERED before implementation, 2026-11-07)

**Problem (docs/DISTRIBUTED.md results).** When a frontend misses an object committed through another frontend,
`ensure_index(refresh)` rescans every pack record of the repository: O(packs) per miss.

## Design
- **Change log, kept by the store.** In memory, ordered by commit LSN: every committed write to a key with prefix
  `pack<SEP>`. It is rebuilt at open from the snapshot and the WAL (each record carries its LSN). It is not persisted
  separately, so no new durable state is added.
- **Stable LSN = committed publication order, not allocation order.**
  - The store assigns LSNs in order under the WAL lock. Transactions waiting on the same group fsync can be applied
    to the map in either order.
  - The store therefore tracks *stable* = the largest L such that every LSN ≤ L has been applied (or abandoned by a
    failed commit). It advances only over a contiguous applied prefix.
  - At open, stable = the largest LSN recovered.
- **Query:** `changes_since(prefix, h) → (s, [(lsn, key, value)])`, for h < lsn ≤ s with keys under `prefix`. It works on
  the local store and over the service socket (a new request).
  - Cost: O(log n + change-log entries in (h, s]), for all repositories (filtered by prefix after the range). It does
    not depend on the total number of packs.
- **Frontend:** the object index keeps a high-water mark h.
  - The first build captures s = stable *before* its full scan, then sets h = s. Everything ≤ s was applied before the
    scan began; extra records are harmless.
  - Each refresh calls `changes_since(pack prefix of the repo, h)`, registers each record, and sets h = s.
- **Duplicates and rewrites are idempotent.** A pack record rewritten later (more ref versions appended) appears again;
  `register_pack` merges its ref versions. Today's full refresh skips already-known packs, so it misses appended ref
  versions; the change log fixes that.

## Required properties → tests
| # | Property | Test |
|---|---|---|
| I1 | **No missed updates** under out-of-order application | unit test (local store): a commit is held after durability and before application (test hook); a later LSN applies first. `changes_since` must not advance past the held LSN; after release, both are returned. Plus a randomised test: 8 committer threads with random apply delays and a reader repeatedly refreshing from its h. The reader's union equals the set of all committed keys. |
| I1' | Across restarts | unit test: h survives reopen semantics. Changes after reopen are found from the old h. Changes before reopen with LSN ≤ h are not needed and not returned. |
| I2 | **Bounded work** | measurement (`verify/dist/incr.py`): a repository with P = 64, 256, 1024 packs pushed through A; then one push through A and a thin push on top of it through B (B misses A's newest pack). Keys read by B's refresh, before (full rescan) vs after (change log). Expected: before ≈ P; after = the new records (1–3). |
| I3 | **Gaps, duplicates, concurrent maintenance** | the dist suite D1–D8 unchanged; a new dist test: 2 × 8 writers pushing thin packs through alternating frontends while `maintain` (covers, copies) runs concurrently on both. Every push succeeds or fails cleanly; no "missing necessary objects" for objects that are committed; final clones through each frontend are fsck-clean. Plus the full regression. |

Stale ref visibility is unaffected: ref pointers are never cached, and are read from the store on every request.

## Amendment during implementation (before any measurement)
`changes_since` returns **every applied** change-log entry above h, including entries above the stable LSN; the caller
advances h only to stable.

Reason, found while implementing: with "only (h, stable]", a pack already acknowledged through one frontend could be
invisible to the other for as long as an unrelated earlier LSN was still being applied. A thin push based on it would
then be refused. Entries above stable are returned again on the next refresh, which is idempotent.

## Results (2026-11-07; implementation f417282)
**I1, held commit** (unit test): with a later LSN applied first, stable stayed below the held commit. The later record
was still returned. After release, the held record was returned, and stable reached the later LSN. Passes.

**I1, randomised** (8 committers × 60 tracked writes, random apply delays, untracked writes interleaved): the reader
using the stable mark saw **480/480** keys in each of 3 runs. A naive reader advancing to the largest returned LSN, run
alongside for comparison, saw **350, 370 and 355 of 480**. That is the permanent-miss failure the stable LSN prevents.

This test also found a **lock-order inversion** in the first implementation (data→changes in commit,
changes→data in the query), which deadlocked. Fixed before any other measurement.

**I1′, reopen:** passes.

**I2, bounded work** (`verify/results/incr-before-0196b03.json`, `incr-after-f417282.json`). B's foreground store keys
scanned during one thin push that misses A's newest pack:
| packs pushed before | before (full rescan) | after (change log) |
|---|---|---|
| 64 | 87 | 5 |
| 256 | 342 | 5 |
| 1024 | 1,365 | 5 |

After the change, the refresh read 4 change-log records: the new pack, its copy and cover records. The cost does not
depend on repository size.

**I3, concurrent maintenance:** 16 writers, 12 thin pushes each, through alternating frontends while `maintain` loops on
both. 192 thin pushes, all accepted; 24–28 incremental refreshes reading 568–644 keys in total. Final clones through both
frontends are fsck-clean at every tip.

The first runs failed 3 of 4 times on the **client**: the test's post-clone `git fetch` started a detached
`git maintenance run --auto` (92–146 packs in a URI clone exceed `gc.autoPackLimit`), which repacked while `fsck` read.
This was confirmed with `GIT_TRACE`. The test now disables client auto-maintenance in its clones and passes 4/4. This is
a harness race, not a server property.

**Simulator** (embedded store path): 8 seeds × 600 steps, 13–21 crashes and faults each: 0 violations.

**Also fixed:** `Store::hook` called the fault closure while holding its mutex, so a hook that blocks one commit
blocked all commits. Only test and simulation hooks are affected.

**Host issue (open):** twice today a stock-git client command in a harness failed with "unable to create temporary file:
Invalid argument" (one P1 run, one I2 run). Both passed on rerun. The cause is not identified. Recorded, not hidden.

## Regression (`verify/results/20261107T121553Z-incr-p7/`, 218dd9f)
hidden 33/1 (known CG3 race); CG5/CG6 15/2 (known two); parallel at 4 jobs passes; P0 4/4; P1 5/5; dist 11/11. No new failure. **Merged.**
