"""R12: diagnose the builders' P1Copies failure here: a fresh reader fetching with packfile URIs (stock git 2.55),
then fsck. Single-process server. Lists the reader's pack directory so the cause (server pack vs client files) is visible."""
import os, tempfile
from rlib import *

R = Report("R12 packfile-URI fetch + fsck")
s = Server(); base = tempfile.mkdtemp(prefix="r12-")
try:
    s.start(); s.create("r"); url = s.url("r")
    w = Work(base, "w")
    lines = [f"line {i} " + "x" * 60 + "\n" for i in range(3000)]
    for v in range(3):
        lines[v * 7] = f"changed {v}\n"
        open(os.path.join(w.dir, "big.txt"), "w").write("".join(lines)); w.git("add", "-A"); w.git("commit", "-qm", f"v{v}")
        w.push(url, "main")
    s.maintain()
    d = os.path.join(base, "reader")
    p = git(["-c", "fetch.uriprotocols=http", "-c", "protocol.version=2", "clone", "-q", url, d], check=False)
    R.note(f"uri clone rc={p.returncode} {p.stderr.decode(errors='replace')[-300:]!r}")
    pd = os.path.join(d, ".git", "objects", "pack")
    if os.path.isdir(pd):
        R.note("reader pack dir: " + ", ".join(f"{f}({oct(os.stat(os.path.join(pd, f)).st_mode & 0o777)})" for f in sorted(os.listdir(pd))))
    ok, err = fsck_ok(d) if p.returncode == 0 else (False, "clone failed")
    R.check(ok, "fsck after packfile-URI clone " + err)
    d2 = os.path.join(base, "reader2"); p2 = git(["clone", "-q", url, d2], check=False)
    ok2, err2 = fsck_ok(d2); R.check(p2.returncode == 0 and ok2, "control: fsck after inline clone " + err2)
finally:
    s.cleanup()
R.done()
