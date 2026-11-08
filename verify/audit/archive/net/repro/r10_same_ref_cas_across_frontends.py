"""R10: per-ref compare-and-swap across two frontends under contention (tcp-s3), with one SIGKILL + restart of the
metadata service in the middle. 6 workers read the tip through a random frontend, build a child commit, and push
old->new through a random frontend. Invariants: (1) no two acknowledged updates share the same old value (lost update);
(2) every acknowledged new tip is an ancestor of (or equal to) the final tip (no acknowledged update lost)."""
import os, random, threading, time
from rlib import *

R = Report("R10 same-ref CAS across frontends")
cl = Cluster(n=2, mode=os.environ.get("R10_MODE", "tcp-s3"))
try:
    a, b = cl.fe; a.create("r"); urls = [a.url("r"), b.url("r")]
    w = Work(cl.base, "w"); base = w.commit(); w.push(urls[0], "main")
    lock = threading.Lock(); acked = []; ng = []; stop = threading.Event(); ctr = [0]
    def worker(i):
        rnd = random.Random(i)
        while not stop.is_set():
            try:
                tip = ls_remote(urls[rnd.randrange(2)]).get("refs/heads/main")
                with lock:
                    ctr[0] += 1; n = ctr[0]
                    new = w.git("commit-tree", f"{tip}^{{tree}}", "-p", tip, "-m", f"w{i}-{n}").stdout.decode().strip()
                    pack = build_pack(w.dir, new, tip)
                un, res = raw_receive(urls[rnd.randrange(2)], [(tip, new, "refs/heads/main")], pack)
                (acked if res.get("refs/heads/main") == "ok" else ng).append((tip, new))
            except Exception as e:
                ng.append(("exc", str(e)[:80])); time.sleep(0.1)
    ths = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(6)]
    for t in ths: t.start()
    time.sleep(8); cl.meta.kill9(); time.sleep(0.3); cl.meta.start(); time.sleep(8); stop.set()
    for t in ths: t.join(timeout=120)
    olds = [o for o, _ in acked]
    dup = len(olds) - len(set(olds))
    R.note(f"{len(acked)} acknowledged, {len(ng)} rejected/failed")
    R.check(dup == 0, f"no two acknowledged updates from the same old tip ({dup} duplicates)")
    final = ls_remote(urls[1]).get("refs/heads/main")
    lost = [n for _, n in acked if w.git("merge-base", "--is-ancestor", n, final, check=False).returncode != 0]
    R.check(not lost, f"every acknowledged tip is an ancestor of the final tip ({len(lost)} lost)")
    d = os.path.join(cl.base, "c"); clone(urls[0], d); ok, e = fsck_ok(d); R.check(ok, "clone fsck " + e)
finally:
    stop.set(); cl.cleanup()
R.done()
