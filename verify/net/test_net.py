"""Network-capable deployment tests (docs/NETWORK-STORAGE.md N2-N5): TCP metadata service + MinIO pack store.
Run: verify/net/run.sh   (the dist suite in this mode: G_DIST_MODE=tcp-s3 verify/dist/run.sh)"""
import json, os, shutil, sys, tempfile, threading, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dist"))
from distlib import Cluster
from harness.lib import Work, git, fsck_ok, raw_receive, build_pack, ZERO


def events(srv):
    p = os.path.join(srv.data, "events", "events.jsonl"); out = []
    if os.path.exists(p):
        for l in open(p):
            try: e = json.loads(l)
            except ValueError: continue
            out.append(e.get("data", e))
    return out


def ostore(srv):
    return {k: v["foreground"] for k, v in srv.stats()["object_store"].items()}


class NetCase(unittest.TestCase):
    def setUp(self):
        self.c = Cluster(2, mode=os.environ.get("G_NET_MODE", "tcp-s3")); self.a, self.b = self.c.fe; self.tmp = tempfile.mkdtemp(prefix="nt-")
        self.addCleanup(lambda: (self.c.cleanup(), shutil.rmtree(self.tmp, ignore_errors=True)))

    def head(self, key):
        r = self.c.minio.curl("-I", "-o", "/dev/null", "-w", "%{http_code}", f"{self.c.minio.url}/pando/{key}")
        return r.stdout.decode()

    def clone(self, srv, name, uris=True, check=True):
        d = os.path.join(self.tmp, name)
        p = git([*(["-c", "fetch.uriprotocols=http"] if uris else []), "clone", "-q", srv.url("r"), d], check=check)
        if check:
            ok, err = fsck_ok(d); self.assertTrue(ok, err)
        return d, p


class N2CreateOnly(NetCase):
    def test_same_pack_published_concurrently_by_two_frontends(self):
        self.a.create("r"); w = Work(self.tmp, "w"); c = w.commit("f.txt", "x" * 5000 + "\n"); pack = build_pack(w.dir, c)
        s0 = [ostore(s) for s in (self.a, self.b)]; res = {}; bar = threading.Barrier(2)
        def go(s, ref):
            bar.wait(); res[ref] = raw_receive(s.url("r"), [(ZERO, c, ref)], pack)[1].get(ref)
        ts = [threading.Thread(target=go, args=(s, f"refs/heads/{n}")) for s, n in ((self.a, "x"), (self.b, "y"))]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(set(res.values()), {"ok"}, res)
        d = [{k: ostore(s)[k] - s0[i][k] for k in s0[i]} for i, s in enumerate((self.a, self.b))]
        created = sum(x["put_created"] for x in d); exists = sum(x["put_exists"] for x in d)
        print("N2:", d)
        self.assertEqual(created, 2, f"pack+index must be created exactly once in total: {d}")   # .pack + .pidx
        self.assertEqual(exists, 2, f"the second publisher must find both objects present: {d}")
        self.clone(self.b, "cb")

    def test_object_overwritten_behind_our_back_is_detected_not_served(self):
        """A faulty writer replaces a published pack (unconditional PUT). A frontend with a cold cache must refuse it."""
        self.a.create("r"); w = Work(self.tmp, "w"); w.commit("f.txt", "y" * 5000 + "\n"); w.push(self.a.url("r"), "main")
        pid = [e for e in events(self.a) if e.get("ev") == "pack_stored"][-1]["pack"]
        key = f"repos/r/packs/{pid}.pack"
        bad = os.path.join(self.tmp, "bad.pack"); open(bad, "wb").write(b"PACK" + os.urandom(200))
        r = self.c.minio.curl("-X", "PUT", "--data-binary", f"@{bad}", "-o", "/dev/null", "-w", "%{http_code}", f"{self.c.minio.url}/pando/{key}")
        self.assertEqual(r.stdout.decode(), "200")
        cold = self.c.add_frontend()
        for uris in (False, True):
            d, p = self.clone(cold, f"cold-{uris}", uris=uris, check=False)
            self.assertNotEqual(p.returncode, 0, "a clone was served from a corrupted object")
            # the refusal must be the verification, not an unrelated failure
            why = p.stderr.decode() + json.dumps([e for e in events(cold) if "error" in json.dumps(e)])
            self.assertIn("failed verification", why, f"clone failed for another reason: {why[-600:]}")
        self.assertFalse(os.path.exists(os.path.join(cold.data, "blobs", "repos", "r", "packs", f"{pid}.pack")), "corrupt object cached")
        self.clone(self.a, "warm")   # A's verified local cache still serves correctly


