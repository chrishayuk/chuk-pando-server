#!/usr/bin/env python3
"""Per-push breakdown, tcp-s3 vs tcp-fs (docs/S3-PROFILE.md). Usage: verify/net/profile_run.sh OUT.json"""
import json, os, statistics, sys, tempfile, threading, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dist"))
from distlib import Cluster
from harness.lib import Work

OUT = sys.argv[1]; M = 10; res = {"server": os.environ.get("G_RUN_SH")}


def evs(srv):
    out = []
    for l in open(os.path.join(srv.data, "events", "events.jsonl")):
        try: e = json.loads(l)
        except ValueError: continue
        out.append(e.get("data", e))
    return out


def med(xs): xs = sorted(xs); return round(statistics.median(xs), 2) if xs else None
def p95(xs): xs = sorted(xs); return round(xs[int(0.95 * len(xs)) - 1], 2) if xs else None


def run(mode, n):
    c = Cluster(2, mode=mode); tmp = tempfile.mkdtemp()
    try:
        fes = c.fe; fes[0].create("r"); seed = Work(tmp, "seed"); seed.commit("f.txt", "base\n"); seed.push(fes[0].url("r"), "main")
        bg0 = [f.stats()["object_store"] for f in fes]
        lat = []; lk = threading.Lock()
        def wr(k):
            w = Work(tmp, f"w{k}"); w.git("pull", "-q", fes[0].url("r"), "main")
            for j in range(M):
                w.commit(f"b{k}.txt", f"{k} {j} " + "x" * 3000 + "\n"); t = time.time()
                w.push(fes[(k + j) % 2].url("r"), f"HEAD:refs/heads/b{k}")
                with lk: lat.append((time.time() - t) * 1e3)
        t0 = time.time(); ts = [threading.Thread(target=wr, args=(k,)) for k in range(n)]; [t.start() for t in ts]; [t.join() for t in ts]
        wall = time.time() - t0
        bg1 = [f.stats()["object_store"] for f in fes]
        rows = []
        for f in fes:
            E = evs(f); by = {}
            for e in E:
                if e.get("push"): by.setdefault(e["push"], {})[e["ev"]] = e
            for pid, d in by.items():
                if "push_reported" not in d or "push_timing" not in d or "pack_ingested" not in d: continue
                r = d["push_reported"]; o = r.get("object_store") or {}
                rows.append({"server_ms": r["server_ms"], "ingest_ms": d["pack_ingested"].get("ingest_ms"), "install_ms": d.get("pack_stored", {}).get("install_ms"),
                             "core_push_ms": d["push_timing"].get("total_ms"), "txn_commit_ms": d["push_timing"].get("txn_commit_ms"),
                             "s3_requests": o.get("requests", 0), "s3_connect_ms": o.get("connect_ms", 0), "s3_hash_sign_ms": o.get("hash_sign_ms", 0),
                             "s3_wire_ms": o.get("wire_and_server_ms", 0), "s3_by_op": o.get("by_op", {}),
                             "cache_fills": o.get("cache_fills", 0), "cache_fill_ms": o.get("cache_fill_ms", 0), "verify_ms": o.get("verify_ms", 0),
                             "cache_write_fsync_ms": o.get("cache_write_fsync_ms", 0),
                             "s3_put_ms": o.get("by_op", {}).get("put", {}).get("ms", 0), "s3_get_ms": o.get("by_op", {}).get("get", {}).get("ms", 0)})
        keys = ["server_ms", "ingest_ms", "install_ms", "core_push_ms", "txn_commit_ms", "s3_requests", "s3_connect_ms", "s3_hash_sign_ms", "s3_wire_ms", "cache_fills", "cache_fill_ms", "verify_ms", "cache_write_fsync_ms", "s3_put_ms", "s3_get_ms"]
        summ = {k: {"median": med([r[k] for r in rows if r[k] is not None]), "p95": p95([r[k] for r in rows if r[k] is not None])} for k in keys}
        ops = {}
        for r in rows:
            for op, v in r["s3_by_op"].items():
                ops.setdefault(op, []).append(v["n"])
        summ["s3_ops_per_push_mean"] = {op: round(sum(v) / len(rows), 2) for op, v in ops.items()}
        bg = {k: sum(b[k]["background"] - a[k]["background"] for a, b in zip(bg0, bg1)) for k in bg0[0]}
        return {"mode": mode, "writers": n, "pushes": len(rows), "pushes_per_s": round(n * M / wall, 1),
                "client_latency_ms": {"median": med(lat), "p95": p95(lat)}, "server_side": summ, "background_object_store": bg}
    finally:
        c.cleanup()


for mode in os.environ.get("G_PROFILE_MODES", "tcp-fs,tcp-s3").split(","):
    for n in [int(x) for x in os.environ.get("G_PROFILE_WRITERS", "8,16").split(",")]:
        r = run(mode, n); res[f"{mode}_{n}"] = r; print(json.dumps(r), flush=True)
json.dump(res, open(OUT, "w"), indent=1)
