#!/usr/bin/env python3
"""Mutation validation v2 (after REVIEW-1): control + author mutants + reviewer mutants, against the fixed behavioral suites.
Usage: mutation_run2.py [workers]. Output heldout/mutation_results_v2.json. A mutant is KILLED if >=1 behavioral test fails."""
import concurrent.futures as cf, json, os, re, subprocess, sys, time
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RUN_SH = os.path.join(ROOT, "heldout", "mutants_review1", "run.sh")
FILES = ["tests_cg1.py", "tests_cg2.py", "tests_cg3.py", "tests_cg4.py", "tests_review1.py"]
AUTHOR = ["lostupdate", "nodurable", "staleread", "backwards", "deleteignore", "missingobj", "globallock"]
REVIEW = ["lostupdate_branch", "createoverwrite", "globalcommit", "nodurable_fakepid", "nodurable_late", "lazydurable", "gcnotags", "atomicignored", "foobar500", "nogzip", "trunkonlyadv", "trunkonly_overwrite"]
MUTANTS = [""] + AUTHOR + REVIEW
WORKERS = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 3
def run(m):
    env = dict(os.environ, G_MUTANT=m, G_N="16", G_RUN_SH=RUN_SH, G_TEST_TIMEOUT="240"); t0 = time.time()
    try: p = subprocess.run([sys.executable, "-m", "pytest", *[os.path.join(ROOT, "heldout", f) for f in FILES], "-q", "--tb=line", "-rfEs", "-p", "no:cacheprovider"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=1500)
    except subprocess.TimeoutExpired: return m or "control", {"failed": ["TIMEOUT(1500s)"], "killed": True, "seconds": 1500, "summary": "timeout (hang counts as detected)"}
    failed = sorted({re.sub(r"^tests_", "", f).split(" ")[0] for f in re.findall(r"^(?:FAILED|SUBFAILED[^ ]*|ERROR) (?:\([^)]*\) )?heldout/(\S+?)(?: |$)", p.stdout, re.M)})
    skipped = re.findall(r"SKIPPED \[\d+\] heldout/\S+: (.*)", p.stdout)
    return m or "control", {"failed": failed, "killed": bool(failed), "skipped": skipped, "seconds": round(time.time() - t0), "summary": (p.stdout.strip().splitlines() or [p.stderr[-200:]])[-1]}
if __name__ == "__main__":
    out = {}
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        for name, r in ex.map(run, MUTANTS):
            out[name] = r; print(f"{name:20s} {'KILLED' if r['killed'] else 'SURVIVED':8s} n_failed={len(r['failed'])} :: {r['summary']}", flush=True)
            json.dump(out, open(os.path.join(ROOT, "heldout", "mutation_results_v2.json"), "w"), indent=1)
    print("DONE")
