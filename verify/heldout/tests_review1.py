"""Hidden tests added in response to REVIEW-1 (finding ids in each docstring). Behavioral; a correct server must pass."""
import gzip, os, random, socket, threading, time, urllib.request
from harness.lib import *


class Throttle:
    """TCP proxy throttling client->server bytes; exposes bytes forwarded upstream."""
    def __init__(self, target_port, bps):
        self.t, self.bps, self.sent = target_port, bps, 0
        self.sock = socket.socket(); self.sock.bind(("127.0.0.1", 0)); self.sock.listen(8); self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.loop, daemon=True).start()
    def loop(self):
        while True:
            try: c, _ = self.sock.accept()
            except OSError: return
            try: s = socket.create_connection(("127.0.0.1", self.t))
            except OSError: c.close(); continue
            threading.Thread(target=self.up, args=(c, s), daemon=True).start(); threading.Thread(target=self.down, args=(s, c), daemon=True).start()
    def up(self, c, s):
        try:
            while True:
                d = c.recv(16384)
                if not d: break
                s.sendall(d); self.sent += len(d); time.sleep(len(d) / self.bps)
        except OSError: pass
        try: s.shutdown(socket.SHUT_WR)
        except OSError: pass
    def down(self, s, c):
        try:
            while True:
                d = s.recv(65536)
                if not d: break
                c.sendall(d)
        except OSError: pass
        try: c.close()
        except OSError: pass
    def close(self): self.sock.close()


def seed():
    s = int(os.environ.get("G_SEED", time.time_ns() % 10**9)); print(f"[seed={s}]", flush=True); return s


