"""D2 investigation probe: the D2StalledCommit scenario, repeated; for every run records each push's wall time and,
from the frontends' push_timing events, which phase any wait was in. Usage: probe.py RUNS MODE OUT.jsonl"""
import json, os, shutil, sys, tempfile, threading, time
sys.path.insert(0, os.path.join(os.environ["SRV_TREE"], "verify", "dist"))
from distlib import Cluster
from harness.lib import Work, git, raw_receive, build_pack, ZERO

def events(srv):
    p = os.path.join(srv.data, "events", "events.jsonl"); out = []
    if os.path.exists(p):
        for l in open(p):
            try: e = json.loads(l)
            except ValueError: continue
            out.append(e.get("data", e))
    return out

runs, mode, outp = int(sys.argv[1]), sys.argv[2], sys.argv[3]
for run in range(runs):
    c = Cluster(2, mode=mode, meta_env={"PANDO_TEST_PAUSE_KEY": "refs/heads/slow"}); tmp = tempfile.mkdtemp()
    try:
        a, b = c.fe; a.create("r"); w = Work(tmp, "w"); base = w.commit("f.txt", "base\n"); w.push(a.url("r"), "main")
        def mk(msg): return w.git("commit-tree", f"{base}^{{tree}}", "-p", base, "-m", msg).stdout.decode().strip()
        slow, rival, f1, f2 = mk("slow"), mk("rival"), mk("f1"), mk("f2")
        out = {}; T0 = time.time()
        def push(key, srv, ref, new, old=ZERO):
            t = time.time(); r = raw_receive(srv.url("r"), [(old, new, ref)], build_pack(w.dir, new, base))
            out[key] = {"start": round(t - T0, 3), "dt": round(time.time() - t, 3), "res": r[1].get(ref), "tip": new}
        ts = threading.Thread(target=push, args=("slow", a, "refs/heads/slow", slow)); ts.start()
        time.sleep(0.6)
        tr = threading.Thread(target=push, args=("rival", b, "refs/heads/slow", rival)); tr.start()
        for key, srv, ref, new in (("fastA", a, "refs/heads/fa", f1), ("fastB", b, "refs/heads/fb", f2)):
            push(key, srv, ref, new)
        ts.join(); tr.join()
        timing = {}
        for name, srv in (("a", a), ("b", b)):
            ev = events(srv)
            ing = {e["push"]: e["pack"] for e in ev if e.get("ev") == "pack_ingested"}
            for e in ev:
                if e.get("ev") == "push_timing":
                    timing.setdefault(name, []).append({k: v for k, v in e.items() if k not in ("node", "seq", "repo")})
        slowrun = any(out[k]["dt"] >= 0.3 for k in ("fastA", "fastB"))
        allev = {n: [e for e in events(srv) if e.get("ev") not in ("object_index_built",)] for n, srv in (("a", a), ("b", b))} if slowrun else None
        rec = {"run": run, "mode": mode, "out": out, "timing": timing, "events": allev, "T0_ms": int(T0 * 1000),
               "fail": any(out[k]["dt"] >= 1.5 for k in ("fastA", "fastB"))}
        open(outp, "a").write(json.dumps(rec) + "\n")
        print(run, "FAIL" if rec["fail"] else "ok", {k: v["dt"] for k, v in out.items()}, flush=True)
    finally:
        c.cleanup(); shutil.rmtree(tmp, ignore_errors=True)
