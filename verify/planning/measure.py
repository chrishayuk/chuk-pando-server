#!/usr/bin/env python3
"""Fetch-planning cost vs ref-version distance Δ, from implementation-level counters.

For one fast-forward chain of V versions on `main`, after compaction has finished, issue one raw protocol-v2 fetch per
(Δ, repetition) with want = tip and have = the commit Δ versions behind. Records, per fetch:
  plan_reads  exact logical metadata reads of that fetch plan (thread-local count; `fetch_served` event)
  plan_steps  planner steps; covers_used; packs selected
  stats_fg    foreground /_stats metadata_reads delta around the request (whole request, not just the plan)

Usage: SERVER_DIR=<checkout> verify/planning/measure.py OUT.json [V] [deltas...]
Needs PYTHONPATH=verify/heldout (harness) — set by verify/planning/run.sh.
"""
import json, os, sys, time, http.client
import harness.lib as lib
from harness.lib import Server, Work, build_pack, raw_receive, ZERO, git

OUT = sys.argv[1]; V = int(sys.argv[2]) if len(sys.argv) > 2 else 4200
DELTAS = [int(x) for x in sys.argv[3:]] or [1, 4, 16, 64, 256, 1024, 4096]
REPS = 3


def pkt(s):
    b = s.encode(); return b"%04x" % (len(b) + 4) + b


def fetch(port, want, have, uris=False):
    body = pkt("command=fetch\n") + pkt("object-format=sha1\n") + b"0001" + pkt("ofs-delta\n") + pkt("no-progress\n")
    if uris:
        body += pkt("packfile-uris http\n")
    body += pkt(f"want {want}\n") + (pkt(f"have {have}\n") if have else b"") + pkt("done\n") + b"0000"
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=300)
    c.request("POST", "/r.git/git-upload-pack", body, {"Git-Protocol": "version=2", "Content-Type": "application/x-git-upload-pack-request"})
    n = len(c.getresponse().read()); c.close(); return n


def last_event(data, kind):
    p = os.path.join(data, "events", "events.jsonl")
    if not os.path.exists(p):
        p = os.path.join(data, "events.jsonl")
    ev = None
    with open(p) as f:
        for line in f:
            if f'"{kind}"' in line:
                ev = json.loads(line)
    return ev


def main():
    srv = Server(); srv.start(); srv.create("r"); url = srv.url("r")
    import tempfile
    tmp = tempfile.mkdtemp(prefix="plan-")
    w = Work(tmp, "w"); commits = []
    t0 = time.time()
    prev = None
    for i in range(V):
        c = w.commit("f.txt", f"{i}\n")
        unpack, res = raw_receive(url, [(prev or ZERO, c, "refs/heads/main")], build_pack(w.dir, c, prev))
        assert res.get("refs/heads/main") == "ok", (i, unpack, res)
        commits.append(c); prev = c
    push_s = time.time() - t0
    srv.maintain()
    st0 = srv.stats()
    rows = []
    for d in DELTAS:
        if d >= V:
            continue
        for rep, uris in [(r, u) for u in (False, True) for r in range(REPS)]:
            have = commits[-1 - d]
            b0 = srv.stats(); s0 = b0["metadata_reads"]
            nbytes = fetch(srv.port, commits[-1], have, uris)
            b1 = srv.stats(); s1 = b1["metadata_reads"]
            brk = {k: b1["metadata_read_breakdown"][k]["foreground"] - b0["metadata_read_breakdown"][k]["foreground"]
                   for k in b1.get("metadata_read_breakdown", {})}
            ev = last_event(srv.data, "fetch_served") or {}
            e = ev.get("data", ev)
            rows.append({"delta": d, "rep": rep, "uris": uris, "breakdown": brk, "plan_reads": e.get("plan_metadata_reads"), "plan_steps": e.get("plan_steps"),
                         "covers_used": e.get("covers_used"), "packs": len(e.get("packs", [])), "stats_fg": s1 - s0, "bytes": nbytes})
            print(json.dumps(rows[-1]), flush=True)
    out = {"V": V, "deltas": DELTAS, "reps": REPS, "push_seconds": push_s, "stats_after_maintain": st0, "rows": rows,
           "server": os.environ.get("G_RUN_SH")}
    json.dump(out, open(OUT, "w"), indent=1)
    srv.cleanup()


if __name__ == "__main__":
    main()
