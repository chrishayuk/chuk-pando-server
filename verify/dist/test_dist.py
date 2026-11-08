"""Distributed milestone 1 tests (docs/DISTRIBUTED.md D1-D8): two frontends, one metadata service, one pack store.
Run: verify/dist/run.sh"""
import glob, json, os, random, shutil, subprocess, sys, tempfile, threading, time, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from distlib import Cluster, MetaFaultProxy
from harness.lib import Work, git, fsck_ok, raw_receive, build_pack, ZERO, CLIENTS

# OpenAI's Git client fork (github.com/openai/git), built locally; required, so a missing client can never silently
# shrink the client matrix (docs/VERIFY.md)
FORK_GIT = os.environ.get("FORK_GIT", "")
if not os.path.isfile(FORK_GIT):
    raise RuntimeError(f"FORK_GIT={FORK_GIT!r}: set FORK_GIT to a built openai/git client (docs/VERIFY.md)")
CLIENT_SET = [("stock", None)] + ([("fork", FORK_GIT)] if os.path.exists(FORK_GIT) else [])


def events(srv):
    p = os.path.join(srv.data, "events", "events.jsonl")
    if not os.path.exists(p): return []
    out = []
    for l in open(p):
        try: e = json.loads(l)
        except ValueError: continue   # a torn last line after SIGKILL
        out.append(e.get("data", e))
    return out


class DistCase(unittest.TestCase):
    def setUp(self):
        self.c = Cluster(2); self.a, self.b = self.c.fe; self.tmp = tempfile.mkdtemp(prefix="dt-")
        self.addCleanup(lambda: (self.c.cleanup(), shutil.rmtree(self.tmp, ignore_errors=True)))

    def work(self, name, g=None): return Work(self.tmp, name, g)

    def clone_ok(self, url, name, g=None, uris=True, tip=None, branch=None):
        d = os.path.join(self.tmp, name)
        extra = ["-c", "fetch.uriprotocols=http"] if uris else []
        git([*extra, "clone", "-q", *(["-b", branch, "--single-branch"] if branch else []), url, d], git_bin=g)
        # Later fetches into this clone must not start a detached `gc --auto` (many URI packs exceed gc.autoPackLimit)
        # that repacks while fsck reads: a client-side race, not a server property (seen as "index not opened").
        for k, v in (("gc.auto", "0"), ("maintenance.auto", "false"), ("gc.autoDetach", "false")):
            git(["config", k, v], cwd=d, git_bin=g)
        ok, err = fsck_ok(d, g); self.assertTrue(ok, f"{name}: {err}")
        if tip: self.assertEqual(git(["rev-parse", "HEAD"], cwd=d).stdout.decode().strip(), tip, name)
        return d

    def all_events(self, ev):
        return [e for s in self.c.fe for e in events(s) if e.get("ev") == ev]


class D1SameRefCAS(DistCase):
    def test_racing_writers_on_one_branch_through_two_frontends(self):
        """Per round, 8 writers (alternating frontends) try to move main from the same old tip to 8 different commits.
        Exactly one succeeds; the version chain of main has no gap, duplicate or lost update."""
        self.a.create("r"); w = self.work("w"); tip = w.commit("f.txt", "base\n"); w.push(self.a.url("r"), "main")
        winners = []
        for rnd in range(15):
            cands = [w.git("commit-tree", f"{tip}^{{tree}}", "-p", tip, "-m", f"r{rnd} k{k}").stdout.decode().strip() for k in range(8)]
            packs = [build_pack(w.dir, c, tip) for c in cands]
            res = [None] * 8; go = threading.Barrier(8)
            def run(k):
                url = (self.a if k % 2 == 0 else self.b).url("r"); go.wait()
                res[k] = raw_receive(url, [(tip, cands[k], "refs/heads/main")], packs[k])
            ts = [threading.Thread(target=run, args=(k,)) for k in range(8)]
            [t.start() for t in ts]; [t.join() for t in ts]
            ok = [k for k in range(8) if res[k][1].get("refs/heads/main") == "ok"]
            self.assertEqual(len(ok), 1, f"round {rnd}: {len(ok)} winners: {res}")
            tip = cands[ok[0]]; winners.append(tip)
        self.assertEqual(git(["ls-remote", self.b.url("r"), "refs/heads/main"]).stdout.decode().split()[0], tip)
        rv = sorted((e["version"], e["tip"]) for e in self.all_events("ref_version_committed") if e.get("ref") == "refs/heads/main")
        self.assertEqual([v for v, _ in rv], list(range(1, 17)), f"version chain {rv}")
        self.assertEqual([t for _, t in rv][1:], winners)
        self.clone_ok(self.a.url("r"), "c", tip=tip)


