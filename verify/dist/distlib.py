"""Two-frontend deployment for the distributed tests (docs/DISTRIBUTED.md): one `pando meta-serve` process, one shared
pack store, N frontends started through the unchanged run.sh interface with PANDO_META / PANDO_BLOBS."""
import os, shutil, signal, subprocess, tempfile, time
from harness.lib import Server, RUN_SH, free_port

# G_DIST_MODE: unix-fs (reference: Unix socket + shared directory), tcp-fs, tcp-s3 (TCP metadata + MinIO packs),
# tcp-s3tls (as tcp-s3, MinIO over HTTPS with a self-signed certificate passed as PANDO_S3_CA)
MODE = os.environ.get("G_DIST_MODE", "unix-fs")
MINIO = os.environ.get("G_MINIO", "/opt/homebrew/bin/minio")
S3_KEY, S3_SECRET = "pando", "pando-dev-secret"


class Minio:
    """A private MinIO for one cluster (docs/NETWORK-STORAGE.md; version pinned by the Homebrew bottle).
    tls=True: HTTPS with a fresh self-signed certificate for 127.0.0.1 (IP SAN); `self.ca` is its PEM."""
    def __init__(self, base, tls=False):
        self.dir = os.path.join(base, "minio"); os.makedirs(self.dir, exist_ok=True); self.port = free_port(); self.p = None
        self.tls = tls; self.certs = os.path.join(base, "minio-certs"); self.ca = os.path.join(self.certs, "public.crt")
        if tls:
            # a throw-away CA and a leaf certificate for 127.0.0.1 signed by it (rustls rejects a CA used as end entity)
            os.makedirs(self.certs, exist_ok=True); d = self.certs; self.ca = os.path.join(d, "ca.crt")
            run = lambda *a: subprocess.run(["openssl", *a], check=True, capture_output=True)
            run("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "2", "-subj", "/CN=pando test CA",
                "-keyout", os.path.join(d, "ca.key"), "-out", self.ca)
            run("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-subj", "/CN=127.0.0.1",
                "-keyout", os.path.join(d, "private.key"), "-out", os.path.join(d, "leaf.csr"))
            ext = os.path.join(d, "leaf.ext"); open(ext, "w").write("subjectAltName=IP:127.0.0.1\nbasicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n")
            run("x509", "-req", "-in", os.path.join(d, "leaf.csr"), "-CA", self.ca, "-CAkey", os.path.join(d, "ca.key"), "-CAcreateserial",
                "-days", "2", "-extfile", ext, "-out", os.path.join(d, "public.crt"))
            for f in ("ca.key", "leaf.csr", "leaf.ext", "ca.srl"):
                if os.path.exists(os.path.join(d, f)): os.rename(os.path.join(d, f), os.path.join(base, f))   # MinIO reads every file in certs-dir
        self.url = f"{'https' if tls else 'http'}://127.0.0.1:{self.port}"

    def start(self):
        env = dict(os.environ, MINIO_ROOT_USER=S3_KEY, MINIO_ROOT_PASSWORD=S3_SECRET, MINIO_BROWSER="off")
        certs = ["--certs-dir", self.certs] if self.tls else []
        self.p = subprocess.Popen([MINIO, "server", "--quiet", *certs, "--address", f"127.0.0.1:{self.port}", "--console-address", f"127.0.0.1:{free_port()}", self.dir],
                                  env=env, stdout=subprocess.DEVNULL, stderr=open(self.dir + ".log", "ab"))
        deadline = time.time() + 20
        while subprocess.run(["curl", "-sf", *(["--cacert", self.ca] if self.tls else []), "-o", "/dev/null", self.url + "/minio/health/live"]).returncode:
            if time.time() > deadline: raise AssertionError("minio did not start")
            time.sleep(0.1)

    def curl(self, *a):
        return subprocess.run(["curl", "-s", *(["--cacert", self.ca] if self.tls else []), "--aws-sigv4", "aws:amz:us-east-1:s3", "--user", f"{S3_KEY}:{S3_SECRET}", *a], capture_output=True)

    def mkbucket(self, b):
        r = self.curl("-X", "PUT", "-o", "/dev/null", "-w", "%{http_code}", f"{self.url}/{b}")
        assert r.stdout.decode() in ("200", "409"), r

    def stop(self):
        if self.p and self.p.poll() is None:
            self.p.terminate(); self.p.wait()


