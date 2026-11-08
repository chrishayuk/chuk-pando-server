# Remediation of the networked-storage re-audit

The independent re-audit of main `3f1fd8b` is archived verbatim, unmodified, in
`verify/audit/archive/net/` (AUDIT.md, coverage inventory, reproducers `repro/r01`–`r12b`; copied from the experiment record). This
document records what was changed in response, on branch `p11-remediate` (from `p10-fly`, `8a89136`). Where we
disagree with the audit, the deviation is stated explicitly.

The auditor's reproducers are run **unmodified** against any tree with `verify/audit/run_repro.sh TREE OUT [r01 …]`.
That script builds the layout they expect from symlinks to the archived files, and refuses to run if the archive
differs from the committed record. `verify/run_suites.sh` runs all of them as the gated suite `repro`.

## Order of work (owner direction)
1. Repair the regression gate, and show that it rejects real regressions.
2. F2.
3. F1, then F3.
4. Investigate D2 (F12) and the P1 crash-test failure without assuming they are environmental.
5. F4–F10.
6. The auditor's reproducers against the final tree.
7. Only then Fly.io.

## 1. Regression gate
**Audit finding.** `run_suites.sh` discarded every suite's exit status, and a breach of the unit-test floor only
printed a message. So earlier "no new failures" summaries were never a gate, and are weaker evidence than they were
taken to be.

**Change.** Every suite writes a per-test record: JUnit XML, or `cargo test` output for the unit tests.
`verify/gate/gate.py` decides the exit status from those records alone. It fails on:
- an unexpected failure or error;
- an unexpected skip;
- a required test that did not run (deleted, renamed, deselected or not collected);
- fewer tests than the suite's floor;
- a missing record, meaning the suite did not run;
- suite totals that exceed the per-test failures.

`verify/gate/expectations.json` lists every registered test (108 at the baseline). Each known failure and each
allowed skip carries a reason and a reference:
- **CG3 `concurrent_writer_and_readers…`:** the test's own precondition race, established by an independent reviewer
  (`docs/record/CG3-ADJUDICATION.md`).
- **CG6 `compaction_levels_reduce_packs_per_clone`:** asserts that `maintain` itself adds covers. Ours are built in
  the background (`campaign/eval/CG5-CG6-SCORING.md`, note 1).
- **CG6 `parallel_pack_download`:** the single-job oracle is unresolved. The `par` suite runs the test as the fork
  client's documentation requires.
- **Allowed skip, `review1` F4:** inconclusive by construction against a streaming server, which ingests while the
  body arrives, so the heavy push finishes before the probe. The same property is tested by `verify/dist` D2.
- **Allowed skip, dist D5 in S3 mode:** there is no shared tmp directory, so the test doesn't apply.

The runner now also builds the tree under test first and records the binary's hash. Before, `run.sh` built only
when the binary was missing, so a stale binary could be tested.

**Evidence.**
- 11 self-tests (`verify/gate/test_gate.py`).
- A baseline run of `8a89136`: gate PASS.
- A live demonstration on real suite runs (`verify/gate/demo_rejects.sh`; record
  `verify/results/20261107T170819Z-gate-demo`). Each of these was rejected for its specific reason:
  - a deleted P1 test;
  - a deliberately failing P1 test;
  - a skipped unit-test module;
  - a suite that did not run.
- That demonstration also caught a real, unplanted, intermittent failure: P1Copies (see below).

Frozen suites (hidden CG1–4 and review1, CG5/6) are unchanged and manifest-checked; only invocation flags were added.

## 2. Findings

Columns: **Before** is the result on the audited code (`8a89136`); **After** is the result on `p11-remediate`.

