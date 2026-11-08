"""R08: meta-serve checkpoint (taken when the WAL exceeds 64 MiB, meta.rs:944-951) followed by SIGKILL + restart.
Checks: every committed key survives; the stable LSN does not go backwards; a commit after restart is returned by the
change log to a reader whose mark is the pre-restart stable LSN (else an incremental index refresh misses a pack).
Also probes the latent case where the newest commit is a delete: a snapshot keeps only live keys (meta.rs:463-466),
so max LSN recovered at open (meta.rs:115-127) can be lower than an LSN already issued."""
import os, signal, time, tempfile, shutil
from rlib import *

R = Report("R08 meta-serve checkpoint + restart")
base = tempfile.mkdtemp(prefix="r08-")
meta = meta_tcp(base)
try:
    c = MetaClient(meta.addr)
    P = "pack\x01r\x01"
    l1 = c.commit([], [(P + "a", b"A")])[1]
    big = os.urandom(1 << 20)
    for i in range(70):
        c.commit([], [(f"junk\x01{i}", big)])
    l2 = c.commit([], [(P + "b", b"B")])[1]
    c.commit([], [("zz\x01tmp", b"x")])
    ldel = c.commit([], [("zz\x01tmp", None)])[1]          # newest commit is a delete
    st_before = c.changes(P, 2**64 - 1)[0]
    R.note(f"before: lsn(a)={l1} lsn(b)={l2} lsn(delete)={ldel} stable={st_before}")
    t = time.time()
    while not any(f.startswith("snapshot.") and not f.endswith(".tmp") for f in os.listdir(meta.dir)) and time.time() - t < 20:
        time.sleep(0.5)
    snaps = [f for f in os.listdir(meta.dir) if f.startswith("snapshot.")]
    R.check(bool(snaps), f"checkpoint happened ({snaps})")
    meta.kill9(); time.sleep(0.3); meta.start()
    c = MetaClient(meta.addr)
    R.check(c.get(P + "a") is not None and c.get(P + "b") is not None and c.get("junk\x0169") is not None, "all committed keys survive checkpoint + SIGKILL")
    st_after = c.changes(P, 2**64 - 1)[0]
    R.note(f"after restart: stable={st_after}")
    lnew = c.commit([], [(P + "new", b"N")])[1]
    seen = [k for _, k in c.changes(P, st_before)[1]]
    R.note(f"new commit lsn={lnew}; changes above old mark {st_before}: {seen}")
    R.check(st_after >= st_before, "stable LSN does not go backwards across checkpoint + restart (latent: needs a delete as newest commit)")
    R.check(P + "new" in seen, "a commit after restart is visible to a reader holding the pre-restart mark")
finally:
    meta.stop(); shutil.rmtree(base, ignore_errors=True)
R.done()