class Meta:
    def __init__(self, base, env=None, tcp=False):
        self.env = dict(os.environ, **(env or {}))
        self.tcp_port = free_port() if tcp else None
        self.dir = os.path.join(base, "meta"); self.sock = os.path.join(base, "meta.sock")
        self.addr = f"tcp:127.0.0.1:{self.tcp_port}" if tcp else self.sock
        self.binary = os.path.join(os.path.dirname(RUN_SH), "target", "release", "pando"); self.p = None

    def start(self):
        listen = ["--listen", self.addr] if self.tcp_port else ["--sock", self.sock]
        self.p = subprocess.Popen([self.binary, "meta-serve", "--dir", self.dir, *listen], env=self.env,
                                  stdout=subprocess.DEVNULL, stderr=open(self.dir + ".log", "ab") if os.path.isdir(os.path.dirname(self.dir)) else None)
        deadline = time.time() + 10
        import socket
        while True:
            try:
                if self.tcp_port: socket.create_connection(("127.0.0.1", self.tcp_port), timeout=1).close(); break
                elif os.path.exists(self.sock): break
            except OSError: pass
            if time.time() > deadline: raise AssertionError("meta-serve did not start")
            time.sleep(0.02)
        time.sleep(0.05)

    def kill9(self):
        self.p.send_signal(signal.SIGKILL); self.p.wait()
        try: os.remove(self.sock)
        except OSError: pass

    def stop(self):
        if self.p and self.p.poll() is None:
            self.p.terminate(); self.p.wait()


class Cluster:
    def __init__(self, n=2, meta_env=None, fe_env=None, mode=None, per_fe_env=None):
        self.mode = mode or MODE
        self.base = tempfile.mkdtemp(prefix="dist-"); self.blobs = os.path.join(self.base, "blobs")
        self.meta = Meta(self.base, meta_env, tcp=self.mode.startswith("tcp")); self.meta.start()
        self.minio = None
        env = {"PANDO_META": self.meta.addr}
        if self.mode.endswith("s3") or self.mode.endswith("s3tls"):
            tls = self.mode.endswith("s3tls")
            self.minio = Minio(self.base, tls=tls); self.minio.start(); self.minio.mkbucket("pando")
            env.update(PANDO_S3=f"{self.minio.url}/pando", PANDO_S3_KEY=S3_KEY, PANDO_S3_SECRET=S3_SECRET)
            if tls: env["PANDO_S3_CA"] = self.minio.ca
        else:
            env["PANDO_BLOBS"] = self.blobs
        self.env = {**env, **(fe_env or {})}
        self.fe = [Server(extra_env=self.env, data_dir=os.path.join(self.base, f"fe{i}")) for i in range(n)]
        self.per_fe_env = per_fe_env
        for s in self.fe:
            if per_fe_env: s.env.update(per_fe_env(s))
            s.start()

    def add_frontend(self, extra=None):
        s = Server(extra_env={**self.env, **(extra or {})}, data_dir=os.path.join(self.base, f"fe{len(self.fe)}"))
        if self.per_fe_env: s.env.update(self.per_fe_env(s))
        s.start(); self.fe.append(s); return s

    @property
    def shared_dir(self):
        return not (self.mode.endswith("s3") or self.mode.endswith("s3tls"))

    def cleanup(self):
        for s in self.fe: s.stop()
        self.meta.stop()
        if self.minio: self.minio.stop()
        shutil.rmtree(self.base, ignore_errors=True)

class MetaFaultProxy:
    """TCP proxy in front of a meta-serve TCP port (protocol: u32 length + payload; op byte first) that can lose the
    next commit ('C') reply after forwarding the request (the commit applies, the frontend never hears back), or
    drop the next commit request without forwarding it (nothing applies). Either way the connection is closed.
    No handshake support: use without PANDO_META_SECRET."""
    def __init__(self, upstream_port):
        import socket, threading
        self.up = upstream_port; self.lose_reply = 0; self.drop_request = 0; self.events = []
        self.ls = socket.socket(); self.ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.ls.bind(("127.0.0.1", 0)); self.ls.listen(64); self.port = self.ls.getsockname()[1]
        self.addr = f"tcp:127.0.0.1:{self.port}"
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        import socket, threading
        while True:
            try: c, _ = self.ls.accept()
            except OSError: return
            u = socket.create_connection(("127.0.0.1", self.up))
            threading.Thread(target=self._pump, args=(c, u), daemon=True).start()

    @staticmethod
    def _frame(s):
        import struct
        h = b""
        while len(h) < 4:
            x = s.recv(4 - len(h))
            if not x: raise EOFError
            h += x
        n = struct.unpack(">I", h)[0]; b = b""
        while len(b) < n:
            x = s.recv(n - len(b))
            if not x: raise EOFError
            b += x
        return h + b

    def _pump(self, c, u):
        try:
            while True:
                req = self._frame(c)
                if req[4:5] == b"C" and self.drop_request > 0:
                    self.drop_request -= 1; self.events.append("dropped request"); break
                u.sendall(req); rep = self._frame(u)
                if req[4:5] == b"C" and self.lose_reply > 0:
                    self.lose_reply -= 1; self.events.append("lost reply"); break
                c.sendall(rep)
        except (EOFError, OSError):
            pass
        for x in (c, u):
            try: x.close()
            except OSError: pass

    def close(self):
        try: self.ls.close()
        except OSError: pass
