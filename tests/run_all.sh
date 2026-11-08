#!/bin/sh
# Run every local gate test plus simulation seeds. Usage: tests/run_all.sh
# CG5 needs the modified client built at $TMPDIR/ogit/src (see README.md); skipped otherwise.
cd "$(dirname "$0")/.." || exit 1
: "${TMPDIR:=/tmp}"
rc=0
port=24000
for t in cg1 cg2 cg3 cg4 cg5 cg6 features; do
  port=$((port + 7))
  if [ "$t" = cg5 ] && [ ! -x "${OGIT:-$TMPDIR/ogit/src/bin-wrappers/git}" ]; then echo "== $t: SKIPPED (modified git not built)"; continue; fi
  out="$TMPDIR/run_all.$t.out"
  G_PORT=$port N="${N:-300}" sh "tests/$t.sh" > "$out" 2>&1
  r=$?
  echo "== $t: $(grep '^# passes' "$out") (exit $r)"
  grep '^FAIL' "$out"
  [ $r = 0 ] || rc=1
  wd=$(grep -o 'work dir [^)]*' "$out" | tail -1 | cut -d' ' -f3)
  if [ -n "$wd" ] && [ -f "$wd/data/events/events.jsonl" ]; then
    tc=$(python3 tests/trace_check.py "$wd/data/events/events.jsonl" | head -1)
    echo "   trace: $tc"
    echo "$tc" | grep -q '"violations": 0' || rc=1
  fi
done
for s in 1 2 3 4 5; do
  if ./server/target/release/pando sim --seed $s --steps 400 --dir "$TMPDIR/run_all_sim$s" > "$TMPDIR/run_all.sim$s.out" &&
     python3 tests/trace_check.py "$TMPDIR/run_all_sim$s/events/events.jsonl" | head -1 | grep -q '"violations": 0'; then
    echo "== sim seed $s: no violations (invariants + trace check)"
  else
    echo "== sim seed $s: VIOLATIONS"; cat "$TMPDIR/run_all.sim$s.out"; rc=1
  fi
done
# exhaustive abstract-model check (MODEL §8) + mutants must be caught
if python3 tests/model_check.py --depth 7 --covers --deletes --spans 1,2 > "$TMPDIR/run_all.mc.out"; then
  echo "== model check (depth 7, covers+deletes): $(head -1 "$TMPDIR/run_all.mc.out")"
else
  echo "== model check: VIOLATIONS"; head -5 "$TMPDIR/run_all.mc.out"; rc=1
fi
for b in deps-first-rv holds-first-rv linked-pack-boundary cover-drop-own-deps; do
  if python3 tests/model_check.py --depth 8 --covers --spans 1,2 --bug "$b" > /dev/null; then
    echo "== mutant $b NOT detected"; rc=1
  else
    echo "== mutant $b detected"
  fi
done
exit $rc