class D2NoGlobalLock(DistCase):
    def test_distinct_branches_progress_concurrently(self):
        """16 writers, each on its own branch, pushing through alternating frontends. Pushes must overlap in time
        (a repository-wide lock held across a push would serialise them); every branch ends at its writer's tip."""
        self.a.create("r"); seed = self.work("seed"); seed.commit("f.txt", "base\n"); seed.push(self.a.url("r"), "main")
        N, M = 16, 8; tips = {}; errs = []
        def writer(k):
            try:
                w = self.work(f"w{k}"); w.git("pull", "-q", self.a.url("r"), "main")
                for j in range(M):
                    w.commit(f"b{k}.txt", f"{k} {j} " + "x" * 3000 + "\n")
                    w.push((self.a if (k + j) % 2 == 0 else self.b).url("r"), f"HEAD:refs/heads/b{k}")
                tips[k] = w.head()
            except Exception as e: errs.append(repr(e))
        ts = [threading.Thread(target=writer, args=(k,)) for k in range(N)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(errs, [])
        refs = dict(reversed(l.split("\t")) for l in git(["ls-remote", self.b.url("r")]).stdout.decode().strip().split("\n"))
        for k in range(N): self.assertEqual(refs[f"refs/heads/b{k}"], tips[k])
        iv = []
        for s in self.c.fe:
            ev = events(s); start = {e["push"]: e["ts_ms"] for e in ev if e.get("ev") == "push_received"}
            iv += [(start[e["push"]], e["ts_ms"]) for e in ev if e.get("ev") == "push_reported" and e["push"] in start]
        self.assertEqual(len(iv), N * M + 1)
        pts = sorted([(a, 1) for a, _ in iv] + [(b, -1) for _, b in iv], key=lambda x: (x[0], x[1]))
        cur = peak = 0
        for _, d in pts: cur += d; peak = max(peak, cur)
        both = [sum(1 for e in events(s) if e.get("ev") == "push_reported") for s in self.c.fe]
        print(f"D2: peak concurrent pushes {peak}; pushes per frontend {both}")
        self.assertGreaterEqual(peak, 4, "pushes to distinct branches were serialised")
        self.assertTrue(all(n > N for n in both))


class D2StalledCommit(unittest.TestCase):
    """While one push's commit is stalled inside the metadata service holding its stripe locks (test hook, 3 s),
    pushes to other branches through either frontend complete; a push to the stalled branch is ordered after it and
    fails its compare-and-swap. G_DIST_MUTANT=1 adds a frontend-wide push lock: this test must then fail."""
    def test_unrelated_branches_do_not_wait_for_a_stalled_commit(self):
        fe_env = {"PANDO_TEST_MUTANT_GLOBAL_LOCK": "1"} if os.environ.get("G_DIST_MUTANT") == "1" else {}
        c = Cluster(2, meta_env={"PANDO_TEST_PAUSE_KEY": "refs/heads/slow"}, fe_env=fe_env); tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: (c.cleanup(), shutil.rmtree(tmp, ignore_errors=True)))
        a, b = c.fe; a.create("r"); w = Work(tmp, "w"); base = w.commit("f.txt", "base\n"); w.push(a.url("r"), "main")
        def mk(msg): return w.git("commit-tree", f"{base}^{{tree}}", "-p", base, "-m", msg).stdout.decode().strip()
        slow, rival, f1, f2 = mk("slow"), mk("rival"), mk("f1"), mk("f2")
        out = {}
        def push(key, srv, ref, new, old=ZERO):
            t = time.time(); r = raw_receive(srv.url("r"), [(old, new, ref)], build_pack(w.dir, new, base)); out[key] = (time.time() - t, r[1].get(ref))
        ts = threading.Thread(target=push, args=("slow", a, "refs/heads/slow", slow)); ts.start()
        time.sleep(0.6)   # slow is now stalled in its commit
        tr = threading.Thread(target=push, args=("rival", b, "refs/heads/slow", rival)); tr.start()
        for key, srv, ref, new in (("fastA", a, "refs/heads/fa", f1), ("fastB", b, "refs/heads/fb", f2)):
            push(key, srv, ref, new)
        slow_running = ts.is_alive()
        ts.join(); tr.join()
        print("D2 stalled:", out)
        self.assertTrue(slow_running, "the stalled push finished before the unrelated pushes: stall not effective")
        for k in ("fastA", "fastB"):
            self.assertEqual(out[k][1], "ok"); self.assertLess(out[k][0], 1.5, f"{k} waited for the stalled commit: {out}")
        self.assertEqual(out["slow"][1], "ok"); self.assertGreater(out["slow"][0], 2.5)
        self.assertNotEqual(out["rival"][1], "ok", "two creations of one branch both succeeded")
        self.assertEqual(git(["ls-remote", b.url("r"), "refs/heads/slow"]).stdout.decode().split()[0], slow)


