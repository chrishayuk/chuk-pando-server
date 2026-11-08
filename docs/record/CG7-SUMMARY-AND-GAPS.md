# CG7 audit summary and gap list toward one Pando implementation (2026-11-06)

**Sources:**
- `campaign/audit/output-a/AUDIT.md`: snapshot 0bfcbdd, est. $6.63, 0.68 h.
- `campaign/audit/output-b/AUDIT.md`: snapshot cab020f, est. $5.44, 0.47 h.

Each auditor saw one implementation, the frozen spec and article extract, and a fixed neutral question list. They saw no test results. Grades use the spec/05 F rubric. Both auditors were `claude-opus-5-5`, the same family as the builders (a limitation).

## A1–A12 side by side

| Mech | Article claim (short) | A | B |
|---|---|---|---|
| A1 | Pack streamed to blob store, single-pass validation, never unpacked | Reproduced (executed) | Reproduced (executed) |
| A2 | Ref version {tip, pack, deps on earlier *versions*}, one transaction | Reproduced (executed) | Reproduced (executed) |
| A3 | O(1) metadata per push; large pushes write a subset synchronously, the rest async | Different-but-equivalent (4R/3W constant; no sync/async split) | Partially (4R/4W per ref constant; sync/async split absent) |
| A4 | Only contended key is the ref pointer | Partially (8 WAL units, global event mutex, in-flight set) | Partially (one WAL, global apply lock, shared stripes) |
| A5 | Causal consistency, no repo-wide snapshot | Reproduced | Different-but-equivalent (single store gives atomic snapshots: stronger than required) |
| A6 | Advertise only trunk | Partially (all refs by default; trunk-only opt-in) | Partially (all refs by default; with explicit-haves, trunk `.have` plus named refs) |
| A7 | Fetch planning O(log Δ) metadata reads | Partially (~2·log₂Δ, then Δ/256 beyond L4; uncounted commit-graph walk grows with history) | Partially (~log Δ to Δ≈1,024, then Δ/256; haves recognised only at exact version tips) |
| A8 | Empty inline pack plus packfile-URIs only; cache tier | Reproduced (with the required inline fallback) | Reproduced (cache tier simulated in-process) |
| A9 | Geometric covers L0..L4, background, packs never discarded, repack improves compression | Partially (dedup-only covers; in-memory trigger queue; **GC can delete a committed pack**) | Reproduced with caveats (dedup-only covers; covers include unlinked versions) |
| A10 | Commodity, horizontally scalable servers, metadata store and blob store | Partially (single process, in-RAM store, fd per pack) | Partially (single process, in-RAM store, one WAL) |
| A11 | Explicit haves: branch tip advertised separately; works with OpenAI client | Partially (own ls-refs contract; incompatible with the fork, confirmed by CG5 trace) | Reproduced (wire semantics; matches the fork, confirmed end-to-end in CG5) |
| A12 | Stock Git workflow compatible | Reproduced (stock workflow) | Reproduced for stock workflow, with gaps |

