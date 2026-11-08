# Review of the remediation (3f1fd8b → e40d36b)

Scope: the changes in `inputs/review/remediation.diff`, focus areas 1–5, plus a brief pass over the rest.
Builders' claims (`impl/docs/AUDIT-NET-REMEDIATION.md`, commit messages, comments) were treated as claims to check.

**Setup.**
- `work/impl` is a copy of `impl/`, built with the given offline cargo command. The binary was copied to
  `work/impl/server/target/release/pando`.
- `work/instr/server` is the same source with one appended `#[cfg(test)]` module. It is otherwise byte-identical
  to `impl/` (checked with `cmp`).
- Reproducers are in `work/repro/`. They reuse the auditor's `rlib.py` unchanged. Each prints
  `DEFECT REPRODUCED` and exits 1 when its defect shows; output is saved as `work/repro/rNN.out`.
- Gate counterexamples are in `work/gate-review/cx/`. That work was delegated; I re-ran and checked the key result.
- Citations are `impl/server/src/<file>:<line>` at e40d36b unless another path is given.
- Sanity check: the auditor's R03 on this snapshot shows no defect (`work/repro/r03.rerun.out`), so the harness works.

## Summary table

| # | Finding | Severity | Evidence | Reproducer |
|---|---------|----------|----------|------------|
| V1 | **F2 is not closed.** On a 412 for an index, a stored index that names a different pack for an external delta base is accepted if *some index* called that name holds the base (`blob.rs:211-215`). The named pack is never checked to be committed, to exist in the store, or even to be a pack id. A name like `../../s/packs/<id>` resolves into **another repository's** local cache on the publishing frontend. The push is acknowledged and the publisher serves it, but a cold frontend cannot (its shallow clone fails). | **major** | executed | `work/repro/r20_f2_ext_base_pack_unusable.py` → `r20.out` |
| V2 | **F1: one broken repository blocks catch-up for all others.** Background work missed before a restart is recovered only by the catch-up `maintain_all`, and a failed catch-up is retried as one whole pass (`compact.rs:191-192`). `maintain_all` stops at the first failing repository (`compact.rs:280`, `?`). So one permanently failing repository stops covers from being built for every repository after it, indefinitely, and the operator's `maintain` fails the same way. Restoring the broken repository unblocks the others (control). | minor | executed, with a control | `work/repro/r21_f1_catchup_blocked_by_one_repo.py` → `r21.out` |
| V3 | **F4 fix misses outages between groups.** The fix covers only an `Err` from a later group (`repo.rs:891-897`). A metadata outage after group 1 commits panics the request in `Remote::get` (`meta.rs:873`). The client gets "the remote end hung up unexpectedly" for both refs although `main` was applied. | minor | executed | `work/repro/r22_f4_outage_between_groups.py` → `r22.out` |
| V4 | **Writers can starve under per-key locks.** A transaction that writes a key waits until *no* transaction holds that key shared (`meta.rs:31-34`). Readers keep re-acquiring shared, so the writer can starve. The old striped mutexes were exclusive and did not have this property. In the test, the writer of `hot` did not commit within 10 s while about 9,500 read-only-on-`hot` commits went through. I did not identify a current server key pattern that triggers this (latent). | minor | executed (unit test in instrumented copy) | `work/instr/server/src/meta.rs` (`review_tests`, appended) → `work/repro/r23_keylocks_writer_starvation.out` |
| V5 | **Gate: a registered known failure hides any new failure in the same test.** `gate.py:74-77` accepts a known-fail id whatever the failure: message, assertion, or `error` vs `failure`. A real monotonicity violation in CG3, a server crash in CG3's setUp, a CG6 corrupt clone, and R11 failing its *control* check (a real A4/D2 regression) all give **GATE: PASS**. | **major** (for the gate's purpose) | executed | `work/gate-review/cx/cx01_known_fail_masking.py` |
| V6 | **Gate: an allowed skip passes whatever the skip reason** (`gate.py:79-83`). For example, review1 F4 skipped because the server failed to start still passes. | minor | executed | `work/gate-review/cx/cx02_skips_dups_junit.py` |
| V7 | **Gate: smaller gaps, all minor.** No record-freshness check (the binary hash in `meta.txt` is never read). `--suites` / `G_GATE_SUITES` narrow the check silently. Unknown flags (`--strickt`) are accepted. Unregistered tests can disappear unless `--strict` is on (off by default, `run_suites.sh:62`). A crash of a *second* cargo test binary would be missed (latent: the crate has only one). The reproducer verdict comes from the last output line only (`verify/audit/run_repro.sh:35-36`). | minor | executed (`cx03`–`cx05`) / read | `work/gate-review/cx/` |
| V8 | **Extra GET cost on 412.** Every 412 GETs the *whole* stored object into memory, next to the whole local file (`blob.rs:197, 202`). Packs are compared byte-for-byte, so a second frontend publishing a deterministic cover or copy downloads the entire cover. That doubles memory per publish and adds store traffic. Failure is fail-safe (the push fails). | minor (cost) | read only | – |
| V9 | **F6 bound is per read, not overall.** The new store and metadata timeouts are per read or write without progress (`PANDO_S3_TIMEOUT`, `PANDO_META_TIMEOUT`). The 10 s retry deadline is still checked only between attempts. A store that trickles bytes can hold a push without bound. | minor | read only | – |

