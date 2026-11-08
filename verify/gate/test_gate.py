"""Self-tests for gate.py: it must reject each way a regression can hide, and accept a clean run.
Run: python3 -m pytest verify/gate/test_gate.py -q"""
import json, os, subprocess, sys, tempfile, textwrap, unittest

GATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gate.py")

EXPECT = {"suites": {
    "s": {"file": "s.xml", "min_total": 3,
          "required": ["m.C::test_a", "m.C::test_b", "m.C::test_c", "m.C::test_known", "m.C::test_skip"],
          "known_fail": {"m.C::test_known": {"reason": "documented race", "ref": "doc.md", "kind": "failed",
                                             "match": ["couldn't find ref t"], "forbid": ["monotonic"]}},
          "allowed_skip": {"m.C::test_skip": {"reason": "needs hardware", "match": ["^no hardware"]}}},
    "u": {"file": "u.txt", "format": "cargo", "min_total": 2, "required": ["a::t1", "a::t2"]},
}}


MSG = {"test_known": "fatal: couldn't find ref t", "test_skip": "no hardware here"}


def junit(cases, failures=None):
    body = ""
    for c in cases:
        name, st = c[0], c[1]
        m = c[2] if len(c) > 2 else MSG.get(name, "x")
        inner = {"passed": "", "failed": f"<failure message=\"{m}\"/>", "error": f"<error message=\"{m}\"/>",
                 "skipped": f"<skipped message=\"{m}\"/>"}[st]
        body += f"<testcase classname='m.C' name='{name}'>{inner}</testcase>"
    nf = failures if failures is not None else sum(1 for c in cases if c[1] in ("failed", "error"))
    return f"<testsuites><testsuite name='pytest' failures='{nf}' errors='0'>{body}</testsuite></testsuites>"


CLEAN = [("test_a", "passed"), ("test_b", "passed"), ("test_c", "passed"), ("test_known", "failed"),
         ("test_skip", "skipped")]
CARGO_OK = "test a::t1 ... ok\ntest a::t2 ... ok\ntest result: ok. 2 passed; 0 failed; 0 ignored\n"


