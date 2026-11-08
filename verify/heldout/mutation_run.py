#!/usr/bin/env python3
"""Mutation validation: run the behavioral hidden suites against B0 (control) and each fault-injected mutant.
A mutant is KILLED if >=1 behavioral test fails. The control must pass everything."""
import concurrent.futures as cf, json, os, re, subprocess, sys, time
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MUTANTS = ["", "lostupdate", "nodurable", "staleread", "backwards", "deleteignore", "missingobj", "globallock"]
FILES = ["tests_cg1.py", "tests_cg2.py", "tests_cg3.py", "tests_cg4.py"]
WORKERS = int(sys.argv[1]) if len(sys.argv) > 1 else 3

def run(m):
    env = dict(os.environ, G_MUTANT=m, G_N="16")
    t0 = time.time()
    p = subprocess.run([sys.executable, "-m", "pytest", *[os.path.join(ROOT, "heldout", f) for f in FILES], "-q", "--tb=no", "-rfE", "-p", "no:cacheprovider"],
                       cwd=ROOT, env=env, capture_output=True, text=True)
    failed = sorted(set(re.findall(r"^(?:FAILED|SUBFAILED[^ ]*|ERROR) (?:\([^)]*\) )?heldout/(\S+?)(?: |$)", p.stdout, re.M)))
    failed = sorted({re.sub(r"^tests_", "", f).split(" ")[0] for f in failed})
    return m or "control(B0)", {"failed": failed, "seconds": round(time.time() - t0), "summary": p.stdout.strip().splitlines()[-1] if p.stdout.strip() else p.stderr[-300:]}

if __name__ == "__main__":
    out = {}
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        for name, r in ex.map(run, MUTANTS): out[name] = r; print(name, "->", r["summary"], flush=True)
    json.dump(out, open(os.path.join(ROOT, "heldout", "mutation_results.json"), "w"), indent=1)
    ctrl = out["control(B0)"]["failed"]
    print("control failures:", ctrl)
    for m, r in out.items():
        if m != "control(B0)": print(f"{m:13s} {'KILLED' if r['failed'] else 'SURVIVED'} by {len(r['failed'])} tests")