Not a finding: the delegated gate review reported that `--strict` fails on the fly suite because `UriExpiry`
inherits three `Access` tests. I checked this, and it is **wrong**: `verify/fly/test_access.py:116-118` set the
inherited tests to `None`, so they are not collected.

## Focus 1 — F2: S3 content-equivalence on 412 — **does not hold**

**What the new code does.**
- On 412, `publish` GETs the whole stored object (`blob.rs:202`) and accepts it if it is byte-equal.
- **Pack:** it must be byte-identical (`blob.rs:222`). This is sound.
- **Index:** `PackIndex::equivalent_to` (`pack.rs:325-365`) compares:
  - the id and pack size;
  - every `Entry` field (all nine stored fields);
  - prerequisites and tags, as sorted sets;
  - the multiset of external-base oids.

  These are all the serialised fields (`pack.rs:370-401`), so **no index field goes uncompared**. It returns the
  (base, other pack) pairs where the stored index chose a different pack. `publish` accepts each pair if
  `self.index(repo, p)` finds the base (`blob.rs:211-215`).
- **Map:** it is parsed and checked with `check_alt` (`blob.rs:230-243`).

**V1 (executed): the external-base pack check accepts packs that cannot be served.**
- **Where `p` comes from.** `p` is a free string in the stored index (`pack.rs:441`). `index()` joins it into a
  local path (`blob.rs:517`) and an S3 key (`blob.rs:161-163`).
- **Variant B (one planted object).** The stored `P2.pidx` names `../../s/packs/<P1>`. The publishing frontend reads
  repository **s**'s cached index, finds the base, and acknowledges the push.
- **Variant A.** The stored index names `FAKE`, and only `FAKE.pidx` exists in the store (no pack, never committed).
  The push is acknowledged.
- **What a cold frontend does.** It resolves the base through the stored index (`blob.rs:585-588`) and fails:
  - A: `pack ffff… is missing from the object store`
  - B: `GET repos/tb/packs/../../s/packs/<P1>.pidx -> 400`
- **Why the publisher looks healthy.** It keeps serving from its own local index, so the failure is invisible there.
- **Which reads fail.** Full clones still succeed, because the thin pack is spliced raw and the client resolves the
  base. A shallow clone fails, because the server resolves the delta (`shallow.rs` → `read_object`). Covers built
  on that frontend take the same path (`build_cover` → `write_spliced`, `no_ref_delta`), so background compaction of
  that repository would fail permanently there (compare V2). That last step is **read only**.
- **Threat model.** It is the same as the original F2 (a foreign, stale or corrupt object already at the key).
- **What a sound check needs.** `p` must be a hex pack id that is committed (`pack/<repo>/<p>` in metadata), as a
  level-0 pack of this repository, and its pack object must be present. Alternatively, the stored index could be
  required to name exactly the pack this frontend chose.

**Other points (read only):**
- **Map entries.** `check_alt` checks only that an entry names an object of the cover and that an index named `p`
  lists that object as a delta. It does not check that `p` is committed, that its pack exists, or that `p` is
  well-formed (`blob.rs:236`, same path join). The fetch path re-checks the entry, and substitution needs the base to
  have been emitted (`packout.rs:122-125`). So a wrong map produces wrong output only if `p`'s *index* lies about
  its pack. Downloaded indexes are verified only by checksum and id, not against the pack (`blob.rs:274-279`). I
  did not execute this; it needs two planted objects.
