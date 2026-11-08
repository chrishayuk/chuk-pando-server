# Background self-contained copies for thin pushes (D-13): registration (before implementation, 2026-11-07)

Principle (owner, 2026-11-07): *pushes create immutable data; background workers prepare reusable representations;
fetches select and serve existing data, not create new persistent data.*

## Today (main 712348e)
A thin or REF_DELTA push pack cannot be served by packfile URI (both clients run `index-pack --fix-thin` before the
hash check; docs/URI-TAIL.md). The first URI fetch that plans such a pack builds its self-contained "thick twin" in
the fetch handler (`proto.rs`, `compact::build_twin`): a blob install plus 2 metadata writes on the fetch path.

## Change
1. The incoming push pack is stored and committed exactly as now (immutable, durable before acknowledgment). The
   push acknowledgment does **not** wait for the copy.
2. After the commit, a copy job is queued to the existing background worker. **No job record is written**: the job
   is reconstructible from committed state (a level-0 pack record that is thin or has REF_DELTA and has no `twin/`
   key). At startup the worker scans for such packs and rebuilds the missing copies, after compaction catch-up.
3. **Fallback window policy:** a URI fetch that plans a pack whose copy is not yet published sends that pack
   **inline** in the same response, beside the URI packs (allowed by protocol v2: packfile-uris are optional per
   pack). This is the existing fallback when a copy cannot be built. It is a temporary deviation from "exclusively
   by URI" (A8) for that window only, and is counted (`fetch_served.inline_fallback_packs`). Fetches never wait.
4. The fetch handler no longer builds copies and performs no metadata writes.
5. Publication: the copy's blob is installed write-once (`link_no_replace`), then one transaction writes the
   `twin/` key (only if absent; conflict retry) and the copy's pack record. A crash before the commit leaves an
   unreferenced immutable blob, never a referenced partial one; the job reruns at startup. Two workers building
   the same copy produce the same bytes and id; the first commit wins, the second is a no-op.
6. Skip: a copy is not built when every ref version of the pack already lies in a committed, linked level-1 cover
   (the planner then never selects the L0 pack; such packs need no URI form). This matters for restart catch-up.

## Acceptance (all required before merge)
| # | Criterion | How tested |
|---|---|---|
| 1 | No metadata writes and no copy built during steady-state URI fetches after preparation | unchanged `verify/tail/measure.py`: `writes` = 0 and `twins_built` = 0 in every row, stock and fork |
| 2 | No copy construction in the fetch handler | code: `build_twin` not reachable from `proto.rs`/`v0.rs`; test 3 below sees inline fallback, not a copy |
| 3 | Acknowledged refs immediately fetchable before preparation | new P1 test with the worker held (`PANDO_TEST_HOLD_COPIES=1`, test-only, documented): stock and fork clients fetch with URIs immediately after push; fsck-clean; event shows inline fallback; 0 fetch-path writes; after release the copy is published and the next fetch uses URIs only |
| 4 | Same object graphs | tail rows: reader reaches the tip, fsck-clean; `cache_bytes` within 1 % of `tail-712348e.json` per row |
| 5 | Crash safety and idempotence | new P1 test: SIGKILL with copies pending; restart rebuilds them (no `twin/` key missing for an unskipped thin pack after `maintain`), no partial pack referenced, fetches correct. Concurrent build: two `build_twin` calls on one pack (unit test) yield one `twin/` key and one record |
| 6 | No compatibility regression | `verify/run_suites.sh all`: hidden 33/1 (CG3 race), CG5/CG6 no new failure, parallel download at 4 jobs, P0 4/4, P1 3/3 plus the new tests |

## Measurements (reported, not pass/fail) — `verify/tail/prep.py`, workload R 40 versions, stock pushes
- foreground reads/writes per push and per fetch;
- background CPU and reads/writes for copy preparation;
- preparation latency: `ref_version_committed` → `twin_committed` (event `ts_ms`), median and max;
- storage amplification: copy bytes vs push-pack bytes, and copies built that no reader in the tail run used
  (the old on-demand scheme built only those used).
