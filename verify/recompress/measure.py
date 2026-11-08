#!/usr/bin/env python3
"""Recompression measurement (docs/RECOMPRESSION-ACCEPTANCE.md, criteria 3 and 6). Same harness before and after.

Workload R: one ~256 KiB text file, one line edited per version, V versions pushed by stock git (thin packs, as a
real client sends them), then `maintain`. Records:
  - stored bytes: L0 (pushed) packs, and covers per level (from `cover_committed` events)
  - maintain wall time and server CPU delta
  - clone (stock git; inline and packfile-URI): bytes out, foreground metadata reads (+ breakdown), server CPU
  - raw v2 fetches at several deltas, inline and URI: plan reads, request reads, bytes, CPU
Usage: verify/recompress/run.sh OUT.json [V]
"""
import json, os, sys, time, tempfile, shutil, http.client, random
from harness.lib import Server, Work, git, fsck_ok

OUT = sys.argv[1]; V = int(sys.argv[2]) if len(sys.argv) > 2 else 300
DELTAS = [4, 16, 64, 256]


def pkt(s):
    b = s.encode(); return b"%04x" % (len(b) + 4) + b


def raw_fetch(port, want, have, uris):
    body = pkt("command=fetch\n") + pkt("object-format=sha1\n") + b"0001" + pkt("ofs-delta\n") + pkt("no-progress\n")
    if uris:
        body += pkt("packfile-uris http\n")
    body += pkt(f"want {want}\n") + pkt(f"have {have}\n") + pkt("done\n") + b"0000"
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=600)
    c.request("POST", "/r.git/git-upload-pack", body, {"Git-Protocol": "version=2", "Content-Type": "application/x-git-upload-pack-request"})
    n = len(c.getresponse().read()); c.close(); return n


def events(data):
    p = os.path.join(data, "events", "events.jsonl")
    if not os.path.exists(p):
        p = os.path.join(data, "events.jsonl")
    with open(p) as f:
        return [json.loads(l) for l in f if l.strip()]


def ev_data(e):
    return e.get("data", e)


def snap(srv):
    s = srv.stats(); b = s.get("metadata_read_breakdown", {})
    return {"reads": s["metadata_reads"], "bytes_out": s["bytes_out"], "cpu": s["cpu_seconds"],
            "alt_map_lookups": s.get("alt_map_lookups", {}).get("foreground", 0),
            **{k: v["foreground"] for k, v in b.items()}}


def diff(a, b):
    return {k: round(b[k] - a[k], 4) if isinstance(b[k], float) else b[k] - a[k] for k in b}


def main():
    rnd = random.Random(7)
    srv = Server(); srv.start(); srv.create("r"); url = srv.url("r")
    tmp = tempfile.mkdtemp(prefix="recomp-")
    w = Work(tmp, "w")
    lines = [f"{i:06d} " + "".join(rnd.choice("abcdefghij") for _ in range(56)) + "\n" for i in range(4096)]  # ~256 KiB
    tips = []
    st_pre = srv.stats(); t0 = time.time()
    for v in range(V):
        lines[rnd.randrange(len(lines))] = f"edit {v} " + "".join(rnd.choice("klmnopqrst") for _ in range(50)) + "\n"
        with open(os.path.join(w.dir, "big.txt"), "w") as f:
            f.writelines(lines)
        w.git("add", "-A"); w.git("commit", "-q", "-m", f"v{v}")
        w.git("push", "-q", url, "main"); tips.append(w.head())
    push_s = time.time() - t0
    srv.maintain()  # compaction also runs in the background during pushes; this waits for it to finish
    st_post = srv.stats()
    whole = {"cpu_seconds": round(st_post["cpu_seconds"] - st_pre["cpu_seconds"], 3),
             "reads_background": st_post.get("metadata_reads_background", 0) - st_pre.get("metadata_reads_background", 0),
             "writes_background": st_post.get("metadata_writes_background", 0) - st_pre.get("metadata_writes_background", 0)}
    s0 = snap(srv); m0 = time.time()
    srv.maintain()
    m_wall = time.time() - m0; s1 = snap(srv)
    ev = events(srv.data)
    covers = [ev_data(e) for e in ev if e.get("kind", e.get("event")) == "cover_committed" or "cover_committed" in json.dumps(e)[:80]]
    covers = [c for c in covers if "level" in c]
    per_level = {}
    for c in covers:
        L = per_level.setdefault(str(c["level"]), {"covers": 0, "bytes": 0, "objects": 0})
        L["covers"] += 1; L["bytes"] += c["bytes"]; L["objects"] += c["objects"]
    l0 = 0; alt_bytes = 0
    for root, _, fs in os.walk(srv.data):
        for f in fs:
            if f.endswith(".pack"):
                l0 += os.path.getsize(os.path.join(root, f))
            elif f.endswith(".alt"):
                alt_bytes += os.path.getsize(os.path.join(root, f))
    stored_total = l0
    l0 -= sum(L["bytes"] for L in per_level.values())
    clones = {}
    for name, extra in (("inline", ()), ("uris", ("-c", "fetch.uriprotocols=http"))):
        d = os.path.join(tmp, f"c-{name}"); a = snap(srv); t = time.time()
        git([*extra, "clone", "-q", url, d]); wall = time.time() - t; b = snap(srv)
        ok, err = fsck_ok(d)
        clones[name] = {"wall_s": round(wall, 3), "fsck_ok": ok, **diff(a, b)}
        shutil.rmtree(d, ignore_errors=True)
    fetches = []
    for dl in [d for d in DELTAS if d < len(tips)]:
        for uris in (False, True):
            a = snap(srv); n = raw_fetch(srv.port, tips[-1], tips[-1 - dl], uris); b = snap(srv)
            fe = [ev_data(e) for e in events(srv.data) if "fetch_served" in json.dumps(e)[:80]][-1]
            fetches.append({"delta": dl, "uris": uris, "response_bytes": n, "plan_reads": fe.get("plan_metadata_reads"),
                            "packs": len(fe.get("packs", [])), **diff(a, b)})
    out = {"workload": "R", "V": V, "file_bytes": sum(len(l) for l in lines), "push_seconds": round(push_s, 1), "push_and_compaction": whole,
           "maintain": {"wall_s": round(m_wall, 3), **diff(s0, s1)},
           "stored_bytes": {"alt_sidecars": alt_bytes, "l0_packs": l0, "covers_per_level": per_level, "all_pack_files": stored_total},
           "clones": clones, "fetches": fetches, "server": os.environ.get("G_RUN_SH")}
    import subprocess
    out["server_rss_kb"] = int(subprocess.run(["ps", "-o", "rss=", "-p", str(srv.pid())], capture_output=True, text=True).stdout.strip() or 0)
    json.dump(out, open(OUT, "w"), indent=1)
    print(json.dumps({"stored": out["stored_bytes"], "maintain": out["maintain"], "clones": clones}, indent=1))
    for f in fetches:
        print(json.dumps(f))
    srv.cleanup(); shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
