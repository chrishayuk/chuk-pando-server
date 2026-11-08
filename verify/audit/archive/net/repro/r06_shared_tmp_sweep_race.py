"""R06: shared pack directory, startup sweep of instance tmp areas (blob.rs:77-99). A starting frontend deletes any
`tmp/i-*` area whose LOCK is missing or not flocked. Between another frontend's create_dir_all(area) (blob.rs:93) and its
flock (blob.rs:95) the area looks dead, so a concurrent starter can delete a live frontend's area; that frontend then
fails every push (File::create in a missing directory). Stress: start many frontends at once, repeatedly; check that every
live frontend still has its area and can accept a push."""
import os, threading, time, tempfile, shutil
from rlib import *

R = Report("R06 shared tmp-area sweep race")
base = tempfile.mkdtemp(prefix="r06-")
meta = meta_tcp(base)
blobs = os.path.join(base, "blobs")
N, ROUNDS = int(os.environ.get("R06_N", 10)), int(os.environ.get("R06_ROUNDS", 6))
lost = startfail = 0
try:
    for rnd in range(ROUNDS):
        fes = [Server(extra_env={"PANDO_META": meta.addr, "PANDO_BLOBS": blobs}, data_dir=os.path.join(base, f"r{rnd}fe{i}")) for i in range(N)]
        errs = []
        def st(s):
            try: s.start()
            except Exception as e: errs.append(str(e)[-200:])
        ths = [threading.Thread(target=st, args=(s,)) for s in fes]
        for t in ths: t.start()
        for t in ths: t.join()
        startfail += len(errs)
        areas = os.listdir(os.path.join(blobs, "tmp"))
        for s in fes:
            pid = s.pid()
            if pid and not any(a.startswith(f"i-{pid}-") for a in areas):
                lost += 1; R.note(f"round {rnd}: live frontend pid {pid} has no tmp area")
                s.create("r")
                w = Work(base, f"w{rnd}-{pid}"); w.commit()
                p = w.push(s.url("r"), f"HEAD:refs/heads/x{rnd}-{pid}", check=False)
                R.note(f"  push through it: rc={p.returncode} {p.stderr.decode(errors='replace').strip()[-200:]!r}")
        if errs:
            R.note(f"round {rnd}: startup errors {errs[:2]}")
            for s in fes:
                if not s.pid(): R.note("  failed frontend log: " + server_log(s.data).strip()[-200:])
        for s in fes: s.stop()
    R.note(f"{ROUNDS} rounds x {N} concurrent starts: {startfail} startup failures, {lost} live frontends without a tmp area")
    R.check(lost == 0 and startfail == 0, "no frontend lost its tmp area or failed to start because of a concurrent sweep")
finally:
    meta.stop(); shutil.rmtree(base, ignore_errors=True)
R.done()
