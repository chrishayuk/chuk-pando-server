"""Finding 3 (cargo): read_cargo (gate.py:50-58) on hand-crafted `cargo test` outputs."""
from _h import *
req = EXP["unit"]["required"]
def ok_lines(drop=(), override=None):
    o = override or {}
    return "\n".join(f"test {t} ... {o.get(t, 'ok')}" for t in req if t not in drop)
hdr = "   Compiling pando v0.1.0\n    Finished `release` profile\n     Running unittests src/main.rs (target/release/deps/pando-abc)\n\nrunning 17 tests\n"
res = lambda p, f, i=0: f"\ntest result: {'ok' if f == 0 else 'FAILED'}. {p} passed; {f} failed; {i} ignored; 0 measured; 0 filtered out; finished in 1.0s\n"
T = "meta::tests::mark_survives_reopen"
run("clean", {"unit.txt": hdr + ok_lines() + res(17, 0)}, "unit")
run("one ignored", {"unit.txt": hdr + ok_lines(override={T: "ignored"}) + res(16, 0, 1)}, "unit")
run("one ignored, with reason (new libtest format)", {"unit.txt": hdr + ok_lines(override={T: "ignored, flaky on CI"}) + res(16, 0, 1)}, "unit")
run("harness abort mid-run (SIGABRT, no 'test result:' line)", {"unit.txt": hdr + ok_lines(drop=(T,)) +
    f"\ntest {T} ... \nthread panicked while processing panic. aborting.\nerror: test failed, to rerun pass `--bin pando`\n\nCaused by:\n  process didn't exit successfully: (signal: 6, SIGABRT: process abort signal)\n"}, "unit")
run("harness abort AFTER all 17 'ok' lines printed, no result line", {"unit.txt": hdr + ok_lines() +
    "\nerror: test failed, to rerun pass `--bin pando`\n  process didn't exit successfully (signal: 11, SIGSEGV)\n"}, "unit")
run("realistic latent case: unit binary passes, a later test binary (tests/*.rs) aborts; crashed test is new/unregistered", {"unit.txt": hdr + ok_lines() + res(17, 0) +
    "     Running tests/it.rs (target/release/deps/it-123)\n\nrunning 1 test\ntest new_regression_test ... \n" +
    "error: test failed, to rerun pass `--test it`\n\nCaused by:\n  process didn't exit successfully (signal: 6, SIGABRT: process abort signal)\n"}, "unit")
run("doctest FAILED line (unparsed name with spaces), totals count it", {"unit.txt": hdr + ok_lines() + res(17, 0) +
    "   Doc-tests pando\nrunning 1 test\ntest src/lib.rs - foo (line 3) ... FAILED\n" + res(0, 1)}, "unit")
run("same test name in two binaries: FAILED then ok", {"unit.txt": hdr + ok_lines(override={T: "FAILED"}) + res(16, 1) + "running 1 test\n" + f"test {T} ... ok\n" + res(1, 0)}, "unit")
run("should_panic test format (not parsed)", {"unit.txt": hdr + ok_lines(override={T: "ok"}).replace(f"test {T} ...", f"test {T} - should panic ...") + res(17, 0)}, "unit")
run("build failed (no test result)", {"unit.txt": "error[E0425]: cannot find value `x`\nerror: could not compile `pando`\n"}, "unit")
run("uncaptured child-process output interleaved on a test's line", {"unit.txt": hdr + ok_lines(drop=(T,)) + f"\ntest {T} ... [child] listening on 127.0.0.1:1\nok\n" + res(17, 0)}, "unit")
