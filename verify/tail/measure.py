#!/usr/bin/env python3
"""URI tail cost by pusher (stock thin pushes vs OpenAI-fork explicit-haves pushes). docs/URI-TAIL.md.
Usage: verify/tail/run.sh OUT.json  (env FORK_GIT = path to the OpenAI fork git wrapper)"""
import json, os, random, sys, tempfile, time
from harness.lib import Server, Work, git, fsck_ok

OUT = sys.argv[1]; V = 40; STATES = [1, 2, 3, 4, 8, 16]
FORK = os.environ["FORK_GIT"]

def events(data):
    p = os.path.join(data, "events", "events.jsonl")
    if not os.path.exists(p): p = os.path.join(data, "events.jsonl")
    return [json.loads(l) for l in open(p)]

def run(pusher):
    rnd = random.Random(7); srv = Server(); srv.start(); srv.create("r"); url = srv.url("r")
    tmp = tempfile.mkdtemp(); w = Work(tmp, "w", FORK if pusher == "fork" else None); tips = []
    lines = [f"{i:06d} " + "".join(rnd.choice("abcdefghij") for _ in range(56)) + "\n" for i in range(4096)]
    t0 = time.time()
    for v in range(V):
        lines[rnd.randrange(len(lines))] = f"edit {v} " + "".join(rnd.choice("klmnopqrst") for _ in range(50)) + "\n"
        open(os.path.join(w.dir, "big.txt"), "w").writelines(lines); w.git("add", "-A"); w.git("commit", "-q", "-m", f"v{v}")
        w.git("push", "-q", url, "main"); tips.append(w.head())
    push_s = time.time() - t0
    srv.maintain()
    l0 = [e.get("data", e) for e in events(srv.data) if '"pack_stored"' in json.dumps(e)[:40] or "pack_ingested" in json.dumps(e)[:60]]
    l0_bytes = sum(d.get("bytes", 0) for d in l0 if "bytes" in d)
    rows = []
    for st in STATES:
        # Reader holds ONLY the history up to tips[-1-st]: fetch that commit from the writer's repo into an empty repo
        # (an earlier version cloned the writer and so already held every object: fetches moved nothing).
        rd = os.path.join(tmp, f"r{st}"); git(["init", "-q", "-b", "main", rd])
        w.git("config", "uploadpack.allowAnySHA1InWant", "true")
        git(["fetch", "-q", w.dir, tips[-1 - st]], cwd=rd); git(["reset", "-q", "--hard", tips[-1 - st]], cwd=rd)
        git(["remote", "add", "origin", url], cwd=rd)
        assert git(["cat-file", "-e", tips[-1]], cwd=rd, check=False).returncode != 0, "reader already holds the tip"
        n0 = len(events(srv.data)); s0 = srv.stats(); t = time.time()
        git(["-c", "fetch.uriprotocols=http", "fetch", "-q", "origin", "main"], cwd=rd); wall = time.time() - t; s1 = srv.stats()
        ev = [e.get("data", e) for e in events(srv.data)[n0:]]
        twins = [d for d in ev if "twin" in d and "orig_bytes" in d]
        fs = [d for d in ev if "uri_packs" in d]
        git(["merge", "-q", "--ff-only", "FETCH_HEAD"], cwd=rd); ok, err = fsck_ok(rd)
        assert git(["rev-parse", "HEAD"], cwd=rd).stdout.decode().strip() == tips[-1], "reader did not reach the tip"
        rows.append({"behind": st, "cache_bytes": s1["bytes_out_cache"] - s0["bytes_out_cache"], "uri_packs": len(fs[-1]["uri_packs"]) if fs else None,
                     "twins_built": len(twins), "twin_bytes": sum(d["bytes"] for d in twins), "reads": s1["metadata_reads"] - s0["metadata_reads"],
                     "writes": s1["metadata_writes"] - s0["metadata_writes"], "wall_s": round(wall, 3), "fsck_ok": ok})
        print(pusher, json.dumps(rows[-1]), flush=True)
    srv.cleanup()
    return {"push_seconds": round(push_s, 2), "l0_events_bytes": l0_bytes, "rows": rows}

out = {p: run(p) for p in ("stock", "fork")}
json.dump(out, open(OUT, "w"), indent=1)
