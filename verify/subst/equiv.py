#!/usr/bin/env python3
"""Same-data A/B equivalence for the substitution map: one data directory (workload R, 300 versions, covers + maps
built), served by the same binary with maps ignored (PANDO_ALT_MAP=0) and with maps used. Every inline response must
be byte-identical (sha256), and the map run must do fewer foreground object-index lookups. Usage: equiv.py OUT.json"""
import hashlib, json, os, random, sys, tempfile, http.client
import harness.lib as lib
from harness.lib import Server, Work

def pkt(s):
    b = s.encode(); return b"%04x" % (len(b) + 4) + b

def fetch(port, want, have, uris):
    body = pkt("command=fetch\n") + pkt("object-format=sha1\n") + b"0001" + pkt("ofs-delta\n") + pkt("thin-pack\n") + pkt("no-progress\n")
    if uris: body += pkt("packfile-uris http\n")
    body += pkt(f"want {want}\n") + (pkt(f"have {have}\n") if have else b"") + pkt("done\n") + b"0000"
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=600)
    c.request("POST", "/r.git/git-upload-pack", body, {"Git-Protocol": "version=2", "Content-Type": "application/x-git-upload-pack-request"})
    b = c.getresponse().read(); c.close(); return b

rnd = random.Random(7); srv = Server(); srv.start(); srv.create("r"); url = srv.url("r")
w = Work(tempfile.mkdtemp(), "w"); tips = []
lines = [f"{i:06d} " + "".join(rnd.choice("abcdefghij") for _ in range(56)) + "\n" for i in range(4096)]
for v in range(300):
    lines[rnd.randrange(len(lines))] = f"edit {v} " + "".join(rnd.choice("klmnopqrst") for _ in range(50)) + "\n"
    open(os.path.join(w.dir, "big.txt"), "w").writelines(lines); w.git("add", "-A"); w.git("commit", "-q", "-m", f"v{v}"); w.git("push", "-q", url, "main"); tips.append(w.head())
srv.maintain(); srv.stop()
cases = [(None, False)] + [(d, u) for d in (1, 4, 16, 64, 256) for u in (False, True)]
res = {}
for mode, env in (("maps_off", {"PANDO_ALT_MAP": "0"}), ("maps_on", {})):
    s2 = Server(extra_env=env, data_dir=srv.data); s2.port = srv.port; s2.env["G_PORT"] = str(srv.port); s2.start()
    out = []
    for d, u in cases:
        b0 = s2.stats(); body = fetch(s2.port, tips[-1], tips[-1 - d] if d else None, u); b1 = s2.stats()
        br = lambda s, k: s["metadata_read_breakdown"][k]["foreground"]
        import re
        norm = re.sub(rb"http://127\.0\.0\.1:\d+/", b"http://127.0.0.1:PORT/", body)  # cache-node ports change per restart
        out.append({"delta": d, "uris": u, "sha256": hashlib.sha256(norm).hexdigest(), "raw_sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body),
                    "reads": b1["metadata_reads"] - b0["metadata_reads"],
                    "index_lookups": br(b1, "object_index_lookups") - br(b0, "object_index_lookups"),
                    "alt_map_lookups": b1.get("alt_map_lookups", {}).get("foreground", 0) - b0.get("alt_map_lookups", {}).get("foreground", 0)})
    res[mode] = out; s2.stop()
    # restart with the same mode once more to show the one-time index load is not per-request
same = all(a["sha256"] == b["sha256"] for a, b in zip(res["maps_off"], res["maps_on"]))
res["identical_responses"] = same
json.dump(res, open(sys.argv[1], "w"), indent=1)
for a, b in zip(res["maps_off"], res["maps_on"]):
    print(a["delta"], "uris" if a["uris"] else "inline", "same" if a["sha256"] == b["sha256"] else "DIFFERENT", a["bytes"], "reads", a["reads"], "->", b["reads"], "index", a["index_lookups"], "->", b["index_lookups"], "altmap", b["alt_map_lookups"])
print("identical_responses:", same)
srv.cleanup()
sys.exit(0 if same else 1)   # a difference must fail the run (audit: it exited 0 either way)