class N3Completeness(NetCase):
    def test_abort_between_pack_and_index_upload(self):
        self.a.create("r"); w = Work(self.tmp, "w"); w.commit("f.txt", "1\n"); w.push(self.a.url("r"), "main"); old = w.head()
        dying = self.c.add_frontend({"PANDO_TEST_ABORT_AFTER_PACK_PUT": "1"})
        w.commit("f.txt", "2\n" * 3000); new = w.head()
        p = w.git("push", "-q", dying.url("r"), "main", check=False)
        self.assertNotEqual(p.returncode, 0, "push acknowledged by a frontend that died before the index upload")
        self.assertEqual(git(["ls-remote", self.b.url("r"), "refs/heads/main"]).stdout.decode().split()[0], old, "ref published")
        pid = [e for e in events(dying) if e.get("ev") == "pack_ingested"][-1]["pack"]
        self.assertEqual(self.head(f"repos/r/packs/{pid}.pack"), "200"); self.assertEqual(self.head(f"repos/r/packs/{pid}.pidx"), "404")
        s0 = ostore(self.b); w.push(self.b.url("r"), "main"); s1 = ostore(self.b)
        print("N3 retry through B:", {k: s1[k] - s0[k] for k in s0})
        self.assertEqual(self.head(f"repos/r/packs/{pid}.pidx"), "200")
        for s, n in ((self.a, "ca"), (self.b, "cb")):
            d, _ = self.clone(s, n); self.assertEqual(git(["rev-parse", "HEAD"], cwd=d).stdout.decode().strip(), new)


class N4StoreDown(NetCase):
    def test_object_store_unavailable(self):
        self.a.create("r"); w = Work(self.tmp, "w"); w.commit("f.txt", "1\n"); w.push(self.a.url("r"), "main"); old = w.head()
        self.c.minio.stop()
        w.commit("f.txt", "2\n"); t = time.time(); p = w.git("push", "-q", self.a.url("r"), "main", check=False, timeout=120)
        print(f"N4: push with store down failed after {time.time() - t:.1f}s")
        self.assertNotEqual(p.returncode, 0, "push acknowledged while the object store was down")
        self.assertEqual(git(["ls-remote", self.b.url("r"), "refs/heads/main"]).stdout.decode().split()[0], old)
        d, _ = self.clone(self.a, "cached", uris=False)   # A holds everything already acknowledged in its cache
        self.c.minio.start()
        w.push(self.a.url("r"), "main"); self.clone(self.b, "after")


class N5ColdCache(NetCase):
    def test_new_frontend_with_empty_cache_serves_history(self):
        self.a.create("r"); w = Work(self.tmp, "w")
        for i in range(12):
            w.commit("f.txt", f"{i} " + "z" * 2000 + "\n"); w.push((self.a if i % 2 else self.b).url("r"), "main")
        self.a.maintain()
        cold = self.c.add_frontend(); s0 = ostore(cold)
        for uris in (False, True):
            d, _ = self.clone(cold, f"cold-{uris}", uris=uris)
            self.assertEqual(git(["rev-parse", "HEAD"], cwd=d).stdout.decode().strip(), w.head())
        s1 = ostore(cold); print("N5 cold frontend:", {k: s1[k] - s0[k] for k in s0})
        self.assertGreater(s1["get"] - s0["get"], 0)


