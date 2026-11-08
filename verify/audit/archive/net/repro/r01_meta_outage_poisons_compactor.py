"""R01: a metadata-service outage longer than the 10 s read-retry window, while the background compactor holds
MAINT_LOCK (compact.rs:216), panics the compactor thread (meta.rs:777 panics on Unavailable) and poisons MAINT_LOCK.
Afterwards, on that frontend: no covers or self-contained copies are ever built again, and `run.sh maintain` fails, until
the frontend process is restarted. A second frontend is used as a control.

Deployment: meta-serve over TCP, shared pack directory; fe0 reaches the service through a fault-injecting proxy."""
import os, tempfile, time, shutil
from rlib import *

R = Report("R01 metadata outage poisons MAINT_LOCK")
base = tempfile.mkdtemp(prefix="r01-")
meta = meta_tcp(base)
proxy = MetaProxy(meta.tcp_port)
blobs = os.path.join(base, "blobs")
fe0 = Server(extra_env={"PANDO_META": proxy.addr, "PANDO_BLOBS": blobs}, data_dir=os.path.join(base, "fe0"))
fe1 = Server(extra_env={"PANDO_META": meta.addr, "PANDO_BLOBS": blobs}, data_dir=os.path.join(base, "fe1"))
try:
    fe0.start(); fe1.start()
    fe0.create("r")
    url0 = fe0.url("r")
    w = Work(base, "w")
    for i in range(3):
        w.commit(); w.push(url0, "main")
    # Arm: the compactor's first metadata read under MAINT_LOCK is the cover lookup (compact.rs:223 -> repo.rs:305).
    proxy.blackout_on(lambda req: req[:1] == b"G" and req[5:].startswith(b"cover\x01"), 13)
    w.commit(); w.push(url0, "main")          # version 4 -> Job::Compact (compact.rs:41)
    t0 = time.time()
    while proxy.blackouts == 0 and time.time() - t0 < 10:
        time.sleep(0.1)
    R.check(proxy.blackouts == 1, "outage was triggered by the compactor's cover read")
    time.sleep(16)                            # outage (13 s) > retry window (10 s); then service is reachable again
    log = server_log(fe0.data)
    R.note("fe0 log panic lines: " + " | ".join(l for l in log.splitlines() if "panic" in l or "Unavailable" in l or "unavailable" in l)[:600])
    # service is back: ordinary pushes work again
    for i in range(4):
        w.commit(); p = w.push(url0, "main", check=False)
    R.check(p.returncode == 0, "pushes through fe0 succeed after the outage")
    time.sleep(3)
    mc = MetaClient(meta.addr)
    covers = [k for k, _ in mc.scan("cover\x01r\x01")]
    R.note(f"covers in metadata after 8 versions (fe0 only): {len(covers)}")
    R.check(len(covers) >= 2, "fe0 built covers for versions 1-4 and 5-8 after the outage (compactor alive)")
    m = fe0.run("maintain", check=False)
    R.note(f"fe0 maintain rc={m.returncode} out={m.stdout.decode()[-200:]!r} err={m.stderr.decode()[-300:]!r}")
    R.check(m.returncode == 0, "run.sh maintain on fe0 succeeds after the outage")
    m1 = fe1.run("maintain", check=False)
    R.note(f"control fe1 maintain rc={m1.returncode} out={m1.stdout.decode()[-200:]!r}")
    covers2 = [k for k, _ in mc.scan("cover\x01r\x01")]
    R.note(f"covers after control frontend maintain: {len(covers2)}")
finally:
    for s in (fe0, fe1):
        s.stop()
    meta.stop()
    keep = os.environ.get("KEEP")
    if not keep: shutil.rmtree(base, ignore_errors=True)
    else: print("kept", base)
R.done()
