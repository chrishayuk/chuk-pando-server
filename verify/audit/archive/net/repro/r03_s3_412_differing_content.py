"""R03: create-only publication treats "412 Precondition Failed" as success after checking only the size of an index
(blob.rs:177-180) and only size + last 20 bytes of a pack (blob.rs:181-186). If the key already holds different bytes,
the push is acknowledged anyway; the publishing frontend serves its own local copy, every other frontend reads the
store's copy and cannot serve the ref. Deployment: TCP metadata + MinIO (2 frontends).
Pre-existing differing content is planted with a direct PUT (stands in for any other writer / earlier bug / corruption)."""
import os, tempfile, shutil, random, subprocess
from rlib import *

R = Report("R03 S3 412 with differing content accepted")
cl = Cluster(n=2, mode="tcp-s3")
fe0, fe1 = cl.fe
mi = cl.minio


def head_size(key):
    r = mi.curl("-I", f"{mi.url}/pando/{key}")
    for l in r.stdout.decode().splitlines():
        if l.lower().startswith("content-length:"):
            return int(l.split(":")[1])
    return None


def plant(key, data):
    f = os.path.join(cl.base, "plant.bin"); open(f, "wb").write(data)
    r = mi.curl("-X", "PUT", "--data-binary", "@" + f, "-o", "/dev/null", "-w", "%{http_code}", f"{mi.url}/pando/{key}")
    assert r.stdout.decode() == "200", r


def get(key):
    return mi.curl(f"{mi.url}/pando/{key}").stdout


try:
    for n in ("q", "r1", "r2"):
        fe0.create(n)
    w = Work(cl.base, "w")
    for i in range(3):
        w.commit(fname=f"f{i}.txt")
    head = w.head()
    pack = build_pack(w.dir, head)
    pid = pack[-20:].hex()
    un, res = raw_receive(fe0.url("q"), [(ZERO, head, "refs/heads/main")], pack)
    R.check(res.get("refs/heads/main") == "ok", f"reference push to q acknowledged ({un}, {res})")
    good_pidx = get(f"repos/q/packs/{pid}.pidx")
    R.note(f"pack {pid}: {len(pack)} B, pidx {len(good_pidx)} B")
    # r1: a different index of the same size is already at the key
    plant(f"repos/r1/packs/{pid}.pidx", bytes(random.getrandbits(8) for _ in range(len(good_pidx))))
    # r2: a different pack with the same size and the same final 20 bytes is already at the key
    bad = bytearray(pack); mid = len(bad) // 2
    for i in range(mid, min(mid + 64, len(bad) - 20)): bad[i] ^= 0xFF
    plant(f"repos/r2/packs/{pid}.pack", bytes(bad))
    for repo in ("r1", "r2"):
        un, res = raw_receive(fe0.url(repo), [(ZERO, head, "refs/heads/main")], pack)
        acked = res.get("refs/heads/main") == "ok"
        R.note(f"{repo}: push via fe0 -> {un!r} {res}")
        d0 = os.path.join(cl.base, f"c0-{repo}"); d1 = os.path.join(cl.base, f"c1-{repo}")
        p0 = git(["clone", "-q", fe0.url(repo), d0], check=False)
        p1 = git(["clone", "-q", fe1.url(repo), d1], check=False)
        R.note(f"{repo}: clone fe0 rc={p0.returncode}; clone fe1 rc={p1.returncode} err={p1.stderr.decode(errors='replace').strip()[-200:]!r}")
        R.check(not (acked and p1.returncode != 0),
                f"{repo}: a push is acknowledged only if every frontend can then serve the ref (store copy differs from what was pushed)")
finally:
    if not os.environ.get("KEEP"): cl.cleanup()
    else: print("kept", cl.base)
R.done()