## Defects that matter for choosing a base
- **A D1 (blocker, executed):** GC deletes a committed pack. The trigger is an identical-bytes re-push racing `maintain` after an older orphan blob of the same bytes exists. The result is a lost acknowledged write, and the repo cannot be served after restart (D3: one missing sidecar makes the whole repo fail). The auditor widened the window with an instrumented sleep; the race exists in unmodified code by reading.
- **A D2 (major, executed):** one fd per pack, never closed. Pushes fail after about 234 packs under the macOS default limit, and a large repo cannot restart.
- **A D5 (major):** a client-triggerable O(K × packs) rescan under the repo load lock.
- **B:** no correctness failure observed in executed checks. The highest items are power-loss durability gaps by reading: D-3 (new repo's blob directory never fsynced in its parent), D-4 and D-5. Also D-1: the counter does not measure planner work.
- **Both:** `/_stats metadata_reads` does not count in-memory index work, so neither counter supports a complexity claim.
  - B's counter reports O(1) with covers and Δ/3 without, while real steps are O(log Δ) and Δ.
  - A's planner has an uncounted commit-graph walk.

## Effect on the CG5–CG6 "provisional" items
- **Planning reads rule:** now **not established for either builder** (auditors: counters miss in-memory work).
- **Real planner behaviour, by execution:**
  - B: ~log Δ up to Δ≈1,024; A: ~2·log₂Δ.
  - Both become linear (Δ/256) beyond the L4 cap.
  - The CG6 test used Δ ≤ 256, inside the logarithmic range.
- **Never unpacked into a repository (claim A1, paraphrased):** **established** for both (A1 executed).

## Recommended base: **B** (owner decision)
- **Correctness:** no executed correctness failure. A has an executed acknowledged-write-loss blocker and a restart-killing fd leak.
- **Interop:** B already matches the OpenAI client's wire contract, end to end. Porting A's contract would break that.
- **Fidelity:** B grades higher on A9 and A11, equal elsewhere apart from A3/A5, which differ in kind rather than quality.
- **Worth borrowing from A:**
  - have-mapping through the commit graph: recognises haves that are not exact version tips, fixing B's D-8 limitation;
  - its galloping/binary search inside fast-forward segments, as a planner reference;
  - A's cover alignment and its auditor's experiment scripts, for regression checks.

## Gap list for the canonical implementation (starting from B), prioritised

**P0, correctness and durability (must fix before any demo):**
1. Fsync new repo blob directories in their parent (B D-3). Fsync on the de-duplication early return (D-4). Propagate `fsync_dir` errors (D-5).
2. Make blob publication write-once: no check-then-rename replacement of `.pidx` or pack inodes (D-6).
3. Reject non-commit objects on `refs/heads/*` (D-12).
4. Decide on full-device flush on macOS (`F_FULLFSYNC`; A's D6 noted the issue, B unchecked) and document it.
5. Regression tests for every executed defect in *either* audit that could apply to B's design: GC/orphan races (A D1; B currently never deletes orphans, see D-16), the fd limit (A D2), missing-prerequisite amplification (B D-10, A D5).

**P1, fidelity to the published mechanisms:**
6. Honest instrumentation. Count every metadata access, including in-memory cache hits, as logical reads, plus planner steps, separately from background work. Then re-run the CG6 planning test against it (B D-1, A D15).
7. O(log Δ) beyond Δ≈1,024. Either add levels above L4, or document the L4 cap as the article's (it describes OpenAI's monorepo as using covers up to L4; paraphrase of https://openai.com/index/scaling-git-at-openai/) and state that planning is then O(log Δ) only up to 4⁴ versions per level span. *Owner decision.*
8. Recognise haves that are not exact ref-version tips (borrow A's commit-graph mapping) (D-8).
9. Repacked covers that improve compression, as the article describes. Both builds only de-duplicate (A D7, B A9 caveat).
10. A3: synchronous subset plus asynchronous remainder for very large pushes, recorded in the same transaction. Absent in both.
11. Exclude unlinked (force-pushed or deleted) versions from covers (D-9). Make fetch read-only by building thick twins asynchronously (D-13).
12. Orphan-blob collection that cannot race committed packs (D-16; designed against A D1's failure).

**P2, scope decisions (owner):**
13. A10 horizontal scaling: an external, shardable metadata store plus multiple server processes. Both builds are single-process with an in-RAM store. Large; it is what "horizontally scalable" means in the article, but not needed for the explainer demos.
14. Parallel packfile-URI download: needs a validated client configuration of the OpenAI fork before any test can serve as an oracle.
15. Trunk-only advertisement by default (A6) conflicts with stock-client correctness (REVIEW-1 F5). Keep all refs for stock clients, as both builds do, and document it.

**P3, observability for the explainer (after P0/P1):** pack-dependency graph, compaction levels, planner trace, and a concurrent-push view, all read-only and driven by real state.

**Verification on the canonical build:**
- hidden CG1–CG4 suites, plus the reviewer's strict CG3 variant D2';
- the frozen CG5/CG6 suite;
- both auditors' experiment scripts as regression checks;
- fault injection (GC/maintain races, SIGKILL, fd limits);
- a fresh CG7-style audit of the changed areas.
