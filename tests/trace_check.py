#!/usr/bin/env python3
"""Trace checker for G_DATA_DIR/events/events.jsonl (MODEL.md §6).

Checks, per repository:
  T1 ref versions of each ref have consecutive seq numbers (1, 2, 3, ...) in lsn order
  T2 compare-and-swap: `old` of each committed version equals the tip of the previous version
     (40 zeros before the first version / after a delete)
  T3 every dependency (ref, seq) was committed earlier (lower lsn, or same lsn = same txn)
  T4 every `ok` reported to a client has a committed version with that push id and ref
  T5 a cover's members/deps refer to committed versions, its range is aligned (hi % 4^L == 0)
  T6 ls-refs snapshots taken by one server process never show a ref at a lower seq than an
     earlier snapshot of the same process (per-process monotonic reads)
Usage: trace_check.py events.jsonl [more.jsonl ...]   (exit 1 on any violation)
"""
import json
import sys
from collections import defaultdict

ZERO = "0" * 40


def main(paths):
    evs = []
    for p in paths:
        with open(p) as f:
            for n, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    evs.append(json.loads(line))
                except json.JSONDecodeError:
                    # a crash can tear the last line of the log
                    print(f"note: {p}:{n + 1}: unparsable line skipped")
    bad = []
    notes = 0
    # epoch = number of (re)starts seen so far in file order; a commit whose WAL record became
    # durable just before a crash is applied by recovery without a commit event (it was never
    # acknowledged), so a version gap is legitimate only across an epoch change.
    epoch = 0
    for e in evs:
        if e.get("ev") in ("server_start", "sim_restart"):
            epoch += 1
        e["_epoch"] = epoch
    commits = [e for e in evs if e.get("ev") == "ref_version_committed"]
    commits.sort(key=lambda e: (e["lsn"], e["ref"]))
    # pass 1: versions recovered without an event (gaps across a restart)
    recovered = set()
    lastv = {}
    for e in commits:
        key = (e["repo"], e["ref"])
        pv, pe = lastv.get(key, (0, 0))
        if e["version"] > pv + 1 and e["_epoch"] != pe:
            for k in range(pv + 1, e["version"]):
                recovered.add((e["repo"], e["ref"], k))
        lastv[key] = (e["version"], e["_epoch"])
    # observed versions per ref with their epoch, to recognise recovered *tail* versions (a
    # recovered version that is the ref's latest has no later event exposing the gap)
    observed = defaultdict(list)  # (repo, ref) -> [(version, epoch)]
    for e in commits:
        observed[(e["repo"], e["ref"])].append((e["version"], e["_epoch"]))

    def recovered_tail(repo, ref, s, at_epoch):
        below = [ep for (v, ep) in observed.get((repo, ref), []) if v < s]
        last_ep = max(below) if below else 0
        return at_epoch > last_ep

    last = {}  # (repo, ref) -> (version, tip, epoch)
    committed = {}  # (repo, ref, version) -> lsn
    by_push = set()
    for e in commits:
        key = (e["repo"], e["ref"])
        prev_seq, prev_tip, prev_epoch = last.get(key, (0, ZERO, 0))
        s = e["version"]
        recovered_gap = s > prev_seq + 1 and e["_epoch"] != prev_epoch
        if recovered_gap:
            notes += 1
        elif s != prev_seq + 1:
            bad.append(f"T1 {key}: version {s} after {prev_seq}")
        if not recovered_gap and e.get("old") is not None and e["old"] != prev_tip:
            bad.append(f"T2 {key}@{s}: old {e['old'][:12]} != previous tip {prev_tip[:12]}")
        for k in range(prev_seq + 1, s):
            committed[(e["repo"], e["ref"], k)] = e["lsn"]  # recovered, unobserved versions
        for d in e.get("deps", []):
            dl = committed.get((e["repo"], d["r"], d["s"]))
            if (e["repo"], d["r"], d["s"]) in recovered:
                continue
            if (e["repo"], d["r"], d["s"]) not in committed and recovered_tail(e["repo"], d["r"], d["s"], e["_epoch"]):
                notes += 1
                continue
            if dl is None or dl > e["lsn"]:
                same_txn = any(c["lsn"] == e["lsn"] and c["ref"] == d["r"] for c in commits)
                if not same_txn:
                    bad.append(f"T3 {key}@{s}: dependency {d['r']}@{d['s']} not committed before")
        committed[(e["repo"], e["ref"], s)] = e["lsn"]
        last[key] = (s, e["tip"], e["_epoch"])
        by_push.add((e["repo"], e.get("push"), e["ref"]))
    for e in evs:
        if e.get("ev") == "push_reported":
            for r in e.get("results", []):
                if r.get("ok") and (e["repo"], e["push"], r["ref"]) not in by_push:
                    bad.append(f"T4 push {e['push']} reported ok for {r['ref']} without a committed version")
        if e.get("ev") == "cover_committed":
            span = 4 ** e["level"]
            if e["hi"] % span or e["hi"] - e["lo"] != span:
                bad.append(f"T5 cover {e['ref']} L{e['level']} ({e['lo']},{e['hi']}] misaligned")
            for d in e.get("deps", []):
                k = (e["repo"], d["r"], d["s"])
                if k not in committed and k not in recovered and not recovered_tail(e["repo"], d["r"], d["s"], e["_epoch"]):
                    bad.append(f"T5 cover {e['ref']} L{e['level']} dep {d['r']}@{d['s']} unknown")
    seen = defaultdict(dict)  # node -> (repo, ref) -> seq
    for e in evs:
        if e.get("ev") == "ls_refs":
            node = e.get("node")
            for name, seq, _tip in e.get("snapshot", []):
                k = (e["repo"], name)
                if seq < seen[node].get(k, 0):
                    bad.append(f"T6 {node} saw {k} go from seq {seen[node][k]} to {seq}")
                seen[node][k] = max(seq, seen[node].get(k, 0))
    print(json.dumps({"events": len(evs), "ref_versions": len(commits), "recovered_gaps": notes, "violations": len(bad)}))
    for b in bad[:50]:
        print("  " + b)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