- **Legitimate concurrent publication.**
  - Two frontends that ingest the same thin pack can choose different base packs. The choice comes from
    object-index registration order (`repo.rs:415` `or_insert`). Both packs are committed level-0 packs, so V1's
    weak check *accepts* this case, and so would the stricter one I suggest.
  - Concurrent covers and copies are deterministic in pack and index bytes. Their maps may differ, and are accepted
    when valid.
  - I found no legitimate case that is wrongly refused, apart from an index in another format version (none exists).
- **Cost and failure modes.** See V8. A GET that returns 404 after a 412 fails the push, which is safe and
  unchanged. A 412 with corrupt stored bytes makes that pack permanently unpushable in that repository (fail-safe,
  by design).
- **Downloaded-pack size check.** The new check against the index size (`blob.rs:541-546`) is sound.

## Focus 2 — F1: background-worker panic recovery — **holds with caveats**

**What the new code does.**
- Each job, the startup catch-up and each retry run under `catch_unwind` (`compact.rs:172-188`).
- A failed job puts its repository in an in-memory `retry` set (`:213`); a failed catch-up puts `""` (= all) there
  (`:192`).
- Retries run after 30 s of idleness, or at most every 30 s (`:195`, `retry_due` `:255-265`).
- `MAINT_LOCK` ignores poison (`:269-271`).
- The release profile does not set `panic = "abort"` (`server/Cargo.toml`), so `catch_unwind` is effective.

**Findings.**
- **V2 (executed): a job can be lost while another repository is broken.** The `""` retry and the operator's
  `maintain` both run `maintain_all`, which aborts at the first failing repository. In `r21`:
  - With repository `a` missing one pack, repository `b` stayed with 0 covers for 15 s across about 11 retry passes,
    plus one operator `maintain`, which returned 500.
  - After `a`'s pack was put back, both repositories were covered within 6 s.
  - Per-repository retries (from failed *jobs*) are independent; only the catch-up path has this problem.
- **Unbounded retries (read).** A permanently failing repository is retried every 30 s forever. Each retry is a full
  per-repository pass that holds `MAINT_LOCK` during `compact_ref` and while copies are prepared, so foreground
  `maintain` queues behind it. It is not a busy loop: `recv_timeout` plus `retry_due` limit it to one pass per
  interval. Foreground pushes do not take `MAINT_LOCK`.
- **State after a recovered panic (read).**
  - The work under `MAINT_LOCK` is: cover and copy files installed write-once, publication (create-only), then one
    metadata transaction. A panic part-way leaves orphan files that a later run reuses, because cover bytes are
    deterministic.
  - I checked the in-memory locks on these paths (`rs.idx` in `ensure_index`, `repo.rs:349-397`; the twin and rv
    caches). None is held across a metadata call, so a `Remote::get` panic does not poison them.
  - The `retry` set is in memory only. After a restart, the catch-up takes over, with the V2 caveat.
  - The claim "leaves nothing inconsistent" holds as far as I read.
- **Panic injection (read).** `PANDO_TEST_PANIC_JOBS` also makes retries panic (only the catch-up is exempt,
  `compact.rs:237-246`). D9 therefore exercises recovery only once the counter runs out.

## Focus 3 — exact per-key commit locks — **holds with caveats**

**What the new code does.**
- `KeyLocks` is one mutex plus a condition variable over `HashMap<key, -1 | n>` (`meta.rs:17-68`).
- A transaction's written keys are exclusive and its read-only keys shared. They are sorted and de-duplicated by
  key (`meta.rs:334-341`) and all acquired at once.
- Validation of read versions (`meta.rs:350-355`), the WAL append, durability and application to `data` all happen
  while the guard is held. The guard drops at function exit (`_locks`).

**Soundness of compare-and-swap (read).**
- Two transactions that touch a common key with at least one writer are mutually excluded, and the second validates
  after the first has applied. So they cannot both commit against the same version, write skew is serialised, and
  there are no lost updates.