class N5RestartEmptiesCache(NetCase):
    """docs/S3-PROFILE.md: cache files are written without fsync, so every start empties the cache; afterwards the
    frontend serves from the store (verified downloads)."""
    def test_restart_starts_with_empty_cache_and_serves_from_store(self):
        self.a.create("r"); w = Work(self.tmp, "w")
        for i in range(4):
            w.commit("f.txt", f"{i} " + "k" * 3000 + "\n"); w.push(self.a.url("r"), "main")
        self.a.maintain()   # no background work pending: startup catch-up would otherwise (legitimately) refill the cache
        packs = os.path.join(self.a.data, "blobs", "repos", "r", "packs")
        self.assertTrue(os.listdir(packs))
        self.a.stop(); self.a.start()
        self.assertFalse(os.path.exists(packs) and os.listdir(packs), "cache survived a restart")
        s0 = ostore(self.a)
        for uris in (False, True):
            d, _ = self.clone(self.a, f"after-{uris}", uris=uris)
            self.assertEqual(git(["rev-parse", "HEAD"], cwd=d).stdout.decode().strip(), w.head())
        self.assertGreater(ostore(self.a)["get"] - s0["get"], 0)


def _u64(b, p):
    return int.from_bytes(b[p:p + 8], "big")


def pidx_rewrite(b, flip_entry_byte=False, rename=None):
    """Rewrite a stored .pidx (server/src/pack.rs serialize) and recompute its SHA-1 trailer, so it stays a VALID
    index: flip_entry_byte changes the first entry's recorded size (a real difference); rename={old_id: new_id}
    renames the pack recorded for external delta bases (the one legitimate difference between frontends)."""
    import hashlib
    b = bytearray(b[:-20]); p = 8; p += 8 + _u64(b, p); p += 8; n = _u64(b, p); p += 8; ent = p; p += n * 82
    if flip_entry_byte:
        b[ent + 20 + 24 + 2 + 7] ^= 0x01            # oid(20) offset/data_off/end(24) otype,kind(2) size(8): low byte
    p += 8 + _u64(b, p) * 20                         # prerequisites
    nb = _u64(b, p); p += 8; names = []
    for _ in range(nb):
        p += 20; l = _u64(b, p); p += 8; names.append(b[p:p + l].decode())
        if rename and names[-1] in rename:
            new = rename[names[-1]].encode(); assert len(new) == l; b[p:p + l] = new
        p += l
    return bytes(b) + hashlib.sha1(bytes(b)).digest(), names


