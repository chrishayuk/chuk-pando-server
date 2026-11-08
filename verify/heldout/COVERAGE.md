# Claim-to-test coverage (hidden suites; Team C)
Legend: B = behavioral (a stock-Git server passes), S = structural (architecture; stock Git must fail), M = mutant that should be caught.

| Gate / claim | Test (heldout/) | Type | Mutants that must be caught |
|---|---|---|---|
| CG1 clone/push/fetch/fsck, branches, tags, delete, force-push, large/chunked (the earlier "gzip" claim was false: stock push does not gzip; gzip-encoded request bodies are covered by R1 F12) | tests_cg1.py::CG1 (per client: stock 2.55.0, Apple 2.54) | B | missingobj, deleteignore, staleread |
| CG1 compatibility matrix (shallow, partial, v0/v1, client gc, --atomic, push options) | tests_cg1.py::CG1Matrix | info | n/a (records Supported/Unsupported) |
| CG2 exactly-one-winner on same-ref race (raw protocol, bypasses client checks) | tests_cg2.py::test_same_ref_race_exactly_one_wins | B | lostupdate |
| CG2 stale old-oid rejected | tests_cg1.py::test_lease_stale_rejected | B | lostupdate |
| CG2 acked push visible to a new reader (read-your-own-write) | tests_cg2.py::test_ack_implies_visible_to_new_reader | B | staleread, backwards |
| CG2 crash durability (SIGKILL, 6 cycles, ref in {last acked, in-flight}, acked commits reachable, fsck) | tests_cg2.py::test_crash_durability_and_recovery | B | nodurable |
| CG2 crash during 60 MB push | tests_cg2.py::test_crash_during_large_push_leaves_consistent_repo | B | nodurable, missingobj |
| CG3 delete+recreate with unrelated history | tests_cg3.py | B | deleteignore, staleread |
| CG3 force-push dropping depended-on history | tests_cg3.py | B | staleread |
| CG3 stacked branches, delete base, maintain | tests_cg3.py | B | deleteignore, missingobj |
| CG3 chain of 40 stock pushes, every intermediate reader converges | tests_cg3.py | B | missingobj |
| CG3 duplicate/empty/no-op pushes, annotated + lightweight tags | tests_cg3.py | B | missingobj |
| CG3 retention after churn + maintenance (GC must not drop live closure) | tests_cg3.py | B | deleteignore, staleread |
| CG3 concurrent writer and readers: complete, monotonic history | tests_cg3.py | B | backwards, missingobj |
| CG3 random-edit workload content equality | tests_cg3.py | B | missingobj |
| CG3 no server-side unpack into a repository | tests_structural.py::S1 | S | n/a (B0 must FAIL) |
| CG3 packs stored immutably, retained, >=1 per push | tests_structural.py::S2 | S | n/a (B0 must FAIL) |
| CG3/A3 metadata ops per push independent of push size (self-reported, audited in CG7) | tests_structural.py::S3 | S | n/a (B0 must FAIL) |
| CG4 N independent writers (N=G_N), all acked + visible | tests_cg4.py::test_independent_writers... | B | staleread, backwards, missingobj |
| CG4 no global lock (slow throttled push must not block unrelated branch) | tests_cg4.py::test_no_global_lock... | B | globallock |
| CG4 per-ref monotonic, no dangling dependency under concurrent updates | tests_cg4.py::test_per_ref_monotonic... | B | backwards |

## Known gaps (not covered by hidden suites yet)
- CG5 (explicit-haves semantics), CG6 (compaction, O(log Δ) planning, parallel download): extension suites not written; they depend on builders' design choices and need the same mutation treatment.
- Permitted-anomaly observation (foo@1,bar@2 vs foo@2,bar@1): not testable as a pass/fail black-box property; only the forbidden behaviours are tested.
- Multi-ref atomic push is matrix-only. HTTPS/auth not covered. N=1000 writers is benchmark-only.
- Structural tests rely on file-layout heuristics (loose-object and bare-repo patterns, `PACK` magic) and self-reported `/_stats`; a determined implementation could game them. CG7 code audit is the backstop.
- Test authorship: written by the same assistant that wrote the spec; independent human or second-model review of the suites has NOT happened.


## v2 additions after REVIEW-1 (tests_review1.py; finding ids in each test; mutants killed in heldout/mutation_results_v2.json)
| Test | Finding | Mutant(s) that must be caught |
|---|---|---|
| 5-way same-ref race, create-over-existing rejected, delete with stale old (non-main refs) | F1 | lostupdate_branch, createoverwrite |
| Kill verified (pid live, dies, port closes); `INTERFACE.md` pid contract | F2 | nodurable_fakepid |
| Kill after >=6 MB of body forwarded (throttle proxy) and kill 0-30 ms after every ack (seed logged) | F3 | lazydurable (6/6 runs), nodurable* |
| Unrelated-branch push during server-side processing (best-effort; inconclusive when processing is short) | F4 | globalcommit SURVIVES (documented black-box limit) |
| Stock clients update/force/delete existing non-trunk branches | F5 | trunkonlyadv, trunkonly_overwrite |
| Uncaught worker-thread exceptions fail the test; per-test timeout | F9 | foobar500 |
| Annotated tags survive maintain; maintain concurrent with pushes | F10 | gcnotags |
| Atomic multi-ref push when `atomic` advertised | F11 | atomicignored |
| gzip-encoded v2 request accepted | F12 | nogzip |
| Clients with `fetch.uriprotocols` enabled | F26 | n/a |
Structural suite is now indicator-only (F6-F8); the F class is set by the CG7 audit.
Harness: server env allow-list (F17); CG4 independent writers use `push_retry` (operational failures retried and counted as reliability; correctness asserted only on acknowledged pushes).
Still not covered: Pando-specific faults (F27), spec/03 CG3 cases 3/5/6 beyond the above, CG5/CG6 suites, HTTPS/auth, Linux.
