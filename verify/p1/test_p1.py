"""P1 fidelity tests, black-box through INTERFACE.md, using the experiment's hidden-suite harness (imported).
Run: verify/p1/run.sh"""
import os
from harness.lib import *


def objects_in_packs(d):
    out = git(["count-objects", "-v"], cwd=d).stdout.decode()
    kv = dict(l.split(": ") for l in out.strip().split("\n"))
    return int(kv["in-pack"]) + int(kv["count"])


def reachable(d):
    return len(git(["rev-list", "--objects", "--all"], cwd=d).stdout.decode().strip().split("\n"))



_NO_MAINT = ["-c", "gc.auto=0", "-c", "maintenance.auto=false"]


def _no_client_maintenance(d, client_git=None):
    for k, v in (("gc.auto", "0"), ("maintenance.auto", "false"), ("gc.autoDetach", "false")):
        git(["config", k, v], cwd=d, git_bin=client_git)


class P1(SrvCase):
    def test_clone_after_force_push_receives_no_discarded_history(self):
        """CG7 audit B D-9: a cover spanning a force-push contains the discarded versions' packs. A fresh clone of
        the new tip must not receive them (amplification), with or without packfile URIs."""
        url = self.repo(); old = self.work("old"); new = self.work("new")
        for i in range(16):
            old.commit("old.txt", f"old {i} " + "x" * 2000 + "\n"); old.push(url, "main")
        for i in range(48):  # unrelated history, force-pushed: version 17 is not linked to 16
            new.commit("new.txt", f"new {i}\n"); new.push(url, "+main")
        self.srv.maintain()
        for name, extra in (("plain", ()), ("uris", ("-c", "fetch.uriprotocols=http"))):
            d = os.path.join(self.tmp, f"c-{name}")
            git([*extra, *_NO_MAINT, "clone", "-q", url, d])
            ok, err = fsck_ok(d); self.assertTrue(ok, err)
            got, need = objects_in_packs(d), reachable(d)
            self.assertEqual(got, need, f"{name}: received {got} objects, only {need} reachable: discarded history was sent")


class P1Covers(SrvCase):
    def test_clone_from_empty_uses_top_cover(self):
        """Regression for the first D-9 fix: covers starting at version 0 are 'unlinked' only because version 1 is a
        branch creation; they contain no discarded history and must remain usable. A clone of 256 versions from empty
        should be planned with the single L4 cover (a handful of steps), not one cover per level per alignment."""
        import json
        url = self.repo(); w = self.work(); prev = None
        for i in range(256):
            c = w.commit("f.txt", f"{i}\n")
            unpack, res = raw_receive(url, [(prev or ZERO, c, "refs/heads/main")], build_pack(w.dir, c, prev))
            self.assertEqual(res.get("refs/heads/main"), "ok"); prev = c
        self.srv.maintain()
        d = os.path.join(self.tmp, "c"); git(["-c", "fetch.uriprotocols=http", *_NO_MAINT, "clone", "-q", url, d])
        ok, err = fsck_ok(d); self.assertTrue(ok, err)
        p = os.path.join(self.srv.data, "events", "events.jsonl")
        if not os.path.exists(p):
            p = os.path.join(self.srv.data, "events.jsonl")
        ev = [json.loads(l) for l in open(p) if "fetch_served" in l][-1]; ev = ev.get("data", ev)
        self.assertLessEqual(ev["plan_steps"], 2, f"clone from empty planned with {ev['plan_steps']} steps / {ev['covers_used']} covers")


