"""R07: acknowledged-write durability under SIGKILL storms in the networked deployment (TCP metadata + MinIO, 2
frontends). 6 pushers, each on its own ref, push sequential commits through alternating frontends while the metadata
service, MinIO and frontends are SIGKILLed and restarted. Afterwards every ref must be at its last acknowledged tip or at
a later attempted (unacknowledged) tip, and a clone from each frontend must contain every acknowledged tip and pass fsck.
Env: G_DIST_MODE=tcp-s3 (default here) or tcp-fs."""
import os, random, signal, threading, time
from rlib import *

MODE = os.environ.get("R07_MODE", "tcp-s3")
R = Report(f"R07 kill storm ({MODE})")
cl = Cluster(n=2, mode=MODE)
stop = threading.Event()
acked, attempted, errors = {}, {}, []
try:
    cl.fe[0].create("r")
    urls = [s.url("r") for s in cl.fe]

    def pusher(i):
        w = Work(cl.base, f"w{i}"); ref = f"refs/heads/p{i}"
        w.commit();
        n = 0
        while not stop.is_set():
            c = w.head()
            p = w.git("push", "-f" if n == 0 else "-q", urls[n % 2], f"HEAD:{ref}", check=False, timeout=120)
            attempted.setdefault(ref, []).append(c)
            if p.returncode == 0:
                acked[ref] = c; w.commit(); n += 1
            else:
                time.sleep(0.2)
                # resync to whatever the server has, so the next push is a fast-forward or retry of the same tip
    ths = [threading.Thread(target=pusher, args=(i,), daemon=True) for i in range(6)]
    for t in ths: t.start()
    rnd = random.Random(7)
    kills = []
    for k in range(9):
        time.sleep(rnd.uniform(1.5, 3.5))
        victim = ["meta", "minio" if cl.minio else "meta", "fe"][k % 3]
        if victim == "meta":
            cl.meta.kill9(); time.sleep(0.3); cl.meta.start()
        elif victim == "minio":
            cl.minio.p.send_signal(signal.SIGKILL); cl.minio.p.wait(); time.sleep(0.3); cl.minio.start()
        else:
            s = cl.fe[k % 2]; s.kill9(); s.start()
        kills.append(victim)
    time.sleep(3); stop.set()
    for t in ths: t.join(timeout=200)
    R.note(f"kills: {kills}; acked pushes per ref: { {r: len([c for c in attempted[r]]) for r in attempted} }")
    final = ls_remote(urls[1])
    for ref, tip in acked.items():
        att = attempted[ref]; idx = max(i for i, c in enumerate(att) if c == tip)
        ok = final.get(ref) in att[idx:]
        R.check(ok, f"{ref}: server at {str(final.get(ref))[:10]}, last acked {tip[:10]}")
    for j, u in enumerate(urls):
        d = os.path.join(cl.base, f"clone{j}"); p = git(["clone", "-q", "--mirror", u, d], check=False)
        R.check(p.returncode == 0, f"mirror clone from fe{j}: {p.stderr.decode(errors='replace')[-200:]}")
        if p.returncode == 0:
            ok, e = fsck_ok(d); R.check(ok, f"fsck of clone from fe{j} {e}")
            for ref, tip in acked.items():
                R.check(git(["cat-file", "-e", tip], cwd=d, check=False).returncode == 0, f"fe{j} clone has acked {ref}@{tip[:10]}")
finally:
    stop.set()
    if not os.environ.get("KEEP"): cl.cleanup()
    else: print("kept", cl.base)
R.done()
