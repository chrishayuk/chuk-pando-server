#!/usr/bin/env python3
"""Background-copy preparation cost (docs/BACKGROUND-COPIES.md, measurements). Workload R reduced to 40 versions,
stock pushes, same content generator as measure.py. Runs against SERVER_DIR (before: main 712348e; after: p4-bgcopy).
Records per push: wall and foreground reads/writes; preparation latency (ref_version_committed -> twin_committed,
event ts_ms) and copy build time; background reads/writes; process CPU over pushes+preparation; storage of push packs
vs copies; then the tail reader states of measure.py, and which copies they used.
Usage: verify/tail/prep_run.sh OUT.json"""
import json, os, random, statistics, sys, tempfile, time
from harness.lib import Server, Work, git, fsck_ok

OUT = sys.argv[1]; V = 40; STATES = [1, 2, 3, 4, 8, 16]


def events(data):
    p = os.path.join(data, "events", "events.jsonl")
    if not os.path.exists(p): p = os.path.join(data, "events.jsonl")
    return [(lambda e: e.get("data", e))(json.loads(l)) for l in open(p) if l.strip()]


rnd = random.Random(7); srv = Server(); srv.start(); srv.create("r"); url = srv.url("r")
tmp = tempfile.mkdtemp(); w = Work(tmp, "w"); tips = []; pushes = []
lines = [f"{i:06d} " + "".join(rnd.choice("abcdefghij") for _ in range(56)) + "\n" for i in range(4096)]
s_start = srv.stats()
for v in range(V):
    lines[rnd.randrange(len(lines))] = f"edit {v} " + "".join(rnd.choice("klmnopqrst") for _ in range(50)) + "\n"
    open(os.path.join(w.dir, "big.txt"), "w").writelines(lines); w.git("add", "-A"); w.git("commit", "-q", "-m", f"v{v}")
    a = srv.stats(); t = time.time(); w.git("push", "-q", url, "main"); wall = time.time() - t; b = srv.stats()
    pushes.append({"wall_s": round(wall, 4), "reads": b["metadata_reads"] - a["metadata_reads"], "writes": b["metadata_writes"] - a["metadata_writes"]})
    tips.append(w.head())
t_pushed = time.time()
srv.maintain()   # waits for compaction and (after the change) any copies still queued
prep_wall_after_last_push = time.time() - t_pushed
s_end = srv.stats(); ev = events(srv.data)
committed = {e["pack"]: e["ts_ms"] for e in ev if e.get("ev") == "ref_version_committed"}
twins = [e for e in ev if e.get("ev") == "twin_committed"]
lat = [t["ts_ms"] - committed[t["pack"]] for t in twins if t["pack"] in committed]
ingested = {e["pack"]: e["bytes"] for e in ev if e.get("ev") == "pack_ingested"}
prep = {"copies_built": len(twins), "copy_bytes": sum(t["bytes"] for t in twins),
        "push_pack_bytes": sum(ingested.get(p, 0) for p in committed),
        "latency_ms": {"median": statistics.median(lat), "max": max(lat)} if lat else None,
        "build_ms": {"median": statistics.median([t.get("build_ms", 0) for t in twins]), "sum": sum(t.get("build_ms", 0) for t in twins)} if twins else None,
        "background_reads": s_end["metadata_reads_background"] - s_start["metadata_reads_background"],
        "background_writes": s_end["metadata_writes_background"] - s_start["metadata_writes_background"],
        "cpu_seconds_pushes_and_preparation": round(s_end["cpu_seconds"] - s_start["cpu_seconds"], 3),
        "wall_s_maintain_after_last_push": round(prep_wall_after_last_push, 3)}
rows = []; used = set(); twin_ids = {t["twin"] for t in twins}
w.git("config", "uploadpack.allowAnySHA1InWant", "true")
for st in STATES:
    rd = os.path.join(tmp, f"r{st}"); git(["init", "-q", "-b", "main", rd])
    git(["fetch", "-q", w.dir, tips[-1 - st]], cwd=rd); git(["reset", "-q", "--hard", tips[-1 - st]], cwd=rd)
    n0 = len(events(srv.data)); s0 = srv.stats(); t = time.time()
    git(["-c", "fetch.uriprotocols=http", "fetch", "-q", url, "main"], cwd=rd); wall = time.time() - t; s1 = srv.stats()
    new = events(srv.data)[n0:]; fs = [e for e in new if "plan_steps" in e][-1]
    git(["merge", "-q", "--ff-only", "FETCH_HEAD"], cwd=rd); ok, _ = fsck_ok(rd)
    assert git(["rev-parse", "HEAD"], cwd=rd).stdout.decode().strip() == tips[-1]
    built_now = [e for e in new if e.get("ev") == "twin_committed"]
    twin_ids |= {e["twin"] for e in built_now}; used |= set(fs["uri_packs"]) & twin_ids
    rows.append({"behind": st, "cache_bytes": s1["bytes_out_cache"] - s0["bytes_out_cache"], "uri_packs": len(fs["uri_packs"]),
                 "inline_fallback_packs": fs.get("inline_fallback_packs"), "copies_built_during_fetch": len(built_now),
                 "reads": s1["metadata_reads"] - s0["metadata_reads"], "writes": s1["metadata_writes"] - s0["metadata_writes"],
                 "cpu_s": round(s1["cpu_seconds"] - s0["cpu_seconds"], 4), "wall_s": round(wall, 3), "fsck_ok": ok})
    print(json.dumps(rows[-1]), flush=True)
s_fin = srv.stats()
out = {"server": os.environ.get("G_RUN_SH"), "pushes": {"n": V, "median_wall_s": statistics.median(p["wall_s"] for p in pushes),
       "reads_per_push_median": statistics.median(p["reads"] for p in pushes), "writes_per_push_median": statistics.median(p["writes"] for p in pushes),
       "rows": pushes}, "preparation": prep, "readers": rows,
       "copies_total_after_readers": len(twin_ids), "copies_used_by_these_readers": len(used),
       "cpu_seconds_total": round(s_fin["cpu_seconds"] - s_start["cpu_seconds"], 3)}
json.dump(out, open(OUT, "w"), indent=1)
print(json.dumps({k: v for k, v in out.items() if k not in ("readers",)} | {"pushes": {k: v for k, v in out["pushes"].items() if k != "rows"}}, indent=1))
srv.cleanup()
