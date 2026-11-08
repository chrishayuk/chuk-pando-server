"""CG3 (behavioral): dependency/closure correctness for stock-client packs. Hidden. B0 must pass; structural checks are in tests_structural.py."""
import os, random, threading, time
from harness.lib import *


def count(d, ref, g=None): return int(git(["rev-list", "--count", ref], cwd=d, git_bin=g).stdout.decode().strip())


class CG3(SrvCase):
    def _push_branch(self, w, url, br, n):
        for _ in range(n): w.commit(f"{br.replace('/', '_')}.txt")
        w.push(url, f"{br}")

    def test_delete_recreate_unrelated_history(self):
        url = self.repo(); w = self.work(); [w.commit() for _ in range(3)]; w.push(url, "main")
        w.git("checkout", "-q", "-b", "topic"); [w.commit("t.txt") for _ in range(5)]; w.push(url, "topic")
        old = clone(url, os.path.join(self.tmp, "old"))
        w.git("push", url, ":topic")
        w.git("checkout", "-q", "--orphan", "topic2"); w.git("rm", "-rqf", "."); newtip = w.commit("u.txt", "unrelated\n")
        w.git("push", url, "topic2:topic")
        self.assertEqual(ls_remote(url)["refs/heads/topic"], newtip)
        git(["fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*"], cwd=old)
        self.assertEqual(git(["rev-parse", "origin/topic"], cwd=old).stdout.decode().strip(), newtip)
        d = clone(url, os.path.join(self.tmp, "fresh")); ok, err = fsck_ok(d); self.assertTrue(ok, err)
        self.assertEqual(count(d, "origin/topic"), 1)

    def test_force_push_drops_history_others_depend_on(self):
        url = self.repo(); w = self.work(); [w.commit() for _ in range(2)]; w.push(url, "main")
        w.git("checkout", "-q", "-b", "t"); [w.commit("t.txt") for _ in range(10)]; w.push(url, "t"); mid = w.git("rev-parse", "HEAD~5").stdout.decode().strip()
        viewer = clone(url, os.path.join(self.tmp, "viewer"))
        w.git("reset", "-q", "--hard", mid); [w.commit("alt.txt") for _ in range(3)]; w.push(url, "+t")
        git(["fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*"], cwd=viewer)
        self.assertEqual(git(["rev-parse", "origin/t"], cwd=viewer).stdout.decode().strip(), w.head())
        for d in (viewer, clone(url, os.path.join(self.tmp, "fresh"))):
            ok, err = fsck_ok(d); self.assertTrue(ok, err); self.assertEqual(count(d, "origin/t"), 2 + 5 + 3)

    def test_stacked_branches_then_delete_base_branch(self):
        url = self.repo(); w = self.work(); [w.commit() for _ in range(3)]; w.push(url, "main")
        w.git("checkout", "-q", "-b", "b1"); self._push_branch(w, url, "b1", 3)
        w.git("checkout", "-q", "-b", "b2"); self._push_branch(w, url, "b2", 3)
        w.git("checkout", "-q", "-b", "b3"); self._push_branch(w, url, "b3", 2)
        w.git("push", url, ":b1"); w.git("push", url, ":b2")
        self.srv.maintain()
        for tag, d in (("fresh", clone(url, os.path.join(self.tmp, "fresh"))),):
            ok, err = fsck_ok(d); self.assertTrue(ok, err); self.assertEqual(count(d, "origin/b3"), 3 + 3 + 3 + 2)
        self.assertNotIn("refs/heads/b1", ls_remote(url))

    def test_chain_of_stock_pushes_every_intermediate_state_converges(self):
        url = self.repo(); w = self.work(); [w.commit() for _ in range(2)]; w.push(url, "main")
        w.git("checkout", "-q", "-b", "topic"); viewers = []
        for i in range(40):
            w.commit("t.txt", f"{i}\n" * 3); w.push(url, "topic")
            if i % 8 == 0: viewers.append(clone(url, os.path.join(self.tmp, f"v{i}"), None, "--branch", "topic"))
        for d in viewers + [clone(url, os.path.join(self.tmp, "fresh"))]:
            git(["fetch", "-q", "origin", "topic"], cwd=d); ok, err = fsck_ok(d); self.assertTrue(ok, err)
            self.assertEqual(git(["rev-parse", "origin/topic"], cwd=d).stdout.decode().strip(), w.head())

    def test_duplicate_empty_and_noop_pushes_and_tags(self):
        url = self.repo(); w = self.work(); [w.commit() for _ in range(3)]; w.push(url, "main")
        w.git("push", url, "main:refs/heads/dup")                       # objects already present
        w.git("push", url, "main")                                      # up to date no-op
        w.git("commit", "-q", "--allow-empty", "-m", "empty"); w.push(url, "main")
        w.git("tag", "-a", "v1", "-m", "ann"); w.git("tag", "light"); w.push(url, "v1", "light")
        w.git("push", url, "v1:refs/tags/v1-copy")
        refs = ls_remote(url); self.assertEqual(refs["refs/heads/dup"], w.git("rev-parse", "HEAD~1").stdout.decode().strip())
        d = clone(url, os.path.join(self.tmp, "c")); ok, err = fsck_ok(d); self.assertTrue(ok, err)

    def test_retention_after_churn_and_maintenance(self):
        url = self.repo(); w = self.work(); [w.commit() for _ in range(3)]; w.push(url, "main"); rng = random.Random(3); live = {}
        old = clone(url, os.path.join(self.tmp, "old"))
        for i in range(12):
            br = f"b{i % 4}"; w.git("checkout", "-q", "-B", br, "main")
            [w.commit(f"{br}.txt") for _ in range(rng.randint(1, 3))]; w.push(url, f"+{br}"); live[br] = w.head()
            if i % 5 == 4: w.git("push", url, f":b{rng.randint(0, 3)}"); live = {k: v for k, v in live.items() if f"refs/heads/{k}" in ls_remote(url)}
        self.srv.maintain(); self.srv.maintain()
        refs = ls_remote(url)
        for k, v in live.items(): self.assertEqual(refs[f"refs/heads/{k}"], v)
        d = clone(url, os.path.join(self.tmp, "fresh")); ok, err = fsck_ok(d); self.assertTrue(ok, err)
        git(["fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*"], cwd=old); ok, err = fsck_ok(old); self.assertTrue(ok, err)

    def test_concurrent_writer_and_readers_see_complete_monotonic_history(self):
        url = self.repo(); w = self.work(); w.commit(); w.push(url, "main"); w.git("checkout", "-q", "-b", "t")
        tips, stop, viol = [], threading.Event(), []
        def writer():
            for i in range(25): tips.append(w.commit("t.txt")); w.push(url, "t")
            stop.set()
        def reader(k):
            d = clone(url, os.path.join(self.tmp, f"r{k}")); last = 0
            while not stop.is_set():
                p = git(["fetch", "origin", "t"], cwd=d, check=False)
                if p.returncode: viol.append(("fetch failed", p.stderr.decode()[-300:])); return
                c = count(d, "FETCH_HEAD")
                if c < last: viol.append(("backwards", last, c))
                last = c
            ok, err = fsck_ok(d); (ok or viol.append(("fsck", err)))
        ts = [threading.Thread(target=writer)] + [threading.Thread(target=reader, args=(k,)) for k in range(3)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(viol, [])

    def test_random_edit_workload_clones_match(self):
        url = self.repo(); w = self.work(); rng = random.Random(11); w.commit("a.txt", "x\n" * 5000); w.push(url, "main")
        w.git("checkout", "-q", "-b", "t")
        for i in range(25):
            p = os.path.join(w.dir, "a.txt"); L = open(p).read().split("\n"); L[rng.randrange(len(L))] = f"edit{i}"; open(p, "w").write("\n".join(L))
            w.git("add", "-A"); w.git("commit", "-q", "-m", f"e{i}"); w.push(url, "t")
        d = clone(url, os.path.join(self.tmp, "c")); git(["checkout", "-q", "t"], cwd=d)
        self.assertEqual(open(os.path.join(d, "a.txt")).read(), open(os.path.join(w.dir, "a.txt")).read())
        ok, err = fsck_ok(d); self.assertTrue(ok, err)
