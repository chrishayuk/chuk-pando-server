"""R21 (review of F1 remediation): background work missed before a restart is recovered only by the startup catch-up
`maintain_all`, and when that fails the worker retries the *whole* pass (`retry.insert("")`, compact.rs:191-193,
220-222). `maintain_all` stops at the first repository that fails (`?`, compact.rs:283-285). So one repository whose
maintenance fails permanently (here: one pack lost from the object store) stops covers from ever being built for every
repository after it in key order, and the operator's `maintain` fails the same way. Work for the healthy repository is
lost until it happens to receive a new push.
Steps (tcp-s3, one frontend):
 1. repos a, b; 4 pushes each (seq 4 => an L1 cover is due); background jobs forced to panic (PANDO_TEST_PANIC_JOBS),
    standing in for an outage, so no cover is built; the frontend is then stopped (lost in-memory retry set).
 2. one pack of repository a is deleted from the store.
 3. restart (no injection, PANDO_TEST_RETRY_SECS=2); wait 15 s; run `maintain`.
 4. control: put a's pack back; wait; b's cover should now appear.
Defect if b has no cover after step 3 although b is intact."""
import os, time
from rlib import *

R = Report("R21 F1: one failing repository blocks catch-up of all later repositories")
cl = Cluster(n=1, mode="tcp-s3", fe_env={"PANDO_TEST_PANIC_JOBS": "100000", "PANDO_TEST_RETRY_SECS": "100000"})
fe = cl.fe[0]
mi = cl.minio
SEP = "\x01"


def covers(repo):
    return [k for k, _ in MetaClient(cl.meta.addr).scan(f"cover{SEP}{repo}{SEP}")]


try:
    first = {}
    for repo in ("a", "b"):
        fe.create(repo)
        w = Work(cl.base, "w" + repo)
        prev = ZERO
        for i in range(4):
            w.commit(fname=f"{repo}{i}.txt")
            h = w.head()
            pack = build_pack(w.dir, h, None if prev == ZERO else prev)
            if i == 0:
                first[repo] = pack[-20:].hex()
            un, res = raw_receive(fe.url(repo), [(prev, h, "refs/heads/main")], pack)
            assert res.get("refs/heads/main") == "ok", (repo, un, res)
            prev = h
    time.sleep(2)
    R.note(f"after pushes with injected job panics: covers a={len(covers('a'))} b={len(covers('b'))}")
    fe.stop()
    key = f"repos/a/packs/{first['a']}.pack"
    saved = mi.curl(f"{mi.url}/pando/{key}").stdout
    r = mi.curl("-X", "DELETE", "-o", "/dev/null", "-w", "%{http_code}", f"{mi.url}/pando/{key}")
    R.note(f"deleted {key} ({len(saved)} B) -> {r.stdout.decode()}")
    fe.env.pop("PANDO_TEST_PANIC_JOBS", None)
    fe.env["PANDO_TEST_RETRY_SECS"] = "2"
    fe.start()
    time.sleep(15)
    p = fe.run("maintain", check=False)
    R.note(f"operator maintain: rc={p.returncode} out={(p.stdout + p.stderr).decode(errors='replace').strip()[-200:]!r}")
    errs = [e for e in events(fe.data) if e.get("ev") == "compaction_error"]
    R.note(f"compaction_error events after restart: {len(errs)} (e.g. {errs[-1] if errs else None})")
    cb = covers("b")
    R.note(f"15 s after restart (+ maintain): covers a={len(covers('a'))} b={len(cb)}")
    R.check(len(cb) > 0, "intact repository b gets its due cover after the outage although repository a is broken")
    # control: repair a, and b is covered by the next retry pass
    f = os.path.join(cl.base, "restore.bin"); open(f, "wb").write(saved)
    mi.curl("-X", "PUT", "--data-binary", "@" + f, "-o", "/dev/null", f"{mi.url}/pando/{key}")
    time.sleep(6)
    R.note(f"control, 6 s after restoring a's pack: covers a={len(covers('a'))} b={len(covers('b'))}")
finally:
    if not os.environ.get("KEEP"): cl.cleanup()
    else: print("kept", cl.base)
R.done()
