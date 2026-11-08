import threading, unittest, pytest
class Sub(unittest.TestCase):
    def test_subtests(self):
        for i in range(3):
            with self.subTest(i=i):
                self.assertNotEqual(i, 1, "subtest 1 fails")
    def test_subtest_then_pass(self):
        with self.subTest("a"):
            self.fail("first subtest fails")
        # rest passes
class FailAndTeardownError(unittest.TestCase):
    def test_x(self):
        self.addCleanup(lambda: 1/0)
        self.fail("call fails")
class ThreadExc(unittest.TestCase):
    def test_thread_raises(self):
        t = threading.Thread(target=lambda: (_ for _ in ()).throw(AssertionError("writer thread saw corruption"))); t.start(); t.join()
@pytest.mark.parametrize("v", ["a::b", "x y", "é[1]"])
def test_param(v): assert v != "x y"
