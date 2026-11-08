"""CG3-S architecture-FIDELITY INDICATORS (v2, after REVIEW-1 F6/F7/F8). These tests NEVER fail a gate.
They collect evidence into G_STRUCT_OUT (json) for the F classification (C-pass/F-pass vs F-different), which is decided by
the CG7 code audit, not by these heuristics: file-layout heuristics and self-reported /_stats are gameable (F6) and can
mis-judge correct designs (F7: Git sends an extra probe RPC for pushes > http.postBuffer; F8: content-addressed or compressed
or database-backed layouts)."""
import hashlib, json, os, re
from harness.lib import *
HEX38 = re.compile(r"^[0-9a-f]{38}$")
OUT = os.environ.get("G_STRUCT_OUT")


def emit(name, data):
    print(f"[indicator {name}] {json.dumps(data)[:600]}")
    if OUT: open(OUT, "a").write(json.dumps({"indicator": name, **data}) + "\n")


def pack_files(d):
    out = {}
    for r, _, fs in os.walk(d):
        for f in fs:
            p = os.path.join(r, f)
            try:
                with open(p, "rb") as fh:
                    if fh.read(4) == b"PACK": out[hashlib.sha256(open(p, "rb").read()).hexdigest()] = p
            except OSError: pass
    return out


class Indicators(SrvCase):
    def _push_n(self, url, n):
        w = self.work(); w.commit("a.txt", "x\n"); w.push(url, "main")
        for i in range(n): w.commit("f.txt", os.urandom(1000).hex()); w.push(url, "main")
        return w

    def test_layout_indicators(self):
        url = self.repo(); w = self._push_n(url, 5); loose, bare = [], []
        for r, ds, fs in os.walk(self.srv.data):
            if re.search(r"/objects/[0-9a-f]{2}$", r): loose += [f for f in fs if HEX38.match(f)]
            if {"objects", "refs"} <= set(ds) and "HEAD" in fs: bare.append(r)
        before = pack_files(self.srv.data)
        for _ in range(5): w.commit("g.txt", "more\n"); w.push(url, "main")
        self.srv.maintain(); after = pack_files(self.srv.data)
        st = {}
        try: st = self.srv.stats()
        except Exception: pass
        emit("layout", dict(loose_object_like_files=len(loose), bare_repo_markers=len(bare), pack_magic_files=len(before), packs_retained_after_pushes_and_maintain=len(set(before) - set(after)) == 0,
                            stats_packs_stored=st.get("packs_stored"), pushes=6,
                            reading="suspect F-different if bare_repo_markers>0 AND loose_object_like_files>0; absent packs may only mean compressed/db-backed storage (F8). Needs audit."))

    def test_metadata_ops_indicator(self):
        url = self.repo(); w = self.work(); w.commit(); w.push(url, "main"); costs = {}
        for size in (1_000, 200_000, 3_000_000, 30_000_000):          # >1 MiB pushes incur an extra probe RPC in stock Git (F7)
            s0 = self.srv.stats()
            with open(os.path.join(w.dir, f"blob{size}.bin"), "wb") as f: f.write(os.urandom(size))
            w.git("add", "-A"); w.git("commit", "-q", "-m", f"b{size}"); w.push(url, "main"); s1 = self.srv.stats()
            costs[size] = (s1["metadata_reads"] - s0["metadata_reads"]) + (s1["metadata_writes"] - s0["metadata_writes"])
        small = [costs[1_000], costs[200_000]]; big = [costs[3_000_000], costs[30_000_000]]
        emit("metadata_ops", dict(ops_per_push_by_size=costs, instrumented=all(v > 0 for v in costs.values()), constant_within_postbuffer_class=len(set(small)) == 1, constant_within_large_class=len(set(big)) == 1,
                                  reading="O(1) claim: compare within the <1MiB class and within the >1MiB class separately; self-reported (gameable), audit the counting code."))
