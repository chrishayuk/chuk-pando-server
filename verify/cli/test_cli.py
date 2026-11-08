"""Client-side CLI slice (docs/CLI-DESIGN.md): pando status, explore fetch, explain last-push, and the diagnostic
boundary they use. Run: verify/cli/run.sh"""
import json, os, re, shutil, socket, subprocess, sys, tempfile, time, unittest, urllib.request, urllib.error
from harness.lib import Server, Work, git, GIT

RUN_SH = os.environ["G_RUN_SH"]
# G_PANDO_BIN: the CLI binary under test, e.g. one extracted from a packaged release archive (the server is G_RUN_SH's)
BIN = os.environ.get("G_PANDO_BIN") or os.path.join(os.path.dirname(RUN_SH), "target", "release", "pando")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dist"))


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1)); return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cli-")
        # an unauthenticated server on a non-loopback address, to test what a non-loopback caller can see: since 0.1.0
        # serve refuses that without PANDO_AUTH, so this test uses the explicit override (test networks only)
        self.srv = Server(extra_env={"PANDO_BIND": "0.0.0.0", "PANDO_ALLOW_UNAUTHENTICATED_BIND": "1"}); self.srv.start(); self.srv.create("demo")
        self.addCleanup(lambda: (self.srv.cleanup(), shutil.rmtree(self.tmp, ignore_errors=True)))
        self.url = self.srv.url("demo")
        self.w = Work(self.tmp, "w"); self.w.git("remote", "add", "origin", self.url)
        self.tokens = []
        for i in range(8):
            open(os.path.join(self.w.dir, "f.txt"), "w").write("".join(f"line {j}\n" for j in range((i + 1) * 200)))
            self.w.git("add", "-A"); self.w.git("commit", "-q", "-m", f"v{i}")
            p = self.w.git("push", "origin", "main")
            self.tokens += re.findall(r"pando: operation ([0-9a-f]{32})", p.stderr.decode())
        time.sleep(0.3)   # operation records are written just after the acknowledgement

    def pando(self, *args, cwd=None, check=True):
        p = subprocess.run([BIN, *args], cwd=cwd or self.w.dir, capture_output=True, text=True,
                           env=dict(os.environ, PATH=os.path.dirname(GIT) + os.pathsep + os.environ["PATH"]))
        if check:
            self.assertEqual(p.returncode, 0, p.stderr)
        return p

    def diag(self, what, host="127.0.0.1"):
        u = self.url.replace("127.0.0.1", host) + "/pando/v1/" + what
        try:
            r = urllib.request.urlopen(u, timeout=10); return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_every_push_gets_a_distinct_unguessable_token(self):
        self.assertEqual(len(self.tokens), 8, "each push should print its operation token")
        self.assertEqual(len(set(self.tokens)), 8)

    def test_status_and_explain_and_plan_end_to_end(self):
        st = json.loads(self.pando("status", "--json").stdout)
        self.assertEqual(st["refs"]["data"]["refs"][0]["version"], 8)
        out = self.pando("explain", "last-push").stdout
        self.assertIn("RECORDED", out); self.assertIn("now version 8", out); self.assertIn("refs/heads/main@7", out)
        plan = json.loads(self.pando("explore", "fetch", "--from", "empty", "--json").stdout)
        self.assertEqual(plan["label"], "hypothetical")
        self.assertLess(plan["data"]["summary"]["packs"], 8, "covers should beat one pack per push")
        # explaining a specific push by its token
        e = json.loads(self.pando("explain", "--op", self.tokens[2], "--json").stdout)
        self.assertEqual(e["label"], "recorded"); self.assertEqual(e["data"]["refs"][0]["version"], 3)

    def test_a_record_is_not_visible_to_another_principal_even_with_its_token(self):
        ip = lan_ip()
        if not ip:
            self.skipTest("no non-loopback address on this host")
        # the pushes above came from loopback, so their records belong to principal "local"
        code, _ = self.diag(f"ops/{self.tokens[0]}", host=ip)
        self.assertEqual(code, 404, "a non-loopback caller saw a local principal's operation")
        code, body = self.diag("ops/last", host=ip)
        self.assertEqual(code, 403, body)
        code, _ = self.diag(f"ops/{self.tokens[0]}")
        self.assertEqual(code, 200)

    def test_naming_a_commit_or_ref_grants_no_operation(self):
        tip = self.w.head()
        for what in (f"ops/{tip}", f"ops/{tip[:32]}", "ops/refs/heads/main", "ops/" + "0" * 32):
            code, _ = self.diag(what)
            self.assertEqual(code, 404, what)

    def test_responses_leak_no_internal_identifiers(self):
        self.srv.create("secret-other-repo")
        bodies = [json.dumps(self.diag(w)[1]) for w in ("capabilities", "refs", "plan?want=main", "ops/last", f"ops/{self.tokens[-1]}")]
        bodies += [self.pando("status", "--json").stdout, self.pando("explore", "fetch", "--json").stdout]
        for b in bodies:
            # internal key separators and prefixes, commit LSNs, other repositories, the principal (as a key or its
            # value), object-store and internal endpoints
            for bad in ("\\u0001", '"lsn"', "optok", "opby", "secret-other-repo", '"principal"', '"local"', "s3", "127.0.0.1:", "_cache"):
                self.assertNotIn(bad, b, f"response contains {bad!r}: {b[:300]}")

    def test_diagnostics_write_nothing(self):
        wal = [os.path.join(self.srv.data, "meta", f) for f in os.listdir(os.path.join(self.srv.data, "meta")) if f.startswith("wal")]
        before = sum(os.path.getsize(f) for f in wal)
        for _ in range(3):
            self.pando("status"); self.pando("explore", "fetch", "--from", "empty"); self.pando("explain", "last-push")
        time.sleep(0.3)
        self.assertEqual(sum(os.path.getsize(f) for f in wal), before, "a diagnostic call wrote to the metadata store")

    def test_a_remote_that_is_not_pando_is_explained_gracefully(self):
        d = tempfile.mkdtemp(dir=self.tmp)
        p = subprocess.Popen([sys.executable, "-u", "-m", "http.server", "0", "--bind", "127.0.0.1"], cwd=d, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.addCleanup(p.kill)
        line = p.stdout.readline().decode(); port = re.search(r"port (\d+)", line).group(1)
        out = self.pando("status", "--remote", f"http://127.0.0.1:{port}/x")
        self.assertIn("is not a Pando server", out.stdout)

    def test_branch_fetch_counts_pushes_over_its_dependency_closure(self):
        """A branch built on main@8 with 2 pushes: a fresh clone of it depends on 10 pushes (8 of main, 2 of the
        branch); a client already at main@8 needs only the branch's 2. (The summary used to count only the branch's
        own versions, i.e. 2 even from empty.)"""
        self.w.git("checkout", "-q", "-b", "topic")
        for i in range(2):
            open(os.path.join(self.w.dir, "t.txt"), "w").write(f"topic {i}\n")
            self.w.git("add", "-A"); self.w.git("commit", "-q", "-m", f"t{i}"); self.w.git("push", "-q", "origin", "topic")
        empty = json.loads(self.pando("explore", "fetch", "--from", "empty", "--to", "topic", "--json").stdout)["data"]
        self.assertEqual(empty["summary"]["pushes_without_covers"], 10, empty["summary"])
        main_tip = self.w.git("rev-parse", "main").stdout.decode().strip()
        frm = json.loads(self.pando("explore", "fetch", "--from", main_tip, "--to", "topic", "--json").stdout)["data"]
        self.assertEqual(frm["summary"]["pushes_without_covers"], 2, frm["summary"])

    def test_explore_history_and_covers_show_committed_state(self):
        h = json.loads(self.pando("explore", "history", "--json").stdout)
        self.assertEqual(h["label"], "state"); self.assertEqual(h["data"]["versions"], 8)
        self.assertEqual([v["version"] for v in h["data"]["history"]], list(range(8, 0, -1)))
        self.assertEqual(h["data"]["history"][0]["depends_on"], ["refs/heads/main@7"])
        c = json.loads(self.pando("explore", "covers", "--json").stdout)
        self.assertEqual(c["label"], "state")
        self.assertTrue(any(x["level"] == 1 and x["pushes"] == 4 for x in c["data"]["covers"]), c)


class ServeDefaults(unittest.TestCase):
    def test_pando_serve_creates_a_repository_on_first_push_and_the_harness_server_does_not(self):
        tmp = tempfile.mkdtemp(prefix="sv-"); self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        p = subprocess.Popen([BIN, "serve", "--port", str(port), "--data", os.path.join(tmp, "d")], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.addCleanup(p.kill)
        banner = p.stdout.readline().decode()
        self.assertIn(f"serving http://localhost:{port}", banner)
        w = Work(tmp, "w"); w.commit()
        self.assertEqual(w.push(f"http://127.0.0.1:{port}/brand-new", "main", check=False).returncode, 0, "first push did not create the repository")
        r = git(["clone", "-q", f"http://127.0.0.1:{port}/never-pushed", os.path.join(tmp, "c")], check=False)
        self.assertNotEqual(r.returncode, 0, "a fetch created a repository")
        h = Server(); h.start(); self.addCleanup(h.cleanup)   # run.sh start: the harness interface, no autocreate
        self.assertNotEqual(w.push(h.url("brand-new"), "main", check=False).returncode, 0, "the harness server created a repository on push")
        for bad in ("a%2F..%2Fb", "_admin", "a%20b"):   # encoded: git would normalise a literal ../
            self.assertNotEqual(w.push(f"http://127.0.0.1:{port}/{bad}", "main", check=False).returncode, 0, bad)


USER, PW = "alice", "correct-horse-battery-9f3"


def tree_state(root):
    out = {}
    for d, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(d, f)
            try: st = os.stat(p); out[p] = (st.st_size, st.st_mtime_ns)
            except OSError: pass
    return out


class CliNetworked(unittest.TestCase):
    """The deployment planned for Fly, locally: TCP metadata with its secret, HTTPS object store (MinIO, test CA),
    Basic auth, two frontends. The client uses only a Git credential helper in a throw-away HOME, exactly as `git push`
    does; nothing touches the real user configuration."""
    def setUp(self):
        from distlib import Cluster
        env = {"PANDO_AUTH": f"{USER}:{PW}", "PANDO_URI_SECRET": "uri-key", "PANDO_META_SECRET": "meta-key"}
        self.c = Cluster(2, mode="tcp-s3tls", meta_env={"PANDO_META_SECRET": "meta-key"}, fe_env=env,
                         per_fe_env=lambda s: {"PANDO_PUBLIC_URL": f"http://127.0.0.1:{s.port}"})
        self.a, self.b = self.c.fe; self.tmp = tempfile.mkdtemp(prefix="clin-")
        self.addCleanup(lambda: (self.c.cleanup(), shutil.rmtree(self.tmp, ignore_errors=True)))
        self.a.create("demo")
        self.home = os.path.join(self.tmp, "home"); os.makedirs(self.home)
        self.creds = os.path.join(self.home, ".git-credentials")
        self.set_creds(PW)
        self.gitcfg = os.path.join(self.home, ".gitconfig")
        open(self.gitcfg, "w").write(f"[credential]\n\thelper = store --file={self.creds}\n[user]\n\tname = t\n\temail = t@x\n[init]\n\tdefaultBranch = main\n")
        self.env = {k: v for k, v in os.environ.items() if k in ("PATH", "TMPDIR", "LANG")}
        self.env.update(HOME=self.home, GIT_CONFIG_GLOBAL=self.gitcfg, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
        self.env["PATH"] = os.path.dirname(GIT) + os.pathsep + self.env.get("PATH", "")
        self.w = os.path.join(self.tmp, "w")
        self.g("init", "-q", self.w); self.g("remote", "add", "origin", self.a.url("demo"), cwd=self.w)
        self.tokens = []
        for i in range(4):
            open(os.path.join(self.w, "f.txt"), "w").write("".join(f"row {j}\n" for j in range((i + 1) * 300)))
            self.g("add", "-A", cwd=self.w); self.g("commit", "-q", "-m", f"v{i}", cwd=self.w)
            p = self.g("push", "origin", "main", cwd=self.w)
            self.tokens += re.findall(r"pando: operation ([0-9a-f]{32})", p.stderr.decode())
        time.sleep(0.5)

    def set_creds(self, pw):
        open(self.creds, "w").write("".join(f"http://{USER}:{pw}@127.0.0.1:{s.port}\n" for s in (self.a, self.b)))

    def g(self, *a, cwd=None):
        p = subprocess.run([GIT, *a], cwd=cwd, env=self.env, capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr.decode()[-400:]); return p

    def pando(self, *a, check=True, timeout=60):
        p = subprocess.run([BIN, *a], cwd=self.w, env=self.env, capture_output=True, text=True, timeout=timeout)
        if check: self.assertEqual(p.returncode, 0, p.stderr)
        return p

    def assert_graceful(self, p, *expect):
        self.assertNotEqual(p.returncode, 0)
        for bad in ("panicked", "RUST_BACKTRACE", PW, "wrong-pass"):
            self.assertNotIn(bad, p.stdout + p.stderr)
        for e in expect:
            self.assertIn(e, p.stdout + p.stderr)

    def test_plain_git_push_explained_through_the_other_frontend_with_gits_own_credentials(self):
        self.assertEqual(len(self.tokens), 4)
        before = tree_state(self.home); before_git = tree_state(os.path.join(self.w, ".git"))
        p = self.pando("explain", "last-push", "--remote", self.b.url("demo"), "--json")
        d = json.loads(p.stdout)["data"]
        self.assertEqual(d["refs"][0]["version"], 4, "the record made via frontend A is found via frontend B")
        st = self.pando("status"); fe = self.pando("explore", "fetch", "--from", "empty", "--remote", self.b.url("demo"))
        for out in (p, st, fe):
            self.assertNotIn(PW, out.stdout + out.stderr, "the credential was printed")
        self.assertEqual(tree_state(self.home), before, "pando wrote to HOME (credentials persisted?)")
        self.assertEqual(tree_state(os.path.join(self.w, ".git")), before_git, "pando wrote to the checkout")

    def test_tokens_appear_nowhere_on_the_server_side(self):
        self.assertEqual(len(self.tokens), 4, "no tokens captured: the check below would be vacuous")
        hits = []
        for d, _, fs in os.walk(self.c.base):
            for f in fs:
                try: b = open(os.path.join(d, f), "rb").read()
                except OSError: continue
                hits += [(f, t) for t in self.tokens if t.encode() in b]
        self.assertEqual(hits, [], "a raw operation token was stored or logged")

    def test_wrong_or_missing_credentials_fail_gracefully(self):
        self.set_creds("wrong-pass")
        self.assert_graceful(self.pando("status", check=False), "credentials")
        open(self.creds, "w").write("")
        self.assert_graceful(self.pando("explain", "last-push", check=False), "credentials")

    def test_malformed_and_unknown_tokens_fail_gracefully(self):
        for t in ("not-a-token", "0" * 32, self.tokens[0][::-1]):
            self.assert_graceful(self.pando("explain", "--op", t, check=False), "no such operation")

    def test_diagnostics_during_faults_fail_gracefully_and_write_nothing(self):
        wal = lambda: sum(os.path.getsize(os.path.join(self.c.meta.dir, f)) for f in os.listdir(self.c.meta.dir) if f.startswith("wal"))
        w0 = wal()
        self.c.meta.kill9()
        t = time.time(); p = self.pando("status", check=False, timeout=120)
        self.assert_graceful(p); self.assertLess(time.time() - t, 60, "a diagnostic hung on a metadata outage")
        self.c.meta.start()
        cold = self.c.add_frontend(); self.set_creds(PW)
        open(self.creds, "a").write(f"http://{USER}:{PW}@127.0.0.1:{cold.port}\n")
        self.c.minio.stop()
        p = self.pando("explore", "fetch", "--from", "empty", "--remote", cold.url("demo"), check=False, timeout=120)
        self.assert_graceful(p)
        self.c.minio.start()
        self.pando("explore", "fetch", "--from", "empty", "--remote", cold.url("demo"))
        self.assertEqual(wal(), w0, "diagnostics wrote to the metadata store")


if __name__ == "__main__":
    unittest.main()
