"""Shared helpers for the audit reproducers. Uses the builders' cluster launcher (impl/verify/dist/distlib.py, imported
read-only) against the copy built in work/impl/server, plus a fault-injecting TCP proxy for the metadata protocol."""
import os, sys, socket, struct, threading, time, subprocess, json

NET = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ.setdefault("G_RUN_SH", os.path.join(NET, "work", "impl", "server", "run.sh"))
os.environ.setdefault("G_CLIENTS", "stock=" + os.path.join(NET, "clients", "git-stock"))
os.environ.setdefault("G_MINIO", os.path.join(NET, "clients", "minio"))
os.environ.setdefault("CARGO_HOME", os.path.join(NET, "impl", ".cargo-home"))
sys.path.insert(0, os.path.join(NET, "clients", "heldout"))
sys.path.insert(0, os.path.join(NET, "impl", "verify", "dist"))
from harness.lib import Server, Work, git, ls_remote, clone, fsck_ok, build_pack, raw_receive, free_port, ZERO  # noqa
import distlib  # noqa
from distlib import Cluster, Meta, Minio  # noqa

BIN = os.path.join(NET, "work", "impl", "server", "target", "release", "pando")


def frame(p):
    return struct.pack(">I", len(p)) + p


def put_bytes(b):
    return struct.pack(">I", len(b)) + b


def recv_exact(s, n):
    b = b""
    while len(b) < n:
        c = s.recv(n - len(b))
        if not c:
            raise EOFError
        b += c
    return b


def recv_frame(s):
    n = struct.unpack(">I", recv_exact(s, 4))[0]
    return recv_exact(s, n)


class MetaClient:
    """Speaks the metadata service frame protocol (meta.rs:616-622) directly."""
    def __init__(self, addr):
        if addr.startswith("tcp:"):
            h, p = addr[4:].rsplit(":", 1)
            self.s = socket.create_connection((h, int(p)))
        else:
            self.s = socket.socket(socket.AF_UNIX); self.s.connect(addr)

    def call(self, p):
        self.s.sendall(frame(p)); return recv_frame(self.s)

    def scan(self, prefix):
        r = self.call(b"S" + put_bytes(prefix.encode()))
        n = struct.unpack(">I", r[:4])[0]; i = 4; out = []
        for _ in range(n):
            kl = struct.unpack(">I", r[i:i + 4])[0]; k = r[i + 4:i + 4 + kl]; i += 4 + kl
            vl = struct.unpack(">I", r[i:i + 4])[0]; v = r[i + 4:i + 4 + vl]; i += 4 + vl
            out.append((k.decode(), v))
        return out

    def get(self, k):
        r = self.call(b"G" + put_bytes(k.encode()))
        if r[0] == 0:
            return None
        ver = struct.unpack(">Q", r[1:9])[0]; vl = struct.unpack(">I", r[9:13])[0]
        return ver, r[13:13 + vl]

    def changes(self, prefix, h):
        r = self.call(b"Q" + put_bytes(prefix.encode()) + struct.pack(">Q", h))
        st = struct.unpack(">Q", r[:8])[0]; n = struct.unpack(">I", r[8:12])[0]; i = 12; out = []
        for _ in range(n):
            lsn = struct.unpack(">Q", r[i:i + 8])[0]; i += 8
            kl = struct.unpack(">I", r[i:i + 4])[0]; k = r[i + 4:i + 4 + kl]; i += 4 + kl
            vl = struct.unpack(">I", r[i:i + 4])[0]; i += 4 + vl
            out.append((lsn, k.decode()))
        return st, out

    def commit(self, reads, writes):
        q = b"C" + struct.pack(">I", len(reads))
        for k, v in reads:
            q += put_bytes(k.encode()) + struct.pack(">Q", v)
        q += struct.pack(">I", len(writes))
        for k, v in writes:
            q += put_bytes(k.encode()) + (b"\x01" + put_bytes(v) if v is not None else b"\x00")
        r = self.call(q)
        return ("ok", struct.unpack(">Q", r[1:9])[0]) if r[0] == 0 else (("conflict",) if r[0] == 1 else ("err", r))