class N6PublishVerification(NetCase):
    """Audit F2/F9/F10: when a create-only PUT finds the key taken (412), the stored object must be equivalent to what
    was pushed before the push is acknowledged. Exact bytes for packs (the auditor's R03 covers a same-size,
    same-trailer pack); semantic equivalence for indexes, accepting the one legitimate difference (which pack an
    external delta base was found in) only when that pack really holds the base."""

    def put(self, key, data):
        f = os.path.join(self.tmp, "plant.bin"); open(f, "wb").write(data)
        r = self.c.minio.curl("-X", "PUT", "--data-binary", f"@{f}", "-o", "/dev/null", "-w", "%{http_code}", f"{self.c.minio.url}/pando/{key}")
        self.assertEqual(r.stdout.decode(), "200")

    def get(self, key):
        return self.c.minio.curl(f"{self.c.minio.url}/pando/{key}").stdout

    def setup_thin(self):
        """X (a large blob) stored in two packs P1 and P2; P3 is a thin pack whose delta base X is external."""
        w = Work(self.tmp, "w")
        big = "".join(f"line {i} {os.urandom(8).hex()}\n" for i in range(3000))
        c1 = w.commit("big.txt", big); p1 = build_pack(w.dir, c1)
        c2 = w.commit("side.txt", "side\n"); p2 = build_pack(w.dir, c2)          # full pack: holds X again
        w.git("checkout", "-q", "-b", "t", c1)
        open(os.path.join(w.dir, "big.txt"), "w").write(big.replace("line 7 ", "line seven "))   # Work.commit appends
        c3 = w.commit("big.txt", ""); w.git("checkout", "-q", "-")
        p3 = git(["pack-objects", "--revs", "--thin", "--stdout", "-q"], cwd=w.dir, inp=f"{c3}\n^{c1}\n".encode()).stdout
        w.git("checkout", "-q", "--orphan", "u"); w.git("rm", "-rqf", "."); c4 = w.commit("u.txt", "unrelated\n")
        p4 = build_pack(w.dir, c4)
        self.assertEqual(w.git("ls-tree", "--name-only", c4).stdout.decode().split(), ["u.txt"])   # P4 holds no base
        ids = [x[-20:].hex() for x in (p1, p2, p3, p4)]
        steps = [([(ZERO, c1, "refs/heads/main")], p1), ([(ZERO, c2, "refs/heads/side")], p2), ([(ZERO, c4, "refs/heads/u")], p4)]
        return steps, ([(c1, c3, "refs/heads/main")], p3), ids

    def push_all(self, srv, repo, steps):
        for upd, pack in steps:
            un, res = raw_receive(srv.url(repo), upd, pack)
            self.assertEqual(set(res.values()), {"ok"}, (repo, un, res))

    def test_valid_but_different_index_at_the_key_refuses_the_push(self):
        self.a.create("q"); self.a.create("r")
        w = Work(self.tmp, "w"); c = w.commit("f.txt", "z" * 3000 + "\n"); pack = build_pack(w.dir, c); pid = pack[-20:].hex()
        self.assertEqual(raw_receive(self.a.url("q"), [(ZERO, c, "refs/heads/main")], pack)[1], {"refs/heads/main": "ok"})
        bad, _ = pidx_rewrite(self.get(f"repos/q/packs/{pid}.pidx"), flip_entry_byte=True)
        self.put(f"repos/r/packs/{pid}.pidx", bad)
        un, res = raw_receive(self.a.url("r"), [(ZERO, c, "refs/heads/main")], pack)
        self.assertNotEqual(res.get("refs/heads/main"), "ok", f"acknowledged against a different stored index: {un}")
        self.assertIn("differs from ours", un)
        self.assertNotIn("refs/heads/main", git(["ls-remote", self.b.url("r")]).stdout.decode())

    def test_equivalent_index_naming_another_pack_holding_the_base_is_accepted_else_refused(self):
        steps, (upd3, p3), (i1, i2, i3, i4) = self.setup_thin()
        for repo in ("q", "r", "s"):
            self.a.create(repo)
            self.push_all(self.a, repo, steps)
        self.assertEqual(raw_receive(self.a.url("q"), upd3, p3)[1], {"refs/heads/main": "ok"})
        ref_pidx = self.get(f"repos/q/packs/{i3}.pidx")
        _, names = pidx_rewrite(ref_pidx)
        self.assertTrue(names, "P3 should be thin (external delta bases)")
        # r: the stored index names, for every base, a different pack that also holds it (P1 and P2 both hold every
        # object of c1; the frontend may have found it in either, or in a cover) -> equivalent, accepted, servable
        alt, _ = pidx_rewrite(ref_pidx, rename={n: (i2 if n != i2 else i1) for n in names})
        self.assertNotEqual(alt, ref_pidx)
        self.put(f"repos/r/packs/{i3}.pidx", alt)
        un, res = raw_receive(self.a.url("r"), upd3, p3)
        self.assertEqual(res, {"refs/heads/main": "ok"}, f"legitimate alternative index refused: {un}")
        cold = self.c.add_frontend()
        d = os.path.join(self.tmp, "cold-r")
        git(["clone", "-q", cold.url("r"), d]); ok, err = fsck_ok(d); self.assertTrue(ok, err)
        self.assertEqual(git(["rev-parse", "HEAD"], cwd=d).stdout.decode().strip(), upd3[0][1])
        # s: the stored index names a pack that does NOT hold the base -> refused, nothing acknowledged
        bogus, _ = pidx_rewrite(ref_pidx, rename={n: i4 for n in names})
        self.put(f"repos/s/packs/{i3}.pidx", bogus)
        un, res = raw_receive(self.a.url("s"), upd3, p3)
        self.assertNotEqual(res.get("refs/heads/main"), "ok", f"acknowledged against an index naming a wrong base pack: {un}")
        self.assertIn("does not hold it", un)

    def test_corrupt_substitution_map_in_the_store_is_rejected_not_used(self):
        """Audit F9: downloaded .alt maps were not verified. A parseable map with wrong entries must be rejected by a
        frontend with a cold cache (never used), and incremental fetches, the ones that use substitution, must still
        be correct."""
        import re
        self.a.create("r"); w = Work(self.tmp, "w")
        bigs = {f: "".join(f"row {i} {os.urandom(6).hex()}\n" for i in range(4000)) for f in ("a.txt", "b.txt")}
        heads = []
        for v in range(16):    # each version edits two big files: L0 packs hold deltas, covers hold whole objects
            for f in bigs:
                bigs[f] = bigs[f].replace(f"row {v * 37} ", f"ROW {v * 37} ")
                open(os.path.join(w.dir, f), "w").write(bigs[f])
            heads.append(w.commit("a.txt", "")); w.push(self.a.url("r"), "main")
        self.a.maintain()
        keys = re.findall(r"<Key>([^<]+)</Key>", self.c.minio.curl(f"{self.c.minio.url}/pando?list-type=2&prefix=repos/r/packs/").stdout.decode())
        maps = []
        for k in keys:
            if k.endswith(".alt"):
                b = self.get(k); n = int.from_bytes(b[4:8], "big")
                if n >= 2: maps.append((k, b, n))
        self.assertTrue(maps, f"workload produced no substitution map with 2+ entries: {[k for k in keys if k.endswith('.alt')]}")
        for k, b, n in maps:
            # parseable but wrong: the first two entries swapped (valid deltas of the wrong objects; the fetch path's
            # own per-entry check, packout.rs, already ignored these) and the last entry pointing past the end of its
            # L0 index (unchecked indexing: the fetch failed)
            recs, p = [], 8
            for _ in range(n):
                l = b[p + 20]; recs.append(bytearray(b[p:p + 21 + l + 4])); p += 21 + l + 4
            recs[0][-4:], recs[1][-4:] = bytes(recs[1][-4:]), bytes(recs[0][-4:])
            recs[-1][-4:] = (0x00FFFFFF).to_bytes(4, "big")
            self.put(k, b[:8] + b"".join(bytes(r) for r in recs))
        cold = self.c.add_frontend()
        # Substitution is used for incremental fetches (the client holds the delta bases), not for fresh clones.
        for v in (1, 4, 7, 10):
            d = os.path.join(self.tmp, f"old{v}"); w.git("branch", "-f", f"v{v}", heads[v])
            git(["init", "-q", d]); git(["fetch", "-q", w.dir, f"v{v}:refs/heads/main"], cwd=d)
            git(["fetch", "-q", cold.url("r"), "main:refs/remotes/o/main"], cwd=d)
            ok, err = fsck_ok(d); self.assertTrue(ok, f"from version {v}: {err}")
            self.assertEqual(git(["rev-parse", "o/main"], cwd=d).stdout.decode().strip(), w.head())
        log = open(os.path.join(cold.data, "server.log"), errors="replace").read()
        self.assertIn("substitution map of", log)
        self.assertIn("rejected", log)

    def test_valid_but_different_substitution_map_at_the_key_is_accepted(self):
        """Audit F10: two frontends building the same cover may record different (both valid) substitution maps.
        The second publisher found the key taken, compared sizes and refused, so maintenance failed on it. Cover
        bytes are deterministic, so identical pushes to a second repository rebuild the same covers; a valid but
        different map (a subset: absent entries just mean "send whole") is planted at each cover's key first."""
        import re
        w = Work(self.tmp, "w"); bigs = {f: "".join(f"row {i} {os.urandom(6).hex()}\n" for i in range(3000)) for f in ("a.txt", "b.txt")}
        steps, prev = [], None
        for v in range(8):
            for f in bigs:
                bigs[f] = bigs[f].replace(f"row {v * 37} ", f"ROW {v * 37} "); open(os.path.join(w.dir, f), "w").write(bigs[f])
            c = w.commit("a.txt", "")
            pk = git(["pack-objects", "--revs", "--thin", "--stdout", "-q"], cwd=w.dir, inp=(f"{c}\n" + (f"^{prev}\n" if prev else "")).encode()).stdout
            steps.append(([(prev or ZERO, c, "refs/heads/main")], pk)); prev = c
        def listing(repo):
            return re.findall(r"<Key>([^<]+)</Key>", self.c.minio.curl(f"{self.c.minio.url}/pando?list-type=2&prefix=repos/{repo}/packs/").stdout.decode())
        self.a.create("q"); self.push_all(self.a, "q", steps); self.a.maintain()
        maps = {k.split("/")[-1]: self.get(k) for k in listing("q") if k.endswith(".alt")}
        planted = 0
        for name, b in maps.items():
            n = int.from_bytes(b[4:8], "big")
            if n >= 2:   # drop the last entry: a different, smaller, still valid map
                p, recs = 8, []
                for _ in range(n):
                    l = b[p + 20]; recs.append(b[p:p + 21 + l + 4]); p += 21 + l + 4
                self.put(f"repos/r/packs/{name}", b"PALT" + (n - 1).to_bytes(4, "big") + b"".join(recs[:-1])); planted += 1
        self.assertGreater(planted, 0, f"no map with 2+ entries to vary: {[(k, len(v)) for k, v in maps.items()]}")
        self.a.create("r"); self.push_all(self.a, "r", steps)
        m = self.a.run("maintain", check=False)
        self.assertEqual(m.returncode, 0, f"maintain failed with a valid differing map at the key: {m.stderr.decode()[-400:]}")
        covers = lambda repo: sorted(k.split("/")[-1] for k in listing(repo) if k.endswith(".alt"))
        self.assertEqual(covers("r"), covers("q"), "every cover (and its map) published for r as for q")
        cold = self.c.add_frontend()
        d = os.path.join(self.tmp, "old"); w.git("branch", "-f", "v2", steps[2][0][0][1])
        git(["init", "-q", d]); git(["fetch", "-q", w.dir, "v2:refs/heads/main"], cwd=d)
        git(["fetch", "-q", cold.url("r"), "main:refs/remotes/o/main"], cwd=d)
        ok, err = fsck_ok(d); self.assertTrue(ok, err)
        self.assertEqual(git(["rev-parse", "o/main"], cwd=d).stdout.decode().strip(), prev)

