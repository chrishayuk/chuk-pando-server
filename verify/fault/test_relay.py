"""The test-only object-store relay (s3relay.py) and the server's behaviour through it, against HTTPS MinIO.
Run: verify/fault/run.sh"""
import json, os, shutil, subprocess, sys, tempfile, time, unittest, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "dist"))
from distlib import Cluster
from harness.lib import Work, git, fsck_ok


class RelayCase(unittest.TestCase):
    def setUp(self):
        self.c = Cluster(2, mode="tcp-s3tls"); self.tmp = tempfile.mkdtemp(prefix="rl-")
        up = self.c.minio.url.split("://", 1)[1]
        self.p = subprocess.Popen([sys.executable, os.path.join(HERE, "s3relay.py"), "127.0.0.1:0", up, "0"], stdout=subprocess.PIPE)
        ports = json.loads(self.p.stdout.readline()); self.ctl = ports["control_port"]
        self.r = self.c.add_frontend({"PANDO_S3_CONNECT": f"127.0.0.1:{ports['listen_port']}"})   # through the relay
        self.direct = self.c.fe[0]
        self.addCleanup(lambda: (self.p.kill(), self.c.cleanup(), shutil.rmtree(self.tmp, ignore_errors=True)))
        self.direct.create("r"); self.w = Work(self.tmp, "w"); self.w.commit(); self.w.push(self.direct.url("r"), "main")

    def relay(self, mode, ms=0):
        return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{self.ctl}/{mode}?ms={ms}", timeout=5).read())

    def tip(self):
        return git(["ls-remote", self.direct.url("r"), "refs/heads/main"]).stdout.decode().split()[0]

    def push_timed(self):
        c = self.w.commit(); t = time.time(); p = self.w.git("push", self.r.url("r"), "main", check=False)
        return c, p.returncode, time.time() - t, p.stderr.decode(errors="replace")

    def test_pass_through_is_tls_end_to_end_and_serves_correctly(self):
        c, rc, dt, err = self.push_timed()
        self.assertEqual(rc, 0, err); self.assertEqual(self.tip(), c)
        st = self.relay("state")
        self.assertGreater(st["bytes_up"], 1000, f"traffic did not go through the relay: {st}")
        self.assertEqual(st["plaintext_http_chunks"], 0, "the relay saw plaintext HTTP: TLS is not end to end")
        d = os.path.join(self.tmp, "c"); git(["clone", "-q", self.direct.url("r"), d]); ok, e = fsck_ok(d); self.assertTrue(ok, e)

    def test_stall_fails_within_the_bound_without_acknowledgement_then_recovers(self):
        before = self.tip(); self.relay("stall")
        c, rc, dt, err = self.push_timed()
        self.relay("pass")
        print(f"stall: rc={rc} after {dt:.1f}s")
        self.assertNotEqual(rc, 0, "push acknowledged while the store was stalled")
        self.assertLess(dt, 40, f"stalled store held the push {dt:.0f}s (bound: 15 s I/O timeout + retry window)")
        self.assertEqual(self.tip(), before, "ref moved without acknowledgement")
        c, rc, dt, err = self.push_timed(); self.assertEqual(rc, 0, err); self.assertEqual(self.tip(), c)

    def test_refused_connections_fail_fast_without_acknowledgement_then_recover(self):
        before = self.tip(); self.relay("refuse")
        c, rc, dt, err = self.push_timed()
        self.relay("pass")
        print(f"refuse: rc={rc} after {dt:.1f}s")
        self.assertNotEqual(rc, 0); self.assertLess(dt, 20); self.assertEqual(self.tip(), before)
        c, rc, dt, err = self.push_timed(); self.assertEqual(rc, 0, err); self.assertEqual(self.tip(), c)

    def test_added_latency_slows_but_does_not_break_pushes(self):
        c, rc, base, err = self.push_timed(); self.assertEqual(rc, 0, err)
        self.relay("delay", 150)
        c, rc, slow, err = self.push_timed(); self.relay("pass")
        print(f"delay: {base:.2f}s -> {slow:.2f}s")
        self.assertEqual(rc, 0, err); self.assertEqual(self.tip(), c)
        self.assertGreater(slow, base + 0.3, "the delay had no effect: traffic may not pass through the relay")

    def test_trickling_store_is_cut_off_by_the_overall_deadline(self):
        """Review V9: a store whose *responses* arrive one byte every 100 ms never trips the per-read timeout (15 s
        without progress), so before the overall allowance a push waited as long as the store chose. With
        PANDO_S3_DEADLINE=5 the push must fail, unacknowledged, well before the trickle would finish, and recover."""
        self.r.stop(); self.r.env["PANDO_S3_DEADLINE"] = os.environ.get("G_TEST_DEADLINE", "5"); self.r.start()
        before = self.tip(); self.relay("trickle", "100&dir=down")
        c, rc, dt, err = self.push_timed()
        self.relay("pass")
        print(f"trickle: rc={rc} after {dt:.1f}s")
        self.assertNotEqual(rc, 0, "push acknowledged through a trickling store")
        self.assertLess(dt, 25, f"trickling store held the push {dt:.0f}s")
        self.assertEqual(self.tip(), before)
        c, rc, dt, err = self.push_timed(); self.assertEqual(rc, 0, err); self.assertEqual(self.tip(), c)

if __name__ == "__main__":
    unittest.main()
