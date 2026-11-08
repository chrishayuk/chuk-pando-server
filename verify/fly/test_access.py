"""Access control for an externally reachable deployment (docs/FLY-DEPLOYMENT-PLAN.md G2-G4), tested locally in the
configuration planned for Fly.io: TCP metadata with PANDO_META_SECRET, S3 over HTTPS (MinIO, test CA), Basic auth on
Git/admin endpoints, packfile URIs under each frontend's public URL, signed. Run: verify/fly/run.sh"""
import base64, json, os, shutil, subprocess, sys, tempfile, time, unittest, urllib.request, urllib.error
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dist"))
from distlib import Cluster, Server
from harness.lib import Work, git, fsck_ok

USER, PW = "demo", "s3cret-pass"
# OpenAI's Git client fork (github.com/openai/git), built locally; required, so a missing client can never silently
# shrink the client matrix (docs/VERIFY.md)
FORK_GIT = os.environ.get("FORK_GIT", "")
if not os.path.isfile(FORK_GIT):
    raise RuntimeError(f"FORK_GIT={FORK_GIT!r}: set FORK_GIT to a built openai/git client (docs/VERIFY.md)")


def events(srv):
    out = []
    for l in open(os.path.join(srv.data, "events", "events.jsonl")):
        try: e = json.loads(l)
        except ValueError: continue
        out.append(e.get("data", e))
    return out


def get(url, auth=None):
    r = urllib.request.Request(url)
    if auth: r.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    try:
        with urllib.request.urlopen(r, timeout=10) as x: return x.status, x.read()
    except urllib.error.HTTPError as e: return e.code, e.read()


