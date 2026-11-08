"""CG4: concurrent independent branches; no global lock; litmus properties. Hidden."""
import os, socket, threading, time
from harness.lib import *

N = int(os.environ.get("G_N", "24"))


class Throttle:
    """TCP proxy that throttles client->server bytes (simulates a slow pusher)."""
    def __init__(self, target_port, bytes_per_s):
        self.t, self.bps = target_port, bytes_per_s
        self.sock = socket.socket(); self.sock.bind(("127.0.0.1", 0)); self.sock.listen(8); self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.loop, daemon=True).start()
    def loop(self):
        while True:
            try: c, _ = self.sock.accept()
            except OSError: return
            s = socket.create_connection(("127.0.0.1", self.t))
            threading.Thread(target=self.up, args=(c, s), daemon=True).start()
            threading.Thread(target=self.down, args=(s, c), daemon=True).start()
    def up(self, c, s):
        try:
            while True:
                d = c.recv(16384)
                if not d: break
                s.sendall(d); time.sleep(len(d) / self.bps)
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


class CG4(SrvCase):
    def test_independent_writers_all_succeed_and_are_visible(self):
        url = self.repo(); base = self.work("base"); base.commit(); base.push(url, "main"); errs = []; tips = {}
        def agent(i):
            try:
                w = Work(self.tmp, f"a{i}"); git(["fetch", "-q", url, "main"], cwd=w.dir); w.git("checkout", "-q", "-b", f"agent/{i}", "FETCH_HEAD")
                for k in range(4):
                    w.commit(f"a{i}.txt"); push_retry(w, url, f"agent/{i}")
                    if ls_remote(url)[f"refs/heads/agent/{i}"] != w.head(): errs.append((i, k, "not visible after ack"))
                tips[i] = w.head()
            except Exception as e: errs.append((i, str(e)[:300]))
        ts = [threading.Thread(target=agent, args=(i,)) for i in range(N)]; [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(errs, []); refs = ls_remote(url)
        for i, t in tips.items(): self.assertEqual(refs[f"refs/heads/agent/{i}"], t)
        d = clone(url, os.path.join(self.tmp, "fresh")); ok, err = fsck_ok(d); self.assertTrue(ok, err)

    def test_no_global_lock_slow_push_does_not_block_other_branch(self):
        url = self.repo(); w = self.work(); w.commit(); w.push(url, "main")
        slow = Work(self.tmp, "slow"); git(["fetch", "-q", url, "main"], cwd=slow.dir); slow.git("checkout", "-q", "-b", "slowbr", "FETCH_HEAD")
        with open(os.path.join(slow.dir, "big.bin"), "wb") as f: f.write(os.urandom(10 * 1024 * 1024))
        slow.git("add", "-A"); slow.git("commit", "-q", "-m", "big")
        th = Throttle(self.srv.port, 2 * 1024 * 1024); res = {}
        def slow_push():
            t0 = time.time(); p = subprocess.run([GIT, "push", f"http://127.0.0.1:{th.port}/r.git", "slowbr"], cwd=slow.dir, env=ENVBASE, capture_output=True)
            res["slow"] = (time.time() - t0, p.returncode)
        ts = threading.Thread(target=slow_push); ts.start(); time.sleep(1.0)   # slow push is now streaming its body
        fast = Work(self.tmp, "fast"); git(["fetch", "-q", url, "main"], cwd=fast.dir); fast.git("checkout", "-q", "-b", "fastbr", "FETCH_HEAD"); fast.commit("f.txt")
        t0 = time.time(); fast.push(url, "fastbr"); dt = time.time() - t0; still_running = ts.is_alive()
        ts.join(); th.close()
        self.assertTrue(still_running, "slow push finished before the fast one started; test inconclusive")
        self.assertLess(dt, 3.0, f"push to an unrelated branch took {dt:.1f}s while another push was in flight (global lock?)")
        self.assertEqual(res["slow"][1], 0)

    def test_per_ref_monotonic_no_dangling_under_concurrent_updates(self):
        url = self.repo(); w = self.work(); w.commit(); w.push(url, "main"); viol = []; stop = threading.Event(); depth = {}
        def writer(ref):
            x = Work(self.tmp, f"w_{ref}"); git(["fetch", "-q", url, "main"], cwd=x.dir); x.git("checkout", "-q", "-b", ref, "FETCH_HEAD")
            for i in range(20): x.commit(f"{ref}.txt", f"{i}\n"); x.push(url, ref)
        def reader(k):
            d = clone(url, os.path.join(self.tmp, f"r{k}")); seen = {}
            while not stop.is_set():
                for ref in ("foo", "bar"):
                    if f"refs/heads/{ref}" not in ls_remote(url): continue
                    p = git(["fetch", "origin", f"{ref}"], cwd=d, check=False)
                    if p.returncode: viol.append(("dangling/fetch failed", ref, p.stderr.decode()[-200:])); return
                    c = int(git(["rev-list", "--count", "FETCH_HEAD"], cwd=d).stdout.decode())
                    if c < seen.get(ref, 0): viol.append(("backwards", ref, seen[ref], c))
                    seen[ref] = c
        ws = [threading.Thread(target=writer, args=(r,)) for r in ("foo", "bar")]; rs = [threading.Thread(target=reader, args=(k,)) for k in range(3)]
        [t.start() for t in ws + rs]; [t.join() for t in ws]; stop.set(); [t.join() for t in rs]
        self.assertEqual(viol, [])
