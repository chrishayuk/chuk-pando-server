"""Helper: build hand-crafted result records and run the (copied) gate on them with the real expectations."""
import json, os, subprocess, sys, tempfile
D = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(D, "gate.py"); EXP = json.load(open(os.path.join(D, "expectations.json")))["suites"]
from xml.sax.saxutils import quoteattr as q

def junit(cases, failures=None, errors=None, wrap=True):
    """cases: list of (id, kind, msg) kind in pass/fail/error/skip/failerr"""
    body = []
    for tid, kind, msg in cases:
        cls, name = tid.split("::")
        inner = {"pass": "", "fail": f"<failure message={q(msg)}>{msg}</failure>",
                 "error": f"<error message={q(msg)}>{msg}</error>", "skip": f"<skipped message={q(msg)}/>",
                 "failerr": f"<failure message='call'/><error message={q(msg)}/>"}[kind]
        body.append(f"<testcase classname={q(cls)} name={q(name)} time='0'>{inner}</testcase>")
    nf = sum(k in ("fail", "failerr") for _, k, _ in cases) if failures is None else failures
    ne = sum(k in ("error", "failerr") for _, k, _ in cases) if errors is None else errors
    s = f"<testsuite name='pytest' tests='{len(cases)}' failures='{nf}' errors='{ne}'>{''.join(body)}</testsuite>"
    return f"<?xml version='1.0'?><testsuites>{s}</testsuites>" if wrap else s

def passing(suite, override=None, drop=()):
    o = dict(override or {})
    return [(t, *o.get(t, ("pass", ""))) for t in EXP[suite]["required"] if t not in drop] + \
           [(t, *v) for t, v in o.items() if t not in EXP[suite]["required"]]

def run(label, files, suites, extra=(), expect=None):
    d = tempfile.mkdtemp(prefix="gx-", dir=os.environ.get("TMPDIR"))
    for fn, text in files.items(): open(os.path.join(d, fn), "w").write(text)
    args = [sys.executable, "-I", GATE, d, f"--suites={suites}" if suites else "", *extra]
    if expect: args.append(f"--expect={expect}")
    p = subprocess.run([a for a in args if a], capture_output=True, text=True)
    verdict = p.stdout.strip().splitlines()[-1].split(" (")[0] if p.stdout.strip() else "(no output)"
    print(f"[{label}] exit={p.returncode} {verdict}")
    for l in p.stdout.splitlines():
        if l.lstrip().startswith(("✗", "·")): print("      " + l.strip()[:170])
    return p.returncode
