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

REVIEW-1 additions (copy; the sealed original is untouched):
  lostupdate_branch  lostupdate, but only on refs other than refs/heads/main (main keeps correct CAS)
  createoverwrite    old-oid 000..0 ("create") on an EXISTING ref overwrites it unconditionally (lost update)
  globalcommit       one global lock serialises ALL push processing (taken after the body is received)
  nodurable_fakepid  acknowledged data reverts to the creation snapshot on restart (as nodurable, but the
                     revert runs only after a successful bind) + run.sh writes a wrong PID to server.pid
  nodurable_late     nodurable with revert-after-bind and a correct PID (control for nodurable_fakepid)
  lazydurable        acknowledged ref updates younger than G_LAZY_MS (default 20) ms at crash time are lost
  gcnotags           `maintain` computes its GC root set from branches only: objects reachable only from
                     tags (incl. every annotated tag object) are pruned; tag refs then dangle
  atomicignored      advertises `atomic` but silently ignores it (multi-ref pushes applied non-atomically)
  foobar500          pushes to refs/heads/foo or refs/heads/bar fail with HTTP 500 after the 3rd one
  t500               pushes to refs/heads/t fail with HTTP 500 after the 5th one
  trunkonlyadv       push advertisement (info/refs?service=git-receive-pack) lists only refs/heads/main
                     (the brief's "Required design": advertise only the trunk)
  trunkonly_overwrite trunkonlyadv + createoverwrite (the only way a trunk-only server accepts stock topic updates)
  structgame         stock Git (loose objects, bare repo) disguised for tests_structural: object dir renamed
                     to odb/, verbatim copy of each pushed pack under packs/, /_stats faked (+1/+1 per push)
  nogzip             any request with Content-Encoding (gzip) is rejected with HTTP 415
  metaprobe          honest-looking instrumentation: G_META_READS_PER_REQ (default 3) metadata reads per
                     receive-pack POST + 1 write per accepted ref update; shows the >1 MB probe-RPC effect on S3
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


LAZY = float(os.environ.get("G_LAZY_MS", "20")) / 1000.0
JOURNAL = os.path.join(A.data, "journal"); HB = os.path.join(A.data, "hb")
counters = {"pushes_t": 0, "pushes_foobar": 0}


def git_env(repo=None):
    e = dict(os.environ); e.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_HTTP_EXPORT_ALL="1",
                                   GIT_PROJECT_ROOT=REPOS, REMOTE_USER="anon")
    if A.mutant == "structgame" and repo: e["GIT_OBJECT_DIRECTORY"] = os.path.join(repo, "odb")
    return e


def parse_cmds(body):
    cmds = []
    for (i, n) in pkts(body):
        head = body[i + 4:i + n].partition(b"\x00")[0].rstrip(b"\n").split(b" ", 2)
        if len(head) != 3 or len(head[0]) != 40: break
        cmds.append(tuple(x.decode() for x in head))
    return cmds


def pack_of(body):
    j = body.find(b"0000PACK")
    return body[j + 4:] if j >= 0 else b""


def filter_adv(resp):
    """Keep only refs/heads/main in a v0 receive-pack advertisement; move capabilities to the first kept line."""
    out, caps, refs, i = [], None, [], 0
    while i + 4 <= len(resp):
        n = int(resp[i:i + 4], 16)
        if n == 0: out.append(("flush", None)); i += 4; continue
        out.append(("pkt", resp[i + 4:i + n])); i += n
    res = b""; seen_service = False
    for kind, data in out:
        if kind == "flush": res += b"0000"; continue
        if data.startswith(b"# service="): res += _pk(data); seen_service = True; continue
        line, sep, c = data.partition(b"\x00")
        if sep: caps = c
        oid, _, ref = line.rstrip(b"\n").partition(b" ")
        if ref == b"refs/heads/main": refs.append((oid, ref))
    body = b""
    if refs:
        for k, (oid, ref) in enumerate(refs):
            body += _pk(oid + b" " + ref + ((b"\x00" + caps) if k == 0 and caps is not None else b"\n"))
    else:
        body += _pk(b"0" * 40 + b" capabilities^{}\x00" + (caps or b"\n"))
    head = _pk(b"# service=git-receive-pack\n") + b"0000" if seen_service else b""
    return head + body + b"0000"