- Shared holders never write the shared key, so their concurrent validation is sound.
- De-duplication cannot mix modes, because a key is pushed as shared only if it is not written (`meta.rs:335-339`).
- On an error or a panic, the `Drop` impl releases the locks (`meta.rs:50-67`), and poison on `held` is ignored.
- A WAL failure aborts the process, as before.

**Deadlock and checkpoint (read).**
- With all-at-once acquisition there is no hold-and-wait, so no deadlock between commits.
- Commits take `gate.read` *before* key locks. Checkpoint takes `gate.write`, which waits for holders to finish; no
  holder waits for the gate again, so there is no cycle.
- F7's checkpoint record reads `wal.next` under `gate.write`, when no commit is in flight.

**Caveats.**
- **V4 (executed): writer starvation** behind continuously overlapping shared holders.
- **Multi-key starvation (read).** A transaction needing several keys waits until all are free *simultaneously*. The
  striped design acquired in order and held, so it did not have this. Under contention on the same refs, OCC
  conflicts dominate anyway.
- **Wake-ups (read).** `notify_all` on every release wakes every waiter (`meta.rs:66`), which is O(waiters) per commit
  on hot keys. The builders' pre-registered throughput check did not include hot-key contention.
- **Remote mode.** Frontends' local `KeyLocks` are unused there; commits go to the service's `Store::commit`
  (`meta.rs:325-332`). Both remote and local modes use the same code.
- **ABA (pre-existing).** `version()` returns 0 for absent keys, so ABA through delete-then-recreate is still possible
  (pre-existing; the server issues no deletes).

## Focus 4 — lost commit-reply recovery (F5) and partial results (F4) — **holds with caveats**

**F5 (`repo.rs:838-848`, `applied_by` `repo.rs:581-592`).** After an outcome-unknown commit, the frontend reads back
every `rv/<repo>/<ref>/<seq>` key it wrote and reports `ok` only if each one carries this push's id and tip.
- **Read-back is reliable.** `rv` keys are written only in the transaction that won that `seq` under compare-and-swap
  on the ref key, together with the ref, tip and pack keys. So a matching `rv` key implies the whole transaction
  applied.
- **Push ids.** They are `p<48-bit ns>-<per-process counter>` (`proto.rs:137-141`), so a cross-frontend collision is
  practically impossible. In deterministic mode the id is `p<counter>` only, but a collision would *also* need the
  same ref, sequence number and tip, so it would report exactly the state the client asked for.
- **A commit still in flight** is not yet visible, so it is reported as unknown, never as `ok`.
- **The read-back cannot hang.** It runs under `catch_unwind`, and a failure there means "not established".
- **Connection reuse.** The connection is dropped on a lost reply (`meta.rs:935-939`), so a late reply cannot be read
  by a later request.
- **Deletes** (`tip` = zero) and **atomic** pushes (all `rv`s) are covered.
- I found **no path that reports `ok` for an unapplied update**. This is read only; the builders' D10 and the auditor's
  R02(a) cover the executed side.

**F4.**
- A `PartialPush` carries the results of earlier groups, both committed and CAS-rejected, and `proto.rs:267-274`
  maps them by name. Groups after the failing one were never attempted and are correctly reported as failed.
- Atomic pushes, or a failure in the first group, still report every command as failed, which is correct.
- **V3 (executed):** the same situation reached through a *panic* (metadata reads panic after the 10 s retry window)
  is not covered. The whole request dies after group 1 committed. It is a false negative, not a false `ok`.

## Focus 5 — regression gate — **holds with caveats** (V5 is major for the gate's purpose)

This was delegated; I re-ran `cx01` and read `gate.py:60-100`.

**What holds (executed):**
- A deleted, renamed or deselected required test fails the gate.
- A suite crash with no record, or a truncated or malformed record, fails the gate.
- A count below the floor fails the gate.
- Suite totals that exceed the per-test failures fail the gate.
- Doctest and ignored-test lines in cargo output are handled.
- `run_suites.sh` exits with `gate.py`'s status: there is no `set -e`, pipe or `|| true` on that line
  (`cx06_run_suites_exit_path.sh`, run with stub suites).
- The binary is rebuilt first (`run_suites.sh:17`).