class Gate(unittest.TestCase):
    def run_gate(self, cases, cargo=CARGO_OK, failures=None, extra=(), drop=()):
        d = tempfile.mkdtemp()
        e = os.path.join(d, "expect.json")
        json.dump(EXPECT, open(e, "w"))
        if "s" not in drop:
            open(os.path.join(d, "s.xml"), "w").write(junit(cases, failures))
        if "u" not in drop:
            open(os.path.join(d, "u.txt"), "w").write(cargo)
        p = subprocess.run([sys.executable, GATE, d, f"--expect={e}", *extra], capture_output=True, text=True)
        return p.returncode, p.stdout

    def test_clean_run_with_known_failure_and_allowed_skip_passes(self):
        rc, out = self.run_gate(CLEAN)
        self.assertEqual(rc, 0, out)
        self.assertIn("known failure", out)
        self.assertIn("allowed skip", out)

    def test_unexpected_failure_fails(self):
        rc, out = self.run_gate([("test_a", "failed")] + CLEAN[1:])
        self.assertEqual(rc, 1, out)
        self.assertIn("UNEXPECTED FAILED m.C::test_a", out)

    def test_error_fails(self):
        rc, out = self.run_gate([("test_a", "error")] + CLEAN[1:])
        self.assertEqual(rc, 1, out)

    def test_deleted_test_fails(self):
        rc, out = self.run_gate(CLEAN[1:])
        self.assertEqual(rc, 1, out)
        self.assertIn("MISSING required test m.C::test_a", out)

    def test_unexpected_skip_fails(self):
        rc, out = self.run_gate([("test_a", "skipped")] + CLEAN[1:])
        self.assertEqual(rc, 1, out)
        self.assertIn("UNEXPECTED SKIP m.C::test_a", out)

    def test_floor_breach_fails_even_if_required_list_is_short(self):
        # all three non-known tests skipped-as-allowed is impossible here, so emulate a run of only 2 executed tests
        cases = [("test_a", "passed"), ("test_b", "passed"), ("test_c", "skipped"), ("test_known", "failed"),
                 ("test_skip", "skipped")]
        rc, out = self.run_gate(cases)
        self.assertEqual(rc, 1, out)

    def test_missing_result_file_fails(self):
        rc, out = self.run_gate(CLEAN, drop=("s",))
        self.assertEqual(rc, 1, out)
        self.assertIn("did not run", out)

    def test_failures_hidden_from_per_test_records_fail(self):
        # e.g. a subtest failure counted in the suite totals but not attached to a test record
        rc, out = self.run_gate(CLEAN, failures=3)
        self.assertEqual(rc, 1, out)
        self.assertIn("reports 3 failures", out)

    def test_cargo_failure_missing_and_build_failure(self):
        rc, out = self.run_gate(CLEAN, cargo="test a::t1 ... FAILED\ntest a::t2 ... ok\ntest result: FAILED. 1 passed; 1 failed;\n")
        self.assertEqual(rc, 1, out)
        rc, out = self.run_gate(CLEAN, cargo="test a::t2 ... ok\ntest result: ok. 1 passed; 0 failed;\n")
        self.assertEqual(rc, 1, out)
        self.assertIn("MISSING required test a::t1", out)
        rc, out = self.run_gate(CLEAN, cargo="error[E0425]: cannot find value\n")
        self.assertEqual(rc, 1, out)

    def test_unregistered_test_is_reported_and_fails_only_when_strict(self):
        cases = CLEAN + [("test_new", "passed")]
        rc, out = self.run_gate(cases)
        self.assertEqual(rc, 0, out)
        self.assertIn("unregistered", out)
        rc, out = self.run_gate(cases, extra=("--strict",))
        self.assertEqual(rc, 1, out)

    def test_known_failure_that_passes_is_reported_not_failed(self):
        cases = [(n, "passed" if n == "test_known" else s) for n, s in CLEAN]
        rc, out = self.run_gate(cases)
        self.assertEqual(rc, 0, out)
        self.assertIn("now PASSES", out)

    # review V5/V6/V7
    def test_known_failure_failing_differently_fails(self):
        for case, why in (((("test_known", "failed", "AssertionError: monotonic violation: t went backwards"),), "match"),
                          ((("test_known", "failed", "fatal: couldn't find ref t; also monotonic violation"),), "forbidden"),
                          ((("test_known", "error", "fatal: couldn't find ref t"),), "outcome error")):
            cases = [c for c in CLEAN if c[0] != "test_known"] + list(case)
            rc, out = self.run_gate(cases)
            self.assertEqual(rc, 1, out)
            self.assertIn("FAILED DIFFERENTLY", out)
            self.assertIn(why, out)

    def test_allowed_skip_for_another_reason_fails(self):
        cases = [c for c in CLEAN if c[0] != "test_skip"] + [("test_skip", "skipped", "server failed to start")]
        rc, out = self.run_gate(cases)
        self.assertEqual(rc, 1, out)
        self.assertIn("SKIPPED FOR ANOTHER REASON", out)

    def test_exception_without_signature_fails(self):
        d = tempfile.mkdtemp(); e = os.path.join(d, "expect.json")
        bad = json.loads(json.dumps(EXPECT)); bad["suites"]["s"]["known_fail"]["m.C::test_known"].pop("match")
        json.dump(bad, open(e, "w"))
        open(os.path.join(d, "s.xml"), "w").write(junit(CLEAN)); open(os.path.join(d, "u.txt"), "w").write(CARGO_OK)
        p = subprocess.run([sys.executable, GATE, d, f"--expect={e}"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("no signature registered", p.stdout)

    def test_unknown_option_is_a_usage_error(self):
        rc, out = self.run_gate(CLEAN, extra=("--strickt",))
        self.assertEqual(rc, 2, out)

    def test_partial_run_is_labelled(self):
        rc, out = self.run_gate(CLEAN, extra=("--suites=s",))
        self.assertEqual(rc, 0, out)
        self.assertIn("PARTIAL PASS", out)
        self.assertIn("NOT CHECKED: u", out)

    def test_require_meta_rejects_missing_meta_and_stale_records(self):
        rc, out = self.run_gate(CLEAN, extra=("--require-meta",))
        self.assertEqual(rc, 1, out)
        self.assertIn("meta.txt missing", out)
        d = tempfile.mkdtemp(); e = os.path.join(d, "expect.json"); json.dump(EXPECT, open(e, "w"))
        for f, t in (("s.xml", junit(CLEAN)), ("u.txt", CARGO_OK)):
            open(os.path.join(d, f), "w").write(t)
        os.utime(os.path.join(d, "s.xml"), (1, 1))                       # a record from long before this run
        open(os.path.join(d, "meta.txt"), "w").write("binary_sha256=" + "0" * 64 + "\n")
        p = subprocess.run([sys.executable, GATE, d, f"--expect={e}", "--require-meta"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("older than this run", p.stdout)

    def test_a_crashed_second_test_binary_fails(self):
        crash = ("     Running unittests src/main.rs\ntest a::t1 ... ok\ntest a::t2 ... ok\ntest result: ok. 2 passed; 0 failed;\n"
                 "     Running tests/other.rs\nerror: test failed, to rerun pass `--test other`\n")
        rc, out = self.run_gate(CLEAN, cargo=crash)
        self.assertEqual(rc, 1, out)
        self.assertIn("binaries started", out)


if __name__ == "__main__":
    unittest.main()