def _old_stripe(k):
    """The former commit-lock striping (FNV-1a mod 4096), used to force what used to be a lock collision."""
    h = 0xcbf29ce484222325
    for b in k.encode():
        h = ((h ^ b) * 0x100000001b3) & 0xFFFFFFFFFFFFFFFF
    return h % 4096


class D2KeyLocks(unittest.TestCase):
    """Audit F12: commit locks were 4096 hash stripes, so an unrelated push whose key shared a stripe with a stalled
    commit's key waited for it (intermittent, ~0.6% of D2 runs; the auditor saw it in tcp-s3 mode). This forces that
    collision deterministically: the branch is chosen so its ref key had the same stripe as refs/heads/slow's."""
    def test_push_to_a_formerly_colliding_branch_does_not_wait_for_a_stalled_commit(self):
        c = Cluster(2, mode=os.environ.get("G_DIST_MODE"), meta_env={"PANDO_TEST_PAUSE_KEY": "refs/heads/slow"}); tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: (c.cleanup(), shutil.rmtree(tmp, ignore_errors=True)))
        key = lambda ref: f"ref\x01r\x01{ref}"
        coll = next(f"refs/heads/c{i}" for i in range(10 ** 6) if _old_stripe(key(f"refs/heads/c{i}")) == _old_stripe(key("refs/heads/slow")))
        a, b = c.fe; a.create("r"); w = Work(tmp, "w"); base = w.commit("f.txt", "base\n"); w.push(a.url("r"), "main")
        def mk(msg): return w.git("commit-tree", f"{base}^{{tree}}", "-p", base, "-m", msg).stdout.decode().strip()
        slow, fast = mk("slow"), mk("fast"); packs = {n: build_pack(w.dir, n, base) for n in (slow, fast)}
        out = {}
        def push(key_, srv, ref, new):
            t = time.time(); r = raw_receive(srv.url("r"), [(ZERO, new, ref)], packs[new]); out[key_] = (time.time() - t, r[1].get(ref))
        ts = threading.Thread(target=push, args=("slow", a, "refs/heads/slow", slow)); ts.start()
        time.sleep(0.6)
        push("fast", b, coll, fast); slow_running = ts.is_alive(); ts.join()
        print("D2 key locks:", coll, out)
        self.assertTrue(slow_running, "stall not effective")
        self.assertEqual(out["fast"][1], "ok"); self.assertEqual(out["slow"][1], "ok")
        self.assertLess(out["fast"][0], 1.5, f"push to {coll} waited for the stalled commit on refs/heads/slow: {out}")


