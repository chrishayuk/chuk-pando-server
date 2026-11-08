"""R20 (review of F2 remediation): on a 412 for an index, blob.rs `publish` accepts a stored index that differs only in
the pack named for an external delta base, provided `self.index(repo, p)` finds the base (blob.rs:208-216). It never
checks that pack `p` is committed, that `p`'s *pack* exists, or that `p` is even a pack id. Two variants, each with a
foreign/stale object already at the key (the F2 threat model, as in R03):
  A  stored P2.pidx names base pack FAKE; only FAKE.pidx exists in the store (no FAKE.pack, never committed).
  B  stored P2.pidx names base pack "../../s/packs/<P1>": a path into ANOTHER repository's cache on the publishing
     frontend. Nothing else is planted.
In both, the push is acknowledged, the publishing frontend serves it (from its own local index), and a cold frontend
cannot serve the ref. Exit 1 + DEFECT REPRODUCED if so."""
import os, struct, hashlib
from rlib import *

R = Report("R20 F2: 412 accepts an index whose external-base pack is unusable")
cl = Cluster(n=2, mode="tcp-s3")
fe0, fe1 = cl.fe
mi = cl.minio


def plant(key, data):
    f = os.path.join(cl.base, "plant.bin"); open(f, "wb").write(data)
    r = mi.curl("-X", "PUT", "--data-binary", "@" + f, "-o", "/dev/null", "-w", "%{http_code}", f"{mi.url}/pando/{key}")
    assert r.stdout.decode() == "200", r


def get(key):
    return mi.curl(f"{mi.url}/pando/{key}").stdout


def parse_pidx(b):
    assert b[:8] == b"PIDX\0\0\0\x01"
    p = 8
    def u64():
        nonlocal p; v = struct.unpack(">Q", b[p:p + 8])[0]; p += 8; return v
    def s():
        nonlocal p; n = u64(); v = b[p:p + n].decode(); p += n; return v
    idx = {"id_off": p}
    idx["id"] = s(); idx["after_id"] = p
    u64(); n = u64(); p += n * 82
    n = u64(); p += n * 20
    idx["ext_off"] = p
    n = u64(); ext = []
    for _ in range(n):
        o = b[p:p + 20]; p += 20; ext.append((o, s()))
    idx["ext"] = ext; idx["ext_end"] = p
    return idx


def put_str(x):
    return struct.pack(">Q", len(x)) + x.encode()


def resign(body):
    return body + hashlib.sha1(body).digest()


def with_ext_pack(b, newp):
    i = parse_pidx(b)
    ext = b"".join(o + put_str(newp) for o, _ in i["ext"])
    body = b[:i["ext_off"]] + struct.pack(">Q", len(i["ext"])) + ext + b[i["ext_end"]:-20]
    return resign(body)


def with_id(b, newid):
    i = parse_pidx(b)
    return resign(b[:i["id_off"]] + put_str(newid) + b[i["after_id"]:-20])


try:
    for n in ("s", "ta", "tb", "ref"):
        fe0.create(n)
    w = Work(cl.base, "w")
    big = "".join(f"line {i} of a reasonably long file so git makes a delta\n" for i in range(400))
    open(os.path.join(w.dir, "big.txt"), "w").write(big)
    git(["add", "big.txt"], cwd=w.dir); git(["commit", "-qm", "c1"], cwd=w.dir)
    c1 = w.head()
    open(os.path.join(w.dir, "big.txt"), "w").write(big + "one more line\n")
    git(["add", "big.txt"], cwd=w.dir); git(["commit", "-qm", "c2"], cwd=w.dir)
    c2 = w.head()
    p1 = build_pack(w.dir, c1)
    p2 = git(["pack-objects", "--revs", "--thin", "--stdout", "-q"], cwd=w.dir, inp=f"{c2}\n^{c1}\n".encode()).stdout
    P1, P2 = p1[-20:].hex(), p2[-20:].hex()
    for repo in ("s", "ta", "tb", "ref"):
        un, res = raw_receive(fe0.url(repo), [(ZERO, c1, "refs/heads/main")], p1)
        assert res.get("refs/heads/main") == "ok", (repo, un, res)
    # the genuine index of the thin pack P2 (pushed to a reference repository through the same frontend)
    un, res = raw_receive(fe0.url("ref"), [(c1, c2, "refs/heads/main")], p2)
    assert res.get("refs/heads/main") == "ok", (un, res)
    good = get(f"repos/ref/packs/{P2}.pidx")
    gi = parse_pidx(good)
    R.note(f"P1={P1} P2={P2}; genuine P2 index names external bases {[(o.hex()[:8], p[:8]) for o, p in gi['ext']]}")
    assert gi["ext"], "the thin pack has no external base; test cannot run"

    FAKE = "f" * 40
    variants = {
        "ta": (with_ext_pack(good, FAKE), [(f"repos/ta/packs/{FAKE}.pidx", with_id(get(f"repos/s/packs/{P1}.pidx"), FAKE))]),
        "tb": (with_ext_pack(good, f"../../s/packs/{P1}"), []),
    }
    for repo, (pidx, extra) in variants.items():
        plant(f"repos/{repo}/packs/{P2}.pidx", pidx)
        for k, v in extra:
            plant(k, v)
        un, res = raw_receive(fe0.url(repo), [(c1, c2, "refs/heads/main")], p2)
        acked = res.get("refs/heads/main") == "ok"
        R.note(f"{repo}: push of thin P2 via fe0 -> {un!r} {res}")
        d0 = os.path.join(cl.base, f"c0-{repo}"); d1 = os.path.join(cl.base, f"c1-{repo}")
        a = git(["clone", "-q", fe0.url(repo), d0], check=False)
        b = git(["clone", "-q", fe1.url(repo), d1], check=False)
        R.note(f"{repo}: full clone via fe0 rc={a.returncode}; full clone via cold fe1 rc={b.returncode} "
               f"(full clones splice the thin pack raw; the client resolves the base)")
        # a shallow clone makes the server resolve the delta itself (shallow.rs -> read_object -> external base)
        sa = git(["clone", "-q", "--depth", "1", fe0.url(repo), d0 + "-sh"], check=False)
        sb = git(["clone", "-q", "--depth", "1", fe1.url(repo), d1 + "-sh"], check=False)
        R.note(f"{repo}: shallow clone via fe0 rc={sa.returncode}; via cold fe1 rc={sb.returncode} "
               f"err={sb.stderr.decode(errors='replace').strip()[-300:]!r}")
        R.check(not (acked and (b.returncode != 0 or sb.returncode != 0)),
                f"{repo}: a push is acknowledged only if every frontend can serve it")
finally:
    if not os.environ.get("KEEP"): cl.cleanup()
    else: print("kept", cl.base)
R.done()
