You are reviewing, with a fresh context, the **remediation** of an independent audit of a Git-compatible server ("Pando"). This is a bounded review of the changes, not a repeat of the full audit. The builders believe the fixes are correct; your job is to find where they are not, above all by **executable counterexamples**.

## What you have
- `inputs/original-audit/AUDIT.md` and `inputs/original-audit/repro/`: the earlier auditor's findings and reproducers. These state what was broken.
- `inputs/review/remediation.diff`: everything that changed between the audited snapshot (`3f1fd8b`) and the code under review (`e40d36b`). `remediation-commits.txt` has the commit messages.
- `impl/`: the exact snapshot under review (results directory removed). The server is `impl/server/src/`, tests are under `impl/verify/`, and the regression gate is `impl/verify/gate/`.
- **Claims, not evidence:** the builders' account of the remediation is `impl/docs/AUDIT-NET-REMEDIATION.md`, and their commit messages and code comments are also claims. Check them; don't rely on them.
- `inputs/source/`, `inputs/spec/01-claims-ledger.md`: the published architecture (A1–A12). `inputs/campaign/INTERFACE.md`: how harnesses drive the server.
- `clients/`: stock Git (`clients/git-stock`), the harness library (`clients/heldout/harness/lib.py`), and MinIO (`clients/minio`).
- **Building:** `impl/` is read-only. Copy it to `work/impl` and build there with `CARGO_HOME=$PWD/impl/.cargo-home CARGO_NET_OFFLINE=true cargo build --release`.
- **Running the builders' suites:** `verify/*/run.sh` with `CHUK_PANDO=$PWD/clients`, `G_CLIENTS=stock=$PWD/clients/git-stock`, `G_MINIO=$PWD/clients/minio`, and `SERVER_DIR` set to your copy.
- **Running the original reproducers:** they expect the layout `<root>/work/repro`, `<root>/work/impl` and `<root>/clients`. Copy them into `work/repro`, and keep your build at `work/impl`.
- There is no network beyond localhost. Write only under `work/`.

## Focus (in priority order). For each: what the new code does, where it can fail, and a reproducing test if it can.
1. **F2: S3 content-equivalence on 412** (`blob.rs` `publish`, `check_alt`, `alt_map`; `pack.rs` `PackIndex::equivalent_to`).
   - Is the check sufficient to guarantee that an acknowledged push's objects, as stored, can be served correctly by every frontend?
   - Look for differences it accepts that matter: index fields not compared; external-base packs that hold the base but are unusable (not committed, or in another repository); map entries that pass validation yet produce wrong output.
   - Look also for differences it wrongly refuses: legitimate concurrent publication by two frontends.
   - Consider the cost and failure modes of the extra GET.
2. **F1: panic recovery in the background worker** (`compact.rs`).
   - Does background work always make progress after an outage, or can a job be lost forever?
   - Can a recovered panic leave in-memory or persisted state inconsistent, now that `MAINT_LOCK` poison is ignored?
   - Can the retry loop starve, busy-loop or block foreground work?
3. **Exact per-key commit locks** (`meta.rs` `KeyLocks`, `commit`).
   - Look for deadlock, starvation, lost updates or broken compare-and-swap.
   - Check shared versus exclusive handling: read-only keys are shared, written keys exclusive. Is that sound for the version check?
   - Check interaction with checkpoints (the `gate` lock), the WAL group commit, and errors or panics while locks are held.
   - Do two transactions touching the same key ever both commit against the same version?
4. **Lost commit-reply recovery** (`repo.rs` `applied_by`, `is_outcome_unknown`, `PartialPush`; `proto.rs`).
   - Can a push ever be reported `ok` when its update was not applied, or was applied by someone else in a way that differs from what the client asked?
   - Consider retries, concurrent pushes to the same ref, multi-ref and atomic pushes, deletes, and a commit still in flight in the service.
   - Is the F4 partial-result reporting correct for every mix of committed and failed refs?
5. **The regression gate** (`verify/gate/gate.py`, `expectations.json`, `verify/run_suites.sh`).
   - Does it faithfully enforce the registered expectations?
   - Look for ways a regression could still pass: a known failure masking a new failure mode in the same test, subtests, renamed classes, duplicate ids, parsing gaps, a suite crashing after writing a partial record, stale binaries, `--suites` subsets, and the exit-status path through `run_suites.sh`.
   - Check whether the registered exceptions are justified by what each test actually does.

Also, briefly: anything else in the diff that introduces a defect (F3–F10, F12, the test changes, `deploy/fly/`).

## Rules for evidence
- Prefer **execution**. Write each counterexample as a minimal script or test under `work/repro/` that fails on this snapshot, and run it. Fault injection is encouraged: proxies, kills, stalls, a corrupted store, and instrumented copies of the code under `work/`.
- Cite `file:line` for every code claim. Say whether each finding was **executed** or **read only**.
- Do not modify `impl/`. Do not fix anything: report.
- This review is bounded. Stay within the diff and its direct consequences; mark anything you did not examine as **not examined**.

## Output
Write `work/REVIEW.md`. Keep it updated as you go, so a budget cut-off still leaves a usable record. It should contain:
- a **summary table**: finding → severity (blocker / major / minor) → executed or read → reproducer path;
- for each focus area, a verdict (**holds**, **holds with caveats**, or **does not hold**) with the evidence;
- what you could not establish, and why.
