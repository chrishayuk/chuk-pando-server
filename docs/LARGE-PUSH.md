# Large pushes: what grows with push size (REGISTERED before measuring, 2026-11-07)

Article (A3): "O(1) metadata reads and writes" per push; large pushes write *a subset of commits synchronously, the
rest asynchronously, in the same transaction*. That implies OpenAI keeps per-commit metadata. Ours keeps none: a push
writes 4 records per ref (ref version, ref pointer, tip→version key, pack record) whatever its size (CG7: "different
but equivalent; split absent"). **Owner direction:** do not add a split because the article describes one. Measure first.

## Work on the push path today (from source, main 502d9cc)
| Phase | Where | Grows with | Needed before ack? |
|---|---|---|---|
| single-pass ingest: inflate, hash, resolve deltas, write .pack + .idx | `ingest.rs` | bytes and objects | **yes** (validation, durability: A1) |
| blob install (link, fsync file and dirs) | `blob.rs` | bytes (fsync) | **yes** |
| prerequisite lookup (`locate` per thin base / prerequisite) | `repo.rs push` step 1 | prerequisites (usually small) | **yes** (dependency closure) |
| tag peeling, branch-tip type check | `repo.rs` | refs pushed | **yes** |
| `links_to`: walk commits inside the pack from the new tip to the old tip | `repo.rs` | commits in the pack | **yes** (sets `linked`, which fetch planning and covers rely on) |
| metadata transaction (4 writes per ref, WAL fsync) | `meta.rs` | refs pushed (not objects) | **yes** |
| `register_pack`: add every object of the pack to the in-memory object→pack index, under its write lock | `repo.rs` | objects | **no** (derived; rebuildable from pack records; `locate` already falls back to a refresh) |
| first `locate` after restart: `ensure_index` rebuilds the whole in-memory index from all L0 packs | `repo.rs` | **repository size** | no (derived), but it runs on the foreground of the first push or inline fetch that needs a lookup |

## Measurement (`verify/largepush/measure.py`)
- **Pushes:** single stock-git pushes of N = 1, 16, 256, 4096, 32768 new commits on top of a 1-commit base. Each commit
  edits one of 64 small files, so a push carries about 3N objects. History is built with `git fast-import`.
  Each push goes into a fresh repository, 3 repetitions; the median is reported.
- **Recorded per push:**
  - client wall time;
  - per-phase server time from a new `push_timing` event: ingest, install, prerequisites, links walk and its commits
    visited, transaction, register and its objects;
  - foreground metadata reads and writes (`/_stats`), process CPU, server RSS, object count.
- **Restart:** stop and start the server holding the N=32768 repository plus 63 copies of 512-commit pushes, then time
  one small push (its `ensure_index` time is reported separately).

## Decision rule (fixed now)
A phase is a candidate for moving off the acknowledgment path only if all three hold:
- it is in the "no" column above;
- its cost grows with push or repository size;
- at the largest size it takes **≥ 10 % of foreground push wall time, or ≥ 100 ms**.

The same thresholds apply to the post-restart `ensure_index` time on the first push. If no phase qualifies, the split
is recorded as unnecessary for this implementation, with the reason. Phases in the "yes" column are reported but never
moved. Validation and durability stay synchronous.

If a phase qualifies, the change must preserve the owner's six invariants:
- no acknowledged push loses its ref, pack or dependency closure;
- a fetch immediately after acknowledgment is correct;
- recovery reconstructs any unfinished work;
- repeated or concurrent background work is idempotent;
- no linear work escapes instrumentation;
- stock and OpenAI-fork compatibility.

It is tested with a SIGKILL immediately after acknowledgment, before the background work finishes.

## Results (`verify/results/largepush-3eba5a5.json`; main 502d9cc plus instrumentation only; medians of 3)
| N commits | objects | pack bytes | client wall | ingest | install | links walk | txn commit | register | fg reads / writes | CPU |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 3 | 275 | 67 ms | 5.0 ms | 9.6 ms | 0.01 ms | 4.0 ms | 0.02 ms | 135 / 4 | 2 ms |
| 16 | 48 | 3.5 KB | 71 ms | 5.8 ms | 10.3 ms | 0.02 ms | 4.5 ms | 0.03 ms | 150 / 4 | 3 ms |
| 256 | 768 | 69 KB | 79 ms | 10.0 ms | 9.3 ms | 0.17 ms | 4.2 ms | 0.06 ms | 176 / 4 | 11 ms |
| 4096 | 12,288 | 1.18 MB | 256 ms | 81.5 ms | 10.5 ms | 28.5 ms | 4.2 ms | 0.53 ms | 171 / 4 | 118 ms |
| 32768 | 98,304 | 9.5 MB | 1,556 ms | 632 ms | 17.1 ms | 242 ms | 4.7 ms | 4.9 ms | 167 / 4 | 1.63 s |

("links walk" = `txn_build_and_links`, which is dominated by `links_to`: 32,768 commits visited at N = 32768.)

**Restart** (65 packs, 195,138 objects, 65,025 commits): the first push after restart rebuilt the in-memory object index in
**20.3 ms**, client wall 94 ms against 68 ms for the next push.

**Reads.** Foreground reads are flat in N: 135–176, of which about 130 are in-memory object-index lookups (no store access).
They validate the 63 unchanged blobs that each new root tree references outside the pack. They scale with tree width, not
push size; workload R (1 file) shows 7. Store point reads are 3 and writes 4, for every N.

## Decision (by the registered rule)
- **register_pack** (derived, the only "no" phase on the push path): 4.9 ms = 0.3 % of wall at N = 32768. It does not
  qualify (< 10 %, < 100 ms).
- **ensure_index after restart**: 20 ms for 195 k objects. It does not qualify. It is linear in repository size; by
  extrapolation (not measured) it would reach 100 ms near 1 M objects.
- **No synchronous/asynchronous metadata split is implemented.** Our push writes no per-commit metadata, so there is no
  "remaining commits" set to defer. Metadata stays constant: 3 store reads and 4 writes per ref. A3's O(1) property holds
  as measured. The article's split is a mechanism for a cost this design does not have. Recorded as *different but
  equivalent*, not as implemented.

**What does grow with push size (reported, required before acknowledgment, not moved):**
- ingest: 41 % of wall at N = 32768, roughly linear in bytes. This is A1's single-pass validation.
- the `links_to` walk: 242 ms, 16 % of wall. It re-reads every commit of the pack (inflate and parse) to decide whether
  the new tip descends from the old one.

  Possible later optimisation, not done: record parent edges during ingest, which already inflates each commit, so the
  walk needs no second read. This is a speed-up of required work, not a deferral.
- The rest of the client wall (≈ 650 ms at N = 32768) is client-side pack generation and transfer.

**Relevance to the distributed milestone:** the object index is per process and rebuilt from shared pack records. With
several server instances, each one rebuilds it at start and must learn of packs that other instances commit. That
design question belongs to the next milestone.

## Regression of the instrumentation (`verify/results/20261107T105847Z-largepush-instr/`, 267e035)
hidden 33/1 (known CG3 race); CG5/CG6 15/2 (the known two); parallel at 4 jobs passes; P0 4/4; P1 5/5. No new failure. Merged.