class P1Recompress(SrvCase):
    def test_sigkill_during_recompression_leaves_no_workspace_and_no_partial_cover(self):
        """Recompression runs in a scratch repository under <data>/blobs/tmp. SIGKILL the server while one exists,
        restart: the workspace must be gone (startup sweep under the data-directory lock), every acknowledged version
        must still be fetchable and fsck-clean, and maintain must complete afterwards."""
        import glob, threading
        url = self.repo(); w = self.work(); prev = None
        for i in range(260):
            for j in range(4):
                w.commit(f"d/{(i * 4 + j) % 40:02d}.txt", f"{i} {j} " + "shared text " * 40 + "\n")
            c = w.head()
            unpack, res = raw_receive(url, [(prev or ZERO, c, "refs/heads/main")], build_pack(w.dir, c, prev))
            self.assertEqual(res.get("refs/heads/main"), "ok"); prev = c
        pattern = os.path.join(self.srv.data, "blobs", "tmp", "in-*", "objects")
        th = threading.Thread(target=lambda: self.srv.run("maintain", check=False)); th.start()
        # Freeze the server (SIGSTOP) as soon as a workspace appears, so what exists at the kill is known exactly; the
        # old version killed after seeing one, and could kill after it was already removed (then nothing was tested).
        import signal
        at_kill = []; deadline = time.time() + 120; pid = self.srv.pid()
        while time.time() < deadline and th.is_alive():
            if glob.glob(pattern):
                os.kill(pid, signal.SIGSTOP)
                at_kill = [p for p in glob.glob(os.path.join(self.srv.data, "blobs", "tmp", "*")) if os.path.isdir(p)]
                if at_kill: break
                os.kill(pid, signal.SIGCONT)
            time.sleep(0.005)
        if not at_kill:
            th.join(); self.skipTest("INCONCLUSIVE: no recompression workspace observed before maintain finished")
        self.srv.kill9(); th.join()
        # Only the killed process's workspaces count: the restarted process's own startup catch-up compaction may
        # create new ones at once (audit: the old whole-directory check raced with them).
        self.srv.start()
        leftovers = [p for p in at_kill if os.path.exists(p)]
        self.assertEqual(leftovers, [], "recompression workspace of the killed process survived restart")
        self.srv.maintain()
        for name, extra in (("plain", ()), ("uris", ("-c", "fetch.uriprotocols=http"))):
            d = os.path.join(self.tmp, f"c-{name}"); git([*extra, *_NO_MAINT, "clone", "-q", url, d])
            ok, err = fsck_ok(d); self.assertTrue(ok, err)
            self.assertEqual(git(["rev-parse", "HEAD"], cwd=d).stdout.decode().strip(), prev)


# OpenAI's Git client fork (github.com/openai/git), built locally; required, so a missing client can never silently
# shrink the client matrix (docs/VERIFY.md)
FORK_GIT = os.environ.get("FORK_GIT", "")
if not os.path.isfile(FORK_GIT):
    raise RuntimeError(f"FORK_GIT={FORK_GIT!r}: set FORK_GIT to a built openai/git client (docs/VERIFY.md)")


def _events(srv):
    import json
    p = os.path.join(srv.data, "events", "events.jsonl")
    if not os.path.exists(p):
        p = os.path.join(srv.data, "events.jsonl")
    return [(lambda e: e.get("data", e))(json.loads(l)) for l in open(p) if l.strip()]


def _write_commit(w, lines, msg):
    open(os.path.join(w.dir, "big.txt"), "w").writelines(lines)
    w.git("add", "-A"); w.git("commit", "-q", "-m", msg)
    return w.head()


def _big(seed=7):
    import random
    rnd = random.Random(seed)
    return [f"{i:06d} " + "".join(rnd.choice("abcdefghij") for _ in range(56)) + "\n" for i in range(4096)]


