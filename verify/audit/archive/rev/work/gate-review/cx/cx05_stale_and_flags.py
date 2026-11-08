"""Findings 4/5: no freshness/binary binding; option parsing narrows or is silently ignored (gate.py:102-116)."""
import os, time
from _h import *
rec = junit(passing("p1"))
run("p1 record whose meta.txt names a different binary sha, record mtime 2020 (gate never reads meta.txt / mtime)",
    {"p1.xml": rec, "meta.txt": "binary_sha256=deadbeef\nstart=2020-01-01T00:00:00Z\n"}, "p1")
bad = {"p1.xml": junit(passing("p1", {"test_p1.P1Covers::test_clone_from_empty_uses_top_cover": ("fail", "regression")})),
       "hidden.xml": junit(passing("hidden"))}
run("p1 has a regression; --suites=hidden narrows it away", bad, "hidden")
run("same; typo'd --strickt flag is silently accepted (no usage error)", bad, "hidden", ["--strickt"])
run("p1 regression; --suites= (empty) means ALL suites -> other suites missing", bad, "", ["--suites="])
