#!/usr/bin/env python3
"""Stock-Git reference server for Teams C/D. SEALED from builders.

Bridges HTTP to `git http-backend` (CGI). Profiles:
  b0: default Git configuration (conventional bare-repo serving)
  b1: tuned conventional configuration (see baselines-sealed/B1.md)
Mutants (--mutant NAME) inject specific faults, used ONLY to prove the hidden suites can fail:
  lostupdate   ref updates are last-writer-wins (no compare-and-swap): pack ingested under a scratch ref, target ref set unconditionally
  nodurable    acknowledged data is lost on restart (state reverts to creation snapshot)
  staleread    cache ls-refs responses for a few seconds (read-your-own-write violated)
  backwards    sometimes serve an OLDER ls-refs response (ref goes backwards)
  deleteignore acknowledge ref deletions without performing them
  missingobj   delete one loose object of every pushed tip (dangling dependency)
  globallock   hold one global lock for the whole duration of any push (incl. receiving the body)
"""
import argparse, gzip, json, os, random, resource, shutil, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ap = argparse.ArgumentParser()
ap.add_argument("--data", required=True); ap.add_argument("--port", type=int, required=True)
ap.add_argument("--profile", default="b0"); ap.add_argument("--mutant", default="")
ap.add_argument("--git", default=os.environ.get("G_SERVER_GIT", "git"))
A = ap.parse_args()
try: os.setsid()
except OSError: pass
REPOS = os.path.join(A.data, "repos"); os.makedirs(REPOS, exist_ok=True)
SNAP = os.path.join(A.data, "snapshot")
rng = random.Random(1234)
stats = {"requests": 0, "bytes_in": 0, "bytes_out": 0}
slock = threading.Lock(); GLOBAL = threading.Lock()
cache = {}  # (path, body) -> list of (t, response)

if A.mutant == "nodurable" and os.path.isdir(SNAP):  # revert to snapshot on every start
    shutil.rmtree(REPOS); shutil.copytree(SNAP, REPOS)


def git_env():
    e = dict(os.environ); e.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_HTTP_EXPORT_ALL="1",
                                   GIT_PROJECT_ROOT=REPOS, REMOTE_USER="anon")
    return e


def read_chunked(f):
    out = b""
    while True:
        n = int(f.readline().split(b";")[0].strip() or b"0", 16)
        if n == 0:
            f.readline(); return out
        out += f.read(n); f.readline()


def pkts(buf):
    i = 0; out = []
    while i + 4 <= len(buf):
        n = int(buf[i:i + 4], 16)
        if n < 4: break
        out.append((i, n)); i += n
    return out


def rewrite_commands(body, fn):
    """Apply fn(old,new,ref) -> (old,new) to every command pkt of a receive-pack request."""
    b = bytearray(body)
    for (i, n) in pkts(body):
        line = bytes(b[i + 4:i + n])
        head, sep, caps = line.partition(b"\x00")
        parts = head.rstrip(b"\n").split(b" ", 2)
        if len(parts) != 3 or len(parts[0]) != 40: break
        old, new, ref = fn(parts[0].decode(), parts[1].decode(), parts[2].decode())
        nl = (b" ".join([old.encode(), new.encode(), ref.encode()]) + sep + caps)
        if len(nl) != len(line) - (1 if head.endswith(b"\n") else 0) + 0 and not sep:
            pass
        b[i + 4:i + n] = nl + (b"\n" if head.endswith(b"\n") and not sep else b"")
    return bytes(b)