class N7ErrorReporting(NetCase):
    def test_error_inside_the_pack_stream_reaches_the_client_as_a_message(self):
        """Audit F8: an error after the response switched to side-band was written as a bare 'ERR' pkt-line, which
        the client reads as band 69 ("bad band #69") instead of the reason. Planted: the pack object of a committed
        version is missing from the store while its index is present, so a cold frontend fails mid-stream."""
        self.a.create("r"); w = Work(self.tmp, "w"); w.commit("f.txt", "x" * 4000 + "\n"); w.push(self.a.url("r"), "main")
        pid = [e for e in events(self.a) if e.get("ev") == "pack_stored"][-1]["pack"]
        r = self.c.minio.curl("-X", "DELETE", "-o", "/dev/null", "-w", "%{http_code}", f"{self.c.minio.url}/pando/repos/r/packs/{pid}.pack")
        self.assertEqual(r.stdout.decode(), "204")
        cold = self.c.add_frontend()
        d, p = self.clone(cold, "cold", uris=False, check=False)
        err = p.stderr.decode(errors="replace")
        self.assertNotEqual(p.returncode, 0, "clone succeeded without the pack")
        self.assertNotIn("bad band", err, f"error not delivered as a message: {err[-300:]}")
        self.assertIn(pid, err, f"the client was not told what failed: {err[-300:]}")


if __name__ == "__main__":
    unittest.main()
