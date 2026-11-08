"""P0 regression tests (correctness and durability), black-box through INTERFACE.md, using the experiment's
hidden-suite harness (imported, not copied). Each test names the CG7 audit finding it guards.

Run: verify/p0/run.sh   (sets PYTHONPATH to verify/heldout and G_RUN_SH to this checkout's server/run.sh)
"""
import os, threading, time
import harness.lib as lib
from harness.lib import *

HERE = os.path.dirname(os.path.abspath(__file__))


def tip(url, ref):
    return ls_remote(url).get(ref)


class P0(SrvCase):
    def test_non_commit_object_on_branch_rejected(self):
        """CG7 audit B D-12: a branch must point at a commit (stock receive-pack refuses blobs/trees on refs/heads/*)."""
        url = self.repo(); w = self.work(); c1 = w.commit(); w.push(url, "main")
        blob = w.git("hash-object", "-w", "--stdin", inp=b"not a commit\n").stdout.decode().strip()
        tree = w.git("rev-parse", "HEAD^{tree}").stdout.decode().strip()
        for oid, ref in ((blob, "refs/heads/blob"), (tree, "refs/heads/tree")):
            unpack, res = raw_receive(url, [(ZERO, oid, ref)], build_pack(w.dir, oid, None))
            self.assertTrue(res.get(ref, "").startswith("ng"), f"{ref} -> {oid}: {unpack} {res}")
            self.assertNotIn(ref, ls_remote(url))
        # tags may point at any object (stock behaviour), and a normal branch push still works
        unpack, res = raw_receive(url, [(ZERO, blob, "refs/tags/blobtag")], build_pack(w.dir, blob, None))
        self.assertEqual(res.get("refs/tags/blobtag"), "ok", f"{unpack} {res}")
        c2 = w.commit(); w.push(url, "main"); self.assertEqual(tip(url, "refs/heads/main"), c2)
        d = clone(url, os.path.join(self.tmp, "c")); ok, err = fsck_ok(d); self.assertTrue(ok, err)

    def test_identical_repush_after_rejection_concurrent_with_maintain_survives_restart(self):
        """Regression for the shape of CG7 audit A D1 (GC deleting a committed pack): an identical pack is first
        rejected (leaving an orphan blob with the same id), then re-pushed successfully while `maintain` runs in a
        loop. After SIGKILL + restart every acknowledged tip must still clone and fsck clean."""
        url = self.repo(); w = self.work(); base = w.commit(); w.push(url, "main")
        stop = threading.Event(); errors = []
        def maint():
            while not stop.is_set():
                p = self.srv.run("maintain", check=False)
                if p.returncode: errors.append(p.stderr.decode()[-300:])
        acked = {}
        for k in range(12):
            w.git("checkout", "-q", "-B", f"t{k}", base); new = w.commit(f"t{k}.txt")
            pack = build_pack(w.dir, new, base)
            # 1) rejected: stale old (create on an existing ref) -> the pack may be stored, but no ref points at it
            if k == 0:
                w.push(url, f"{base}:refs/heads/seed")
            unpack, res = raw_receive(url, [(ZERO, new, "refs/heads/seed")], pack)
            self.assertTrue(res.get("refs/heads/seed", "").startswith("ng"), f"expected rejection: {unpack} {res}")
            # 2) identical bytes re-pushed successfully while maintain loops
            if k == 0:
                th = threading.Thread(target=maint); th.start()
            unpack, res = raw_receive(url, [(ZERO, new, f"refs/heads/t{k}")], pack)
            self.assertEqual(res.get(f"refs/heads/t{k}"), "ok", f"{unpack} {res}"); acked[f"refs/heads/t{k}"] = new
        stop.set(); th.join()
        self.assertEqual(errors, [], "maintain failed")
        self.srv.kill9(); self.srv.start()
        refs = ls_remote(url)
        for ref, oid in acked.items():
            self.assertEqual(refs.get(ref), oid, f"acknowledged {ref} lost after restart")
        d = clone(url, os.path.join(self.tmp, "c"), None, "--mirror"); ok, err = fsck_ok(d); self.assertTrue(ok, err)

    def test_many_packs_under_low_soft_fd_limit_restart(self):
        """Regression for the shape of CG7 audit A D2 (one fd per pack, EMFILE): 400 pushes, then restart,
        with the server started under a soft RLIMIT_NOFILE of 256 (the macOS shell default)."""
        signal.alarm(1500)
        wrapper = os.path.join(HERE, "lowfd-run.sh")
        old = lib.RUN_SH; lib.RUN_SH = wrapper
        try:
            self.fresh()
            url = self.repo(); w = self.work(); w.commit(); w.push(url, "main")
            for i in range(400):
                w.commit(); p = w.push(url, "main", check=False)
                self.assertEqual(p.returncode, 0, f"push {i} failed: {p.stderr.decode()[-400:]}")
            head = w.head()
            self.srv.maintain(); self.srv.kill9(); self.srv.start()
            self.assertEqual(tip(url, "refs/heads/main"), head)
            d = clone(url, os.path.join(self.tmp, "c")); ok, err = fsck_ok(d); self.assertTrue(ok, err)
            d2 = os.path.join(self.tmp, "c2")
            git(["-c", "fetch.uriprotocols=http", "clone", "-q", url, d2]); ok, err = fsck_ok(d2); self.assertTrue(ok, err)
        finally:
            lib.RUN_SH = old

    def test_published_packs_are_readonly_and_never_replaced(self):
        """CG7 audit B D-6 (write-once publication): pushing identical pack bytes again (to another ref) must not
        replace the stored pack or its index (same inode, same bytes, read-only). Black-box GUARD only: the replacement
        in the seed needs a race or a crash between renames; the deterministic check is the Rust unit test
        `blob::tests::install_never_replaces_existing_index`."""
        url = self.repo(); w = self.work(); base = w.commit(); w.push(url, "main")
        w.git("checkout", "-q", "-b", "x"); new = w.commit("x.txt"); pack = build_pack(w.dir, new, base)
        self.assertEqual(raw_receive(url, [(ZERO, new, "refs/heads/x1")], pack)[1].get("refs/heads/x1"), "ok")
        files = {}
        for root, _, fs in os.walk(self.srv.data):
            for f in fs:
                if f.endswith((".pack", ".pidx")):
                    p = os.path.join(root, f); st = os.stat(p)
                    files[p] = (st.st_ino, open(p, "rb").read(), st.st_mode & 0o222)
        self.assertTrue(files)
        self.assertTrue(all(m == 0 for _, _, m in files.values()), "published pack files must be read-only")
        self.assertEqual(raw_receive(url, [(ZERO, new, "refs/heads/x2")], pack)[1].get("refs/heads/x2"), "ok")
        for p, (ino, b, _) in files.items():
            st = os.stat(p)
            self.assertEqual((st.st_ino, open(p, "rb").read()), (ino, b), f"{p} was replaced")
