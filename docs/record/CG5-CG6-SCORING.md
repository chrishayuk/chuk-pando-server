# CG5–CG6 scoring of A and B (2026-11-06)

## How this was run
- **Suite:** the frozen blind-authored suite from commit 7229587 (`campaign/blind/output-session-01/`, 13 code/config files manifest-verified at each run). No test logic was changed.
- **Seed:** 11.
- **Targets:** the gate snapshots A 0bfcbdd and B cab020f, run only through the sandboxed `.evalbin/run.sh`. No builder code was modified and no builder was given feedback.
- **Configurations:** pre-registered in `campaign/RUNLOG.md` before any A/B run.
- **Raw outputs (unedited):**
  - `campaign/eval/results/cg56-{b-fork,a-fork,a-optin}-*/` (log.txt, junit.xml, metrics.jsonl, meta.txt with target tar hash and host load);
  - `campaign/eval/results/cg56-reproduction/`;
  - `campaign/eval/results/diag-cg5-eh-*/`.
  - Manifest: `campaign/eval/results/CG56-RESULTS.MANIFEST.sha256`.

| Run | Started (load 1/5/15 min) | Result line |
|---|---|---|
| b-fork (scored) | 20:31Z (13.7 / 25.9 / 41.5) | 2 failed, 15 passed, 16 subtests passed |
| a-fork (CG6 scored; CG5 informational) | 20:40Z (7.3 / 10.0 / 24.5) | 9 failed, 10 passed, 14 subtests passed |
| a-optin (CG5 only) | 20:52Z (9.4 / 13.4 / 18.0) | 7 failed, 2 passed |

pytest counts include subtest failures. Per-test outcomes are listed in each log's "short test summary".

**Before scoring**, the controller reproduced the author's own results:
- control: per-test outcomes identical to the author's;
- mutation kill matrix (unmutated fake plus 10 mutants): identical cell for cell.

## Classification rules (owner, fixed before results)
- **Pass:** a validated test passes.
- **Fail:** a validated test demonstrates a genuine server violation.
- **Unresolved:** the test is unvalidated, rests on questionable instrumentation, or assumes an unnecessarily specific layout or behaviour.
- **Client interop** is reported separately from server architecture. Any failure involving the fork is classified from a minimal reproduction plus a protocol trace. A pass resting on `/_stats metadata_reads` is provisional until the CG7 audit.

## CG5 failure on A: minimal reproduction and trace
`campaign/eval/diagnostics/cg5_eh_trace.sh`, output `results/diag-cg5-eh-20261106T205408Z/`. The scenario: push `main`, create `topic` with the explicit-haves client, then update `topic` with the same client.

| Target / client | create | update | What the trace shows on the update |
|---|---|---|---|
| A / fork | ok | **rejected** | A advertises only `refs/heads/main` plus caps `explicit-haves explicit-tips=ls-refs`. The fork does no ls-refs and sends `0000…0 <new> refs/heads/topic` (a create). A answers `ng … reference already exists`. |
| A / stock + `X-Pando-Explicit-Haves: 1` | ok | **rejected** | Identical to A / fork. |
| B / fork | ok | ok | B advertises `main`, `topic`, and `.have <main>`. The fork sends `<old topic> <new>` with `explicit-haves` and gets `ok`. |
| B / stock + header | ok | ok | (Not a B configuration; included for contrast only.) |

**Reading:**
- A's server does what its DECISIONS.md D9 specifies. In explicit mode it advertises trunk only and expects the client to fetch branch tips through ls-refs. It rejects create-over-existing, which is a correct compare-and-swap; no data is lost or corrupted.
- The OpenAI fork uses a different contract: it expects named refs in the advertisement and trunk as `.have`, which is B's choice. A also turns this mode on exactly when the fork's `Git-Protocol: explicit-haves` arrives, so A's opt-in actively breaks the real client's updates.
- Classification: **client-protocol mismatch**, not a server data-integrity defect.
- A's own semantics could only be exercised by A's Python reference client (`tests/explicit_client.py`). The frozen suite cannot drive it without new glue, so A's CG5 server semantics remain **unresolved**, not failed.

Every A CG5 failure in both A runs is this same rejection, at the explicit-haves client's first update of an existing branch. That covers the 5 failed tests and the 2 failed stock-client subtests, which fail at `tests_cg5.py:257` on the explicit-haves push. Assertions after that point did not run.

## Comparison table