class D3D4Visibility(DistCase):
    def test_ack_on_one_frontend_visible_and_complete_on_the_other(self):
        self.a.create("r")
        for cname, g in CLIENT_SET:
            w = self.work(f"w-{cname}", g)
            for i in range(4):
                src, dst = (self.a, self.b) if i % 2 == 0 else (self.b, self.a)
                w.commit("big.txt", "".join(f"{cname} {i} {j}\n" for j in range(300)))
                w.push(src.url("r"), f"HEAD:refs/heads/{cname}")
                tip = w.head()
                seen = git(["ls-remote", dst.url("r"), f"refs/heads/{cname}"], git_bin=g).stdout.decode().split()
                self.assertEqual(seen[0] if seen else None, tip, f"{cname} v{i}: not visible on the other frontend")
                for uris in (False, True):
                    self.clone_ok(dst.url("r"), f"c-{cname}-{i}-{uris}", g, uris, tip, branch=cname)

    def test_pack_introducing_two_refs_then_dependent_push_through_other_frontend(self):
        """One push introduces x and y (same objects). A client holding only x pushes z on top through B; a client
        holding only y fetches z through A and must receive a complete history."""
        self.a.create("r"); w = self.work("w"); w.commit("f.txt", "one\n" * 50); c = w.head()
        w.git("push", "-q", "--atomic", self.a.url("r"), "HEAD:refs/heads/x", "HEAD:refs/heads/y")
        dx = self.clone_ok(self.b.url("r"), "hx", branch="x")
        open(os.path.join(dx, "f.txt"), "a").write("two\n"); git(["commit", "-qam", "z"], cwd=dx)
        git(["push", "-q", "origin", "HEAD:refs/heads/z"], cwd=dx); z = git(["rev-parse", "HEAD"], cwd=dx).stdout.decode().strip()
        dy = self.clone_ok(self.a.url("r"), "hy", branch="y")
        git(["-c", "fetch.uriprotocols=http", "fetch", "-q", "origin", "z"], cwd=dy); git(["merge", "-q", "--ff-only", "FETCH_HEAD"], cwd=dy)
        ok, err = fsck_ok(dy); self.assertTrue(ok, err)
        self.assertEqual(git(["rev-parse", "HEAD"], cwd=dy).stdout.decode().strip(), z)


class D5D6Crash(DistCase):
    def test_sigkill_frontend_during_pushes_other_continues(self):
        """Writers push through A while A is SIGKILLed at varying delays (5 times); a writer on B keeps going.
        Every acknowledged push is visible and complete through B; A restarts without assuming ownership."""
        self.a.create("r"); acked = {}; failed = []; history = {}; lock = threading.Lock(); stop = threading.Event()
        def check_ref(name, server_tip):
            """Git semantics: an acknowledged push must not be lost; an UNacknowledged push may still have been applied
            (e.g. committed, then the frontend killed before replying). So the server tip must be the last acknowledged
            commit, or a later commit whose push the client saw fail; never older, never anything else."""
            h = history.get(name, []); last_ok = max((i for i, (_, ok) in enumerate(h) if ok), default=-1)
            allowed = {c for i, (c, ok) in enumerate(h) if i >= last_ok and (ok or i > last_ok)}
            self.assertIn(server_tip, allowed, f"{name}: server at {server_tip}; acknowledged {h[last_ok][0] if last_ok >= 0 else None}; history {h}")
            if server_tip != (h[last_ok][0] if last_ok >= 0 else None):
                unacked_applied.append(name)
        unacked_applied = []
        def writer(name, srv):
            w = self.work(name)
            j = 0
            while not stop.is_set():
                j += 1; w.commit("f.txt", f"{name} {j} " + "".join(random.choice("abcdef") for _ in range(20000)) + "\n")
                p = w.git("push", "-q", srv.url("r"), f"HEAD:refs/heads/{name}", check=False)
                if p.returncode == 0:
                    with lock: acked[name] = w.head(); history.setdefault(name, []).append((w.head(), True))
                else:
                    with lock: failed.append(name); history.setdefault(name, []).append((w.head(), False))
        tb = threading.Thread(target=writer, args=("onb", self.b)); tb.start()
        for round_ in range(5):
            ta = threading.Thread(target=writer, args=(f"ona{round_}", self.a)); ta.start()
            time.sleep(0.3 + 0.37 * round_)
            dead_area = glob.glob(os.path.join(self.c.blobs, "tmp", f"i-{self.a.pid()}-*")) if self.c.shared_dir else None
            self.a.kill9()
            ta_stop = time.time() + 30
            while ta.is_alive() and time.time() < ta_stop:
                ta.join(0.1)
                if ta.is_alive(): stop.set()
            ta.join(); stop.clear()
            if not tb.is_alive():
                tb = threading.Thread(target=writer, args=("onb", self.b)); tb.start()
            # B serves everything acknowledged so far, while A is down
            with lock: snap = dict(acked)
            refs = dict(reversed(l.split("\t")) for l in git(["ls-remote", self.b.url("r")]).stdout.decode().strip().split("\n") if l)
            for name, tip in snap.items():
                if name.startswith("ona"):
                    check_ref(name, refs.get(f"refs/heads/{name}"))
            if self.c.shared_dir:
                self.assertTrue(dead_area and all(os.path.isdir(d) for d in dead_area), "A's tmp area vanished while B was live")
            self.a.start()   # restart sweeps A's dead area
            if self.c.shared_dir:
                self.assertFalse([d for d in dead_area if os.path.isdir(d)], "dead tmp area not swept on restart")
        stop.set(); tb.join()
        print(f"D5: acknowledged refs {sorted(acked)}; failed (unacknowledged) pushes {len(failed)}: {sorted(set(failed))}")
        self.assertTrue(len([n for n in acked if n.startswith("ona")]) >= 3 and len(set(failed)) >= 3, "kills did not interrupt pushes")
        d = self.clone_ok(self.b.url("r"), "all", uris=True)
        git(["fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*"], cwd=d)
        ok, err = fsck_ok(d); self.assertTrue(ok, err)
        for name, tip in acked.items():
            if name.startswith("ona"):
                check_ref(name, git(["rev-parse", f"origin/{name}"], cwd=d).stdout.decode().strip())
            else:   # B was never killed: its last acknowledged push is its tip
                self.assertEqual(git(["rev-parse", f"origin/{name}"], cwd=d).stdout.decode().strip(), tip, name)
        print(f"D5: unacknowledged-but-applied pushes observed (allowed): {sorted(set(unacked_applied))}")
        w = self.work("after"); w.commit("g.txt", "after\n"); w.push(self.a.url("r"), "HEAD:refs/heads/after")
        self.assertTrue(self.all_events("push_reported"))

    def test_live_frontend_area_never_swept(self):
        if not self.c.shared_dir: self.skipTest("S3 mode: no shared tmp directory (each frontend's cache is private)")
        area_a = glob.glob(os.path.join(self.c.blobs, "tmp", f"i-{self.a.pid()}-*"))
        self.assertEqual(len(area_a), 1)
        self.b.stop(); self.b.start()   # B's startup sweep runs while A is live
        self.assertTrue(os.path.isdir(area_a[0]), "live frontend's tmp area was removed")


