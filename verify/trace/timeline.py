#!/usr/bin/env python3
"""Causal timeline from frontend event logs (docs/TRACING.md). Read-only: joins existing events on stable ids.
Usage: timeline.py OUT.json NAME=DATA_DIR [NAME=DATA_DIR ...]   (prints a text rendering)"""
import json, os, sys

KEEP = {"push_received", "pack_ingested", "pack_stored", "ref_version_committed", "push_reported", "object_index_built",
        "cover_committed", "twin_committed", "ls_refs", "fetch_served", "cover_recompress"}


def load(name, data):
    out = []
    p = os.path.join(data, "events", "events.jsonl")
    for line in open(p):
        try: e = json.loads(line)
        except ValueError: continue
        e = e.get("data", e)
        if e.get("ev") in KEEP:
            e["frontend"] = name; out.append(e)
    return out


def build(evs):
    for i, e in enumerate(evs): e["id"] = f'{e["frontend"]}#{e["seq"]}'
    edges = []
    def edge(a, b, why): edges.append({"from": a["id"], "to": b["id"], "why": why})
    by_push = {}
    for e in evs:
        if e.get("push"): by_push.setdefault((e["frontend"], e["push"]), []).append(e)
    for k, chain in by_push.items():
        chain.sort(key=lambda e: e["seq"])
        for a, b in zip(chain, chain[1:]): edge(a, b, f"push {k[1][:8]}")
    origin = {}     # pack id -> event that made it exist (ref version, cover or copy)
    for e in evs:
        if e["ev"] == "ref_version_committed" and e.get("pack"): origin.setdefault(e["pack"], e)
        if e["ev"] == "cover_committed": origin.setdefault(e["pack"], e)
        if e["ev"] == "twin_committed": origin.setdefault(e["twin"], e)
    for e in evs:
        if e["ev"] == "object_index_built" and e.get("incremental"):
            for d in e.get("discovered", []):
                o = origin.get(d["pack"])
                if o and o["frontend"] != e["frontend"]:
                    edge(o, e, f'discovered pack {d["pack"][:8]} (commit LSN {d["lsn"]}) via change log')
        if e["ev"] == "cover_committed":
            for m in e.get("members", []):
                if m in origin: edge(origin[m], e, f"member pack {m[:8]} of L{e['level']} cover ({e['lo']},{e['hi']}]")
        if e["ev"] == "twin_committed" and e["pack"] in origin:
            edge(origin[e["pack"]], e, f"self-contained copy of {e['pack'][:8]}")
        if e["ev"] == "fetch_served":
            for p in e.get("packs", []):
                if p in origin: edge(origin[p], e, f"planned pack {p[:8]}")
            for p in e.get("uri_packs", []):
                if p in origin and p not in e.get("packs", []): edge(origin[p], e, f"served by URI {p[:8]} (copy)")
    return edges


def describe(e):
    ev = e["ev"]
    if ev == "push_received": return f"push {e['push'][:8]} received: " + ", ".join(f"{c['ref']} {c['old'][:7]}→{c['new'][:7]}" for c in e.get("cmds", []))
    if ev == "pack_ingested": return f"pack {e['pack'][:8]} validated in one pass: {e['objects']} objects, {e['bytes']} B, {e.get('thin_bases', 0)} thin bases"
    if ev == "pack_stored": return f"pack {e['pack'][:8]} stored immutably"
    if ev == "ref_version_committed": return f"TXN LSN {e['lsn']}: {e['ref']} v{e['version']} → {e['tip'][:7]} (pack {str(e.get('pack'))[:8]}, deps {e.get('deps')})"
    if ev == "push_reported": return "push acknowledged: " + ", ".join(f"{r['ref']} {'ok' if r['ok'] else r['msg']}" for r in e.get("results", []))
    if ev == "object_index_built": return (f"index refresh ({'incremental' if e.get('incremental') else 'full'}): {e.get('keys_read')} change-log records "
                                           f"from LSN {e.get('hwm_from')} → {e.get('hwm')}, packs {[d['pack'][:8] for d in e.get('discovered', [])]}")
    if ev == "cover_committed": return f"TXN LSN {e['lsn']}: L{e['level']} cover {e['pack'][:8]} of {e['ref']} ({e['lo']},{e['hi']}], {e['objects']} objects, {e['bytes']} B"
    if ev == "twin_committed": return f"TXN LSN {e['lsn']}: self-contained copy {e['twin'][:8]} of {e['pack'][:8]}"
    if ev == "fetch_served": return (f"fetch planned in {e.get('plan_steps')} steps / {e.get('plan_metadata_reads')} metadata reads: packs {[p[:8] for p in e.get('packs', [])]}, "
                                     f"by URI {[p[:8] for p in e.get('uri_packs', [])]}, inline {e.get('inline_bytes')} B")
    if ev == "cover_recompress": return f"cover recompressed in a scratch repository: {e.get('spliced_bytes')} B spliced → {e.get('recompressed_bytes') or 'kept spliced'} ({e.get('result', '')}, {e.get('millis')} ms)"
    if ev == "ls_refs": return "ref advertisement: " + ", ".join(f"{n} v{s}" for n, s, _ in e.get("snapshot", []))
    return ev


def main():
    out = sys.argv[1]; evs = []
    for a in sys.argv[2:]:
        n, d = a.split("=", 1); evs += load(n, d)
    evs.sort(key=lambda e: (e["ts_ms"], e["frontend"], e["seq"]))
    edges = build(evs)
    t0 = evs[0]["ts_ms"] if evs else 0
    json.dump({"note": "wall-clock ms are per host; causal order is given by the edges (ids, LSNs)", "events": evs, "edges": edges}, open(out, "w"), indent=1)
    into = {}
    for x in edges: into.setdefault(x["to"], []).append(x)
    for e in evs:
        print(f"+{e['ts_ms'] - t0:6d} ms  [{e['frontend']}] {describe(e)}")
        for x in into.get(e["id"], []):
            print(f"{'':14}  ↑ {x['why']}  (from {x['from']})")


if __name__ == "__main__":
    main()
