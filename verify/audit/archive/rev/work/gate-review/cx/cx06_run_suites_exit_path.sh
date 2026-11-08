#!/usr/bin/env bash
# Finding 6: exit-status path through an UNMODIFIED copy of run_suites.sh with stubbed suites/tools.
# A stub 'par' suite exits 0 but writes a JUnit with a failing test; another variant writes no record at all.
set -u
G="$(cd "$(dirname "$0")/.." && pwd)"; S="$(mktemp -d "${TMPDIR:-/tmp}/rs.XXXXXX")"
mkdir -p "$S/here/verify/gate" "$S/here/server/target/release" "$S/bin" "$S/rec/heldout" "$S/cg56/work"
cp "$G/run_suites.sh" "$S/here/verify/"; cp "$G/gate.py" "$G/expectations.json" "$S/here/verify/gate/"
echo bin > "$S/here/server/target/release/pando"
printf '#!/bin/sh\nexit 0\n' > "$S/bin/cargo"; printf '#!/bin/sh\necho 0000000\n' > "$S/bin/git"; printf '#!/bin/sh\necho "{ 1.00 1.00 1.00 }"\n' > "$S/bin/sysctl";  # host load >16 would make run_suites wait
 chmod +x "$S/bin/"*
echo x > "$S/rec/heldout/f"; (cd "$S/rec" && shasum -a 256 heldout/f > heldout/MANIFEST.sha256)
echo y > "$S/cg56/work/g"; (cd "$S/cg56/work" && shasum -a 256 ./g > MANIFEST.sha256)
cat > "$S/cg56/work/run.sh" <<'R'
#!/bin/sh
for a in "$@"; do case "$a" in --junitxml=*) X="${a#--junitxml=}";; esac; done
[ -n "${NO_RECORD:-}" ] && { echo "crashed"; exit 0; }
echo "<testsuites><testsuite failures='${NF:-1}' errors='0'><testcase classname='tests_cg6.CG6' name='test_cg6_parallel_pack_download'>${BODY-<failure message='x'/>}</testcase></testsuite></testsuites>" > "$X"
echo "1 failed"; exit 0
R
chmod +x "$S/cg56/work/run.sh"
go() { echo "== $1"; shift; ( env PATH="$S/bin:$PATH" CHUK_PANDO="$S/rec" CG56="$S/cg56" "$@" bash "$S/here/verify/run_suites.sh" t par >/dev/null 2>&1 ); echo "   run_suites.sh exit=$?"; }
go "par: suite exits 0, record shows a failure"
go "par: suite exits 0, writes no record (crash)" NO_RECORD=1
go "par: clean record" NF=0 BODY=
rm -rf "$S"
