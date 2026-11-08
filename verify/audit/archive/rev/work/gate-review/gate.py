#!/usr/bin/env python3
"""Regression gate: decides pass/fail for one run_suites.sh result directory from per-test outcomes.

It does not trust a suite's exit status or its summary line. For every suite in verify/gate/expectations.json it reads the
per-test record (pytest JUnit XML, or `cargo test` output for the unit suite) and FAILS the run when:
  - a suite's result file is missing or unreadable (the suite did not run, or crashed before reporting);
  - a test failed or errored and is not listed under that suite's `known_fail` (with a reason and a reference);
  - a test was skipped and is not listed under `allowed_skip` (with a reason);
  - a test listed under `required` did not run at all (deleted, renamed, deselected, or not collected);
  - fewer tests ran than the suite's `min_total` floor;
  - the suite's own totals (JUnit <testsuite failures/errors>, cargo "N failed") report more failures than the
    per-test records show (e.g. subtest failures), so a failure cannot hide between the two.
A known failure that passes is reported (so the baseline can be tightened) but does not fail the run. A test that ran
but is not in `required` is reported as unregistered; it fails the run only with --strict (adding a test must also
register it, so its later disappearance is caught).

Usage: gate.py RESULT_DIR [--suites a,b,...] [--expect FILE] [--strict]     exit 0 = pass, 1 = fail, 2 = usage
"""
import json, os, re, sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))


def read_junit(path):
    """-> (outcomes {id: passed|failed|error|skipped}, reported_failures_total)"""
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    out, reported = {}, 0
    for s in suites:
        reported += int(s.get("failures", 0) or 0) + int(s.get("errors", 0) or 0)
    for tc in root.iter("testcase"):
        tid = f"{tc.get('classname')}::{tc.get('name')}"
        tags = {c.tag for c in tc}
        if "error" in tags:
            st = "error"
        elif "failure" in tags:
            st = "failed"
        elif "skipped" in tags:
            st = "skipped"
        else:
            st = "passed"
        # the same id twice (e.g. a test reported once for its call and once for a teardown error): worst wins
        rank = {"passed": 0, "skipped": 1, "failed": 2, "error": 3}
        if tid not in out or rank[st] > rank[out[tid]]:
            out[tid] = st
    return out, reported


def read_cargo(path):
    text = open(path, errors="replace").read()
    out = {}
    for name, res in re.findall(r"^test (\S+) \.\.\. (ok|FAILED|ignored)\b", text, re.M):
        out[name] = {"ok": "passed", "FAILED": "failed", "ignored": "skipped"}[res]
    reported = sum(int(n) for n in re.findall(r"^test result: \w+\. \d+ passed; (\d+) failed", text, re.M))
    if "test result:" not in text:
        raise ValueError("no 'test result:' line (build failed or tests did not run)")
    return out, reported


def judge(name, spec, rdir):
    """-> (problems [str], notes [str], counts dict)"""
    problems, notes = [], []
    path = os.path.join(rdir, spec["file"])
    try:
        if not os.path.exists(path):
            raise FileNotFoundError(spec["file"])
        outcomes, reported = (read_cargo if spec.get("format") == "cargo" else read_junit)(path)
    except Exception as e:
        return [f"{name}: result unreadable or missing ({e}): the suite did not run or did not report"], [], {}
    known = spec.get("known_fail", {})
    allowed = spec.get("allowed_skip", {})
    required = spec.get("required", [])
    bad = [t for t, s in outcomes.items() if s in ("failed", "error")]
    for t in sorted(bad):
        if t in known:
            notes.append(f"{name}: known failure {t} ({known[t]['reason']}; {known[t]['ref']})")
        else:
            problems.append(f"{name}: UNEXPECTED {outcomes[t].upper()} {t}")
    for t, s in sorted(outcomes.items()):
        if s == "skipped":
            if t in allowed:
                notes.append(f"{name}: allowed skip {t} ({allowed[t]})")
            else:
                problems.append(f"{name}: UNEXPECTED SKIP {t}")
    for t in sorted(known):
        if outcomes.get(t) == "passed":
            notes.append(f"{name}: known failure now PASSES {t} (baseline can be tightened)")
    for t in required:
        if t not in outcomes:
            problems.append(f"{name}: MISSING required test {t} (deleted, renamed, deselected or not collected)")
    ran = sum(1 for s in outcomes.values() if s != "skipped")
    if ran < spec.get("min_total", 0):
        problems.append(f"{name}: only {ran} tests ran, floor is {spec['min_total']}")
    if reported > len(bad):
        problems.append(f"{name}: suite reports {reported} failures/errors but only {len(bad)} test records failed")
    extra = sorted(set(outcomes) - set(required) - set(known) - set(allowed))
    counts = {k: sum(1 for s in outcomes.values() if s == k) for k in ("passed", "failed", "error", "skipped")}
    return problems, notes, dict(counts, unregistered=extra)


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    opts = dict(a[2:].split("=", 1) if "=" in a else (a[2:], "1") for a in argv[1:] if a.startswith("--"))
    if len(args) != 1:
        print(__doc__)
        return 2
    rdir = args[0]
    exp = json.load(open(opts.get("expect", os.path.join(HERE, "expectations.json"))))
    suites = exp["suites"]
    pick = opts.get("suites")
    names = pick.split(",") if pick else list(suites)
    unknown = [n for n in names if n not in suites]
    if unknown:
        print(f"GATE: unknown suite(s) {unknown}")
        return 2
    all_problems, report = [], {}
    for n in names:
        p, notes, counts = judge(n, suites[n], rdir)
        if counts.get("unregistered"):
            msg = f"{n}: {len(counts['unregistered'])} unregistered test(s): {', '.join(counts['unregistered'])}"
            (p.append if "strict" in opts else notes.append)(msg)
        all_problems += p
        report[n] = {"counts": counts, "problems": p, "notes": notes}
        c = counts or {}
        print(f"{n:12s} {'FAIL' if p else 'ok  '}  passed={c.get('passed','-')} failed={c.get('failed','-')} "
              f"error={c.get('error','-')} skipped={c.get('skipped','-')}")
        for line in p:
            print("   ✗ " + line)
        for line in notes:
            print("   · " + line)
    json.dump({"pass": not all_problems, "suites": report}, open(os.path.join(rdir, "gate.json"), "w"), indent=1)
    print(f"GATE: {'PASS' if not all_problems else 'FAIL'} ({len(all_problems)} problem(s)) -> {rdir}/gate.json")
    return 0 if not all_problems else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
