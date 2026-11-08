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
  - a known failure fails in a different way than registered: each known failure has a signature (kind: failure or
    error; `match`: regexes that must all occur in the failure text; `forbid`: regexes that must not), so a new
    failure mode of the same test is never masked (review V5);
  - an allowed skip has a different reason than registered (`match` on the skip message, review V6);
  - with --require-meta (always used by run_suites.sh): the run's meta.txt is missing, or a record is older than it
    (a stale record from an earlier run), or the tree's current binary differs from the one recorded (--tree).
A known failure that passes is reported (so the baseline can be tightened) but does not fail the run. A test that ran
but is not in `required` is reported as unregistered; it fails the run with --strict (run_suites.sh uses --strict).
--suites restricts the check; the verdict is then labelled PARTIAL and gate.json records it (review V7).

Usage: gate.py RESULT_DIR [--suites=a,b] [--expect=FILE] [--strict] [--require-meta] [--tree=DIR]
       exit 0 = pass, 1 = fail, 2 = usage or bad expectations
"""
import json, os, re, sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))


def read_junit(path):
    """-> (outcomes {id: passed|failed|error|skipped}, reported_failures_total, texts {id: failure/skip text})"""
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    out, texts, reported = {}, {}, 0
    for s in suites:
        reported += int(s.get("failures", 0) or 0) + int(s.get("errors", 0) or 0)
    rank = {"passed": 0, "skipped": 1, "failed": 2, "error": 3}
    for tc in root.iter("testcase"):
        tid = f"{tc.get('classname')}::{tc.get('name')}"
        tags = {c.tag for c in tc}
        st = "error" if "error" in tags else "failed" if "failure" in tags else "skipped" if "skipped" in tags else "passed"
        text = "\n".join((c.get("message") or "") + "\n" + (c.text or "") for c in tc if c.tag in ("error", "failure", "skipped"))
        # the same id twice (e.g. a call failure and a teardown error): worst wins, texts are joined
        if tid not in out or rank[st] > rank[out[tid]]:
            out[tid] = st
        texts[tid] = (texts.get(tid, "") + "\n" + text).strip()
    return out, reported, texts


def read_cargo(path):
    text = open(path, errors="replace").read()
    out = {}
    for name, res in re.findall(r"^test (\S+) \.\.\. (ok|FAILED|ignored)\b", text, re.M):
        out[name] = {"ok": "passed", "FAILED": "failed", "ignored": "skipped"}[res]
    reported = sum(int(n) for n in re.findall(r"^test result: \w+\. \d+ passed; (\d+) failed", text, re.M))
    if "test result:" not in text:
        raise ValueError("no 'test result:' line (build failed or tests did not run)")
    # every test binary that started must have finished (a crash of one binary would leave its tests unreported)
    started = len(re.findall(r"^\s+Running ", text, re.M))
    finished = len(re.findall(r"^test result:", text, re.M))
    if started and finished < started:
        raise ValueError(f"{started} test binaries started but only {finished} reported a result (crash?)")
    return out, reported, {}


def signature_mismatch(entry, outcome, text):
    """None if the outcome and its text match the registered signature, else why not. An entry without a signature
    is itself an error (every exception must say what it looks like)."""
    if not isinstance(entry, dict) or not entry.get("match"):
        return "no signature registered for this exception"
    kind = entry.get("kind", "skipped" if outcome == "skipped" else "failed")
    if outcome != kind:
        return f"outcome {outcome}, registered {kind}"
    for rx in entry["match"]:
        if not re.search(rx, text, re.S):
            return f"text does not match {rx!r}"
    for rx in entry.get("forbid", []):
        if re.search(rx, text, re.S):
            return f"text matches forbidden {rx!r}"
    return None


def judge(name, spec, rdir):
    """-> (problems [str], notes [str], counts dict)"""
    problems, notes = [], []
    path = os.path.join(rdir, spec["file"])
    try:
        if not os.path.exists(path):
            raise FileNotFoundError(spec["file"])
        outcomes, reported, texts = (read_cargo if spec.get("format") == "cargo" else read_junit)(path)
    except Exception as e:
        return [f"{name}: result unreadable or missing ({e}): the suite did not run or did not report"], [], {}
    known = spec.get("known_fail", {})
    allowed = spec.get("allowed_skip", {})
    required = spec.get("required", [])
    bad = [t for t, s in outcomes.items() if s in ("failed", "error")]
    for t in sorted(bad):
        if t in known:
            why = signature_mismatch(known[t], outcomes[t], texts.get(t, ""))
            if why:
                problems.append(f"{name}: known failure {t} FAILED DIFFERENTLY ({why}): {texts.get(t, '')[:200]!r}")
            else:
                notes.append(f"{name}: known failure {t} ({known[t]['reason']}; {known[t]['ref']})")
        else:
            problems.append(f"{name}: UNEXPECTED {outcomes[t].upper()} {t}")
    for t, s in sorted(outcomes.items()):
        if s == "skipped":
            if t in allowed:
                why = signature_mismatch(allowed[t], "skipped", texts.get(t, ""))
                if why:
                    problems.append(f"{name}: allowed skip {t} SKIPPED FOR ANOTHER REASON ({why}): {texts.get(t, '')[:200]!r}")
                else:
                    notes.append(f"{name}: allowed skip {t} ({allowed[t]['reason']})")
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


FLAGS = {"suites", "expect", "strict", "require-meta", "tree"}


def freshness(rdir, files, tree):
    """Problems if the run's meta.txt is missing, a record predates it, or the tree's binary is not the one recorded."""
    meta = os.path.join(rdir, "meta.txt")
    if not os.path.exists(meta):
        return ["meta.txt missing: cannot tell whether these records belong to this run"]
    probs, t0 = [], os.path.getmtime(meta) - 1
    for f in files:
        p = os.path.join(rdir, f)
        if os.path.exists(p) and os.path.getmtime(p) < t0:
            probs.append(f"{f} is older than this run's meta.txt (a stale record?)")
    if tree:
        import hashlib
        m = re.search(r"binary_sha256=([0-9a-f]{64})", open(meta).read())
        b = os.path.join(tree, "server", "target", "release", "pando")
        if not m:
            probs.append("meta.txt records no binary_sha256")
        elif not os.path.exists(b) or hashlib.sha256(open(b, "rb").read()).hexdigest() != m.group(1):
            probs.append("the tree's current binary is not the one this run recorded (rebuilt since?)")
    return probs


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    opts = dict(a[2:].split("=", 1) if "=" in a else (a[2:], "1") for a in argv[1:] if a.startswith("--"))
    bad_flags = sorted(set(opts) - FLAGS)
    if len(args) != 1 or bad_flags:
        print(__doc__)
        if bad_flags:
            print(f"GATE: unknown option(s) {bad_flags}")
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
    partial = bool(pick) and set(names) != set(suites)
    all_problems, report = [], {}
    if "require-meta" in opts:
        fp = freshness(rdir, [suites[n]["file"] for n in names], opts.get("tree"))
        for line in fp:
            print("   ✗ run: " + line)
        all_problems += fp
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
    skipped_suites = sorted(set(suites) - set(names))
    json.dump({"pass": not all_problems, "partial": partial, "suites_not_checked": skipped_suites, "strict": "strict" in opts,
               "suites": report}, open(os.path.join(rdir, "gate.json"), "w"), indent=1)
    label = ("PARTIAL " if partial else "") + ("PASS" if not all_problems else "FAIL")
    extra = f"; NOT CHECKED: {', '.join(skipped_suites)}" if partial else ""
    print(f"GATE: {label} ({len(all_problems)} problem(s)){extra} -> {rdir}/gate.json")
    return 0 if not all_problems else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