class P1Copies(SrvCase):
    """docs/BACKGROUND-COPIES.md criteria 3 and 5. The server is started with PANDO_TEST_HOLD_COPIES=1 (test-only):
    copy jobs are not run, which holds open the window between push acknowledgment and copy publication."""

    def _hold(self, on):
        self.srv.stop()
        if on:
            self.srv.env["PANDO_TEST_HOLD_COPIES"] = "1"
        else:
            self.srv.env.pop("PANDO_TEST_HOLD_COPIES", None)
        self.srv.start()

    def _uri_fetch(self, url, w0, tip, name, client_git=None):
        """Fresh reader holding the parent of `tip`, fetching main (up to the writer's head) with packfile URIs. Returns (fetch_served, writes)."""
        d = os.path.join(self.tmp, name); parent = w0.git("rev-parse", tip + "~1").stdout.decode().strip()
        git(["init", "-q", "-b", "main", d], git_bin=client_git)
        # No client auto-maintenance: after a fetch git starts a detached `maintenance run --auto` that repacks
        # (`repack -d -l --cruft --write-midx`) while the fsck below reads, which then fails on the repack's
        # temporary .tmp-<pid>-pack (GIT_TRACE-confirmed; the cause of this test's intermittent failures).
        _no_client_maintenance(d, client_git)
        w0.git("config", "uploadpack.allowAnySHA1InWant", "true")
        git(["fetch", "-q", w0.dir, parent], cwd=d, git_bin=client_git)
        git(["reset", "-q", "--hard", parent], cwd=d, git_bin=client_git)
        self.assertNotEqual(git(["cat-file", "-e", tip], cwd=d, check=False).returncode, 0, "reader already holds the tip")
        n0 = len(_events(self.srv)); s0 = self.srv.stats()
        git(["-c", "fetch.uriprotocols=http", "-c", "protocol.version=2", "fetch", "-q", url, "main"], cwd=d, git_bin=client_git)
        s1 = self.srv.stats()
        git(["merge", "-q", "--ff-only", "FETCH_HEAD"], cwd=d, git_bin=client_git)
        ok, err = fsck_ok(d); self.assertTrue(ok, f"{name}: {err}")
        self.assertEqual(git(["rev-parse", "HEAD"], cwd=d).stdout.decode().strip(), w0.head(), f"{name}: did not reach main")
        fs = [e for e in _events(self.srv)[n0:] if "plan_steps" in e]
        return fs[-1], s1["metadata_writes"] - s0["metadata_writes"]

    def test_thin_push_fetchable_before_copy_then_served_by_uri(self):
        url = self.repo(); w = self.work("w"); lines = _big()
        _write_commit(w, lines, "v0"); w.push(url, "main")
        self._hold(True)
        lines[7] = "edited line\n"; tip = _write_commit(w, lines, "v1"); w.push(url, "main")   # stock push: thin pack
        thin = [e for e in _events(self.srv) if e.get("ev") == "ref_version_committed"][-1]["pack"]
        for name, cg in (("stock", None), ("fork", FORK_GIT)):
            ev, writes = self._uri_fetch(url, w, tip, f"held-{name}", cg)
            self.assertEqual(writes, 0, f"{name}: fetch wrote metadata before the copy existed")
            self.assertGreaterEqual(ev["inline_fallback_packs"], 1, f"{name}: expected the inline fallback, got {ev}")
            self.assertNotIn(thin, ev["uri_packs"])
        self.assertFalse([e for e in _events(self.srv) if e.get("ev") == "twin_committed"], "a copy was built while held")
        self._hold(False)   # restart: copies are reconstructed from committed state, no job record
        self.srv.maintain()
        self.assertTrue([e for e in _events(self.srv) if e.get("ev") == "twin_committed" and e["pack"] == thin], "copy not built after restart")
        for name, cg in (("stock", None), ("fork", FORK_GIT)):
            ev, writes = self._uri_fetch(url, w, tip, f"ready-{name}", cg)
            self.assertEqual(writes, 0, f"{name}: steady-state URI fetch wrote metadata")
            self.assertEqual(ev["inline_fallback_packs"], 0, f"{name}: copy published but not used: {ev}")
            self.assertEqual(len(ev["uri_packs"]), len(ev["packs"]))

    def test_sigkill_with_copies_pending_rebuilds_them_on_restart(self):
        url = self.repo(); w = self.work("w"); lines = _big()
        _write_commit(w, lines, "v0"); w.push(url, "main")
        self._hold(True)
        tips = []
        for v in range(6):
            lines[100 * v + 3] = f"edit {v}\n"; tips.append(_write_commit(w, lines, f"v{v + 1}")); w.push(url, "main")
        thin = {e["pack"] for e in _events(self.srv) if e.get("ev") == "ref_version_committed"} - {None}
        self.srv.kill9()
        self.srv.env.pop("PANDO_TEST_HOLD_COPIES", None); self.srv.start(); self.srv.maintain()
        built = {e["pack"] for e in _events(self.srv) if e.get("ev") == "twin_committed"}
        self.assertFalse([e for e in _events(self.srv) if e.get("ev") in ("compaction_error", "twin_error")])
        for i, tip in enumerate(tips):
            ev, writes = self._uri_fetch(url, w, tip, f"r{i}")
            self.assertEqual(writes, 0); self.assertEqual(ev["inline_fallback_packs"], 0, f"version {i}: {ev}")
        d = os.path.join(self.tmp, "clone"); git(["-c", "fetch.uriprotocols=http", *_NO_MAINT, "clone", "-q", url, d])
        ok, err = fsck_ok(d); self.assertTrue(ok, err)
        # every copy rebuilt is a copy of a pack pushed here (was `built <= thin | built`, always true: audit)
        self.assertTrue(built, "no copies rebuilt"); self.assertTrue(built <= thin, f"copies of unknown packs: {built - thin}")
