#!/usr/bin/env python3
"""Large-push cost by push size (docs/LARGE-PUSH.md, registered). Usage: verify/largepush/run.sh OUT.json"""
import json, os, statistics, subprocess, sys, tempfile, time
from harness.lib import Server, git, GIT, ENVBASE

OUT = sys.argv[1]; SIZES = [1, 16, 256, 4096, 32768]; REPS = 3; FILES = 64
RESTART_EXTRA, RESTART_CHUNK = 63, 512
TOTAL = SIZES[-1] + RESTART_EXTRA * RESTART_CHUNK


def history(d, n):
    """Linear history: commit 0 creates 64 files of 20 lines; commit k edits one line of file k % 64."""
    git(["init", "-q", "-b", "main", d])
    files = {f: [f"file {f} line {i}\n" for i in range(20)] for f in range(FILES)}
    out = []
    def blob(f):
        b = "".join(files[f]).encode(); return b"data %d\n" % len(b) + b + b"\n"
    for k in range(n + 1):
        out.append(b"commit refs/heads/main\nmark :%d\ncommitter t <t@t> %d +0000\ndata 4\nc%03d\n" % (k + 1, 1700000000 + k, k % 1000))
        if k:
            out.append(b"from :%d\n" % k)
            f = k % FILES; files[f][k % 20] = f"edit {k}\n"
            out.append(b"M 100644 inline f%02d.txt\n" % f + blob(f))
        else:
            for f in range(FILES):
                out.append(b"M 100644 inline f%02d.txt\n" % f + blob(f))
    git(["fast-import", "--quiet"], cwd=d, inp=b"".join(out))
    return git(["rev-list", "--reverse", "main"], cwd=d).stdout.decode().split()


def events(data):
    p = os.path.join(data, "events", "events.jsonl")
    if not os.path.exists(p): p = os.path.join(data, "events.jsonl")
    return [(lambda e: e.get("data", e))(json.loads(l)) for l in open(p) if l.strip()]


def push(srv, src, url, commit):
    n0 = len(events(srv.data)); a = srv.stats(); t = time.time()
    git(["push", "-q", url, f"{commit}:refs/heads/main"], cwd=src, timeout=1800)
    wall = time.time() - t; b = srv.stats(); ev = events(srv.data)[n0:]
    pt = [e for e in ev if e.get("ev") == "push_timing"][-1]
    ing = [e for e in ev if e.get("ev") == "pack_ingested"]; st = [e for e in ev if e.get("ev") == "pack_stored"]
    ib = [e for e in ev if e.get("ev") == "object_index_built"]
    return {"client_wall_ms": round(wall * 1e3, 1), "objects": ing[-1]["objects"] if ing else 0, "pack_bytes": ing[-1]["bytes"] if ing else 0,
            "ingest_ms": ing[-1].get("ingest_ms") if ing else None, "install_ms": st[-1].get("install_ms") if st else None,
            **{k: v for k, v in pt.items() if k.endswith("_ms") or k == "link_commits_visited"},
            "index_rebuild": [{k: e[k] for k in ("packs", "objects", "ms")} for e in ib],
            "fg_reads": b["metadata_reads"] - a["metadata_reads"], "fg_writes": b["metadata_writes"] - a["metadata_writes"],
            "cpu_s": round(b["cpu_seconds"] - a["cpu_seconds"], 4)}


def rss(srv):
    return int(subprocess.run(["ps", "-o", "rss=", "-p", str(srv.pid())], capture_output=True, text=True).stdout.strip() or 0)


def main():
    tmp = tempfile.mkdtemp(prefix="lp-"); src = os.path.join(tmp, "src")
    t = time.time(); c = history(src, TOTAL); print(f"history {len(c)} commits in {time.time() - t:.1f}s", flush=True)
    srv = Server(); srv.start(); res = {"sizes": {}, "server": os.environ.get("G_RUN_SH")}
    for n in SIZES:
        rows = []
        for rep in range(REPS):
            name = f"r{n}_{rep}"; srv.create(name); url = srv.url(name)
            git(["push", "-q", url, f"{c[0]}:refs/heads/main"], cwd=src)   # base
            rows.append(push(srv, src, url, c[n])); print(n, json.dumps(rows[-1]), flush=True)
        med = {k: statistics.median([r[k] for r in rows]) for k in rows[0] if isinstance(rows[0][k], (int, float))}
        res["sizes"][str(n)] = {"median": med, "rows": rows, "rss_kb_after": rss(srv)}
    # restart: one repository holding the 32768-commit push plus 63 pushes of 512 commits
    srv.create("big"); url = srv.url("big")
    git(["push", "-q", url, f"{c[0]}:refs/heads/main"], cwd=src); push(srv, src, url, c[SIZES[-1]])
    for i in range(1, RESTART_EXTRA + 1):
        push(srv, src, url, c[SIZES[-1] + i * RESTART_CHUNK])
    srv.stop(); srv.start()
    # one small push of a new commit on top: needs a prerequisite lookup -> the in-memory index is rebuilt
    w = os.path.join(tmp, "w"); git(["clone", "-q", "--no-local", src, w])
    open(os.path.join(w, "new.txt"), "w").write("x\n"); git(["add", "-A"], cwd=w); git(["commit", "-q", "-m", "new"], cwd=w, env={"GIT_AUTHOR_NAME": "t"})
    first = push(srv, w, url, "HEAD")
    open(os.path.join(w, "new.txt"), "w").write("y\n"); git(["commit", "-qam", "new2"], cwd=w)
    second = push(srv, w, url, "HEAD")
    res["restart"] = {"repo_commits": TOTAL + 1, "first_push_after_restart": first, "second_push": second, "rss_kb": rss(srv)}
    print("restart", json.dumps(res["restart"]), flush=True)
    json.dump(res, open(OUT, "w"), indent=1); srv.cleanup()


if __name__ == "__main__":
    main()