class D7Invalidation(DistCase):
    def test_thin_push_on_other_frontend_and_covers_built_by_one_served_by_other(self):
        self.a.create("r"); w = self.work("w")
        lines = [f"{i:05d} " + "abcdefghij" * 5 + "\n" for i in range(3000)]
        for v in range(20):
            lines[v * 7] = f"edit {v}\n"; open(os.path.join(w.dir, "big.txt"), "w").writelines(lines)
            w.git("add", "-A"); w.git("commit", "-q", "-m", f"v{v}")
            w.push((self.a if v < 10 else self.b).url("r"), "main")   # B's first push is thin against A's pack
        ing = [e for e in events(self.b) if e.get("ev") == "pack_ingested"]
        self.assertTrue(ing[0]["thin_bases"] > 0, f"first push through B was not thin: {ing[0]}")
        self.clone_ok(self.a.url("r"), "ca", tip=w.head())
        self.a.maintain()
        covers = self.all_events("cover_committed"); self.assertTrue(covers)
        cover_packs = {e["pack"] for e in covers}
        d = self.clone_ok(self.b.url("r"), "cb", tip=w.head())
        fs = [e for e in events(self.b) if e.get("ev") == "fetch_served"][-1]
        self.assertTrue(set(fs["uri_packs"]) & cover_packs, f"B did not serve A's covers by URI: {fs}")
        self.assertEqual(fs["inline_fallback_packs"], 0)