Expected cost, stated now: about one full file copy per stock push on workload R (≈ the fork's upload, stored
server-side), i.e. ≈5 MB extra for 40 versions versus ≈0.4 MB on-demand for the tail run.

## Amendment before any measurement (2026-11-07)
Rule 6 (skip copies for packs inside a linked L1 cover) is **withdrawn**. A reader whose position lies inside a
cover's range (e.g. 2 behind a tip that ends a cover) is planned with the individual L0 packs, not the cover, so a
skipped copy would force the inline fallback in steady state and violate criterion 1. Every thin / REF_DELTA L0
pack now gets a copy. Found by reading the planner while implementing; no code with the rule was measured.

## Results (2026-11-07; implementation 33b7c66; before = main 712348e)
Files: `verify/results/tail-33b7c66.json` (unchanged tail harness), `prep-before-712348e.json`, `prep-after-33b7c66.json`.

**Fetch path (unchanged tail harness, stock and fork pushers, 6 reader states each):**
| | before (712348e) | after (33b7c66) |
|---|---|---|
| metadata writes during fetch | 2 (behind 1, 2, 3; stock pusher) | **0 in all 12 rows** |
| copies built during fetch | 1 (behind 1, 2, 3; stock pusher) | **0 in all 12 rows** |
| foreground reads, stock reader 1 / 2 / 3 behind | 41 / 43 / 45 | 36 / 38 / 40 |
| bytes delivered by cache (per row) | e.g. 129,534 / 259,049 / 520,401 | 129,535 / 259,051 / 520,395 (max diff 6 B, <0.01 %; pack-header/order noise) |
| server CPU per fetch, stock reader 1 behind | 7.1 ms | 0.7 ms |
| fsck, reader reaches tip | all | all |

**Push path (40 stock pushes, `prep.py`):** median wall 50 ms before / 58 ms after (single run, within run-to-run noise on
this host, not a claimed effect); foreground reads 7 and writes 4 per push, unchanged. The acknowledgment does not wait.

**Background preparation (after):** 39 copies (every thin push; the first push is self-contained).
- latency push commit → copy published: median 52 ms, max 253 ms; build time median 23 ms, 0.9 s in total;
- background metadata: reads 331 → 682, writes 24 → 102 (+2 writes per copy);
- process CPU over pushes and preparation: 0.229 s → 0.483 s;
- whole run including the 6 readers: 0.256 s → 0.489 s.

**Storage amplification (the cost of this change):** copies hold 5,029,997 B against 139,858 B of push packs (≈36×; about one full
256 KiB-file copy per version, the same bytes the fork uploads). The on-demand scheme stored only the 3 copies these readers used.
36 of the 39 copies were not used by this reader set. Readers 4, 8 and 16 behind were served from covers.
Workload R is the worst case: a 256 KiB file rewritten every version. Copy size scales with the content each push changes.

**Acceptance criteria:**
1 met; 2 met (no call path from `proto.rs`/`v0.rs`); 3 met (P1 window test, stock and fork); 4 met; 5 met (P1 SIGKILL test,
concurrency unit test, 16 simulator runs over 8 seeds with crashes/faults, copies normal or held: 0 violations); 6: see regression below.
Both new P1 tests fail on 712348e (fetch-path writes 2 and 12), as they should.

**Not claimed:** that every fetch is read-only. Measured read-only: steady-state protocol-v2 URI and inline fetches on these
workloads. Not separately audited: shallow fetches, protocol v0, and the events log (an append-only observability file).
In the fallback window a fetch is still correct, with the uncopied pack sent inline.

**Possible follow-up (not done):** limit copies to versions a URI reader can be planned onto as L0 packs. Every L0 pack can be
selected today (a reader inside a cover's range), so this needs a planner change; the trade-off is storage against inline fallbacks.

## Regression (`verify/results/20261107T102444Z-bgcopy-33b7c66/`, clean tree at 33b7c66)
hidden 33 passed / 1 failed (the known CG3 start race, same message as the 712348e baseline); CG5/CG6 15 / 2 (the two known
failures: compaction-levels, parallel at the default single job); parallel download at 4 jobs passes; P0 4/4; P1 5/5 (3 existing
plus the 2 new copy tests). No new failure. **Merged.**
