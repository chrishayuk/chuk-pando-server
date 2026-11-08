#!/usr/bin/env python3
"""Registered measurements (docs/NETWORK-STORAGE.md): push latency/throughput in three deployment modes, TCP metadata
round trip, cold-cache clone. One host; comparison only. Usage: verify/net/measure_run.sh OUT.json"""
import json, os, socket, statistics, sys, tempfile, threading, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dist"))
from distlib import Cluster
from harness.lib import Work, git

OUT = sys.argv[1]; M = 10; res = {"server": os.environ.get("G_RUN_SH")}


def writers(fes, n, tmp):
    fes[0].create("r"); seed = Work(tmp, "seed"); seed.commit("f.txt", "base\n"); seed.push(fes[0].url("r"), "main")
    lat = []; lk = threading.Lock(); errs = []
    def run(k):
        try:
            w = Work(tmp, f"w{k}"); w.git("pull", "-q", fes[0].url("r"), "main")
            for j in range(M):
                w.commit(f"b{k}.txt", f"{k} {j} " + "x" * 3000 + "\n")
                t = time.time(); w.push(fes[(k + j) % len(fes)].url("r"), f"HEAD:refs/heads/b{k}")
                with lk: lat.append(time.time() - t)
        except Exception as e: errs.append(repr(e))
    t0 = time.time(); ts = [threading.Thread(target=run, args=(k,)) for k in range(n)]; [t.start() for t in ts]; [t.join() for t in ts]
    wall = time.time() - t0; assert not errs, errs; lat.sort()
    return {"writers": n, "pushes": n * M, "pushes_per_s": round(n * M / wall, 1),
            "latency_ms": {"median": round(statistics.median(lat) * 1e3, 1), "p95": round(lat[int(0.95 * len(lat)) - 1] * 1e3, 1)}}


def rtt_tcp(port):
    s = socket.create_connection(("127.0.0.1", port)); s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1); n = 2000; t = time.time()
    for _ in range(n):
        s.sendall(b"\x00\x00\x00\x01P"); got = b""
        while len(got) < 5: got += s.recv(5 - len(got))
    s.close(); return round((time.time() - t) / n * 1e6, 1)


for mode in ("unix-fs", "tcp-fs", "tcp-s3"):
    r = {}
    for n in (8, 16):
        c = Cluster(2, mode=mode); tmp = tempfile.mkdtemp()
        try: r[f"writers_{n}"] = writers(c.fe, n, tmp)
        finally: c.cleanup()
        print(mode, n, json.dumps(r[f"writers_{n}"]), flush=True)
    if mode.startswith("tcp"):
        c = Cluster(1, mode=mode)
        try: r["metadata_rpc_rtt_us"] = rtt_tcp(c.meta.tcp_port)
        finally: c.cleanup()
    res[mode] = r
c = Cluster(1, mode="tcp-s3"); tmp = tempfile.mkdtemp()
try:
    a = c.fe[0]; a.create("r"); w = Work(tmp, "w")
    for i in range(50):
        w.commit(f"d{i % 8}.txt", f"{i} " + "q" * 4000 + "\n"); w.push(a.url("r"), "main")
    a.maintain(); cold = c.add_frontend(); s0 = cold.stats()["object_store"]["get"]["foreground"]
    t = time.time(); git(["-c", "fetch.uriprotocols=http", "clone", "-q", cold.url("r"), os.path.join(tmp, "cold")]); cw = time.time() - t
    s1 = cold.stats()["object_store"]["get"]["foreground"]
    t = time.time(); git(["-c", "fetch.uriprotocols=http", "clone", "-q", cold.url("r"), os.path.join(tmp, "warm")]); ww = time.time() - t
    res["cold_cache_clone"] = {"versions": 50, "cold_s": round(cw, 3), "warm_s": round(ww, 3), "object_gets": s1 - s0}
    print("cold", json.dumps(res["cold_cache_clone"]), flush=True)
finally: c.cleanup()
json.dump(res, open(OUT, "w"), indent=1)