class I3ConcurrentMaintenance(DistCase):
    """docs/INCREMENTAL-INDEX.md I3: 2 x 8 writers push thin packs (one shared large file each edits) through
    alternating frontends, while `maintain` (covers, copies) loops on both frontends. No push may fail for missing
    objects that are committed; final clones through each frontend are fsck-clean and at every writer's tip."""
    def test_thin_pushes_across_frontends_during_maintenance(self):
        self.a.create("r"); seed = self.work("seed")
        lines = [f"{i:05d} " + "abcdefghij" * 5 + "\n" for i in range(2000)]
        open(os.path.join(seed.dir, "big.txt"), "w").writelines(lines); seed.git("add", "-A"); seed.git("commit", "-q", "-m", "base")
        seed.push(self.a.url("r"), "main")
        stop = threading.Event(); errs = []; tips = {}
        def maint(s):
            while not stop.is_set():
                p = s.run("maintain", check=False)
                if p.returncode: errs.append(("maintain", p.stderr.decode()[-300:]))
        def writer(k):
            try:
                w = self.work(f"w{k}"); w.git("pull", "-q", self.a.url("r"), "main")
                for j in range(12):
                    ls = open(os.path.join(w.dir, "big.txt")).readlines(); ls[(k * 97 + j * 13) % len(ls)] = f"w{k} e{j}\n"
                    open(os.path.join(w.dir, "big.txt"), "w").writelines(ls); w.git("commit", "-qam", f"{k}-{j}")
                    p = w.git("push", "-q", (self.a if (k + j) % 2 else self.b).url("r"), f"HEAD:refs/heads/w{k}", check=False)
                    if p.returncode: errs.append((k, j, p.stderr.decode()[-300:]))
                tips[k] = w.head()
            except Exception as e: errs.append((k, repr(e)))
        ms = [threading.Thread(target=maint, args=(s,)) for s in self.c.fe]; [m.start() for m in ms]
        ws = [threading.Thread(target=writer, args=(k,)) for k in range(16)]; [t.start() for t in ws]; [t.join() for t in ws]
        stop.set(); [m.join() for m in ms]
        self.assertEqual(errs, [])
        thin = sum(1 for s in self.c.fe for e in events(s) if e.get("ev") == "pack_ingested" and e.get("thin_bases", 0) > 0)
        inc = [e for s in self.c.fe for e in events(s) if e.get("ev") == "object_index_built" and e.get("incremental")]
        print(f"I3: thin pushes {thin}; incremental refreshes {len(inc)}, keys read {sum(e['keys_read'] for e in inc)}")
        self.assertGreater(thin, 100)
        for s, n in ((self.a, "ca"), (self.b, "cb")):
            d = self.clone_ok(s.url("r"), n)
            git(["fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*"], cwd=d)
            ok, err = fsck_ok(d); self.assertTrue(ok, err)
            for k, t in tips.items():
                self.assertEqual(git(["rev-parse", f"origin/w{k}"], cwd=d).stdout.decode().strip(), t)


class D9BackgroundRecovery(DistCase):
    """Audit F1: a background job that panics (as on a metadata-service outage) must not stop background maintenance.
    The worker survives, and the failed work (covers, self-contained copies) is completed later WITHOUT any further
    push. Deterministic via the test-only PANDO_TEST_PANIC_JOBS hook; the auditor's R01 (verify/audit/run_repro.sh)
    is the real-outage version."""
    def test_failed_background_jobs_are_retried_until_done(self):
        c = self.c.add_frontend({"PANDO_TEST_PANIC_JOBS": "4", "PANDO_TEST_RETRY_SECS": "1"})
        self.a.create("r"); w = self.work("w")
        lines = [f"{i:05d} " + "abcdefghij" * 5 + "\n" for i in range(3000)]
        for v in range(4):      # versions 2-4 are thin (copy jobs); version 4 queues a cover job
            lines[v * 7] = f"edit {v}\n"; open(os.path.join(w.dir, "big.txt"), "w").writelines(lines)
            w.git("add", "-A"); w.git("commit", "-q", "-m", f"v{v}"); w.push(c.url("r"), "main")
        thin = [e["pack"] for e in events(c) if e.get("ev") == "pack_ingested" and e.get("thin_bases", 0) > 0]
        self.assertTrue(thin, "no thin push: nothing to copy")
        t0 = time.time()
        while time.time() - t0 < 30:
            ev = events(c)
            done = (any(e.get("ev") == "cover_committed" for e in ev)
                    and set(thin) <= {e["pack"] for e in ev if e.get("ev") == "twin_committed"})
            if done: break
            time.sleep(0.5)
        ev = events(c)
        panics = [e for e in ev if e.get("ev") == "compaction_error" and "panic" in e]
        self.assertGreaterEqual(len(panics), 1, "the hook injected no panic: test inconclusive")
        self.assertTrue(done, f"background work not completed after {len(panics)} injected panics: "
                              f"{[e for e in ev if e.get('ev') in ('compaction_error', 'compaction_recovered', 'cover_committed', 'twin_committed')]}")
        self.assertTrue(any(e.get("ev") == "compaction_recovered" for e in ev))
        m = c.run("maintain", check=False)
        self.assertEqual(m.returncode, 0, m.stderr.decode()[-300:])
        self.clone_ok(c.url("r"), "cc", tip=w.head())


