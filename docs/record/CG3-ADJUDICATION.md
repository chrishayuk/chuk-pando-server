# CG3 adjudication: `test_concurrent_writer_and_readers_see_complete_monotonic_history`

Independent reviewer. Date 2026-11-06. Nothing under `heldout/`, `campaign/eval/results/`, `campaign/RUNLOG.md` or any existing file was edited by me.
(`git status` shows `campaign/RUNLOG.md` modified in the working tree. That was not me; it is the controller's own concurrent edit.)
New files: `review/cg3/{review_cg3.py,run_review.sh,batch2.sh,batch3.sh,analyze.py,mut/}` and this report.
Raw outputs: `review-cg3-20261106T160240Z/` (kept in the private experiment record).
Legend: **V** = verified by me (code reading or my own rerun), **U** = taken from the controller's material without re-checking.

## Verdict

**(a) Diagnosis: ESTABLISHED, with one correction to the controller's framing.**
The failure is a start-up race in the test's precondition. Readers `clone` and then immediately `git fetch origin t` while the writer is still creating `t`. I found no evidence of a server defect in A or B.
Correction: the race is not "bad luck on a correct test". Its outcome depends deterministically on server speed.
- B loses it every time: 0/20 original-form passes, 0/10 instrumented, 0/2 in-file.
- A loses it intermittently: about 10-25% of original-form runs.
- B0 wins it only because stock Git is slower (clone ends at about 0.39 s, against about 0.20-0.33 s for A and B). It won 43/43 of my original-form runs (standalone, in-file, instrumented) but has no real margin; D1 (writer delayed) fails it 3/3 at 0.5 s and at 3 s.

**(b) Status of the ORIGINAL CG3 gate.**
- Both A and B are **not a pass as pre-registered**. The registered test failed, and nothing in `heldout/` was changed.
- The failure is **no longer "unresolved"** in the spec/05 sense, because a cause is established. Cause: test precondition race. The ruling that this does or does not convert the gate to a pass belongs to the owner, because spec/05 section 2 does not define a "resolved, test-attributable" state.
- Suggested record for each of A and B: "CG3 hidden suite: 7/8 pass, 1 fail. The failing test is a reader/writer start race (cause established, reviewer-confirmed). No correctness violation observed. Gate not claimed as passed. An amended-test result is reported separately and labelled post-hoc."

**(c) The "corrected" diagnostic.**
It is a fair correction and does not weaken the checks the original test actually makes. But "Corrected CG3 diagnostic: passes 5/5 on A, B and B0" should be reworded:
- It is a post-hoc variant of one test, authored after seeing the results. It is not a CG3 gate result.
- n=5 per target is small. The 95% Clopper-Pearson upper bound on the failure rate is about 52% at 0/5 and about 31% at 0/10, and the spec/05 convention is that a pass states its bound.
- The D2 form (and the original) cannot detect a stale-read server. It also has no "complete history" check beyond fsck. A stronger form I added does detect it (see Findings 6-7).
- Suggested wording: "Post-hoc variant of the one failing CG3 test (readers wait until `t` is advertised; monotonic and fsck assertions unchanged): 0 violations in 5/5 controller runs and 10/10 reviewer runs on each of A, B and B0. Diagnostic only; not a gate result."

**(d) Is any part of CG3 untested by the passing suites?** Yes. See Finding 9.
- In the official failing run, the substantive assertions of this test (monotonic, fsck on readers) were skipped for every failing reader. That was all three readers for B and two of three for A.
- The black-box coverage of "concurrent branch movement while a dependent pack is being accepted" (spec/03 item 3) is thin. The structural half of CG3 is unverified.

**(e) Overstatements: see Finding 10.**

## Findings

### 1. The race exists in the test code (V)
`heldout/tests_cg3.py:79-98`:
- The writer does `w.commit(); w.push(url, "main"); checkout -b t`, then starts the threads.
- `t` is first created by the writer's first `w.push(url, "t")` inside `writer()`, after another `git add`, `git commit` and `git rev-parse`.
- Each reader, in parallel, does `clone` and then loops `git fetch origin t` with no wait for `t` to exist.
- On a fetch error the reader appends `("fetch failed", ...)` and returns. So the first reader fetch to land before `t` exists fails the test.
- Nothing else in the test can produce `couldn't find remote ref t` against a correct server. `t` is only created by that push. The `main` setup push happens before the threads start. There is no delete.
- The sister test `tests_cg4.py::test_per_ref_monotonic...` already guards with `if refs/heads/{ref} not in ls_remote(url): continue`. So the author used the guard there and omitted it here.

### 2. Timing of each failure relative to the writer (V)
Instrumented copy of the original logic: `review/cg3/review_cg3.py`, with `RV_DELAY=0 RV_WAIT=0`. It differs from the original only in logging timestamps and continuing after a failed fetch. It still fails whenever the original would.
Output: `.../I0-a/`, `.../I0-b/`, `.../I0-b0/`, summarised by `review/cg3/analyze.py`. All times are seconds from the start of the test.

| target | runs | runs with a violation | failing fetches | start before first push began | overlap the first push window (start..ack) | start after first push was acked | fail after an earlier successful fetch |
|---|---|---|---|---|---|---|---|
| A | 10 | 3 | 6 | 0 | 6 | **0** | **0** |
| B | 10 | 10 | 30 | 0 | 30 | **0** | **0** |
| B0 | 10 | 0 | 0 | | | | |

- Typical B run: first push started 0.195 and was acked 0.255. All three readers finished the clone at about 0.212 and had their first fetch fail at 0.212-0.240, inside the push. Their next fetch started at 0.255 and succeeded with count 2.
- Typical failing A run: push 0.182-0.230. Clones finished at about 0.200, first fetch failed 0.200-0.227, next fetch succeeded.
- Every failure is a fetch concurrent with the push that creates `t`. No fetch that began after the push was acked failed. `t` never disappeared after being seen. No backwards counts. All fsck checks passed.
- Linearizability allows any outcome for an operation concurrent with the creating push. So there is no read-your-own-write or closure violation in this data.
- Caveat: the client timestamps cannot show whether a failing fetch began after the server had committed the ref but before the client received the ack. The existing CG2 test (`test_ack_implies_visible_to_new_reader`, passed by both) covers the "acked implies visible" half.
- Machine-load caveat: a 359% CPU training job (`wsp001_c1_run.py`) ran on the host throughout. Load average was 5-10, so timings are noisy.

### 3. D1 is a valid control (V)
D1 delays the writer, so every reader fetch that starts before `t` exists must fail on any correct server.
- I reran it at delays of 0.5 s and 3 s, 3 runs each. A: 0/3 and 0/3. B: 0/3 and 0/3. B0: 0/3 and 0/3 (all fail, "pass" counts 0).
- All failing fetches began before or during the first push; none after its ack. In the 3 s case there were hundreds of early failures per target, as expected.
- The controller's D1 on B0 (1 s) is reproduced in kind. The runs are `.../D1h-*` and `.../D1x-*`.
- Limit: D1 shows "an early fetch fails on stock Git too". It does not by itself show that A's and B's original failures had that cause. The timelines in Finding 2 are what show that.

### 4. D2 is a fair correction of the original's checks (V)
D2 adds a wait for `refs/heads/t` in `ls-remote` before the fetch loop (60 s cap, then violation "t never advertised"). The loop, monotonic check and fsck are identical to the original. I read `campaign/eval/diagnostics/diag_cg3_reader_race.py` line by line.
- A server that never creates or advertises `t` fails D2 after 60 s. This is by code reading only; I did not run it.
- A server that creates `t` and then hides it from fetch fails D2 as "fetch failed". I tested this with a purpose-built mutant (Finding 6).
- D2 pass counts in my reruns: A 10/10, B 10/10, B0 10/10 (`.../D2-*`). Each reader did 27-41 successful fetches. The monotonic check therefore had real exposure.

### 5. Controller's D2 runs (U, plus V for consistency)
The controller's 5/5 on each target is consistent with my 10/10. Their raw files are in `campaign/eval/results/diag-cg3-20261106T044353Z/`.

### 6. Mutation test: does D2 keep its power? (V)
Mutant servers are `heldout/mutants_review1/` (read only) plus one new mutant, `vanish`. `vanish` is a copy at `review/cg3/mut/` where `ls-refs` stops advertising `refs/heads/t` after 3 acked pushes. Runs are sequential; "kill" = test fails.

| mutant | original test (n) | D2 (n) | strict D2' (n) |
|---|---|---|---|
| backwards (ref goes backwards) | killed 10/10 | killed 10/10 (backwards violation) | killed 10/10 |
| vanish (t lost after creation) | killed 6/6 | killed 6/6 (fetch failed after prior success) | killed 6/6 |
| missingobj (dangling object) | killed 6/6 | killed 6/6 (clone or push error) | killed 6/6 |
| staleread (3 s ls-refs cache) | **survived 6/6** | **survived 6/6** | killed 6/6 |

- So D2 retains the original test's power on the mutants it was designed for. D2 does not weaken it.
- The original test also does not kill `staleread`. That matches `heldout/mutation_results_v2.json`, where `staleread` was killed by other tests only.
- "Strict D2'" is my addition, not the controller's. It asserts a reader sees at least 3 successful fetches, its last observed commit count equals 26 after the writer stops, the final fetched tip equals the writer head, and fsck passes. It passed 10/10 on each of A, B and B0.
- Note: the `orig` runs against mutants also hit the race, so a "kill" there cannot be attributed to the mutant alone. The D2 and strict columns are the clean ones.

### 7. What the test checks and does not check (V)
- The original and D2 check: no failed fetch, per-reader non-decreasing commit count, and fsck on the reader clone.
- They do not check that the reader ever sees the final state, so a reader frozen at an old but valid tip passes. That is why `staleread` survives.
- "Complete" history is only checked as fsck on a clone (closure). That is the right thing for dependencies but silent about progress.
- The unused `tips` list in the original (written but never read) suggests an intended final-state check that was never made.
- An early-return on the first failed fetch also skips the end-of-test fsck for that reader.

### 8. A is genuinely intermittent (V)
The same standalone original test (`pytest heldout/tests_cg3.py -k concurrent_writer`):

| target | passes / runs |
|---|---|
| B0 | 20/20, then 10/10 clean (43/43 across all original-form runs incl. in-file and the instrumented form) |
| A, round 1 | 20/20 |
| A, round 3 clean (`round3-clean/`) | 18/20 |
| A, round 2 (`round2/`) | 15/20 (**confounded**, see below) |
| B | 0/20 |

- A in-file, `pytest heldout/tests_cg3.py` ×3: 8/8 passed each time. The controller's full-suite run failed once for A.
- B in-file ×2: failed both times.
- Round 2 is confounded. An orphaned loop from a killed batch kept running and overlapped it, so there was CPU contention. I disclose it and exclude it from conclusions.
- Reading: A's pass probability depends on load. On a quiet host A mostly passes; on a loaded one it often fails. The controller's single failing run for A is real, but the framing "A and B each fail the same way" blurs that A's failure rate is intermittent and B's is deterministic.

### 9. Remaining CG3 coverage (spec/03 and `visible-gates.md`) (V by code reading; U for structural detail)
Spec/03 required tests: (1) delete and recreate, (2) force-push dropping depended-on history, (3) concurrent movement while a dependent pack is being accepted, (4) GC/retention, (5) pack depending on a later-superseded ref version, (6) stale haves / unrelated-history reader, (7) duplicate, empty and tag pushes.
- Items 1, 2, 4 and 7 have dedicated passing tests (`tests_cg3.py`).
- Items 5 and 6 are partly covered by the chain-of-pushes and force-push tests, which passed.
- **Item 3 is the weakest.**
  - The only test that has a reader fetching a branch while stock thin-pack pushes keep landing on it is the one that failed.
  - `tests_cg4.py::test_per_ref_monotonic...` does a similar thing on two branches and passed for both, with `ls-remote` gating.
  - Neither test has two concurrent writers moving the *same* branch while a dependent pack is in flight.
  - Consequently, for B (all three readers aborted) and for A (two of three readers aborted) the test's assertions on monotonic history and reader fsck were not exercised in the official run. The post-hoc runs fill this in (A and B: ≥33 and ≥37 successful fetches per reader, 0 violations), but those are not registered results.
- The first part of CG3, "packs stored unmodified as immutable blobs, never unpacked; record each pack's exact prerequisites", is not tested by behaviour. The structural indicators (`tests_structural.py`, both 2/2 for A and B, per the controller) are self-reported and gameable (`heldout/COVERAGE.md`), and by spec/05 section 3b they never gate C. Prerequisite recording is untested black-box and depends on the CG7 code audit.
- `tests_review1.py` F4 (unrelated branch during a heavy push) was skipped as inconclusive for all three targets (U; visible in `behavioral.txt`). It is not a pass.

### 10. What the controller got wrong or overstated
1. "D1 shows it is a defect in the test's precondition" over-reaches. D1 shows only that an early fetch fails on stock Git. The timelines in Finding 2 are the real evidence, and the controller did not collect any (U).
2. "Faster servers (A, B: clone ~2x faster than B0)" is unsupported in the diagnostic output as I read it (U). I measured it (V): reader clone end A about 0.20-0.33 s, B about 0.22 s, B0 about 0.37-0.39 s. First-push duration A and B about 0.05-0.07 s, B0 about 0.12 s.
3. "A and B each failed the same way" hides A's intermittency (Finding 8) and B's determinism.
4. "D2: assertions unchanged" is true, but the implied "so the test is still as strong" is incomplete. The test is not strong against stale reads (Finding 7), so passing D2 should not be read as "no stale-read problem", and D2 gives no assurance there.
5. "passes 5/5 on A, B and B0" is too confident without a bound and without saying it is post-hoc (see the proposed wording in (c)).
6. The RUNLOG states a cause with a causal sentence ("a defect in the test's precondition") before the reviewer confirmed it. I now agree with it, but it was premature at the time of writing.
7. The controller correctly did not edit `heldout/` or the scoring, and flagged owner decision. I found no misreporting of counts: 33 passed, 1 failed, 1 skipped for A and B, 34 passed for B0, all match `behavioral.txt` (V).

## Reruns (all sequential; builder servers only through the sandboxed `.evalbin/run.sh` wrappers; `PYTHONPATH=heldout`)

| Run | A | B | B0 | Output |
|---|---|---|---|---|
| Original test, standalone (round 1) | 20 pass / 0 fail | 0 / 20 | 20 / 0 | `round1-orig/`, `summary-orig.txt` |
| Original test, standalone (round 2, confounded) | 15 / 5 | | | `round2/` |
| Original test, standalone (round 3, clean) | 18 / 2 | | 10 / 0 | `round3-clean/`, `summary-round3.txt` |
| Whole `tests_cg3.py` file (8 tests) | 3/3 files all pass | 0/2 (1 fail each, same test) | 2/2 pass | `file-*.txt` |
| Instrumented original form, delay 0 | 7 / 3 | 0 / 10 | 10 / 0 | `I0-*` |
| D1, writer delayed 0.5 s | 0 / 3 | 0 / 3 | 0 / 3 | `D1h-*` |
| D1, writer delayed 3 s | 0 / 3 | 0 / 3 | 0 / 3 | `D1x-*` |
| D2 (readers wait for `t`) | 10 / 0 | 10 / 0 | 10 / 0 | `D2-*` |
| D2' strict (adds final-state checks) | 10 / 0 | 10 / 0 | 10 / 0 | `D2s-*` |
| Mutant `backwards`: original / D2 / D2' | | | | killed 10/10 each |
| Mutant `vanish`: original / D2 / D2' | | | | killed 6/6 each |
| Mutant `missingobj`: original / D2 / D2' | | | | killed 6/6 each |
| Mutant `staleread`: original / D2 / D2' | | | | survived 6/6 / survived 6/6 / killed 6/6 |

(Entries are "pass / fail" except in the mutant rows. Pass = test passed.)
Total test wall-clock was about 40 minutes. The host was shared with an unrelated CPU-heavy job throughout.

## Recommendations
1. Keep the pre-registered result as recorded. Report CG3 for A and B as "7/8 hidden tests pass; the 1 failure is a harness start race (cause established), no server defect found; gate not claimed as passed".
2. Treat the D2' strict form (or D2) as an owner-approved, labelled post-hoc replacement test. Report it with n and the bound, for example "0/10 violations on each of A, B and B0".
3. Add a final-state check to any amended test so that stale-read behaviour is caught (D2' does).
4. Have the owner or CG7 audit confirm the immutable-pack and prerequisite-recording requirements, which no behavioural suite covers.
5. Add a same-branch concurrent-writer test if spec/03 item 3 is meant to be covered black-box.
