"""CG1: stock-Git push/fetch/clone (behavioral, black-box). Hidden."""
import os, random, subprocess
from harness.lib import *


class CG1(SrvCase):
    @for_each_client
    def test_clone_empty(self, g, name):
        url = self.repo(); d = clone(url, os.path.join(self.tmp, "c"), g)
        self.assertTrue(os.path.isdir(os.path.join(d, ".git")))

    @for_each_client
    def test_push_new_branch_and_clone(self, g, name):
        url = self.repo(); w = self.work(client=name)
        for _ in range(5): w.commit()
        w.push(url, "main"); tip = w.head()
        self.assertEqual(ls_remote(url, g)["refs/heads/main"], tip)
        d = clone(url, os.path.join(self.tmp, "c"), g)
        self.assertEqual(git(["rev-parse", "HEAD"], cwd=d, git_bin=g).stdout.decode().strip(), tip)
        ok, err = fsck_ok(d, g); self.assertTrue(ok, err)

    @for_each_client
    def test_update_and_incremental_fetch(self, g, name):
        url = self.repo(); w = self.work(client=name); w.commit(); w.push(url, "main")
        d = clone(url, os.path.join(self.tmp, "c"), g)
        for _ in range(4): w.commit(); w.push(url, "main")
        git(["fetch", "-q", "origin"], cwd=d, git_bin=g)
        self.assertEqual(git(["rev-parse", "origin/main"], cwd=d, git_bin=g).stdout.decode().strip(), w.head())
        ok, err = fsck_ok(d, g); self.assertTrue(ok, err)

    @for_each_client
    def test_branches_and_tags(self, g, name):
        url = self.repo(); w = self.work(client=name); w.commit(); w.push(url, "main")
        w.git("checkout", "-q", "-b", "feat/x"); w.commit(); w.push(url, "feat/x")
        w.git("tag", "lw"); w.git("tag", "-a", "ann", "-m", "annotated"); w.push(url, "lw", "ann")
        refs = ls_remote(url, g)
        for r in ("refs/heads/main", "refs/heads/feat/x", "refs/tags/lw", "refs/tags/ann", "refs/tags/ann^{}"): self.assertIn(r, refs)
        d = clone(url, os.path.join(self.tmp, "c"), g)
        self.assertIn("ann", git(["tag"], cwd=d, git_bin=g).stdout.decode())
        ok, err = fsck_ok(d, g); self.assertTrue(ok, err)

    @for_each_client
    def test_delete_branch(self, g, name):
        url = self.repo(); w = self.work(client=name); w.commit(); w.push(url, "main")
        w.git("checkout", "-q", "-b", "gone"); w.commit(); w.push(url, "gone"); self.assertIn("refs/heads/gone", ls_remote(url, g))
        w.git("push", url, ":gone")
        self.assertNotIn("refs/heads/gone", ls_remote(url, g))

    @for_each_client
    def test_force_push_accepted(self, g, name):
        url = self.repo(); w = self.work(client=name)
        for _ in range(4): w.commit()
        w.push(url, "main"); w.git("reset", "-q", "--hard", "HEAD~2"); new = w.commit(); w.push(url, "+main")
        self.assertEqual(ls_remote(url, g)["refs/heads/main"], new)
        d = clone(url, os.path.join(self.tmp, "c"), g); ok, err = fsck_ok(d, g); self.assertTrue(ok, err)

    @for_each_client
    def test_lease_stale_rejected(self, g, name):
        url = self.repo(); a = self.work("a", name); a.commit(); a.push(url, "main"); old = a.head()
        b = Work(self.tmp, "b", g); clone(url, os.path.join(self.tmp, "bb"), g)
        a.commit(); a.push(url, "main"); newtip = a.head()
        # raw update claiming stale old value must be refused by the server
        pack = build_pack(a.dir, newtip, old, g); c2 = a.commit("x.txt", "z\n"); pack2 = build_pack(a.dir, c2, newtip, g)
        unpack, res = raw_receive(url, [(old, c2, "refs/heads/main")], build_pack(a.dir, c2, None, g))
        self.assertTrue(res.get("refs/heads/main", "").startswith("ng"), f"stale old-oid accepted: {res}")
        self.assertEqual(ls_remote(url, g)["refs/heads/main"], newtip)

    @for_each_client
    def test_large_push_and_many_files(self, g, name):
        url = self.repo(); w = self.work(client=name)
        with open(os.path.join(w.dir, "big.bin"), "wb") as f: f.write(os.urandom(12 * 1024 * 1024))
        for i in range(1500):
            os.makedirs(os.path.join(w.dir, f"d{i % 30}"), exist_ok=True)
            open(os.path.join(w.dir, f"d{i % 30}", f"f{i}.txt"), "w").write(f"{i}\n" * 20)
        w.git("add", "-A"); w.git("commit", "-q", "-m", "big"); w.push(url, "main")
        d = clone(url, os.path.join(self.tmp, "c"), g); ok, err = fsck_ok(d, g); self.assertTrue(ok, err)
        self.assertEqual(os.path.getsize(os.path.join(d, "big.bin")), 12 * 1024 * 1024)


class CG1Matrix(SrvCase):
    """Informational: records Supported/Unsupported per feature and client (never fails a gate)."""
    def _feature(self, feature, fn):
        for name, g in CLIENTS.items():
            try: fn(name, g); record(feature, name, True)
            except Exception as e: record(feature, name, False, str(e)[:200])

    def test_matrix(self):
        url = self.repo(); w = self.work(); [w.commit() for _ in range(6)]; w.push(url, "main")
        def shallow(n, g): d = os.path.join(self.tmp, "sh" + n); git(["clone", "-q", "--depth", "1", url, d], git_bin=g); assert fsck_ok(d, g)[0]
        def partial(n, g): d = os.path.join(self.tmp, "pf" + n); git(["clone", "-q", "--filter=blob:none", url, d], git_bin=g)
        def v0(n, g): d = os.path.join(self.tmp, "v0" + n); git(["-c", "protocol.version=0", "clone", "-q", url, d], git_bin=g)
        def v1(n, g): d = os.path.join(self.tmp, "v1" + n); git(["-c", "protocol.version=1", "clone", "-q", url, d], git_bin=g)
        def clientgc(n, g):
            d = os.path.join(self.tmp, "gc" + n); clone(url, d, g); git(["gc", "-q"], cwd=d, git_bin=g); git(["fetch", "-q"], cwd=d, git_bin=g)
        def atomic(n, g):
            x = Work(self.tmp, "at" + n, g); git(["fetch", "-q", url, "main"], cwd=x.dir, git_bin=g); x.git("checkout", "-q", "FETCH_HEAD", "-b", "a1"); x.commit()
            x.git("push", "--atomic", url, "a1:refs/heads/a1", "a1:refs/heads/a2")
        def sparsefetchdepth(n, g):
            d = os.path.join(self.tmp, "dp" + n); clone(url, d, g); git(["fetch", "-q", "--deepen=1"], cwd=d, git_bin=g, check=False)
        def pushopts(n, g):
            x = Work(self.tmp, "po" + n, g); git(["fetch", "-q", url, "main"], cwd=x.dir, git_bin=g); x.git("checkout", "-q", "FETCH_HEAD", "-b", "po"); x.commit()
            x.git("push", "-o", "k=v", url, "po")
        for f, fn in [("shallow clone", shallow), ("partial clone (blob:none)", partial), ("protocol v0", v0), ("protocol v1", v1),
                      ("client git gc then fetch", clientgc), ("push --atomic", atomic), ("push options", pushopts)]:
            self._feature(f, fn)
