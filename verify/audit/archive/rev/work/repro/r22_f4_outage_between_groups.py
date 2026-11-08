"""R22 (review of F4 remediation): a non-atomic two-ref push where the metadata service becomes unreachable *after* the
first ref's commit was applied and acknowledged to the frontend, and before the second ref's transaction reads.
`PartialPush` (repo.rs:890-897) only covers an `Err` from the second group; a metadata read during an outage panics
(meta.rs:873 `Remote::get`), which unwinds through `push_inner`/`handle_receive`, so the committed first ref is not
reported as ok either. Deployment: meta-serve over TCP + shared pack dir; fe0 through the fault proxy, fe1 direct.
Defect if the client is told the first ref failed (or gets no report) although the server applied it."""
import os, tempfile, time
from rlib import *

R = Report("R22 F4: outage between the groups of a non-atomic push")
base = tempfile.mkdtemp(prefix="r22-")
meta = meta_tcp(base)
proxy = MetaProxy(meta.tcp_port)
blobs = os.path.join(base, "blobs")
fe0 = Server(extra_env={"PANDO_META": proxy.addr, "PANDO_BLOBS": blobs}, data_dir=os.path.join(base, "fe0"))
fe1 = Server(extra_env={"PANDO_META": meta.addr, "PANDO_BLOBS": blobs}, data_dir=os.path.join(base, "fe1"))
try:
    fe0.start(); fe1.start(); fe0.create("r")
    u0, u1 = fe0.url("r"), fe1.url("r")
    w = Work(base, "w")
    w.commit(); w.push(u0, "main")
    w.git("checkout", "-q", "-b", "topic"); ct = w.commit(); w.git("checkout", "-q", "main"); cm = w.commit()
    state = {"committed_main": False}

    def pred(req):
        if req[:1] == b"C" and b"refs/heads/main" in req:
            state["committed_main"] = True
            return False
        return state["committed_main"] and req[:1] in (b"G", b"S") and b"refs/heads/topic" in req

    proxy.blackout_on(pred, 14)
    t = time.time()
    p = w.push(u0, "main", "topic", check=False)
    el = time.time() - t
    out = p.stderr.decode(errors="replace")
    refs = ls_remote(u1)
    R.note(f"push rc={p.returncode} after {el:.1f}s, blackouts={proxy.blackouts}; client said: "
           f"{[l.strip() for l in out.splitlines() if '->' in l or 'error' in l.lower() or 'fatal' in l.lower()][:6]}")
    R.note(f"server via fe1: main={refs.get('refs/heads/main','')[:10]} (pushed {cm[:10]}), "
           f"topic={refs.get('refs/heads/topic','')[:10]} (pushed {ct[:10]})")
    main_applied = refs.get("refs/heads/main") == cm
    main_reported_ok = any("main -> main" in l and "!" not in l and "rejected" not in l for l in out.splitlines())
    R.check(proxy.blackouts == 1, "the outage was injected between the two groups")
    R.check(not (main_applied and not main_reported_ok),
            "a ref whose commit was applied and acknowledged to the frontend is reported ok to the client")
    log = server_log(fe0.data)
    R.note(f"fe0 log tail: {log.strip()[-300:]!r}")
finally:
    for s in (fe0, fe1): s.stop()
    meta.stop()
R.done()
