#!/usr/bin/env python3
"""Release smoke test of ONE packaged artifact, on the platform it was built for (docs/RELEASING.md).
Portable: macOS, Linux and Windows (Python 3.8+, stock Git on PATH). Nothing outside a fresh temporary directory
is touched: HOME/USERPROFILE and Git's global config point into it, and GIT_CONFIG_NOSYSTEM=1.

  smoke.py ARCHIVE [--sha256 FILE] [--port N] [--junit OUT.xml]
  smoke.py --bin PATH/TO/pando ...          (an installed binary instead of an archive, e.g. after an installer)

Checks, in order (each one is a JUnit test case; the exit status is 0 only if all pass):
  checksum      the archive's SHA-256 equals its .sha256 file
  contents      the archive holds the binary, LICENSE, NOTICE, THIRD-PARTY-LICENSES.txt and README.md
  version       `pando version` names the crate version and this platform's target
  serve         `pando serve` starts on a free loopback port and prints its URLs
  port_in_use   a second `pando serve` on the same port fails instead of sharing it
  data_locked   a second `pando serve` on the same data directory (another port) fails: the data lock holds
  bind_refused_without_auth   `pando serve --bind 0.0.0.0` without PANDO_AUTH refuses to start, says how to set
                credentials, listens on nothing and creates no data directory
  bind_with_auth              with PANDO_AUTH it starts on 0.0.0.0 and requires HTTP Basic (401 without, accepted with)
  push_clone    stock git: push (repository created on first push), clone, fsck, the clone has the pushed tip
  explain       `pando explain last-push` inside the checkout reports the recorded push
  explore       `pando explore history|covers|fetch` succeed, with the expected labels (--json)
  incremental   a second push, then an incremental fetch in the clone
  concurrent    8 concurrent pushes to 8 branches all succeed and are all visible
  crash_restart the server is killed hard (SIGKILL / TerminateProcess) and restarted on the same data:
                every acknowledged ref is intact, and pushing still works
  clean_stop    (Unix) SIGINT stops the server with status 0 and removes its pid file; (Windows) skipped: a
                console Ctrl-C cannot be delivered to a child of this script without also reaching the script
"""
import base64, hashlib, json, urllib.error, urllib.request, os, re, shutil, signal, socket, subprocess, sys, tarfile, tempfile, threading, time, zipfile
import xml.sax.saxutils as sx

