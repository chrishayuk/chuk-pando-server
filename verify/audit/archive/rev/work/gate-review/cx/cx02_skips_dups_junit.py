"""Finding 2/3: allowed_skip ignores the skip reason; duplicate ids; JUnit parsing edge cases."""
from _h import *
F4 = "heldout.tests_review1.R1::test_F4_unrelated_push_not_blocked_while_another_push_is_being_processed"
D5 = "test_dist.D5D6Crash::test_live_frontend_area_never_swept"
run("F4 skipped for an unrelated reason", {"hidden.xml": junit(passing("hidden", {F4: ("skip", "could not start server: port in use")}))}, "hidden")
run("D5 in tcp-s3 reported as xfail-skip", {"dist-tcp-s3.xml": junit(passing("dist-tcp-s3", {D5: ("skip", "pytest.xfail: expected failure")}))}, "dist-tcp-s3")
P = "test_p1.P1::test_clone_after_force_push_receives_no_discarded_history"
run("duplicate id: pass then fail (totals say 1 failure)", {"p1.xml": junit(passing("p1") + [(P, "fail", "boom")])}, "p1")
run("duplicate id: fail then pass, totals edited to 0", {"p1.xml": junit([(P, "fail", "boom")] + passing("p1"), failures=0)}, "p1")
K = "test_p1.P1Covers::test_clone_from_empty_uses_top_cover"
run("<error> + <skipped> both on one case", {"p1.xml": junit(passing("p1")).replace(
    'name="test_clone_from_empty_uses_top_cover" time=\'0\'>', 'name="test_clone_from_empty_uses_top_cover" time=\'0\'><skipped/><error message="x"/>'
).replace("errors='0'", "errors='1'")}, "p1")
run("known-fail CG3 also has a teardown error (failure+error on one case) -> false alarm?", {"hidden.xml": junit(passing("hidden", {
    "heldout.tests_cg3.CG3::test_concurrent_writer_and_readers_see_complete_monotonic_history": ("failerr", "teardown: rmtree failed"),
    F4: ("skip", "x")}))}, "hidden")
x = junit(passing("p1"), wrap=False)
run("nested <testsuite> inside <testsuite> with aggregated totals (no failures)", {"p1.xml":
    f"<testsuites><testsuite name='outer' failures='0' errors='0'>{x}</testsuite></testsuites>"}, "p1")
x = junit(passing("p1", {K: ("fail", "f")}), wrap=False)
run("nested testsuites, outer aggregates the 1 failure -> double count", {"p1.xml":
    f"<testsuites><testsuite name='outer' failures='1' errors='0'>{x}</testsuite></testsuites>"}, "p1")
full = junit(passing("p1"))
run("truncated XML (suite crashed mid-write)", {"p1.xml": full[: len(full)//2]}, "p1")
run("zero testcases", {"p1.xml": "<testsuites><testsuite name='p' tests='0' failures='0' errors='0'/></testsuites>"}, "p1")
run("missing classname attrs", {"p1.xml": full.replace("classname=", "data-x=")}, "p1")
run("non-integer failures attr", {"p1.xml": full.replace("failures='0'", "failures='?'")}, "p1")
run("testcase-level only, failures attr absent", {"p1.xml": junit(passing("p1", {K: ("fail", "f")})).replace("failures='1'", "")}, "p1")
fly = [("test_access.UriExpiry::" + t.split("::")[1], "pass", "") for t in EXP["fly"]["required"] if t.startswith("test_access.Access::")]
run("fly: the 3 inherited UriExpiry(Access) tests present (as the real suite produces), non-strict", {"fly.xml": junit(passing("fly") + fly)}, "fly")
run("fly: same, --strict", {"fly.xml": junit(passing("fly") + fly)}, "fly", ["--strict"])
run("fly: inherited UriExpiry tests silently DROPPED, non-strict", {"fly.xml": junit(passing("fly"))}, "fly")