class R1(SrvCase):
    def _two_branch_repo(self):
        url = self.repo(); w = self.work(); [w.commit() for _ in range(3)]; w.push(url, "main")
        w.git("checkout", "-q", "-b", "feat/x"); [w.commit("x.txt") for _ in range(2)]; w.push(url, "feat/x"); return url, w

    def test_F1_cas_holds_on_non_main_refs_and_races_on_branch(self):
        """F1: exactly-one-winner and stale-old rejection on a NON-main branch."""
        url, w = self._two_branch_repo(); w.git("checkout", "-q", "feat/x"); base = w.head()
        res = []
        for rnd in range(6):
            base = w.head(); cands = []
            for k in range(5):
                w.git("checkout", "-q", "-B", f"c{k}", base); sha = w.commit(f"r{rnd}k{k}.txt", f"{rnd}\n"); cands.append((sha, build_pack(w.dir, sha, base)))
            bar = threading.Barrier(len(cands)); out = [None] * len(cands)
            def go(i): bar.wait(); out[i] = raw_receive(url, [(base, cands[i][0], "refs/heads/feat/x")], cands[i][1])
            ts = [threading.Thread(target=go, args=(i,)) for i in range(len(cands))]; [t.start() for t in ts]; [t.join(60) for t in ts]
            win = [i for i, o in enumerate(out) if o and o[1].get("refs/heads/feat/x") == "ok"]
            self.assertEqual(len(win), 1, f"round {rnd}: {len(win)} winners on feat/x: {[o[1] if o else None for o in out]}")
            self.assertEqual(ls_remote(url)["refs/heads/feat/x"], cands[win[0]][0])
            w.git("fetch", "-q", url, "feat/x"); w.git("checkout", "-q", "-B", "feat/x", "FETCH_HEAD")

    def test_F1_create_over_existing_ref_is_rejected(self):
        """F1: an update claiming old=0000 (create) on an existing branch/tag must be rejected, not overwrite."""
        url, w = self._two_branch_repo(); tip = ls_remote(url)["refs/heads/feat/x"]
        w.git("checkout", "-q", "main"); other = w.commit("o.txt", "o\n"); pack = build_pack(w.dir, other, None)
        _, res = raw_receive(url, [(ZERO, other, "refs/heads/feat/x")], pack)
        self.assertTrue(res.get("refs/heads/feat/x", "").startswith("ng"), f"create-over-existing accepted: {res}")
        self.assertEqual(ls_remote(url)["refs/heads/feat/x"], tip)

    def test_F1_delete_with_wrong_old_rejected(self):
        url, w = self._two_branch_repo(); tip = ls_remote(url)["refs/heads/feat/x"]; wrong = ls_remote(url)["refs/heads/main"]
        _, res = raw_receive(url, [(wrong, ZERO, "refs/heads/feat/x")])
        self.assertTrue(res.get("refs/heads/feat/x", "").startswith("ng"), f"delete with stale old accepted: {res}")
        self.assertEqual(ls_remote(url)["refs/heads/feat/x"], tip)
        _, res = raw_receive(url, [(tip, ZERO, "refs/heads/feat/x")]); self.assertEqual(res.get("refs/heads/feat/x"), "ok")
        self.assertNotIn("refs/heads/feat/x", ls_remote(url))

    def test_F5_stock_client_can_update_force_and_delete_existing_non_trunk_branch(self):
        """F5: a fresh stock clone must be able to update, force-update and delete a non-trunk branch (breaks trunk-only advertisement without a fallback)."""
        url, w = self._two_branch_repo()
        for name, g in CLIENTS.items():
            b = Work(self.tmp, f"b_{name}", g); git(["fetch", "-q", url, "feat/x"], cwd=b.dir, git_bin=g); b.git("checkout", "-q", "-B", "feat/x", "FETCH_HEAD")
            t1 = b.commit(f"{name}1.txt"); b.push(url, "feat/x"); self.assertEqual(ls_remote(url, g)["refs/heads/feat/x"], t1, f"{name}: update of existing non-trunk branch")
            b.git("reset", "-q", "--hard", "HEAD~1"); t2 = b.commit(f"{name}2.txt"); b.push(url, "+feat/x"); self.assertEqual(ls_remote(url, g)["refs/heads/feat/x"], t2, f"{name}: force update")
            d = clone(url, os.path.join(self.tmp, f"cl_{name}"), g); ok, err = fsck_ok(d, g); self.assertTrue(ok, err)
        b.git("push", url, ":feat/x"); self.assertNotIn("refs/heads/feat/x", ls_remote(url))

    def test_F3b_ack_then_immediate_kill_never_loses_acknowledged_push(self):
        """F3b: SIGKILL immediately after each acknowledgement (0-30 ms): the acknowledged tip must be the visible tip after restart."""
        rng = random.Random(seed()); url = self.repo(); w = self.work(); w.commit(); w.push(url, "main")
        for cycle in range(12):
            tip = w.commit(); w.push(url, "main")                 # acknowledged
            time.sleep(rng.uniform(0, 0.03)); self.srv.kill9(); self.srv.start()
            got = ls_remote(url)["refs/heads/main"]
            self.assertEqual(got, tip, f"cycle {cycle}: acknowledged tip {tip[:8]} lost after kill (found {got[:8]})")
        d = clone(url, os.path.join(self.tmp, "c")); ok, err = fsck_ok(d); self.assertTrue(ok, err)

    def test_F3a_kill_while_pack_body_is_in_flight(self):
        """F3a: kill the server after a measured part of the pack body has been sent (not before the transfer starts)."""
        url = self.repo(); w = self.work(); base = w.commit(); w.push(url, "main")
        with open(os.path.join(w.dir, "big.bin"), "wb") as f: f.write(os.urandom(24 * 1024 * 1024))
        w.git("add", "-A"); w.git("commit", "-q", "-m", "big")
        th = Throttle(self.srv.port, 3 * 1024 * 1024)
        p = subprocess.Popen([GIT, "push", f"http://127.0.0.1:{th.port}/r.git", "main"], cwd=w.dir, env=ENVBASE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        t0 = time.time()
        while th.sent < 6 * 1024 * 1024 and time.time() - t0 < 60 and p.poll() is None: time.sleep(0.05)
        self.assertGreaterEqual(th.sent, 6 * 1024 * 1024, "harness could not get 6 MB of the body to the server before killing")
        self.assertIsNone(p.poll(), "push finished before the kill: test not mid-transfer")
        self.srv.kill9(); p.wait(); th.close(); self.srv.start()
        self.assertEqual(ls_remote(url)["refs/heads/main"], base, "partial push must not move the ref")
        d = clone(url, os.path.join(self.tmp, "c")); ok, err = fsck_ok(d); self.assertTrue(ok, err)
        w.push(url, "main"); self.assertEqual(ls_remote(url)["refs/heads/main"], w.head())   # server fully usable afterwards

    def test_F4_unrelated_push_not_blocked_while_another_push_is_being_processed(self):
        """F4: lock held during server-side processing (after the body arrived) must not block a push to another branch. Needs /_stats.bytes_in."""
        url = self.repo(); w = self.work(); w.commit(); w.push(url, "main")
        fast = Work(self.tmp, "fast"); git(["fetch", "-q", url, "main"], cwd=fast.dir); fast.git("checkout", "-q", "-b", "fastbr", "FETCH_HEAD")
        fast.commit("f.txt"); t0 = time.time(); fast.push(url, "fastbr"); base = time.time() - t0
        heavy = Work(self.tmp, "heavy"); git(["fetch", "-q", url, "main"], cwd=heavy.dir); heavy.git("checkout", "-q", "-b", "heavybr", "FETCH_HEAD")
        N = int(os.environ.get("G_HEAVY_FILES", "60000"))
        for i in range(0, N, 1000):
            os.makedirs(os.path.join(heavy.dir, f"d{i // 1000}"), exist_ok=True)
            for j in range(1000): open(os.path.join(heavy.dir, f"d{i // 1000}", f"f{j}"), "w").write(f"{i + j}-{random.random()}")
        heavy.git("add", "-A"); heavy.git("commit", "-q", "-m", "heavy")
        s0 = self.srv.stats()["bytes_in"]; res = {}
        def hp(): t = time.time(); res["rc"] = heavy.push(url, "heavybr", check=False).returncode; res["dt"] = time.time() - t
        th = threading.Thread(target=hp); th.start()
        last, still, t1 = -1, 0, time.time()
        while th.is_alive() and time.time() - t1 < 120:                      # wait until the body has fully arrived (bytes_in plateaus)
            cur = self.srv.stats()["bytes_in"]
            if cur - s0 > 100_000 and cur == last: still += 1
            else: still = 0
            if still >= 3: break
            last = cur; time.sleep(0.15)
        fast.commit("f2.txt"); t2 = time.time(); fast.push(url, "fastbr"); dt = time.time() - t2; busy = th.is_alive()
        th.join(300)
        self.assertEqual(res.get("rc"), 0)
        if not busy: self.skipTest(f"heavy push finished (took {res['dt']:.1f}s) before the probe: inconclusive, increase G_HEAVY_FILES")
        self.assertLess(dt, max(3.0, base * 6 + 1.0), f"push to unrelated branch took {dt:.2f}s (baseline {base:.2f}s) while another push was being processed")

    def test_F10_tags_survive_maintenance_and_maintenance_concurrent_with_pushes(self):
        """F10: annotated tags (objects reachable only via tags) must survive maintain; maintain must be safe during pushes."""
        url = self.repo(); w = self.work(); [w.commit() for _ in range(3)]; w.push(url, "main")
        w.git("checkout", "-q", "-b", "side"); w.commit("t.txt", "only reachable via the tag\n"); w.git("tag", "-a", "rel", "-m", "release"); w.git("tag", "light")
        w.push(url, "rel", "light"); w.git("checkout", "-q", "main"); w.git("branch", "-D", "side")
        stop = threading.Event()
        def m():
            while not stop.is_set(): self.srv.maintain(); time.sleep(0.1)
        th = threading.Thread(target=m); th.start()
        try:
            for i in range(10): w.commit(); w.push(url, "main")
        finally: stop.set(); th.join(120)
        self.srv.maintain()
        d = clone(url, os.path.join(self.tmp, "c")); ok, err = fsck_ok(d); self.assertTrue(ok, err)
        self.assertEqual(sorted(git(["tag"], cwd=d).stdout.decode().split()), ["light", "rel"])
        self.assertEqual(git(["cat-file", "-t", "rel"], cwd=d).stdout.decode().strip(), "tag")

    def test_F11_atomic_push_is_atomic_if_advertised(self):
        """F11: if `atomic` is advertised, a multi-ref update with one stale old must apply NOTHING."""
        url, w = self._two_branch_repo()
        adv = urllib.request.urlopen(url + "/info/refs?service=git-receive-pack").read()
        if b" atomic" not in adv and b"atomic " not in adv: self.skipTest("atomic not advertised (allowed)")
        refs = ls_remote(url); main, fx = refs["refs/heads/main"], refs["refs/heads/feat/x"]
        w.git("checkout", "-q", "main"); n1 = w.commit("n1.txt"); n2 = w.commit("n2.txt"); pack = build_pack(w.dir, n2, main)
        _, res = raw_receive(url, [(main, n1, "refs/heads/main"), (ZERO, n2, "refs/heads/fx2")][:1] + [(main, n2, "refs/heads/feat/x")], pack, caps=" report-status atomic")
        # second command's old (main) is stale for feat/x => whole push must fail
        self.assertTrue(all(v.startswith("ng") for v in res.values()) or not res, f"atomic violated: {res}")
        after = ls_remote(url); self.assertEqual((after["refs/heads/main"], after["refs/heads/feat/x"]), (main, fx), "partial application under atomic")

    def test_F12_gzip_encoded_request_bodies_are_accepted(self):
        """F12: git gzips large upload-pack request bodies; the server must accept Content-Encoding: gzip."""
        url = self.repo(); w = self.work(); w.commit(); w.push(url, "main")
        body = b"0014command=ls-refs\n00010009peel\n0000"
        req = urllib.request.Request(url + "/git-upload-pack", data=gzip.compress(body), headers={"Content-Type": "application/x-git-upload-pack-request", "Content-Encoding": "gzip", "Git-Protocol": "version=2"})
        out = urllib.request.urlopen(req, timeout=30).read(); self.assertIn(b"refs/heads/main", out)

    def test_F26_clients_with_packfile_uris_enabled_still_work(self):
        """F26: stock clients configured with fetch.uriprotocols must clone/fetch (server must fall back or serve URIs correctly)."""
        url = self.repo(); w = self.work(); [w.commit() for _ in range(4)]; w.push(url, "main")
        for name, g in CLIENTS.items():
            d = os.path.join(self.tmp, f"u_{name}"); git(["-c", "protocol.version=2", "-c", "fetch.uriprotocols=http,https", "clone", "-q", url, d], git_bin=g)
            ok, err = fsck_ok(d, g); self.assertTrue(ok, err)
            w.commit(); w.push(url, "main"); git(["-c", "fetch.uriprotocols=http,https", "fetch", "-q", "origin"], cwd=d, git_bin=g)
            self.assertEqual(git(["rev-parse", "origin/main"], cwd=d, git_bin=g).stdout.decode().strip(), w.head())