def _pk(s): return b"%04x" % (len(s) + 4) + s


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
            with slock: d = dict(stats, metadata_reads=stats.get("metadata_reads", 0), metadata_writes=stats.get("metadata_writes", 0),
                                 packs_stored=len(os.listdir(os.path.join(A.data, "packs"))) if os.path.isdir(os.path.join(A.data, "packs")) else 0,
                                 cpu_seconds=ru.ru_utime + ru.ru_stime + rc.ru_utime + rc.ru_stime)
            return self.out(200, [("Content-Type", "application/json")], json.dumps(d).encode())
        is_push = path.endswith("git-receive-pack")
        is_push_post = is_push and self.command == "POST"
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
            if A.mutant == "nogzip" and self.headers.get("Content-Encoding"):
                with open(os.path.join(A.data, "gzip-rejected.log"), "a") as f: f.write(path + "\n")
                return self.out(415, [], b"gzip not supported")
            if is_push_post and A.mutant == "globalcommit":
                GLOBAL.acquire(); locked = True          # serialise all push processing after the body arrives
            tmpmap = {}
            hdrs_override = False
            if is_push_post and A.mutant in ("structgame", "metaprobe", "foobar500", "t500", "lazydurable"):
                if self.headers.get("Content-Encoding") == "gzip": body = gzip.decompress(body); hdrs_override = True
                cmds = parse_cmds(body)
                if A.mutant == "structgame" and cmds:
                    pk = pack_of(body)
                    if pk:
                        os.makedirs(os.path.join(A.data, "packs"), exist_ok=True)
                        import hashlib
                        with open(os.path.join(A.data, "packs", hashlib.sha1(pk).hexdigest() + ".pack"), "wb") as f: f.write(pk)
                    with slock: stats["metadata_reads"] = stats.get("metadata_reads", 0) + 1; stats["metadata_writes"] = stats.get("metadata_writes", 0) + 1
                if A.mutant == "metaprobe":
                    with slock: stats["metadata_reads"] = stats.get("metadata_reads", 0) + int(os.environ.get("G_META_READS_PER_REQ", "3"))
                if A.mutant == "foobar500" and any(r in ("refs/heads/foo", "refs/heads/bar") for _, _, r in cmds):
                    with slock: counters["pushes_foobar"] += 1; c = counters["pushes_foobar"]
                    if c > 3: return self.out(500, [], b"injected failure")
                if A.mutant == "t500" and any(r == "refs/heads/t" for _, _, r in cmds):
                    with slock: counters["pushes_t"] += 1; c = counters["pushes_t"]
                    if c > 5: return self.out(500, [], b"injected failure")
            if is_push and A.mutant in ("lostupdate", "deleteignore", "lostupdate_branch", "createoverwrite", "atomicignored", "trunkonly_overwrite") and self.command == "POST":
                zero = "0" * 40
                if self.headers.get("Content-Encoding") == "gzip": body = gzip.decompress(body)
                def fn(old, new, ref):
                    lu = A.mutant == "lostupdate" or (A.mutant == "lostupdate_branch" and ref != "refs/heads/main")
                    if A.mutant in ("createoverwrite", "trunkonly_overwrite") and old == zero and new != zero and ref_value(repo, ref) != zero: lu = True
                    if lu and new != zero and len(ref) >= 12:
                        tmp = "refs/m/" + "".join(rng.choice("0123456789abcdef") for _ in range(len(ref) - 7))
                        tmpmap[tmp] = (ref, new); return zero, new, tmp   # ingest under scratch ref; real ref is set last-writer-wins below
                    if A.mutant == "deleteignore" and new == zero: return old, old, ref
                    return old, new, ref
                body = rewrite_commands(body, fn)
                if A.mutant == "atomicignored": body = body.replace(b" atomic", b" xtomic", 1)   # same length: unknown cap ignored
                hdrs_override = True
            ck = (path, body) if (A.mutant in ("staleread", "backwards") and b"command=ls-refs" in body) else None
            if ck and ck in cache:
                lst = cache[ck]; now = time.time()
                if A.mutant == "staleread" and now - lst[-1][0] < 3: return self.out(200, lst[-1][2], lst[-1][1])
                if A.mutant == "backwards" and len(lst) > 1 and rng.random() < 0.3:
                    t, b, h = rng.choice(lst[:-1]); return self.out(200, h, b)
            env = git_env(repo)
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
            if A.mutant in ("trunkonlyadv", "trunkonly_overwrite") and self.command == "GET" and "service=git-receive-pack" in qs and code == 200:
                resp = filter_adv(resp)
            if is_push_post and A.mutant == "lazydurable" and code == 200:
                with slock:
                    with open(JOURNAL, "a") as f:
                        for o, n, r in cmds:
                            if (b"ok " + r.encode()) in resp: f.write(json.dumps({"t": time.time(), "ref": r, "old": o, "repo": repo}) + "\n")
            if is_push_post and A.mutant == "metaprobe" and code == 200:
                with slock: stats["metadata_writes"] = stats.get("metadata_writes", 0) + resp.count(b"ok refs/")
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


def recover_lazy():
    """lazydurable: updates acknowledged within LAZY seconds of the last heartbeat were 'not yet flushed'."""
    try: last = float(open(HB).read() or 0)
    except (OSError, ValueError): return
    try: ents = [json.loads(l) for l in open(JOURNAL) if l.strip()]
    except OSError: return
    lost = [e for e in ents if e["t"] > last - LAZY]
    for e in reversed(lost):
        if e["old"] == "0" * 40: subprocess.run([A.git, "-C", e["repo"], "update-ref", "-d", e["ref"]], capture_output=True)
        else: subprocess.run([A.git, "-C", e["repo"], "update-ref", e["ref"], e["old"]], capture_output=True)
    with open(os.path.join(A.data, "lazy-recovery.log"), "a") as f: f.write(json.dumps({"last_hb": last, "lost": lost}) + "\n")
    os.remove(JOURNAL)


def heartbeat():
    while True:
        try:
            with open(HB + ".tmp", "w") as f: f.write(repr(time.time()))
            os.replace(HB + ".tmp", HB)
        except OSError: pass
        time.sleep(0.002)


if __name__ == "__main__":
    srv = Srv(("127.0.0.1", A.port), H)        # bind first; a restart that fails to bind changes nothing
    if A.mutant in ("nodurable_fakepid", "nodurable_late") and os.path.isdir(SNAP):
        shutil.rmtree(REPOS); shutil.copytree(SNAP, REPOS)
    if A.mutant == "lazydurable":
        recover_lazy(); threading.Thread(target=heartbeat, daemon=True).start()
    if A.mutant == "fakepid_marker": pass
    with open(os.path.join(A.data, "real.pid"), "w") as f: f.write(str(os.getpid()))
    srv.serve_forever()
