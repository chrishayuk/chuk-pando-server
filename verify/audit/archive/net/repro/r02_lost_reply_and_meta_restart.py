"""R02: (a) a metadata commit whose reply is lost; (b) a non-atomic two-ref push where only the second commit's reply is
lost; (c) SIGKILL + restart of meta-serve under live frontends: acknowledged refs survive, and a push through the other
frontend that depends on objects committed through the first still succeeds (incremental index after a service restart).
Deployment: meta-serve over TCP + shared pack directory; fe0 through the fault proxy, fe1 direct."""
import os, tempfile, time, shutil
from rlib import *

R = Report("R02 lost commit reply / meta restart")
base = tempfile.mkdtemp(prefix="r02-")
meta = meta_tcp(base)
proxy = MetaProxy(meta.tcp_port)
blobs = os.path.join(base, "blobs")
fe0 = Server(extra_env={"PANDO_META": proxy.addr, "PANDO_BLOBS": blobs}, data_dir=os.path.join(base, "fe0"))
fe1 = Server(extra_env={"PANDO_META": meta.addr, "PANDO_BLOBS": blobs}, data_dir=os.path.join(base, "fe1"))
try:
    fe0.start(); fe1.start(); fe0.create("r")
    u0, u1 = fe0.url("r"), fe1.url("r")
    w = Work(base, "w")
    c1 = w.commit(); w.push(u0, "main")
    # (a) lost reply on the single ref commit
    c2 = w.commit()
    proxy.drop_commit_reply = 1
    p = w.push(u0, "main", check=False)
    msg = p.stderr.decode(errors="replace")
    R.note(f"(a) push rc={p.returncode}; client saw: {msg.strip().splitlines()[-3:] if msg else ''}")
    refs = ls_remote(u1)
    R.note(f"(a) server state via fe1: main={refs.get('refs/heads/main','')[:10]} (pushed {c2[:10]})")
    R.check(not (p.returncode != 0 and refs.get("refs/heads/main") == c2),
            "(a) a push reported as failed to the client did not in fact update the ref")
    p2 = w.push(u0, "main", check=False)
    R.note(f"(a) client retry rc={p2.returncode}: {p2.stderr.decode(errors='replace').strip()[-120:]!r}")
    # (b) two refs, non-atomic: first commit's reply delivered, second's lost
    w.git("checkout", "-q", "-b", "topic"); ct = w.commit(); w.git("checkout", "-q", "main"); cm = w.commit()
    proxy.drop_commit_reply = 1; proxy.drop_skip = 1
    p = w.push(u0, "main", "topic", check=False)
    out = p.stderr.decode(errors="replace")
    refs = ls_remote(u1)
    R.note(f"(b) push rc={p.returncode}; client lines: {[l for l in out.splitlines() if '->' in l or 'error' in l][:4]}")
    R.note(f"(b) server: main={refs.get('refs/heads/main','')[:10]} (pushed {cm[:10]}), topic={refs.get('refs/heads/topic','')[:10]} (pushed {ct[:10]})")
    first_committed = refs.get("refs/heads/main") == cm or refs.get("refs/heads/topic") == ct
    R.check(not (first_committed and "[rejected]" not in out and "server error" in out and out.count("server error") >= 2),
            "(b) a ref whose commit succeeded and whose reply was received is not reported as failed")
    w.push(u0, "main", "topic", check=False)
    # (c) SIGKILL + restart the metadata service under live frontends
    acked = ls_remote(u1)
    meta.kill9(); time.sleep(0.5); meta.start()
    c3 = w.commit(); p = w.push(u0, "main", check=False)
    R.check(p.returncode == 0, f"(c) first push via fe0 after meta restart succeeds ({p.stderr.decode(errors='replace').strip()[-160:]!r})")
    after = ls_remote(u1)
    for k, v in acked.items():
        if k != "refs/heads/main" and k != "HEAD":
            R.check(after.get(k) == v, f"(c) acknowledged {k} survives the service SIGKILL")
    w2d = os.path.join(base, "w2"); clone(u1, w2d)
    w2 = Work.__new__(Work); w2.dir = w2d; w2.g = None; w2.n = 100
    import harness.lib as hl; w2.g = hl.GIT
    c4 = w2.commit(); p = w2.push(u1, "main", check=False)
    R.check(p.returncode == 0, f"(c) push via fe1 on top of fe0's post-restart commit succeeds ({p.stderr.decode(errors='replace').strip()[-160:]!r})")
    ok, err = fsck_ok(w2d); R.check(ok, "(c) clone from fe1 passes fsck " + err)
finally:
    for s in (fe0, fe1): s.stop()
    meta.stop()
    if not os.environ.get("KEEP"): shutil.rmtree(base, ignore_errors=True)
    else: print("kept", base)
R.done()