| # | Audit severity | Our triage | Fix | Failing-before / passing-after evidence |
|---|---|---|---|---|
| F2 | major | **critical**: acknowledged data correctness | On 412 the stored object is downloaded and verified. Packs must be byte-identical. Indexes must be semantically equivalent (`PackIndex::equivalent_to`): the only accepted difference is the pack an external delta base was found in, and that pack must hold the base. Substitution maps must be identical or valid entry by entry. | Auditor's **R03**: Before, defect (2 checks). After, none. **N6** (3 tests): a valid but non-equivalent index is refused; an equivalent index naming another pack that holds the base is accepted and served by a cold frontend, while one naming a pack without the base is refused. Unit: `index_equivalence_accepts_only_a_different_base_pack`. |
| F1 | major | high | `MAINT_LOCK` is taken poison-tolerantly. Each background job and the startup catch-up run under `catch_unwind`. A repository whose job failed is retried by a full per-repository pass every 30 s until it succeeds. | Auditor's **R01** (real outage through a fault proxy): Before, defect (2 checks). After, none. **D9**: with injected job panics, covers and copies complete with no further push. |
| F3 | major | high | Instance tmp areas are created and locked under a staging name, then renamed into place, so the sweep never sees an unlocked live area. Abandoned staging areas are removed by age. Names carry a per-process sequence number. | Auditor's **R06**: Before, 4 failed starts and 5 lost areas in 60 concurrent starts. After, none. Unit `concurrent_shared_starts_never_sweep_a_live_area` (50 rounds × 12): fails with the old order. |
| F12 (D2) | minor, cause unknown | high (A4) | **Investigated.** Commit locks were 4096 hash stripes over read and write keys, so an unrelated push waited for a stalled commit when one of its keys shared a stripe. They are now **exact per-key locks**: written keys exclusive, read-only keys shared, acquired all at once. Other slow runs were host stalls (see below). | **D2KeyLocks** forces a former collision (`refs/heads/c4002` shares `refs/heads/slow`'s old stripe): Before, waited 2.44 s (unix-fs and tcp-s3). After, 0.02 s. Unit `unrelated_key_is_not_delayed_by_a_held_commit_even_on_an_old_stripe_collision`: Before, waited 1.32 s. Probe, 40 tcp-s3 runs: no fast push over 0.3 s. Throughput: pre-registered check passed (below). |
| F4 | minor | medium | The results of committed groups are kept when a later group fails (`PartialPush`). | Auditor's **R02(b)**. **D10**. |
| F5 | minor, by design | medium | After a lost commit reply, the frontend reads back the version keys the transaction wrote. They are write-once and record the push id, so if all carry this push's id the commit was applied and the update is reported `ok`. Otherwise "outcome unknown" stands: a request possibly still in flight is never reported as either outcome. | Auditor's **R02(a)**: Before, defect. After, none. **D10**: a lost reply is reported `ok` and the ref is present; a commit request dropped before reaching the service leaves the ref unchanged and is **not** reported `ok`. |
| F6 | minor | medium | 3 s TCP connect timeout. Object-store reads and writes time out after 15 s without progress (`PANDO_S3_TIMEOUT`); metadata-service reads and writes after 20 s (`PANDO_META_TIMEOUT`). | Auditor's **R05**: a stalled store held a push 120.1 s Before, 15.0 s After; a stalled metadata service hung a request more than 45 s Before, 20.0 s After. |
| F7 | minor, latent | medium | The snapshot carries the highest allocated LSN, as a tombstone under a reserved key. | Auditor's **R08**: Before, defect (2 checks). After, none. Unit `lsn_never_regresses_across_checkpoint_after_deletes`: fails without the record ("3 after 8"). |
| F8 | minor | low | A per-request flag records the switch to side-band; later errors go out on band 3. | **N7**: Before, "bad band #69". After, "remote: pack <id> is missing from the object store". |
| F9 | minor | medium | Downloaded and shared substitution maps are validated before use, and an invalid one is never used. A transient fetch failure is not remembered as "no map". A downloaded pack must match its index's size. The fetch path no longer indexes a map entry out of range. | **N6** corrupt-map test: Before, an incremental fetch failed ("bad pack header", an out-of-range entry). After, the map is rejected and every fetch is correct. |
| F10 | minor, read only | medium | Covered by the F2 map rule: a valid stored map that differs is accepted. | **N6** (now *executed*): a valid differing map planted at a cover's key. Before, `maintain` failed ("has 73 bytes, expected 138: refusing to proceed"). After, it completes, and fetches are correct. |
| F11 | minor | — | Fixed earlier on `p10-fly` (public signed packfile URIs, `PANDO_BIND`). | `verify/fly/test_access.py` (5). |
| F13 | minor (fidelity) | kept, documented | Pushes to **different** refs that set the same new tip or carry the same pack share `tip/<oid>` and `pack/<id>` in their transaction. Those keys hold genuinely shared write-once state, so contention there is inherent, and correctness is preserved. A deviation from claim A4, where the article (https://openai.com/index/scaling-git-at-openai/) describes the ref pointer as the only contended key (our paraphrase, not OpenAI's words). | Auditor's **R11** stays a known failure of the `repro` suite, with this reason. |

### Deviations from the audit's findings
- **F9, partly overstated.** The fetch path already re-checked every map entry before substituting
  (`packout.rs`: same object, stored as a delta), so a map pointing at the *wrong objects* was harmless. The real
  defects were an entry pointing past the end of an index (a panic, so the fetch failed) and the cached negative
  result. Both are fixed; the test covers both kinds of corruption.
- **F12 cause.** The auditor left the cause open, suggesting an occasional A4 violation or host load. Both
  contributed:
  - **Stripe collisions** (probe and its raw data: `docs/evidence/d2-probe/`). Probe run 12: `slow`'s ref key and fastA's tip key were in the same stripe. fastA's commit
    waited 2375 ms and ended 4 ms after the stalled commit.
  - **Host CPU stalls.** No phase waited on a lock. The time went to the client's pack building, the advertisement,
    and two tiny object-store PUTs (667 ms, normally about 4 ms).

  The test's own hook also stalls the *rival* commit for 3 s (it writes `refs/heads/slow` too). That explains the
  auditor's 5.4 s case: a collision with the rival's stripes.
- **F2 severity.** We raised F2 to critical, as the owner directed: it breaks acknowledged-data correctness under a
  demonstrated pre-existing-object conflict.

## 3. Test defects the audit found
| Defect | Fix |
|---|---|
| P1Copies crash test failing intermittently (auditor 3 of 3; cause not established) | **A client-side race in the test.** GIT_TRACE shows git starting a detached `maintenance run --auto` after each fetch. It runs `repack -d -l --cruft --write-midx` in the reader while the test's `fsck` reads, and fsck then fails on the repack's temporary `.tmp-<pid>-pack`. The readers now disable client auto-maintenance, as `verify/dist` already did. Same server, similar load: old test file 3/8 failed, new 0/16. |
| P1Recompress leftover-workspace race (the restarted process's own compaction) | The server is frozen with SIGSTOP when a workspace appears, so what exists at the kill is exact, and only those workspaces must be gone afterwards. Previously it could also kill after the workspace was gone, testing nothing. |
| `p1:179` `built <= thin \| built`, always true | `built <= thin` |
| `subst/equiv.py` always exits 0 | Exits 1 when responses differ |
| Randomised out-of-order test never checks that reordering happened | Asserts records were observed above the stable mark (about 1M per run), and that a naive max-LSN reader misses keys under the same schedule (260–280 of 480) |
| SHA-256 padding-boundary loop with no assertion | Checked against independently computed values (Python `hashlib`) for 50–69 bytes |
| `tests/cg2.sh:78` checks only that a directory exists | **Not changed.** `tests/*.sh` are inherited from the original build and are not run by the gate. Recorded here rather than fixed in place. |

## Pre-registered check: exact per-key commit locks (F12)

Registered before measuring, 2026-11-07.

- **Change.** The metadata store's commit locks go from 4096 hash stripes to exact per-key locks (one mutex and
  condition variable over a held-key table). This is on the commit hot path.
- **Hypothesis.** No loss of push throughput.
- **Method.**
  - Script: `verify/net/measure.py`, unchanged, the same one used for the p8/p9 figures. Modes unix-fs, tcp-fs and
    tcp-s3, with 8 and 16 writers, 10 pushes each.
  - Order: base (`8a89136`, main checkout), then fix, then base, then fix, back to back.
  - Load: each run starts only when the 1-minute load average is below 16. The load is recorded before and after.
- **Criterion.** In each mode at 16 writers, the fix's mean pushes/s over its two runs must be at least 0.85 × the
  base's mean. If any mode falls below that, the change is not merged until the cause is understood.
- **Reported:** all eight numbers, whatever the outcome. This is a regression check on one host, not a capacity
  claim.

**Result** (added after measuring; the registration above is unchanged; run 2026-11-07, 17:37–17:39, load average
1.5–6.1; raw data copied to `docs/evidence/keylock-perf/`):

| mode | writers | base1 | fix1 | base2 | fix2 | base mean | fix mean | ratio | criterion |
|---|---|---|---|---|---|---|---|---|---|
| unix-fs | 8 | 35.7 | 34.6 | 35.6 | 35.7 | 35.7 | 35.2 | 0.99 | – |
| unix-fs | 16 | 49.5 | 50.0 | 50.0 | 50.4 | 49.8 | 50.2 | 1.01 | **pass** |
| tcp-fs | 8 | 15.0 | 37.2 | 36.6 | 37.0 | 25.8 | 37.1 | 1.44 | – |
| tcp-fs | 16 | 48.9 | 40.9 | 52.3 | 52.7 | 50.6 | 46.8 | 0.92 | **pass** |
| tcp-s3 | 8 | 45.7 | 44.5 | 34.3 | 48.8 | 40.0 | 46.6 | 1.17 | – |
| tcp-s3 | 16 | 56.5 | 56.7 | 30.5 | 46.9 | 43.5 | 51.8 | 1.19 | **pass** |

The check passed in every mode. Run-to-run noise is large on this shared host: single runs of 15.0 and 30.5 are far
below their pairs. So this shows **no regression**, not an improvement.

## Remaining, and what it means
- **Metadata service:** still a single point of failure (unchanged and documented). Its TCP protocol is
  authenticated only by the optional shared-secret handshake.
- **F13:** kept as a documented A4 deviation (above).
- **Not re-tested here:** power loss, real-disk `F_FULLFSYNC` behaviour, NFS `flock` semantics across hosts,
  multi-host operation. These remain as stated in the audit's "could not establish".
- **For the video:**
  - The implementation can be described as *independently audited, with every finding remediated or explicitly
    accepted, and the auditor's own reproducers passing*.
  - It should not be called *independently verified* without a further fresh-context review of these changes.
  - None of this supports a claim of equivalence with OpenAI's unpublished production implementation.

## Final regression (2026-11-07, `p11-remediate` at the merge `4c4edc6`, record `verify/results/20261107T174330Z-remediate-4c4edc6`)
**Gate PASS, including strict mode, with 136 registered tests.**
- Every suite passed except the documented known failures: CG3 race, CG6 compaction levels, CG6 single-job
  parallel, and R11 (F13).
- The skips were the two allowed ones.
- Unit: 17 tests. dist: 14 (unix-fs) and 13 + 1 allowed skip (tcp-s3). net: 11. fly: 5. p0: 4. p1: 5.
- **Auditor's reproducers: 12 of 13 no longer reproduce their defect.** These include the controls (R04, R07 kill
  storm, R10) and R09, the D2 replay. R11 (F13) is kept as a deviation.
- The 15 tests added for this remediation were registered from that run's records. The change to the expectations
  file was additions only, checked mechanically.

## 4. Bounded review of this remediation (2026-11-07), and fixes
A fresh-context reviewer examined the diff `3f1fd8b..e40d36b`. Its outputs are archived verbatim in
`verify/audit/archive/rev/`. Its reproducers run unmodified with
`G_REPRO_SET=rev verify/audit/run_repro.sh`; its gate counterexamples with `verify/gate/run_review_cx.sh old|new`.

**Verdicts:**
- F2: did not hold.
- F1, per-key locks, lost-reply recovery and the gate: held with caveats.
- **Confirmed:** no lost updates or deadlocks from the per-key locks, and no path that reports `ok` for an
  unapplied update.

Columns: **Before** is the result on `e40d36b`; **After** is the result with the fix.

| # | Finding | Fix | Evidence (Before → After) |
|---|---|---|---|
| V1 (major) | The F2 check accepted a stored index naming any string as an external base's pack: a path into another repository's cache, or an index-only object | The name must be a pack id (40 lowercase hex), its pack object must be in the store, and its index must hold the base. Pack ids are validated wherever they become a path or a key (`index`, `file`, `fetch`, `ensure_local_pack`, substitution maps). | Reviewer's R20, both variants: acknowledged → refused |
| V2 | One broken repository blocked catch-up and `maintain` for every later one | Repositories are processed independently; catch-up runs per repository; failures are reported together | R21: reproduced → clean |
| V3 | A metadata outage between groups of a non-atomic push killed the request | Each group runs under `catch_unwind`; a panic becomes a `PartialPush` error | R22: "remote end hung up" → `main` ok, `topic` rejected with the reason |
| V4 | Writer starvation behind overlapping shared holders | Ticketed writer preference: a waiting writer blocks *later* shared requests for its key. Blocking all of them deadlocks crossing writers, which a new test shows. | Reviewer's starvation test (adopted): starved more than 10 s → passes; `crossing_writers_with_shared_reads_do_not_deadlock` fails with the naive rule and passes with tickets |
| V5 (major) | A known failure masked any new failure of the same test | Signatures: kind, `match` and `forbid` patterns per known failure; an exception without one is a failure | Reviewer's cx01, 5 masking cases: PASS → FAIL |
| V6 | An allowed skip passed whatever its reason | Skip reasons matched | cx02: PASS → FAIL |
| V7 | No freshness check; silent `--suites`; unknown flags accepted; strict off; a crashed second test binary missed; reproducer verdict from the last line only | `--require-meta` (records newer than the run's `meta.txt`, the tree's binary equal to the recorded one) and `--strict`, both always used by `run_suites.sh`; PARTIAL labelling; usage errors; a binary started without a result fails; reproducers need exit 0 | cx04 and cx05: PASS → FAIL or usage error; gate self-tests 11 → 18 |
| V8 | A 412 downloads the whole stored object (memory and traffic) | **Not changed, documented.** It happens only when the same object is published twice: retries, or duplicate background work by two frontends. Packs must be compared exactly; anything less re-opens F2. | — |
| V9 | The I/O timeouts bounded a read *without progress*, so a trickling store could hold a push indefinitely | An overall allowance per attempt (`PANDO_S3_DEADLINE`, 60 s, plus 1 s per MiB moved) | Relay test with a response-only trickle: held 95.7 s with the allowance disabled → failed unacknowledged after 10.1 s with it |

The new suites `repro-rev` (R20–R22) and `fault` (the object-store relay: pass, stall, refuse, latency, trickle)
are part of `run_suites.sh`.
