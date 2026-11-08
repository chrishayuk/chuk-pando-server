"""Hidden-suite harness (Teams C/D). Drives ANY server via campaign/INTERFACE.md. Never mounted into builder workspaces.

Env: G_RUN_SH (path to server/run.sh), G_CLIENTS ("name=/path/git,name2=/path2/git"), G_EXPECT ("behavioral" or "structural")
"""
import json, os, random, shutil, signal, socket, subprocess, tempfile, threading, time, urllib.request, unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RUN_SH = os.environ.get("G_RUN_SH", os.path.join(ROOT, "baselines-sealed", "server", "run.sh"))
DEFAULT_CLIENTS = "stock=" + os.path.join(ROOT, "g0", "vendor", "stock-install", "bin", "git") + ",apple=/usr/bin/git"
CLIENTS = dict(kv.split("=", 1) for kv in os.environ.get("G_CLIENTS", DEFAULT_CLIENTS).split(",") if "=" in kv)
GIT = list(CLIENTS.values())[0]
ENVBASE = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def git(args, cwd=None, git_bin=None, check=True, inp=None, env=None, timeout=300):
    e = dict(ENVBASE); e.update(env or {})
    p = subprocess.run([git_bin or GIT, *args], cwd=cwd, capture_output=True, input=inp, env=e, timeout=timeout)
    if check and p.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed rc={p.returncode}\n{p.stderr.decode(errors='replace')[-1500:]}")
    return p


class Server:
    def __init__(self, extra_env=None, data_dir=None):
        self.data = data_dir or tempfile.mkdtemp(prefix="gsrv-")
        self.port = free_port()
        keep = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER", "SHELL")}
        # Sanitised: never pass test identity (PYTEST_CURRENT_TEST), G_EXPECT, G_CLIENTS, G_RUN_SH, etc. to the server under test (REVIEW-1 F17).
        self.env = dict(keep, G_PORT=str(self.port), G_DATA_DIR=self.data, **(extra_env or {}))

    def run(self, *args, check=True):
        p = subprocess.run([RUN_SH, *args], env=self.env, capture_output=True, timeout=120)
        if check and p.returncode: raise AssertionError(f"run.sh {args}: {p.stderr.decode()[-800:]}")
        return p

    def start(self): self.run("start")
    def stop(self): self.run("stop", check=False)
    def create(self, name): self.run("create-repo", name)
    def maintain(self): self.run("maintain")
    def url(self, name): return f"http://127.0.0.1:{self.port}/{name}.git"

    def pid(self):
        try: return int(open(os.path.join(self.data, "server.pid")).read())
        except Exception: return None

    def kill9(self):
        """SIGKILL the server and VERIFY it died (REVIEW-1 F2). A wrong/foreign server.pid makes the test fail, never pass vacuously."""
        pid = self.pid()
        if not pid: raise AssertionError("server.pid missing or invalid: cannot kill the server, crash test would be vacuous")
        try: os.kill(pid, 0)
        except OSError: raise AssertionError(f"server.pid={pid} is not a live process before the kill")
        for f in (lambda: os.killpg(pid, signal.SIGKILL), lambda: os.kill(pid, signal.SIGKILL)):
            try: f(); break
            except Exception: pass
        deadline = time.time() + 5
        while time.time() < deadline:
            try: os.kill(pid, 0)
            except OSError: break
            time.sleep(0.05)
        else: raise AssertionError(f"pid {pid} still alive 5 s after SIGKILL")
        try:
            socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
            raise AssertionError(f"port {self.port} still accepts connections after killing pid {pid}: server.pid is not the listening process")
        except (ConnectionRefusedError, socket.timeout, OSError) as e:
            if isinstance(e, AssertionError): raise
        try: os.remove(os.path.join(self.data, "server.pid"))
        except OSError: pass

    def stats(self):
        return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/_stats", timeout=10).read())

    def cleanup(self):
        self.stop(); shutil.rmtree(self.data, ignore_errors=True)


class Work:
    """A local working clone with helpers."""
    def __init__(self, base, name="w", git_bin=None):
        self.dir = os.path.join(base, name); self.g = git_bin or GIT
        os.makedirs(self.dir, exist_ok=True)
        git(["init", "-q", "-b", "main"], cwd=self.dir, git_bin=self.g)
        self.n = 0

    def git(self, *a, **k): return git(list(a), cwd=self.dir, git_bin=self.g, **k)

    def commit(self, fname="f.txt", text=None, msg=None):
        self.n += 1
        text = text if text is not None else f"line {self.n} {random.random()}\n"
        os.makedirs(os.path.dirname(os.path.join(self.dir, fname)) or self.dir, exist_ok=True)
        with open(os.path.join(self.dir, fname), "a") as f: f.write(text)
        self.git("add", "-A"); self.git("commit", "-q", "-m", msg or f"c{self.n}")
        return self.git("rev-parse", "HEAD").stdout.decode().strip()

    def head(self): return self.git("rev-parse", "HEAD").stdout.decode().strip()
    def push(self, url, *refspec, check=True, extra=()): return self.git("push", *extra, url, *refspec, check=check)


def ls_remote(url, git_bin=None):
    out = git(["ls-remote", url], git_bin=git_bin).stdout.decode().split("\n")
    return {l.split("\t")[1]: l.split("\t")[0] for l in out if "\t" in l}


def clone(url, dest, git_bin=None, *extra):
    git(["clone", "-q", *extra, url, dest], git_bin=git_bin); return dest


