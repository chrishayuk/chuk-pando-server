"""R05: (a) object store down during a push: never acknowledged; after restart pushes work and the other frontend serves
everything. (b) object store stalled (SIGSTOP: TCP accepts, never answers): how long a push hangs (s3.rs:238-259 checks
its 10 s deadline only between attempts; each attempt has a 120 s read timeout, s3.rs:289). (c) metadata service
stalled (SIGSTOP): a frontend request hangs with no timeout (meta.rs:747-752, no read timeout on the connection).
Deployment: TCP metadata + MinIO, 2 frontends."""
import os, signal, time, subprocess
from rlib import *

R = Report("R05 store outage / stalls")
cl = Cluster(n=2, mode="tcp-s3")
fe0, fe1 = cl.fe
try:
    fe0.create("r"); u0, u1 = fe0.url("r"), fe1.url("r")
    w = Work(cl.base, "w"); w.commit(); w.push(u0, "main")
    # (a)
    cl.minio.stop()
    c = w.commit(); t = time.time(); p = w.push(u0, "main", check=False); dt = time.time() - t
    R.note(f"(a) push with store down: rc={p.returncode} after {dt:.1f}s: {p.stderr.decode(errors='replace').strip()[-160:]!r}")
    R.check(p.returncode != 0 and ls_remote(u1).get("refs/heads/main") != c, "(a) not acknowledged and ref not moved while the store is down")
    cl.minio.start()
    p = w.push(u0, "main", check=False)
    R.check(p.returncode == 0, "(a) same push succeeds after the store returns")
    d = os.path.join(cl.base, "c1"); clone(u1, d); ok, e = fsck_ok(d); R.check(ok, "(a) clone from fe1 is complete " + e)
    # (b)
    os.kill(cl.minio.p.pid, signal.SIGSTOP)
    c = w.commit(); t = time.time()
    try:
        p = w.git("push", u0, "main", check=False, timeout=200); rc = p.returncode
    except subprocess.TimeoutExpired:
        rc = "timeout"
    dt = time.time() - t
    os.kill(cl.minio.p.pid, signal.SIGCONT)
    R.note(f"(b) push with store stalled returned rc={rc} after {dt:.1f}s")
    R.check(dt < 60, f"(b) a stalled object store makes a push fail within ~60 s (took {dt:.0f} s)")
    # (c)
    w.push(u0, "main", check=False)
    os.kill(cl.meta.p.pid, signal.SIGSTOP)
    t = time.time()
    try:
        p = git(["ls-remote", u0], check=False, timeout=45); rc = p.returncode
    except subprocess.TimeoutExpired:
        rc = "timeout"
    dt = time.time() - t
    os.kill(cl.meta.p.pid, signal.SIGCONT)
    R.note(f"(c) ls-remote with metadata service stalled: rc={rc} after {dt:.1f}s")
    R.check(rc != "timeout", "(c) a stalled metadata service makes requests fail rather than hang (>45 s)")
    time.sleep(1)
    R.check(git(["ls-remote", u0], check=False, timeout=30).returncode == 0, "(c) frontend recovers after the stall")
finally:
    if not os.environ.get("KEEP"): cl.cleanup()
    else: print("kept", cl.base)
R.done()