class D10LostCommitReply(unittest.TestCase):
    """Audit F4/F5. A commit whose reply is lost was reported as rejected though the ref moved (F5), and in a
    multi-ref push every ref was then reported failed, including ones already committed (F4). A frontend now reads
    back the version keys the transaction wrote: if they carry this push's id it was applied and is reported ok.
    A commit request that never reached the service must NOT be reported ok."""
    def test_lost_reply_is_resolved_and_a_dropped_request_is_not_reported_ok(self):
        c = Cluster(2, mode="tcp-fs"); tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: (c.cleanup(), shutil.rmtree(tmp, ignore_errors=True)))
        px = MetaFaultProxy(c.meta.tcp_port); self.addCleanup(px.close)
        f = c.add_frontend({"PANDO_META": px.addr}); other = c.fe[0]
        other.create("r"); w = Work(tmp, "w"); w.commit(); w.push(other.url("r"), "main")
        # (1) single ref, reply lost after the commit applied: reported ok, and it is the server state
        c1 = w.commit(); px.lose_reply = 1
        p = w.git("push", f.url("r"), "main", check=False)
        self.assertEqual(px.events, ["lost reply"])
        self.assertEqual(git(["ls-remote", other.url("r"), "refs/heads/main"]).stdout.decode().split()[0], c1)
        self.assertEqual(p.returncode, 0, f"applied update reported as failed: {p.stderr.decode()[-300:]}")
        # (2) two refs, non-atomic, second commit's reply lost: both applied, both reported ok
        w.git("checkout", "-q", "-b", "topic"); ct = w.commit(); w.git("checkout", "-q", "main"); cm = w.commit()
        px.lose_reply = 1; px.events.clear()
        p = w.git("push", f.url("r"), "main", "topic", check=False)
        refs = dict(reversed(l.split("\t")) for l in git(["ls-remote", other.url("r")]).stdout.decode().splitlines())
        self.assertEqual((refs["refs/heads/main"], refs["refs/heads/topic"]), (cm, ct))
        self.assertEqual(p.returncode, 0, f"multi-ref push with a lost reply misreported: {p.stderr.decode()[-400:]}")
        # (3) commit request dropped before reaching the service: not applied, and NOT reported ok
        c3 = w.commit(); px.drop_request = 1; px.events.clear()
        p = w.git("push", f.url("r"), "main", check=False)
        self.assertEqual(px.events, ["dropped request"])
        self.assertEqual(git(["ls-remote", other.url("r"), "refs/heads/main"]).stdout.decode().split()[0], cm, "ref moved")
        self.assertNotEqual(p.returncode, 0, "an update that was never applied was reported as successful")
        self.assertIn("outcome unknown", p.stderr.decode())


class D8Restart(DistCase):
    def test_metadata_service_and_frontends_restart_in_any_order(self):
        self.a.create("r"); w = self.work("w"); w.commit("f.txt", "1\n"); w.push(self.a.url("r"), "main")
        self.c.meta.kill9(); self.c.meta.start()
        w.commit("f.txt", "2\n"); w.push(self.b.url("r"), "main")          # B reconnects
        self.b.stop(); self.a.stop(); self.a.start(); self.b.start()
        w.commit("f.txt", "3\n"); w.push(self.a.url("r"), "main")
        self.a.maintain(); self.b.maintain()
        for s, n in ((self.a, "ca"), (self.b, "cb")): self.clone_ok(s.url("r"), n, tip=w.head())

    def test_no_ack_while_metadata_service_down(self):
        self.a.create("r"); w = self.work("w"); w.commit("f.txt", "1\n"); w.push(self.a.url("r"), "main")
        self.c.meta.kill9()
        w.commit("f.txt", "2\n"); p = w.git("push", "-q", self.a.url("r"), "main", check=False, timeout=120)
        self.assertNotEqual(p.returncode, 0, "push acknowledged while the metadata service was down")
        self.c.meta.start()
        self.assertEqual(git(["ls-remote", self.b.url("r"), "refs/heads/main"]).stdout.decode().split()[0], w.git("rev-parse", "HEAD~1").stdout.decode().strip())
        w.push(self.a.url("r"), "main")
        self.clone_ok(self.b.url("r"), "c", tip=w.head())


if __name__ == "__main__":
    unittest.main()