class MetaProxy:
    """TCP proxy between frontends and `meta-serve --listen tcp:`; understands frames so faults can be aimed at a
    particular request. Fault knobs (set from the test):
      drop_commit_reply: forward the next 'C' request, swallow its reply, close the client connection.
      blackout_on(pred, secs): when a request frame satisfies pred, close every connection and refuse new ones for secs."""
    def __init__(self, upstream_port):
        self.up = upstream_port
        self.port = free_port()
        self.lock = threading.Lock()
        self.conns = []
        self.drop_commit_reply = 0
        self.dropped = 0
        self.pred = None; self.secs = 0; self.black_until = 0; self.blackouts = 0
        self.log = []
        self.ls = socket.socket(); self.ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.ls.bind(("127.0.0.1", self.port)); self.ls.listen(256)
        threading.Thread(target=self._accept, daemon=True).start()

    @property
    def addr(self):
        return f"tcp:127.0.0.1:{self.port}"

    def blackout_on(self, pred, secs):
        self.pred, self.secs = pred, secs

    def _kill_all(self):
        with self.lock:
            for c in self.conns:
                for s in c:
                    try: s.shutdown(socket.SHUT_RDWR)
                    except OSError: pass
                    try: s.close()
                    except OSError: pass
            self.conns = []

    def _accept(self):
        while True:
            c, _ = self.ls.accept()
            if time.time() < self.black_until:
                c.close(); continue
            try:
                u = socket.create_connection(("127.0.0.1", self.up))
            except OSError:
                c.close(); continue
            with self.lock:
                self.conns.append((c, u))
            threading.Thread(target=self._pump, args=(c, u), daemon=True).start()

    def _pump(self, c, u):
        try:
            while True:
                req = recv_frame(c)
                op = req[:1]
                self.log.append((time.time(), op, req[1:80]))
                if self.pred and self.pred(req):
                    self.pred = None
                    self.black_until = time.time() + self.secs; self.blackouts += 1
                    self._kill_all(); return
                u.sendall(frame(req))
                rep = recv_frame(u)
                if op == b"C" and self.drop_commit_reply > 0 and getattr(self, "drop_skip", 0) > 0:
                    self.drop_skip -= 1
                elif op == b"C" and self.drop_commit_reply > 0:
                    self.drop_commit_reply -= 1; self.dropped += 1
                    for s in (c, u):
                        try: s.close()
                        except OSError: pass
                    return
                c.sendall(frame(rep))
        except (EOFError, OSError):
            for s in (c, u):
                try: s.close()
                except OSError: pass


def meta_tcp(base):
    m = Meta(base, tcp=True); m.start(); return m


def events(fe_data):
    """All JSON events written by a frontend (events/*.jsonl under its data dir)."""
    out = []
    d = os.path.join(fe_data, "events")
    if not os.path.isdir(d):
        return out
    for f in sorted(os.listdir(d)):
        with open(os.path.join(d, f), errors="replace") as fh:
            for l in fh:
                try: out.append(json.loads(l))
                except Exception: pass
    return out


def server_log(fe_data):
    try:
        return open(os.path.join(fe_data, "server.log"), errors="replace").read()
    except OSError:
        return ""


class Report:
    def __init__(self, name):
        self.name = name; self.fails = []; self.notes = []

    def check(self, cond, msg):
        print(("PASS " if cond else "FAIL ") + msg, flush=True)
        if not cond:
            self.fails.append(msg)

    def note(self, msg):
        print("NOTE " + msg, flush=True); self.notes.append(msg)

    def done(self):
        print(f"== {self.name}: {'DEFECT REPRODUCED' if self.fails else 'no defect observed'} ({len(self.fails)} failed checks)")
        sys.exit(1 if self.fails else 0)
