#!/usr/bin/env python3
"""Tracing demonstration (docs/TRACING.md): tcp-s3 deployment, two frontends.
1. version 1 through A; version 2 through B (a thin push: B's object index is built now); versions 3-4 through A (A's background compaction builds the
   L1 cover of versions 1-4);
2. a thin push of version 5 through B (B discovers A's packs incrementally through the change log);
3. a packfile-URI clone through B (plan: cover + version 5).
Usage: verify/trace/demo_run.sh OUTDIR"""
import os, shutil, subprocess, sys, tempfile, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dist"))
from distlib import Cluster
from harness.lib import Work, git, fsck_ok

out = os.path.abspath(sys.argv[1]); os.makedirs(out, exist_ok=True)
c = Cluster(2, mode=os.environ.get("G_DIST_MODE", "tcp-s3")); a, b = c.fe; tmp = tempfile.mkdtemp()
try:
    a.create("demo"); w = Work(tmp, "w")
    lines = [f"{i:04d} the quick brown fox jumps over the lazy dog\n" for i in range(800)]
    for v in range(1, 6):
        lines[v * 37] = f"edit for version {v}\n"
        open(os.path.join(w.dir, "story.txt"), "w").writelines(lines); w.git("add", "-A"); w.git("commit", "-q", "-m", f"version {v}")
        w.push((b if v in (2, 5) else a).url("demo"), "main")   # B's thin v2 builds its index; at v5 it must discover A's work
        if v == 4:
            deadline = time.time() + 20       # wait for A's background compaction of versions 1-4 (not forced)
            while time.time() < deadline and '"cover_committed"' not in open(os.path.join(a.data, "events", "events.jsonl")).read():
                time.sleep(0.05)
    d = os.path.join(tmp, "reader"); git(["-c", "fetch.uriprotocols=http", "clone", "-q", b.url("demo"), d])
    ok, err = fsck_ok(d); assert ok, err
    for n, s in (("A", a), ("B", b)):
        shutil.copy(os.path.join(s.data, "events", "events.jsonl"), os.path.join(out, f"events-{n}.jsonl"))
    r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "timeline.py"), os.path.join(out, "timeline.json"),
                        f"A={a.data}", f"B={b.data}"], capture_output=True, text=True)
    open(os.path.join(out, "timeline.txt"), "w").write(r.stdout + r.stderr); print(r.stdout + r.stderr)
finally:
    c.cleanup(); shutil.rmtree(tmp, ignore_errors=True)
