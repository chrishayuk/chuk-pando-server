#!/usr/bin/env bash
# Run the independent auditor's ORIGINAL reproducers (archived read-only in the experiment record,
# campaign/audit/output-net/repro/) unmodified against a server tree. They expect the auditor's sandbox layout
# (<root>/work/repro, <root>/work/impl, <root>/impl, <root>/clients/...); this builds that layout from symlinks, so
# nothing of the reproducers is copied or edited. Each reproducer exits 1 when it reproduces its defect, 0 when not.
# Usage: verify/audit/run_repro.sh SERVER_TREE OUT_DIR [r01 r03 ...]     (default: all)
# Writes OUT_DIR/<name>.out per reproducer, summary.txt, and repro.xml (JUnit: one test per reproducer, failed when
# it reproduces its defect) for verify/gate/gate.py.
set -u
TREE="$(cd "$1" && pwd)"; OUT="$2"; shift 2; NAMES="${*:-}"
# G_REPRO_SET: net = the re-audit's reproducers (default); rev = the remediation review's. Both are archived verbatim in
# verify/audit/archive/{net,rev} (copied from the retired experiment record) and checked against their SHA256SUMS.
ARCH="$(cd "$(dirname "$0")" && pwd)/archive"
case "${G_REPRO_SET:-net}" in net) REPRO="$ARCH/net/repro" ;; rev) REPRO="$ARCH/rev/work/repro" ;; *) echo "G_REPRO_SET: net|rev"; exit 2 ;; esac
[ -n "$NAMES" ] || NAMES=$(cd "$REPRO" && ls r[0-9]*_*.py | sed 's/_.*//' | sort -u | tr '\n' ' ')
[ "${G_REPRO_SET:-net}" = rev ] && [ -z "${*:-}" ] && NAMES="r20 r21 r22"   # the review's own reproducers
# Archive integrity. full (default): every file in each archive's SHA256SUMS must be present and unchanged.
# public: the public verification package (docs/PUBLIC-SOURCE.md). The public source omits archive files that are raw
# agent transcripts or carry personal paths (verify/audit/WITHHELD.tsv). It is checked against PUBLIC-ARCHIVE.sha256:
# every line of that manifest must be a verbatim line of the archive's own SHA256SUMS, and every file it names must
# be present and unchanged. Nothing else is accepted: an absent file that is not in the public manifest fails.
case "${G_ARCHIVE_CHECK:-full}" in
  full) for a in net rev; do ( cd "$ARCH/$a" && shasum -a 256 -c --quiet SHA256SUMS ) || { echo "archive $a modified or incomplete: refusing (the public source checks the public package: G_ARCHIVE_CHECK=public)"; exit 2; }; done ;;
  public) PM="$(dirname "$ARCH")/PUBLIC-ARCHIVE.sha256"
    [ -f "$PM" ] || { echo "no $PM: refusing"; exit 2; }
    for a in net rev; do
      grep "  $a/" "$PM" | sed "s#  $a/#  ./#" | while IFS= read -r line; do grep -qxF "$line" "$ARCH/$a/SHA256SUMS" || { echo "public manifest line not in $a/SHA256SUMS: $line"; exit 1; }; done || exit 2
    done
    ( cd "$ARCH" && shasum -a 256 -c --quiet "$PM" ) || { echo "public archive package modified: refusing"; exit 2; } ;;
  *) echo "G_ARCHIVE_CHECK: full|public"; exit 2 ;;
esac
export PYTHONDONTWRITEBYTECODE=1   # never write into the archive
L="$(mktemp -d "${TMPDIR:-/tmp}/repro-layout.XXXXXX")"; mkdir -p "$L/work" "$L/clients" "$OUT"
mkdir "$L/work/repro"; for f in "$REPRO"/*.py; do ln -s "$f" "$L/work/repro/"; done; ln -s "$TREE" "$L/work/impl"; ln -s "$TREE" "$L/impl"
ln -s "$(cd "$(dirname "$0")/.." && pwd)/heldout" "$L/clients/heldout"
export G_RUN_SH="$TREE/server/run.sh" G_CLIENTS="stock=${G_STOCK_GIT:?set G_STOCK_GIT to the stock Git client (docs/VERIFY.md)}"   # as the auditors ran them
export G_MINIO="${G_MINIO:-/opt/homebrew/bin/minio}" CARGO_HOME="${CARGO_HOME:-$TREE/.cargo-home}"
( cd "$TREE/server" && cargo build --release --offline -q ) || { echo "build failed"; exit 2; }
echo "archive_check=${G_ARCHIVE_CHECK:-full} tree=$TREE commit=$(git -C "$TREE" rev-parse HEAD) dirty=$(git -C "$TREE" status --porcelain -- server | wc -l | tr -d ' ')" > "$OUT/meta.txt"
rc=0
for n in $NAMES; do
  f=$(ls "$REPRO"/${n}_*.py | head -1)
  # runpy keeps __file__ at the layout path (a plain `python3 script` resolves symlinks to the archive's location)
  ( cd "$L/work" && python3 -c 'import runpy,sys; sys.path.insert(0, sys.argv[1]); sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0], run_name="__main__")' \
      "$L/work/repro" "$L/work/repro/$(basename "$f")" ) > "$OUT/$n.out" 2>&1; r=$?
  echo "$n: $(tail -1 "$OUT/$n.out") [exit $r]" | tee -a "$OUT/summary.txt"; [ $r -eq 0 ] || rc=1
done
python3 - "$OUT" $NAMES <<'PY'
import sys, os, xml.sax.saxutils as x
out, names = sys.argv[1], sys.argv[2:]
cases = []
for n in names:
    t = open(os.path.join(out, n + ".out"), errors="replace").read()
    last = t.strip().splitlines()[-1] if t.strip() else "(no output)"
    # clean only if the reproducer both exited 0 and said so (review V7: the last line alone was trusted)
    rc = next((l.strip().rsplit("[exit ", 1)[1].rstrip("]") for l in open(os.path.join(out, "summary.txt")) if l.startswith(n + ":")), "?")
    ok = "no defect observed" in last and rc == "0"
    body = "" if ok else f"<failure message={x.quoteattr(last)}>{x.escape(t[-4000:])}</failure>"
    cases.append(f"<testcase classname='audit_repro' name='{n}'>{body}</testcase>")
nf = sum(1 for c in cases if "<failure" in c)
open(os.path.join(out, "repro.xml"), "w").write(f"<testsuites><testsuite name='repro' tests='{len(cases)}' failures='{nf}' errors='0'>{''.join(cases)}</testsuite></testsuites>")
PY
rm -rf "$L"; exit $rc