class Access(unittest.TestCase):
    def setUp(self):
        env = {"PANDO_AUTH": f"{USER}:{PW}", "PANDO_URI_SECRET": "uri-signing-key", "PANDO_META_SECRET": "meta-key", "PANDO_URI_TTL": os.environ.get("G_URI_TTL", "3600")}
        self.c = Cluster(2, mode="tcp-s3tls", meta_env={"PANDO_META_SECRET": "meta-key"}, fe_env=env,
                         per_fe_env=lambda s: {"PANDO_PUBLIC_URL": f"http://127.0.0.1:{s.port}"})
        self.a, self.b = self.c.fe; self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: (self.c.cleanup(), shutil.rmtree(self.tmp, ignore_errors=True)))
        self.a.create("r")   # run.sh create-repo sends the credentials itself

    def url(self, s, creds=True): return s.url("r").replace("http://", f"http://{USER}:{PW}@") if creds else s.url("r")

    def test_unauthenticated_requests_are_refused(self):
        for u in (self.a.url("r") + "/info/refs?service=git-upload-pack", f"http://127.0.0.1:{self.a.port}/_stats"):
            self.assertEqual(get(u)[0], 401, u)
            self.assertEqual(get(u, f"{USER}:wrong")[0], 401, u)
        self.assertEqual(get(f"http://127.0.0.1:{self.a.port}/_stats", f"{USER}:{PW}")[0], 200)
        self.assertNotEqual(git(["ls-remote", self.url(self.a, creds=False)], check=False).returncode, 0)
        r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-X", "POST", f"http://127.0.0.1:{self.a.port}/_admin/maintain"], capture_output=True)
        self.assertEqual(r.stdout.decode(), "401")

    def test_authenticated_push_and_fetch_with_signed_uris_stock_and_fork(self):
        clients = [("stock", None)] + ([("fork", FORK_GIT)] if os.path.exists(FORK_GIT) else [])
        for name, g in clients:
            w = Work(self.tmp, f"w-{name}", g)
            for i in range(5):
                w.commit("f.txt", f"{name} {i} " + "z" * 3000 + "\n"); w.push(self.url(self.a if i % 2 else self.b), f"HEAD:refs/heads/{name}")
            self.a.maintain()
            for uris in (False, True):
                d = os.path.join(self.tmp, f"c-{name}-{uris}")
                git([*(["-c", "fetch.uriprotocols=http"] if uris else []), "clone", "-q", "-b", name, self.url(self.b), d], git_bin=g)
                ok, err = fsck_ok(d, g); self.assertTrue(ok, err)
                self.assertEqual(git(["rev-parse", "HEAD"], cwd=d).stdout.decode().strip(), w.head())
            fs = [e for e in events(self.b) if e.get("ev") == "fetch_served" and e.get("packfile_uris")][-1]
            self.assertTrue(fs["uri_packs"], f"{name}: no packfile URIs used: {fs}")

    def signed_uri(self):
        """A real packfile URI as the server advertises it (raw protocol-v2 fetch with packfile-uris)."""
        import http.client
        w = Work(self.tmp, "wu"); w.commit("f.txt", "y" * 5000 + "\n"); w.push(self.url(self.a), "main"); tip = w.head()
        def pkt(s): b = s.encode(); return b"%04x" % (len(b) + 4) + b
        body = pkt("command=fetch\n") + pkt("object-format=sha1\n") + b"0001" + pkt("ofs-delta\n") + pkt("no-progress\n") + pkt("packfile-uris http\n") + pkt(f"want {tip}\n") + pkt("done\n") + b"0000"
        cn = http.client.HTTPConnection("127.0.0.1", self.a.port, timeout=30)
        cn.request("POST", "/r.git/git-upload-pack", body, {"Git-Protocol": "version=2", "Content-Type": "application/x-git-upload-pack-request",
                                                           "Authorization": "Basic " + base64.b64encode(f"{USER}:{PW}".encode()).decode()})
        resp = cn.getresponse().read().decode("latin-1")
        uris = [l[l.index("http"):].strip() for l in resp.split("\n") if "/_cache/packs/" in l]
        self.assertTrue(uris, resp[:400]); return uris[0]

    def test_uri_signatures_bind_path_and_expiry(self):
        u = self.signed_uri()
        self.assertIn("sig=", u)
        self.assertEqual(get(u)[0], 200, "a valid signed URI must work without credentials")
        self.assertEqual(get(u.replace("sig=", "sig=00"))[0], 403)
        self.assertEqual(get(u.split("?")[0])[0], 403, "unsigned URI accepted")
        pid = u.split("/")[-1].split(".pack")[0]
        self.assertEqual(get(u.replace(pid, "0" * 40))[0], 403, "signature does not bind the path")


class MetaHandshake(unittest.TestCase):
    def test_frontend_with_wrong_or_missing_secret_cannot_use_the_metadata_service(self):
        c = Cluster(1, mode="tcp-s3tls", meta_env={"PANDO_META_SECRET": "right"}, fe_env={"PANDO_META_SECRET": "right"})
        try:
            for bad in ("wrong", ""):
                s = Server(extra_env={**c.env, "PANDO_META_SECRET": bad}, data_dir=os.path.join(c.base, f"bad-{bad or 'none'}"))
                p = s.run("start", check=False)
                self.assertNotEqual(p.returncode, 0, f"frontend with secret {bad!r} started")
                s.stop()
            c.fe[0].create("r")   # the right secret works
        finally:
            c.cleanup()


class UriExpiry(Access):
    """A real advertised URI with a 1 s lifetime: accepted at once, refused after it expires."""
    def setUp(self):
        os.environ["G_URI_TTL"] = "1"
        try: super().setUp()
        finally: del os.environ["G_URI_TTL"]

    def test_expired_uri_refused(self):
        u = self.signed_uri()
        self.assertEqual(get(u)[0], 200)
        time.sleep(2.2)
        self.assertEqual(get(u)[0], 403, "expired URI accepted")

    # the inherited Access tests are not re-run with a 1 s lifetime
    test_unauthenticated_requests_are_refused = None
    test_authenticated_push_and_fetch_with_signed_uris_stock_and_fork = None
    test_uri_signatures_bind_path_and_expiry = None


if __name__ == "__main__":
    unittest.main()
