"""R11: A4 says only the per-ref pointer is contended. Pushes to different refs that set the same new tip (or carry the
same pack) also share `tip/<oid>` and `pack/<id>` in their read/write sets (repo.rs:749-752, 773-774), so they serialise.
One commit is stalled 3 s inside the metadata service (builders' test hook PANDO_TEST_PAUSE_KEY); a push of the same
commit to a different branch through the other frontend waits for it."""
import os, threading, time
from rlib import *

R = Report("R11 A4: unrelated refs contend on shared tip/pack keys")
c = Cluster(2, meta_env={"PANDO_TEST_PAUSE_KEY": "refs/heads/slow"}, mode=os.environ.get("R11_MODE", "tcp-fs"))
try:
    a, b = c.fe; a.create("r"); w = Work(c.base, "w"); base = w.commit("f.txt", "base\n"); w.push(a.url("r"), "main")
    x = w.git("commit-tree", f"{base}^{{tree}}", "-p", base, "-m", "shared").stdout.decode().strip()
    y = w.git("commit-tree", f"{base}^{{tree}}", "-p", base, "-m", "unrelated").stdout.decode().strip()
    pk = build_pack(w.dir, x, base); out = {}
    def push(key, srv, ref, new, pack):
        t = time.time(); r = raw_receive(srv.url("r"), [(ZERO, new, ref)], pack); out[key] = (round(time.time() - t, 2), r[1].get(ref))
    ts = threading.Thread(target=push, args=("slow", a, "refs/heads/slow", x, pk)); ts.start(); time.sleep(0.6)
    push("same-tip-other-ref", b, "refs/heads/other", x, pk)
    push("control-different-tip", b, "refs/heads/ctl", y, build_pack(w.dir, y, base))
    ts.join()
    R.note(str(out))
    R.check(out["control-different-tip"][0] < 1.0, "control: unrelated ref with its own tip is not delayed")
    R.check(out["same-tip-other-ref"][0] < 1.0, "a push to a different ref is not delayed by a stalled commit on another ref (A4)")
finally:
    c.cleanup()
R.done()