def ref_value(repo, ref):
    r = subprocess.run([A.git, "-C", repo, "rev-parse", "-q", "--verify", ref], capture_output=True)
    return r.stdout.decode().strip() or "0" * 40


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass

    def out(self, code, headers, body):
        self.send_response(code)
        for k, v in headers: self.send_header(k, v)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        with slock: stats["bytes_out"] += len(body)

    def handle_all(self):
        path, _, qs = self.path.partition("?")
        if path == "/_stats":
            ru = resource.getrusage(resource.RUSAGE_SELF); rc = resource.getrusage(resource.RUSAGE_CHILDREN)
            with slock: d = dict(stats, metadata_reads=0, metadata_writes=0, packs_stored=0,
                                 cpu_seconds=ru.ru_utime + ru.ru_stime + rc.ru_utime + rc.ru_stime)
            return self.out(200, [("Content-Type", "application/json")], json.dumps(d).encode())
        is_push = path.endswith("git-receive-pack")
        locked = False
        if is_push and A.mutant == "globallock":
            GLOBAL.acquire(); locked = True
        try:
            body = b""
            if self.command == "POST":
                if self.headers.get("Transfer-Encoding", "").lower() == "chunked": body = read_chunked(self.rfile)
                else: body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            with slock: stats["requests"] += 1; stats["bytes_in"] += len(body)
            repo = os.path.join(REPOS, path.lstrip("/").split(".git/")[0] + ".git")
            tmpmap = {}
            if is_push and A.mutant in ("lostupdate", "deleteignore"):
                zero = "0" * 40
                if self.headers.get("Content-Encoding") == "gzip": body = gzip.decompress(body)
                def fn(old, new, ref):
                    if A.mutant == "lostupdate" and new != zero and len(ref) >= 12:
                        tmp = "refs/m/" + "".join(rng.choice("0123456789abcdef") for _ in range(len(ref) - 7))
                        tmpmap[tmp] = (ref, new); return zero, new, tmp   # ingest under scratch ref; real ref is set last-writer-wins below
                    if A.mutant == "deleteignore" and new == zero: return old, old, ref
                    return old, new, ref
                body = rewrite_commands(body, fn)
                hdrs_override = True
            else: hdrs_override = False
            ck = (path, body) if (A.mutant in ("staleread", "backwards") and b"command=ls-refs" in body) else None
            if ck and ck in cache:
                lst = cache[ck]; now = time.time()
                if A.mutant == "staleread" and now - lst[-1][0] < 3: return self.out(200, lst[-1][2], lst[-1][1])
                if A.mutant == "backwards" and len(lst) > 1 and rng.random() < 0.3:
                    t, b, h = rng.choice(lst[:-1]); return self.out(200, h, b)
            env = git_env()
            env.update(REQUEST_METHOD=self.command, PATH_INFO=path, QUERY_STRING=qs,
                       CONTENT_TYPE=self.headers.get("Content-Type", ""), CONTENT_LENGTH=str(len(body)),
                       HTTP_GIT_PROTOCOL=self.headers.get("Git-Protocol", ""))
            if self.headers.get("Content-Encoding") and not hdrs_override: env["HTTP_CONTENT_ENCODING"] = self.headers["Content-Encoding"]
            p = subprocess.run([A.git, "http-backend"], input=body, capture_output=True, env=env)
            head, _, resp = p.stdout.partition(b"\r\n\r\n")
            if not _: head, _, resp = p.stdout.partition(b"\n\n")
            code, hdrs = 200, []
            for l in head.decode(errors="replace").splitlines():
                k, _, v = l.partition(":")
                if k.lower() == "status": code = int(v.strip().split()[0])
                elif k.lower() not in ("content-length",): hdrs.append((k, v.strip()))
            if ck:
                cache.setdefault(ck, []).append((time.time(), resp, hdrs))
                if A.mutant == "staleread" and len(cache[ck]) == 1: pass
            if tmpmap and code == 200:
                for tmp, (ref, new) in tmpmap.items():
                    if (b"ok " + tmp.encode()) in resp:
                        subprocess.run([A.git, "-C", repo, "update-ref", ref, new], capture_output=True)   # no old-value check: last writer wins
                        subprocess.run([A.git, "-C", repo, "update-ref", "-d", tmp], capture_output=True)
                    resp = resp.replace(tmp.encode(), ref.encode())
            if is_push and A.mutant == "missingobj" and code == 200: self.drop_object(repo)
            return self.out(code, hdrs, resp)
        finally:
            if locked: GLOBAL.release()

    def drop_object(self, repo):
        r = subprocess.run([A.git, "-C", repo, "for-each-ref", "--format=%(objectname)"], capture_output=True)
        for tip in r.stdout.decode().split():
            ob = subprocess.run([A.git, "-C", repo, "rev-list", "--objects", tip], capture_output=True).stdout.decode().split("\n")
            names = [o.split()[0] for o in ob if o.strip()]
            for oid in names[::-1]:
                fp = os.path.join(repo, "objects", oid[:2], oid[2:])
                if os.path.exists(fp): os.chmod(fp, 0o644); os.remove(fp); return

    do_GET = do_POST = handle_all


class Srv(ThreadingHTTPServer):
    request_queue_size = 1024
    daemon_threads = True


if __name__ == "__main__":
    Srv(("127.0.0.1", A.port), H).serve_forever()
