"""CG2: atomic ref updates, race resolution, read-your-own-write, crash safety. Hidden."""
import os, random, threading, time
from harness.lib import *


class CG2(SrvCase):
    def test_same_ref_race_exactly_one_wins(self):
        url = self.repo(); w = self.work(); base = w.commit(); w.push(url, "main")
        for rnd in range(10):
            base = w.head(); cands = []
            for k in range(6):
                w.git("checkout", "-q", "-B", f"c{k}", base); sha = w.commit(f"r{rnd}k{k}.txt", f"{rnd}/{k}\n"); cands.append((sha, build_pack(w.dir, sha, base)))
            w.git("checkout", "-q", "main", "-f"); w.git("reset", "-q", "--hard", base)
            bar = threading.Barrier(len(cands)); out = [None] * len(cands)
            def go(i):
                bar.wait(); out[i] = raw_receive(url, [(base, cands[i][0], "refs/heads/main")], cands[i][1])
            ts = [threading.Thread(target=go, args=(i,)) for i in range(len(cands))]; [t.start() for t in ts]; [t.join() for t in ts]
            winners = [i for i, o in enumerate(out) if o[1].get("refs/heads/main") == "ok"]
            self.assertEqual(len(winners), 1, f"round {rnd}: winners={len(winners)} results={[o[1] for o in out]}")
            self.assertEqual(ls_remote(url)["refs/heads/main"], cands[winners[0]][0])
            w.git("fetch", "-q", url, "main"); w.git("reset", "-q", "--hard", "FETCH_HEAD")

    def test_ack_implies_visible_to_new_reader(self):
        url = self.repo(); w = self.work(); w.commit(); w.push(url, "main")
        for i in range(30):
            tip = w.commit(); w.push(url, "main")
            self.assertEqual(ls_remote(url)["refs/heads/main"], tip, f"push {i} acked but not visible")
        d = clone(url, os.path.join(self.tmp, "c")); self.assertEqual(git(["rev-parse", "HEAD"], cwd=d).stdout.decode().strip(), tip)

    def test_crash_durability_and_recovery(self):
        url = self.repo(); w = self.work(); w.commit(); w.push(url, "main"); rng = random.Random(7)
        acked = [w.head()]
        for cycle in range(6):
            inflight = []; stop = threading.Event()
            def pusher():
                while not stop.is_set():
                    sha = w.commit(); inflight.append(sha)
                    try: w.push(url, "main"); acked.append(sha); inflight.clear()
                    except Exception: return
            t = threading.Thread(target=pusher); t.start(); time.sleep(rng.uniform(0.15, 0.9)); self.srv.kill9(); stop.set(); t.join()
            self.srv.start()
            tip = ls_remote(url)["refs/heads/main"]
            allowed = {acked[-1], *inflight}
            self.assertIn(tip, allowed, f"cycle {cycle}: ref {tip[:8]} not last-acked {acked[-1][:8]} or in-flight; acknowledged push lost or ref corrupt")
            d = os.path.join(self.tmp, f"v{cycle}"); clone(url, d); ok, err = fsck_ok(d); self.assertTrue(ok, f"cycle {cycle}: fsck failed after recovery: {err}")
            for a in acked[-3:]:  # every acknowledged commit still reachable
                self.assertEqual(git(["cat-file", "-t", a], cwd=d, check=False).stdout.decode().strip(), "commit", f"acked commit {a[:8]} lost")
            w.git("fetch", "-q", url, "main"); w.git("reset", "-q", "--hard", "FETCH_HEAD"); acked.append(tip)

    def test_crash_during_large_push_leaves_consistent_repo(self):
        url = self.repo(); w = self.work(); base = w.commit(); w.push(url, "main")
        with open(os.path.join(w.dir, "big.bin"), "wb") as f: f.write(os.urandom(60 * 1024 * 1024))
        w.git("add", "-A"); w.git("commit", "-q", "-m", "big")
        p = subprocess.Popen([GIT, "push", url, "main"], cwd=w.dir, env=ENVBASE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.4); self.srv.kill9(); p.wait(); self.srv.start()
        tip = ls_remote(url)["refs/heads/main"]; self.assertIn(tip, {base, w.head()})
        d = os.path.join(self.tmp, "c"); clone(url, d); ok, err = fsck_ok(d); self.assertTrue(ok, err)