**What does not:** V5, V6 and V7.
- **Known failures accepted for any failure.** The registered known failures (CG3 race, CG6 levels, CG6 single-job
  parallel, R11) are accepted for *any* failure or error. For CG3 that removes the gate's coverage of the monotonic
  history property, which is the property the test exists for.
- **Exceptions I could not justify.** I could not check the justification for CG3, CG6 and review1 F4: the frozen
  suites and the cited adjudication documents are outside the readable tree (`clients/heldout` holds only
  `harness/`).
- **Exceptions that are justified.** The D5 skip is tied to `not self.c.shared_dir` (`verify/dist/test_dist.py:260`).
  R11's expected failure matches the auditor's original output, but registering it also masks R11's control check.
- **Mixed test sources.** p0 and p1 tests come from `$HERE` and the other suites from `$SRV` (`run_suites.sh:39-57`).
  With a different `SERVER_DIR`, the tree under test supplies its own tests, which are checked only by id.
- **Possible hang.** The load-average wait loop (`run_suites.sh:13`) spins forever if `sysctl` fails (seen in the
  sandbox). That is a hang, not a false pass.

## Other changes (brief)

- **F3 (read).** The tmp area is created as `n-<tag>`, locked, then renamed to `i-<tag>` (`blob.rs:107-116`), so the
  sweep never sees an unlocked live `i-` area. `n-` areas are removed after an hour (`blob.rs:83-92`). This looks
  sound on local POSIX filesystems; NFS was not examined.
- **F6 (read).** See V9. The meta read timeout of 20 s is longer than the 10 s read-retry window, so one stalled read
  panics the request (`meta.rs:852-866`). A slow commit (more than 20 s) becomes outcome-unknown and goes through the
  F5 read-back.
- **F7 (read).** The snapshot carries the highest LSN as a reserved-key tombstone (`meta.rs` checkpoint). This looks
  correct. The builders' unit test covers it.
- **F8 (read).** The side-band flag is reset per request on reused blocking threads (`http.rs:280`). It is set at both
  v2 packfile switch points and in v0 (`v0.rs:115`). Sound.
- **F9/F10 (read).**
  - Maps are validated on load (`blob.rs:468-499`).
  - A transient failure to fetch a map is no longer cached.
  - Pack size is checked against the index.
  - The out-of-range index in `packout.rs:121` is guarded.
  - Remaining weaknesses are as in V1 (no check that `p` is committed or well-formed).
- **`deploy/fly/entrypoint.sh` (read).**
  - The meta role refuses to start without `PANDO_META_SECRET` and binds only to the 6PN address. Good.
  - The frontend role binds `[::]` but does not require `PANDO_AUTH`. The bind comment at `main.rs:251-252` says
    exposure should be paired with auth; that is not enforced here.
  - The metadata handshake authenticates the connection only. Frames after it are neither encrypted nor
    integrity-protected, so the design relies on the 6PN WireGuard network.
  - The builders say this entrypoint is untested; I did not run it.
- **Test changes.** I did not re-run the builders' suites (budget). The changed tests that I read (N6, D9, D10,
  D2KeyLocks, the unit tests) match their descriptions.

## Auditor's reproducers on this snapshot (executed, partial)

- R01 (F1), R02 (F4/F5), R03 (F2), R06 (F3) and R08 (F7): **no defect observed** on e40d36b
  (`work/repro/r0{1,2,3,6,8}.rerun.out`). This agrees with the builders' claim for these five.
- R04, R05, R07 and R09–R12: not re-run (budget). The fixes pass the auditor's specific cases; V1–V3 are variants
  that those cases do not cover.

## What I could not establish, and why

- **The frozen suites and their exceptions** (CG3, CG6, review1 F4): their test files and adjudication documents are
  not readable here.
- **Full builders' suites and a full gate run:** not run, for budget and time. I ran only my reproducers, a subset of
  the auditor's, and gate.py on hand-made records.
- **A production key pattern that triggers V4:** not identified, so V4 is shown on the primitive only.
- **V1 through covers and copies on the cold frontend** (rather than a shallow clone): read only.
- **Reference mode (Unix sockets), multi-host, NFS, power loss:** not possible in this sandbox (as in the original
  audit).
- **Not examined:** auth.rs beyond the handshake, the rest of the Fly deployment files, recompression, the simulator,
  and the keylock-perf evidence data.