WIN = os.name == "nt"
EXE = "pando.exe" if WIN else "pando"


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def wait_port(port, proc, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if proc.poll() is not None:
            return False
        try:
            socket.create_connection(("127.0.0.1", port), 0.3).close(); return True
        except OSError:
            time.sleep(0.1)
    return False


class Smoke:
    def __init__(self, args):
        self.tmp = tempfile.mkdtemp(prefix="pando-smoke-")
        self.results, self.procs = [], []
        self.args = args
        home = os.path.join(self.tmp, "home"); os.makedirs(home)
        gcfg = os.path.join(home, ".gitconfig")
        with open(gcfg, "w") as f:
            f.write("[user]\n\tname = Smoke Test\n\temail = smoke@example.invalid\n[init]\n\tdefaultBranch = main\n"
                    "[core]\n\tautocrlf = false\n[credential]\n\thelper =\n")
        self.env = dict(os.environ, HOME=home, USERPROFILE=home, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=gcfg,
                        GIT_TERMINAL_PROMPT="0", XDG_CONFIG_HOME=os.path.join(home, ".config"))
        for k in ("PANDO_AUTH", "PANDO_META", "PANDO_BLOBS", "PANDO_BIND", "PANDO_ALLOW_UNAUTHENTICATED_BIND", "GIT_DIR", "GIT_WORK_TREE"):
            self.env.pop(k, None)
        self.port = args.get("port") or free_port()
        self.data = os.path.join(self.tmp, "data")
        self.bin = os.path.abspath(args["bin"]) if args.get("bin") else None
        self.srv = None

    # -- helpers --------------------------------------------------------------------------------------------------
    def run(self, cmd, cwd=None, check=True, timeout=120):
        p = subprocess.run(cmd, cwd=cwd, env=self.env, capture_output=True, text=True, timeout=timeout)
        if check and p.returncode != 0:
            raise AssertionError(f"{' '.join(map(str, cmd))} -> {p.returncode}\nstdout: {p.stdout[-2000:]}\nstderr: {p.stderr[-2000:]}")
        return p

    def git(self, *a, cwd=None, check=True):
        return self.run(["git", *a], cwd=cwd, check=check)

    def pando(self, *a, cwd=None, check=True):
        return self.run([self.bin, *a], cwd=cwd, check=check)

    def start(self, port=None, data=None):
        log = open(os.path.join(self.tmp, f"serve-{len(self.procs)}.log"), "w")
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WIN else {}
        p = subprocess.Popen([self.bin, "serve", "--port", str(port or self.port), "--data", data or self.data],
                             env=self.env, stdout=log, stderr=subprocess.STDOUT, **kw)
        p.log = log.name
        self.procs.append(p)
        return p

    def check(self, name, fn):
        t0 = time.time()
        try:
            fn(); self.results.append((name, None, time.time() - t0)); print(f"ok    {name}", flush=True)
        except Exception as e:  # noqa: BLE001 - every failure is reported, none is fatal to later checks
            msg = f"{type(e).__name__}: {e}"
            self.results.append((name, msg, time.time() - t0)); print(f"FAIL  {name}: {msg}", flush=True)

    def skip(self, name, why):
        self.results.append((name, "SKIP:" + why, 0.0)); print(f"skip  {name}: {why}", flush=True)

    def commit(self, wdir, fname, n, msg):
        with open(os.path.join(wdir, fname), "w", newline="\n") as f:
            f.write("".join(f"{msg} line {j}\n" for j in range(n)))
        self.git("add", "-A", cwd=wdir); self.git("commit", "-q", "-m", msg, cwd=wdir)
        return self.git("rev-parse", "HEAD", cwd=wdir).stdout.strip()

    # -- checks ---------------------------------------------------------------------------------------------------
    def t_checksum(self):
        a, s = self.args["archive"], self.args.get("sha256") or self.args["archive"] + ".sha256"
        want = open(s).read().split()[0].lower()
        got = hashlib.sha256(open(a, "rb").read()).hexdigest()
        assert got == want, f"sha256 {got} != {want} ({s})"

    def t_contents(self):
        a = self.args["archive"]; out = os.path.join(self.tmp, "x")
        if a.endswith(".zip"):
            with zipfile.ZipFile(a) as z:
                z.extractall(out)
        else:
            with tarfile.open(a) as t:
                t.extractall(out)
        found = {}
        for root, _, files in os.walk(out):
            for f in files:
                found[f] = os.path.join(root, f)
        for need in (EXE, "LICENSE", "NOTICE", "THIRD-PARTY-LICENSES.txt", "README.md"):
            assert need in found, f"{need} missing from the archive (has: {sorted(found)})"
        self.bin = found[EXE]
        if not WIN:
            assert os.access(self.bin, os.X_OK), "binary is not executable"

    def t_version(self):
        out = self.pando("version").stdout
        m = re.match(r"pando (\d+\.\d+\.\d+\S*) \(commit (\S+), (\S+)\)", out)
        assert m, f"unexpected version output {out!r}"
        if self.args.get("target"):
            assert m.group(3) == self.args["target"], f"binary is for {m.group(3)}, expected {self.args['target']}"
        self.version_line = out.strip()

    def t_serve(self):
        self.srv = self.start()
        assert wait_port(self.port, self.srv), "server did not start: " + open(self.srv.log).read()[-2000:]
        time.sleep(0.3)
        log = open(self.srv.log).read()
        assert f"localhost:{self.port}" in log, f"serve banner missing: {log!r}"

    def t_port_in_use(self):
        p = self.start(data=os.path.join(self.tmp, "data-other"))
        try:
            rc = p.wait(20)
        except subprocess.TimeoutExpired:
            p.kill(); raise AssertionError("a second server bound the same port (it should have failed)")
        assert rc != 0, f"second server exited {rc}"

    def t_data_locked(self):
        p = self.start(port=free_port())
        try:
            rc = p.wait(20)
        except subprocess.TimeoutExpired:
            p.kill(); raise AssertionError("a second server opened the same data directory")
        assert rc != 0 and "locked" in open(p.log).read(), open(p.log).read()[-1000:]

    def t_bind_refused_without_auth(self):
        port, data = free_port(), os.path.join(self.tmp, "data-bind")
        env = dict(self.env); env.pop("PANDO_AUTH", None); env.pop("PANDO_ALLOW_UNAUTHENTICATED_BIND", None)
        p = subprocess.run([self.bin, "serve", "--port", str(port), "--bind", "0.0.0.0", "--data", data], env=env,
                           capture_output=True, text=True, timeout=30)
        out = p.stdout + p.stderr
        assert p.returncode != 0, f"started without credentials (exit {p.returncode}): {out[-500:]}"
        assert "PANDO_AUTH" in out and "refusing" in out and "Loopback" in out, out[-800:]
        assert not os.path.exists(data), "the refused server created its data directory"
        try:
            socket.create_connection(("127.0.0.1", port), 0.3).close(); raise AssertionError("something listens on the refused port")
        except OSError:
            pass

    def t_bind_with_auth(self):
        port, data = free_port(), os.path.join(self.tmp, "data-auth")
        env = dict(self.env, PANDO_AUTH="smoke:s3cret-pass")
        log = open(os.path.join(self.tmp, "serve-auth.log"), "w")
        p = subprocess.Popen([self.bin, "serve", "--port", str(port), "--bind", "0.0.0.0", "--data", data], env=env,
                             stdout=log, stderr=subprocess.STDOUT)
        self.procs.append(p)
        try:
            assert wait_port(port, p), "did not start with PANDO_AUTH: " + open(log.name).read()[-800:]
            url = f"http://127.0.0.1:{port}/x/info/refs?service=git-upload-pack"
            try:
                urllib.request.urlopen(url, timeout=10); raise AssertionError("an unauthenticated request was accepted")
            except urllib.error.HTTPError as e:
                assert e.code == 401, f"without credentials: HTTP {e.code}, expected 401"
            req = urllib.request.Request(url, headers={"Authorization": "Basic " + base64.b64encode(b"smoke:s3cret-pass").decode()})
            try:
                code = urllib.request.urlopen(req, timeout=10).status
            except urllib.error.HTTPError as e:
                code = e.code
            assert code != 401, "the configured credentials were rejected"
        finally:
            p.kill(); p.wait(10)

    def t_push_clone(self):
        self.url = f"http://127.0.0.1:{self.port}/demo"
        self.w = os.path.join(self.tmp, "w"); os.makedirs(self.w)
        self.git("init", "-q", cwd=self.w)
        for i in range(5):
            self.tip = self.commit(self.w, "f.txt", (i + 1) * 100, f"v{i}")
            if i == 0:
                self.git("remote", "add", "origin", self.url, cwd=self.w)
                p = self.git("push", "-u", "origin", "main", cwd=self.w)
                assert re.search(r"pando: operation [0-9a-f]{32}", p.stderr), f"no operation token: {p.stderr!r}"
            else:
                self.git("push", "origin", "main", cwd=self.w)
        self.c = os.path.join(self.tmp, "c")
        self.git("clone", "-q", self.url, self.c)
        self.git("fsck", "--full", "--strict", cwd=self.c)
        got = self.git("rev-parse", "HEAD", cwd=self.c).stdout.strip()
        assert got == self.tip, f"clone HEAD {got} != pushed {self.tip}"

    def t_explain(self):
        time.sleep(0.5)  # operation records are written just after the acknowledgement
        out = self.pando("explain", "last-push", cwd=self.w).stdout
        assert "RECORDED" in out and "now version 5" in out, out
        j = json.loads(self.pando("explain", "last-push", "--json", cwd=self.w).stdout)
        assert j["label"] == "recorded" and j["schema"].startswith("pando.explain/"), j

    def t_explore(self):
        h = json.loads(self.pando("explore", "history", "--json", cwd=self.w).stdout)
        assert h["label"] == "state" and len(h["data"]["history"]) == 5, h
        cv = json.loads(self.pando("explore", "covers", "--json", cwd=self.w).stdout)
        assert cv["label"] == "state", cv
        f = json.loads(self.pando("explore", "fetch", "--from", "empty", "--json", cwd=self.w).stdout)
        assert f["label"] == "hypothetical" and f["data"]["summary"]["packs"] >= 1, f
        for v in ("history", "covers", "fetch"):
            assert self.pando("explore", v, cwd=self.w).stdout.strip(), f"explore {v}: empty output"
        assert self.pando("status", cwd=self.w).stdout.strip()

    def t_incremental(self):
        self.tip = self.commit(self.w, "g.txt", 50, "v5")
        self.git("push", "origin", "main", cwd=self.w)
        self.git("fetch", "-q", "origin", cwd=self.c)
        got = self.git("rev-parse", "origin/main", cwd=self.c).stdout.strip()
        assert got == self.tip, f"fetched {got} != {self.tip}"
        self.git("fsck", "--full", cwd=self.c)

    def t_concurrent(self):
        tips, errs = {}, []
        for i in range(8):
            d = os.path.join(self.tmp, f"b{i}")
            self.git("clone", "-q", self.url, d)
            self.git("checkout", "-q", "-b", f"b{i}", cwd=d)
            tips[i] = (d, self.commit(d, f"b{i}.txt", 200, f"branch {i}"))

        def push(i):
            try:
                self.git("push", "-q", "origin", f"b{i}", cwd=tips[i][0])
            except Exception as e:  # noqa: BLE001
                errs.append(f"b{i}: {e}")
        th = [threading.Thread(target=push, args=(i,)) for i in range(8)]
        [t.start() for t in th]; [t.join(180) for t in th]
        assert not errs, errs
        refs = dict(reversed(l.split("\t")) for l in self.git("ls-remote", self.url).stdout.splitlines())
        for i in range(8):
            assert refs.get(f"refs/heads/b{i}") == tips[i][1], f"b{i}: {refs.get(f'refs/heads/b{i}')} != {tips[i][1]}"
        self.acked = refs

    def t_crash_restart(self):
        self.srv.kill(); self.srv.wait(30)   # SIGKILL on Unix, TerminateProcess on Windows
        self.srv = self.start()
        assert wait_port(self.port, self.srv), "restart failed: " + open(self.srv.log).read()[-2000:]
        refs = dict(reversed(l.split("\t")) for l in self.git("ls-remote", self.url).stdout.splitlines())
        for k, v in self.acked.items():
            if k.startswith("refs/heads/"):
                assert refs.get(k) == v, f"{k}: {refs.get(k)} after restart, acknowledged {v}"
        d = os.path.join(self.tmp, "c2"); self.git("clone", "-q", self.url, d); self.git("fsck", "--full", cwd=d)
        self.tip = self.commit(self.w, "h.txt", 10, "after restart")
        self.git("push", "origin", "main", cwd=self.w)

    def t_clean_stop(self):
        self.srv.send_signal(signal.SIGINT)
        rc = self.srv.wait(30)
        assert rc == 0, f"exit {rc}: {open(self.srv.log).read()[-1000:]}"
        assert not os.path.exists(os.path.join(self.data, "server.pid")), "pid file left behind"

    def main(self):
        try:
            if self.args.get("archive"):
                self.check("checksum", self.t_checksum)
                self.check("contents", self.t_contents)
            if not self.bin:
                print("no binary to test"); return 1
            self.check("version", self.t_version)
            self.check("serve", self.t_serve)
            if self.srv and self.srv.poll() is None:
                self.check("port_in_use", self.t_port_in_use)
                self.check("data_locked", self.t_data_locked)
                self.check("bind_refused_without_auth", self.t_bind_refused_without_auth)
                self.check("bind_with_auth", self.t_bind_with_auth)
                self.check("push_clone", self.t_push_clone)
                self.check("explain", self.t_explain)
                self.check("explore", self.t_explore)
                self.check("incremental", self.t_incremental)
                self.check("concurrent", self.t_concurrent)
                self.check("crash_restart", self.t_crash_restart)
                if WIN:
                    self.skip("clean_stop", "console Ctrl-C cannot be sent to a child alone on Windows")
                else:
                    self.check("clean_stop", self.t_clean_stop)
        finally:
            for p in self.procs:
                if p.poll() is None:
                    p.kill()
                    try:
                        p.wait(10)
                    except Exception:  # noqa: BLE001
                        pass
            self.report()
            shutil.rmtree(self.tmp, ignore_errors=True)
        return 0 if all(m is None or m.startswith("SKIP:") for _, m, _ in self.results) else 1

    def report(self):
        fails = [n for n, m, _ in self.results if m and not m.startswith("SKIP:")]
        print(f"smoke: {len(self.results) - len(fails)} of {len(self.results)} passed or skipped"
              + (f"; FAILED: {', '.join(fails)}" if fails else ""), flush=True)
        if self.args.get("junit"):
            cases = []
            for n, m, t in self.results:
                body = "" if m is None else (f'<skipped message="{sx.escape(m[5:], {chr(34): "&quot;"})}"/>' if m.startswith("SKIP:")
                                             else f'<failure message="failed">{sx.escape(m)}</failure>')
                cases.append(f'<testcase classname="release.smoke" name="{n}" time="{t:.2f}">{body}</testcase>')
            nf = len(fails); ns = sum(1 for _, m, _ in self.results if m and m.startswith("SKIP:"))
            with open(self.args["junit"], "w") as f:
                f.write(f'<?xml version="1.0"?>\n<testsuite name="release-smoke" tests="{len(cases)}" failures="{nf}" '
                        f'errors="0" skipped="{ns}">\n' + "\n".join(cases) + "\n</testsuite>\n")


def parse(argv):
    a, i = {}, 0
    while i < len(argv):
        k = argv[i]
        if k in ("--sha256", "--port", "--junit", "--bin", "--target"):
            a[k[2:]] = int(argv[i + 1]) if k == "--port" else argv[i + 1]; i += 2
        else:
            a["archive"] = k; i += 1
    return a


if __name__ == "__main__":
    sys.exit(Smoke(parse(sys.argv[1:])).main())
