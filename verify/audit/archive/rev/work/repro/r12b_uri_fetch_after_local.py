"""R12b: the builders' P1Copies reader sequence (verify/p1/test_p1.py:124-136): reader gets the parent by a local fetch
from the writer, then fetches main from the server with packfile URIs; fsck. Shows which packs fsck rejects."""
import os, tempfile, subprocess
from rlib import *
R = Report("R12b URI fetch after local fetch")
s = Server(); base = tempfile.mkdtemp(prefix="r12b-")
try:
    s.start(); s.create("r"); url = s.url("r"); w = Work(base, "w")
    import random; rnd = random.Random(7)
    lines = [f"{i:06d} " + "".join(rnd.choice("abcdefghij") for _ in range(56)) + "\n" for i in range(4096)]
    for v in range(4):
        lines[100 * v + 3] = f"edit {v}\n"; open(os.path.join(w.dir, "big.txt"), "w").writelines(lines)
        w.git("add", "-A"); w.git("commit", "-qm", f"v{v}"); w.push(url, "main")
    s.maintain(); tip = w.head(); parent = w.git("rev-parse", tip + "~1").stdout.decode().strip()
    d = os.path.join(base, "reader"); git(["init", "-q", "-b", "main", d])
    w.git("config", "uploadpack.allowAnySHA1InWant", "true")
    git(["fetch", "-q", w.dir, parent], cwd=d); git(["reset", "-q", "--hard", parent], cwd=d)
    p = git(["-c", "fetch.uriprotocols=http", "-c", "protocol.version=2", "fetch", "-q", url, "main"], cwd=d, check=False)
    R.note(f"uri fetch rc={p.returncode} {p.stderr.decode(errors='replace')[-300:]!r}")
    pd = os.path.join(d, ".git", "objects", "pack"); R.note("pack dir: " + " ".join(sorted(os.listdir(pd))))
    ok, err = fsck_ok(d); R.check(ok, "fsck after local fetch + URI fetch: " + err[-400:])
    for f in sorted(os.listdir(pd)):
        if f.endswith(".pack"):
            v = git(["verify-pack", os.path.join(pd, f[:-5] + ".idx")], cwd=d, check=False)
            R.note(f"verify-pack {f[:14]}: rc={v.returncode} {v.stderr.decode(errors='replace')[-150:]!r}")
finally:
    s.cleanup()
R.done()
