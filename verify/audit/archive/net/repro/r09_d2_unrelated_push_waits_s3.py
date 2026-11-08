"""R09: builders' D2 scenario (verify/dist/test_dist.py:104-131) replayed in tcp-s3 mode, repeated, with the receiving
frontend's push_timing events dumped for any unrelated push that waited > 1.5 s for a stalled commit on another branch
(A4: pushes to unrelated refs never contend)."""
import os, threading, time, json
from rlib import *

R = Report("R09 unrelated push waits for stalled commit (tcp-s3)")
MODE = os.environ.get("R09_MODE", "tcp-s3"); N = int(os.environ.get("R09_N", 6))
bad = 0
for it in range(N):
    c = Cluster(2, meta_env={"PANDO_TEST_PAUSE_KEY": "refs/heads/slow"}, mode=MODE)
    try:
        a, b = c.fe; a.create("r"); w = Work(c.base, "w"); base = w.commit("f.txt", "base\n"); w.push(a.url("r"), "main")
        mk = lambda m: w.git("commit-tree", f"{base}^{{tree}}", "-p", base, "-m", m).stdout.decode().strip()
        slow, rival, f1, f2 = mk("slow"), mk("rival"), mk("f1"), mk("f2")
        out = {}
        def push(key, srv, ref, new):
            t = time.time(); r = raw_receive(srv.url("r"), [(ZERO, new, ref)], build_pack(w.dir, new, base)); out[key] = (round(time.time() - t, 2), r[1].get(ref))
        ts = threading.Thread(target=push, args=("slow", a, "refs/heads/slow", slow)); ts.start(); time.sleep(0.6)
        tr = threading.Thread(target=push, args=("rival", b, "refs/heads/slow", rival)); tr.start()
        push("fastA", a, "refs/heads/fa", f1); push("fastB", b, "refs/heads/fb", f2)
        ts.join(); tr.join()
        waited = [k for k in ("fastA", "fastB") if out[k][0] > 1.5]
        print(f"run {it}: {out}", flush=True)
        if waited:
            bad += 1
            srv = a if waited[0] == "fastA" else b
            for e in events(srv.data):
                if e.get("ev") == "push_timing" and e.get("total_ms", 0) > 1000:
                    R.note("slow push_timing on " + ("a" if srv is a else "b") + ": " + json.dumps({k: (round(v) if isinstance(v, float) else v) for k, v in e.items() if k.endswith("_ms") or k == "push"}))
            for e in events(srv.data):
                if e.get("ev") in ("object_index_built",) and e.get("ms", 0) > 500:
                    R.note("slow index build: " + json.dumps(e)[:300])
    finally:
        c.cleanup()
R.check(bad == 0, f"{bad}/{N} runs: a push to an unrelated branch waited > 1.5 s for another branch's stalled commit")
R.done()