def fsck_ok(d, git_bin=None):
    p = git(["fsck", "--full", "--strict"], cwd=d, git_bin=git_bin, check=False)
    return p.returncode == 0, p.stderr.decode()[-500:]


THREAD_ERRORS = []
def _hook(args):
    import traceback
    THREAD_ERRORS.append(f"{args.exc_type.__name__}: {args.exc_value}\n" + "".join(traceback.format_tb(args.exc_traceback))[-600:])
threading.excepthook = _hook   # uncaught exceptions in worker threads now FAIL the test (REVIEW-1 F9)

RETRIES = []
def push_retry(w, url, *refspec, tries=3, extra=()):
    """Operational failures (rejected/failed push with no ack) are retried and counted as a RELIABILITY metric; correctness
    assertions apply only to acknowledged operations (decision: correctness vs reliability)."""
    last = None
    for i in range(tries):
        p = w.push(url, *refspec, check=False, extra=extra)
        if p.returncode == 0:
            if i: RETRIES.append(i)
            return p
        last = p; time.sleep(0.2 * (i + 1))
    RETRIES.append(tries)
    raise AssertionError(f"push failed {tries} times: {last.stderr.decode(errors='replace')[-300:]}")


class SrvCase(unittest.TestCase):
    """Per-test fresh server; set SERVER_ENV on subclass or via G_MUTANT/G_PROFILE env."""
    def fresh(self):
        old = getattr(self, "srv", None)
        if old: old.cleanup(); shutil.rmtree(self.tmp, ignore_errors=True)
        self.srv = Server({k: v for k, v in os.environ.items() if k in ("G_MUTANT", "G_PROFILE", "G_SERVER_GIT")})
        self.tmp = tempfile.mkdtemp(prefix="gtest-"); self.srv.start()

    def setUp(self):
        THREAD_ERRORS.clear(); RETRIES.clear()
        self.fresh(); self.addCleanup(lambda: (self.srv.cleanup(), shutil.rmtree(self.tmp, ignore_errors=True)))
        def _alarm(*a): raise TimeoutError(f"test exceeded {os.environ.get('G_TEST_TIMEOUT', 420)} s (hung)")
        signal.signal(signal.SIGALRM, _alarm); signal.alarm(int(os.environ.get("G_TEST_TIMEOUT", "420"))); self.addCleanup(signal.alarm, 0)
        def _chk():
            if THREAD_ERRORS: raise AssertionError("uncaught exception in a worker thread: " + THREAD_ERRORS[0])
        self.addCleanup(_chk)
        def _rel():
            if RETRIES and os.environ.get("G_RELIABILITY_OUT"):
                open(os.environ["G_RELIABILITY_OUT"], "a").write(json.dumps({"test": self.id(), "retries": list(RETRIES)}) + "\n")
        self.addCleanup(_rel)

    def repo(self, name="r"): self.srv.create(name); return self.srv.url(name)
    def work(self, name="w", client=None): return Work(self.tmp, name, CLIENTS.get(client) if client else None)


def for_each_client(fn):
    """Decorator: run the test body once per configured client (subTest)."""
    def w(self):
        for i, (name, path) in enumerate(CLIENTS.items()):
            if i: self.fresh()
            with self.subTest(client=name):
                fn(self, path, name)
    w.__name__ = fn.__name__; w.__doc__ = fn.__doc__; return w


# ---------- raw receive-pack client (bypasses client-side checks; lets tests send arbitrary old-oid) ----------
def _pkt(s):
    s = s.encode() if isinstance(s, str) else s
    return b"%04x" % (len(s) + 4) + s


def build_pack(repo_dir, new, base=None, git_bin=None):
    inp = (new + "\n" + (f"^{base}\n" if base else "")).encode()
    return git(["pack-objects", "--revs", "--stdout", "-q"], cwd=repo_dir, git_bin=git_bin, inp=inp).stdout


def raw_receive(url, updates, pack=b"", timeout=60, caps=" report-status"):
    """updates: [(old,new,ref)]. Returns (unpack_status, {ref: 'ok'|'ng reason'})."""
    urllib.request.urlopen(url + "/info/refs?service=git-receive-pack", timeout=timeout).read()
    body = b""
    for i, (o, n, r) in enumerate(updates):
        body += _pkt(f"{o} {n} {r}" + ("\x00" + caps if i == 0 else "") + "\n")
    body += b"0000" + pack
    req = urllib.request.Request(url + "/git-receive-pack", data=body,
                                 headers={"Content-Type": "application/x-git-receive-pack-request"})
    try: resp = urllib.request.urlopen(req, timeout=timeout).read()
    except urllib.error.HTTPError as e: return f"http {e.code}", {r: "ng http" for _, _, r in updates}
    i, lines = 0, []
    while i + 4 <= len(resp):
        n = int(resp[i:i + 4], 16)
        if n == 0: i += 4; continue
        lines.append(resp[i + 4:i + n].decode(errors="replace").rstrip("\n")); i += n
    unpack = next((l for l in lines if l.startswith("unpack")), "unpack ?")
    res = {}
    for l in lines:
        if l.startswith("ok "): res[l[3:]] = "ok"
        elif l.startswith("ng "): res[l[3:].split(" ", 1)[0]] = "ng " + (l[3:].split(" ", 1) + [""])[1]
    return unpack, res


ZERO = "0" * 40
MATRIX_OUT = os.environ.get("G_MATRIX_OUT")


def record(feature, client, supported, note=""):
    if MATRIX_OUT:
        with open(MATRIX_OUT, "a") as f: f.write(json.dumps(dict(feature=feature, client=client, supported=supported, note=note)) + "\n")