| Mechanism | A | B | Evidence (test → outcome) |
|---|---|---|---|
| Git compatibility | Previously tested: 33/34 hidden. CG3 not passed under original scoring (test start race established) | same | `campaign/RUNLOG.md`, `review/CG3-ADJUDICATION.md` |
| Trunk-relative pushes (server architecture) | **Unresolved**: no documented git-binary client drives A's mechanism. Server-side verification passes | **Pass** | B: `push_pack_built_against_trunk_only` PASS, `server_verifies_pack_completes_branch` PASS, `stale_advertised_tip_rejected` PASS. A: `server_verifies_pack_completes_branch` PASS (raw protocol); the other three blocked by the interop rejection above |
| Trunk-relative pushes (interop with OpenAI client) | **Fail (interop; informational: spec/03 scores fork interop for B only)** | **Pass** | `fork_interop_push_update_delete_lease_fetch`: B PASS; A FAIL on the first topic update (trace above) |
| Dependency version tracking | **Pass** (stock-client workloads); trunk-relative variant **unresolved** | **Pass** | `every_ref_version_complete_after_maintain` PASS both (kills `dep_by_name`, `planner_incomplete_cover`); `uri_and_non_uri_clients` PASS both (kills `dep_by_name`); `trunk_relative_pack_depends_on_trunk_version`: B PASS, A blocked by interop |
| Immutable pack storage | **Pass** | **Pass** | `pushed_packs_byte_identical_after_maintain` PASS both (37 stored PACK files, byte-identical after 2× maintain). The layout assumption did not bite either builder. "Never unpacked into a repository" remains a CG7 audit item |
| Compaction: correctness | **Pass** | **Pass** | `every_ref_version_complete_after_maintain`, `maintain_concurrent_with_pushes`, `sigkill_during_maintain` PASS both |
| Compaction: levels created | **Unresolved** | **Unresolved** | `compaction_levels_reduce_packs_per_clone` FAIL both at the same single assertion; see note 1 |
| Packfile URI reuse | **Pass** (general); trunk-only push-pack reuse **unresolved** | **Pass** | `packfile_reuse_across_readers` PASS both; `uri_and_non_uri_clients` PASS both (all subtests); `trunk_only_pack_reused_by_readers`: B PASS, A blocked by interop |
| Logarithmic fetch planning | **Pack count: Pass. Reads: provisional pass** | **Pack count: Pass. Reads: provisional pass** | `planning_reads_and_pack_count_vs_delta` PASS both. URI pack count (client-measured) at Δ = 4/16/64/256 is 1/4/7/10 for both. `metadata_reads` (self-reported) is B 8/15/22/23 and A 18/18/18/6; see note 2 |
| Concurrent maintenance and crash safety | **Pass** | **Pass** | `maintain_concurrent_with_pushes` PASS; `sigkill_during_maintain` PASS (not skipped, so at least 1 of 5 kills landed mid-maintain; the exact count is not logged); `async_commit_subset_survives_crash` PASS, both |
| Parallel downloading | **Unresolved oracle** | **Unresolved oracle** | `parallel_pack_download` FAIL everywhere, including the author's fake; not scored (owner decision 4) |

**Note 1, compaction levels.** Both builders fail only `assertGreater(n1, n0, "maintain produced no new (cover) packs")`, with `/_stats packs_stored` = 398 before and 398 after `maintain`.
- 398 stored packs for about 300 pushes means cover packs already existed **before** `maintain` was called. Both builders compact in the background. A URI clone needed 6 packs (A) and 9 then 6 (B) out of 300 pushes.
- The test assumes the new levels are created inside `maintain`. INTERFACE.md allows `maintain` to be a no-op, and the count comes from a self-reported counter. So this is an over-specific assertion, not a demonstrated violation.
- The remaining assertions (≥1 URI; ≤16 URI packs, pre-registered) **did not execute** because of assertion order. The recorded metric (6 packs, both) would satisfy them, but that is a metric, not an executed check.

**Note 2, planning reads.**
- B's reads grow sub-linearly and pass the pre-registered rule.
- A's reads are flat-to-decreasing (18/18/18/6). The rule passes, but a counter that falls as Δ grows is unusual. It needs the CG7 audit before it supports any complexity claim.
- Both pass provisionally only. The client-measured pack count (1/4/7/10, identical for A and B) is the stronger evidence of log-structured covers.

## Caveats
- The suite, its fake and its mutants share one author (blind to the builders). Validation shows each test can pass and fail on that author's fake. It cannot show the tests catch every way a real implementation might go wrong.
- One run per target (seed 11), on a shared host with heavy unrelated load before and between runs (load-gated below 16 at each start).
- CG7 (code audit) and CG8 (performance) have not been started. Every "Pass" above is behavioural, not architectural.

## Post-audit note (added 2026-11-06 after CG7; the text above is unchanged)
- **Planning reads rule: downgraded from "provisional pass" to "not established" for both builders.** Both CG7 auditors found that `/_stats metadata_reads` misses in-memory index and cache work (B audit D-1; A audit Q7/D15). The client-measured pack-count result stands.
- **Never unpacked into a repository (claim A1, paraphrased): established for both** (A1 executed by both auditors).
- See `campaign/audit/CG7-SUMMARY-AND-GAPS.md`.
