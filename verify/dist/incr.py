#!/usr/bin/env python3
"""I2 bounded refresh work (docs/INCREMENTAL-INDEX.md). Usage: SERVER_DIR=... verify/dist/incr_run.sh OUT.json
For P in 64, 256, 1024: P pushes through A; B performs one thin push (its first index build, not counted); one more
push through A; then a thin push on top of it through B, which misses A's newest pack and refreshes. Reported: B's
foreground store keys scanned and store read ops during that last push, and its object_index_built event if any."""
import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from distlib import Cluster
from harness.lib import Work, raw_receive, build_pack, ZERO

OUT = sys.argv[1]; res = {"server": os.environ.get("G_RUN_SH"), "rows": []}


def fg(s, k): return s["metadata_read_breakdown"][k]["foreground"]


for P in (64, 256, 1024):
    c = Cluster(2); a, b = c.fe
    try:
        a.create("r"); w = Work(tempfile.mkdtemp(), "w"); prev = None
        def push(srv, n=1):
            global prev
            for _ in range(n):
                cm = w.commit("f.txt", "line\n")
                _, r = raw_receive(srv.url("r"), [(prev or ZERO, cm, "refs/heads/main")], build_pack(w.dir, cm, prev))
                assert r.get("refs/heads/main") == "ok", r
                prev = cm
        push(a, P); push(b); push(a)
        s0 = b.stats(); push(b); s1 = b.stats()
        p = os.path.join(b.data, "events", "events.jsonl")
        ib = [json.loads(l) for l in open(p) if "object_index_built" in l]
        ib = [e.get("data", e) for e in ib]
        row = {"P": P, "store_scan_keys": fg(s1, "store_scan_keys") - fg(s0, "store_scan_keys"),
               "store_point_reads": fg(s1, "store_point_reads") - fg(s0, "store_point_reads"),
               "store_read_ops": s1["store_read_ops"] - s0["store_read_ops"],
               "last_index_event": {k: ib[-1].get(k) for k in ("refresh", "incremental", "keys_read", "packs", "ms")} if ib else None}
        print(json.dumps(row), flush=True); res["rows"].append(row)
    finally:
        c.cleanup()
json.dump(res, open(OUT, "w"), indent=1)
