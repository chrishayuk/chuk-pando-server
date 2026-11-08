#!/usr/bin/env python3
"""Distributed milestone 1 measurements (docs/DISTRIBUTED.md, registered). Usage: verify/dist/measure_run.sh OUT.json
Configurations: single (one process, embedded store; the baseline), dist1 (metadata service + 1 frontend),
dist2 (metadata service + 2 frontends, writers alternate). Writers push real commits with stock git to their own branch."""
import json, os, socket, statistics, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from distlib import Cluster
from harness.lib import Server, Work, git, raw_receive, build_pack, ZERO

OUT = sys.argv[1]; M = 10


def ev(srv, kind):
    p = os.path.join(srv.data, "events", "events.jsonl")
    out = []
    for l in open(p):
        try: e = json.loads(l)
        except ValueError: continue
        e = e.get("data", e)
        if e.get("ev") == kind: out.append(e)
    return out


def deploy(kind):
    if kind == "single":
        s = Server(); s.start(); return [s], (lambda: s.cleanup()), None
    c = Cluster(1 if kind == "dist1" else 2); return c.fe, c.cleanup, c


def writers(fes, n, tmp):
    fes[0].create("r"); seed = Work(tmp, "seed"); seed.commit("f.txt", "base\n"); seed.push(fes[0].url("r"), "main")
    lat = []; lock = threading.Lock(); errs = []
    def run(k):
        try:
            w = Work(tmp, f"w{k}"); w.git("pull", "-q", fes[0].url("r"), "main")
            for j in range(M):
                w.commit(f"b{k}.txt", f"{k} {j} " + "x" * 3000 + "\n")
                t = time.time(); w.push(fes[(k + j) % len(fes)].url("r"), f"HEAD:refs/heads/b{k}")
                with lock: lat.append(time.time() - t)
        except Exception as e: errs.append(repr(e))
    s0 = [f.stats() for f in fes]; t0 = time.time()
    ts = [threading.Thread(target=run, args=(k,)) for k in range(n)]; [t.start() for t in ts]; [t.join() for t in ts]
    wall = time.time() - t0; s1 = [f.stats() for f in fes]
    assert not errs, errs
    d = lambda k: sum(b[k] - a[k] for a, b in zip(s0, s1))
    pushes = n * M
    lat.sort()
    return {"writers": n, "pushes": pushes, "wall_s": round(wall, 2), "pushes_per_s": round(pushes / wall, 1),
            "latency_ms": {"median": round(statistics.median(lat) * 1e3, 1), "p95": round(lat[int(0.95 * len(lat)) - 1] * 1e3, 1)},
            "per_push": {"metadata_reads": round(d("metadata_reads") / pushes, 1), "metadata_writes": round(d("metadata_writes") / pushes, 2),
                         "store_read_ops": round(d("store_read_ops") / pushes, 2), "store_write_ops": round(d("store_write_ops") / pushes, 2)},
            "cpu_s_frontends": round(d("cpu_seconds"), 3)}


def contention(fes, tmp):
    fes[0].create("c"); w = Work(tmp, "cw"); tip = w.commit("f.txt", "b\n"); w.push(fes[0].url("c"), "main")
    wins = fails = 0
    for rnd in range(10):
        cands = [w.git("commit-tree", f"{tip}^{{tree}}", "-p", tip, "-m", f"{rnd} {k}").stdout.decode().strip() for k in range(8)]
        packs = [build_pack(w.dir, c, tip) for c in cands]; res = [None] * 8; bar = threading.Barrier(8)
        def run(k):
            bar.wait(); res[k] = raw_receive(fes[k % len(fes)].url("c"), [(tip, cands[k], "refs/heads/main")], packs[k])[1].get("refs/heads/main")
        ts = [threading.Thread(target=run, args=(k,)) for k in range(8)]; [t.start() for t in ts]; [t.join() for t in ts]
        ok = [k for k in range(8) if res[k] == "ok"]; wins += len(ok); fails += 8 - len(ok); tip = cands[ok[0]]
    return {"rounds": 10, "attempts": 80, "successes": wins, "cas_failures": fails}


def rtt(sock):
    s = socket.socket(socket.AF_UNIX); s.connect(sock); n = 2000; t = time.time()
    for _ in range(n):
        s.sendall(b"\x00\x00\x00\x01P"); got = b""
        while len(got) < 5: got += s.recv(5 - len(got))
    s.close(); return round((time.time() - t) / n * 1e6, 1)


def background(c, tmp):
    """Compaction and copies triggered on both frontends for one repository: duplicates and orphan blobs."""
    a, b = c.fe; a.create("bg"); w = Work(tmp, "bgw")
    lines = [f"{i:05d} " + "abcdefghij" * 5 + "\n" for i in range(3000)]
    for v in range(32):
        lines[v * 13] = f"edit {v}\n"; open(os.path.join(w.dir, "big.txt"), "w").writelines(lines)
        w.git("add", "-A"); w.git("commit", "-q", "-m", f"v{v}"); w.push((a if v % 2 else b).url("bg"), "main")
    ta = threading.Thread(target=a.maintain); tb = threading.Thread(target=b.maintain); ta.start(); tb.start(); ta.join(); tb.join()
    committed = {e["pack"] for f in c.fe for e in ev(f, "cover_committed") if e.get("repo") == "bg"}
    twins = {e["twin"] for f in c.fe for e in ev(f, "twin_committed") if e.get("repo") == "bg"}
    pushed = {e["pack"] for f in c.fe for e in ev(f, "pack_stored") if e.get("repo") == "bg"}
    from harness.lib import Server as _S
    pdir = [os.path.join(r, "") for r, ds, fs in os.walk(c.blobs) if r.endswith("packs") and "bg" in r]
    files = {f[:-5] for d in pdir for f in os.listdir(d) if f.endswith(".pack")}
    refreshes = [e for f in c.fe for e in ev(f, "object_index_built") if e.get("refresh")]
    return {"covers_committed": len(committed), "copies_committed": len(twins), "pushed_packs": len(pushed), "pack_files": len(files),
            "orphan_pack_files": len(files - committed - twins - pushed),
            "cover_builds_attempted": sum(1 for f in c.fe for e in ev(f, "cover_recompress") if e.get("repo") == "bg") or None,
            "index_refreshes": len(refreshes), "index_refresh_ms_total": round(sum(e["ms"] for e in refreshes), 2)}


def main():
    res = {"server": os.environ.get("G_RUN_SH"), "M_pushes_per_writer": M}
    for kind in ("single", "dist1", "dist2"):
        r = {}
        for n in (8, 16):
            fes, cleanup, c = deploy(kind); tmp = tempfile.mkdtemp()
            try: r[f"writers_{n}"] = writers(fes, n, tmp)
            finally: cleanup()
            print(kind, n, json.dumps(r[f"writers_{n}"]), flush=True)
        fes, cleanup, c = deploy(kind); tmp = tempfile.mkdtemp()
        try:
            r["same_branch_contention"] = contention(fes, tmp)
            if c: r["metadata_rpc_rtt_us"] = rtt(c.meta.sock)
            if kind == "dist2": r["background_duplicates"] = background(c, tmp)
        finally: cleanup()
        print(kind, json.dumps({k: v for k, v in r.items() if not k.startswith("writers")}), flush=True)
        res[kind] = r
    json.dump(res, open(OUT, "w"), indent=1)


if __name__ == "__main__":
    main()
