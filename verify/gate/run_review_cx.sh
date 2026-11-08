#!/usr/bin/env bash
# Run the remediation reviewer's gate counterexamples (archived unmodified in the experiment record,
# campaign/audit/output-rev/work/gate-review/cx/) against a gate: the archived copy beside them ("old") or this
# tree's verify/gate ("new"). They locate the gate next to their own directory, so a layout of per-file symlinks is
# built; runpy keeps __file__ at the layout path. Usage: verify/gate/run_review_cx.sh old|new
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; ARCH="$HERE/../audit/archive"
CX="$ARCH/rev/work/gate-review"
( cd "$ARCH/rev" && shasum -a 256 -c --quiet SHA256SUMS ) || { echo "archive modified: refusing"; exit 2; }
export PYTHONDONTWRITEBYTECODE=1
L="$(mktemp -d)"; mkdir -p "$L/cx"
for f in "$CX"/cx/*.py; do ln -s "$f" "$L/cx/"; done; ln -s "$CX/cx/pyt" "$L/cx/pyt"
case "$1" in old) G="$CX" ;; new) G="$HERE" ;; *) echo "old|new"; exit 2 ;; esac
ln -s "$G/gate.py" "$L/gate.py"; ln -s "$G/expectations.json" "$L/expectations.json"
for f in "$L"/cx/cx0[1-5]*.py; do
  echo "#### $(basename "$f")"
  ( cd "$L/cx" && python3 -c 'import runpy,sys; sys.path.insert(0, sys.argv[1]); sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0], run_name="__main__")' "$L/cx" "$f" ) 2>&1 | grep -E "^\[|exit=" 
done
rm -rf "$L"
