#!/usr/bin/env bash
# Live demonstration that the gate rejects real regressions (not only synthetic records, see test_gate.py):
#  1. P1 run from a scratch copy of its test file with one test DELETED and one assertion made to FAIL;
#  2. unit tests run with one test module skipped (as when a module is deleted by an edit);
#  3. a suite that did not run at all (its record is missing).
# Each must make verify/gate/gate.py exit 1. Usage: verify/gate/demo_rejects.sh OUT_DIR
set -u
HERE="$(cd "$(dirname "$0")/../.." && pwd)"; mkdir -p "$1"; OUT="$(cd "$1" && pwd)"; GATE="$HERE/verify/gate/gate.py"; rc=0
export CARGO_HOME="${CARGO_HOME:-$HERE/.cargo-home}"
S="$(mktemp -d)"; cp -R "$HERE/verify/p1" "$S/p1"
python3 - "$S/p1/test_p1.py" <<'PY'
import re, sys
p = sys.argv[1]; s = open(p).read()
s, n = re.subn(r"\n    def test_clone_after_force_push_receives_no_discarded_history\(self\):", "\n    def _deleted_clone_after_force_push(self):", s)
assert n == 1, "could not delete the test"
s, n = re.subn(r"(\n    def test_clone_from_empty_uses_top_cover\(self\):\n)", r"\1        self.assertTrue(False, 'deliberate failure (gate demonstration)')\n", s)
assert n == 1, "could not inject the failure"
open(p, "w").write(s)
PY
( cd "$S/p1" && G_CLIENTS="${G_CLIENTS:?set G_CLIENTS (docs/VERIFY.md)}" PYTHONPATH="$HERE/verify/heldout" G_RUN_SH="$HERE/server/run.sh" python3 -m pytest test_p1.py -q -p no:cacheprovider \
    --junitxml="$OUT/p1.xml" > "$OUT/p1.txt" 2>&1 )
# each rejection must be for the planted reason, not an incidental one (e.g. a missing record)
expect() { G=$(python3 "$GATE" "$OUT" --suites=$1); r=$?; echo "$G"; echo "gate exit $r (expected 1)"; [ $r -eq 1 ] || rc=1
           shift; for want in "$@"; do echo "$G" | grep -qF "$want" || { echo "MISSING EXPECTED REASON: $want"; rc=1; }; done; }
echo "== 1. P1 with a deleted test and a deliberately failing one"
expect p1 "MISSING required test test_p1.P1::test_clone_after_force_push_receives_no_discarded_history" "UNEXPECTED FAILED test_p1.P1Covers::test_clone_from_empty_uses_top_cover"
( cd "$HERE/server" && cargo test --release --offline -- --skip meta::tests > "$OUT/unit.txt" 2>&1 )
echo "== 2. unit tests with the meta::tests module not run"
expect unit "MISSING required test meta::tests::mark_survives_reopen" "only 10 tests ran"
echo "== 3. a suite that did not run (no record)"
expect net "did not run or did not report"
rm -rf "$S"; echo "demonstration: $([ $rc -eq 0 ] && echo 'gate rejected all three' || echo 'GATE FAILED TO REJECT')"; exit $rc
