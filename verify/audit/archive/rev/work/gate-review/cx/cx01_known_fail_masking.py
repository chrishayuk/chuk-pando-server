"""Finding 1: known_fail is matched on verdict only (gate.py:74-79). Any failure/ERROR mode of a registered known-fail
test passes the gate, and a known failure that now passes is only a note (gate.py:86-88)."""
from _h import *
CG3 = "heldout.tests_cg3.CG3::test_concurrent_writer_and_readers_see_complete_monotonic_history"
run("CG3 fails with a REAL monotonicity violation (not the precondition race)", {"hidden.xml": junit(passing("hidden", {
    CG3: ("fail", "AssertionError: reader 2 saw refs/heads/t go backwards: c41 -> c17; fsck: missing blob 9f3e"),
    "heldout.tests_review1.R1::test_F4_unrelated_push_not_blocked_while_another_push_is_being_processed": ("skip", "x")}))}, "hidden")
run("CG3 ERRORs: server crashed in setUp (connection refused)", {"hidden.xml": junit(passing("hidden", {
    CG3: ("error", "ConnectionRefusedError: [Errno 61] server died (panic in repo.rs)")}))}, "hidden")
run("CG3 known-fail now PASSES", {"hidden.xml": junit(passing("hidden"))}, "hidden")
cg = "tests_cg6.CG6::test_cg6_compaction_levels_reduce_packs_per_clone"
run("CG6 compaction fails for a different reason (clone fsck corrupt)", {"cg56.xml": junit(passing("cg56", {
    cg: ("fail", "AssertionError: git fsck failed on clone: missing tree 77aa"),
    "tests_cg6.CG6::test_cg6_parallel_pack_download": ("error", "TimeoutError: server hung")}))}, "cg56")
run("repro r11 fails on its CONTROL check (unrelated ref with its own tip delayed = real D2/A4 regression)", {"repro.xml": junit(passing("repro", {
    "audit_repro::r11": ("fail", "FAIL control: unrelated ref with its own tip is not delayed")}))}, "repro")
run("repro r11 crashes (Traceback, cluster never started)", {"repro.xml": junit(passing("repro", {
    "audit_repro::r11": ("fail", "ModuleNotFoundError: No module named 'rlib'")}))}, "repro")
