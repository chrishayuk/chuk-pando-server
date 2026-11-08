"""Run REAL pytest (installed version) on test shapes the gate must handle, then feed its JUnit to the gate with a
matching expectations file. Shows what the gate sees for unittest subTest, fail+cleanup-error, a thread exception."""
import json, os, subprocess, sys, tempfile
from _h import run, D
here = os.path.dirname(os.path.abspath(__file__)); out = tempfile.mkdtemp(prefix="pyt-", dir=os.environ.get("TMPDIR"))
x = os.path.join(out, "shapes.xml")
p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={x}", "test_shapes.py"],
                   cwd=os.path.join(here, "pyt"), capture_output=True, text=True)
print("pytest:", subprocess.run([sys.executable, "-m", "pytest", "--version"], capture_output=True, text=True).stdout.strip(), "| summary:", p.stdout.strip().splitlines()[-1])
print(open(x).read().replace("><", ">\n<")[:3000])
req = ["test_shapes.Sub::test_subtests", "test_shapes.Sub::test_subtest_then_pass", "test_shapes.ThreadExc::test_thread_raises"]
for label, spec in [
    ("subtests + thread exception, nothing known-failing", {"required": req}),
    ("fail+cleanup-error test registered as known_fail", {"required": req + ["test_shapes.FailAndTeardownError::test_x"],
        "known_fail": {"test_shapes.FailAndTeardownError::test_x": {"reason": "r", "ref": "r"},
                       "test_shapes.Sub::test_subtests": {"reason": "r", "ref": "r"}, "test_shapes.Sub::test_subtest_then_pass": {"reason": "r", "ref": "r"},
                       "test_shapes::test_param[x y]": {"reason": "r", "ref": "r"}}}),
]:
    spec["file"] = "shapes.xml"; e = os.path.join(out, "exp.json"); json.dump({"suites": {"shapes": spec}}, open(e, "w"))
    run(label, {"shapes.xml": open(x).read()}, "shapes", expect=e)
