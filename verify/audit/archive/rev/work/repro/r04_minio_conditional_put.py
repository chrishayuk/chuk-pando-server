"""R04: what the code assumes about the object store's create-only PUT (s3.rs:310-317): `If-None-Match: *` is honoured
and is atomic under concurrency (no two creators both get 200). Tests the bundled MinIO directly; no Pando code."""
import os, tempfile, subprocess, shutil, collections
from rlib import *

R = Report("R04 MinIO If-None-Match semantics")
base = tempfile.mkdtemp(prefix="r04-")
mi = Minio(base); mi.start(); mi.mkbucket("bkt")
try:
    fa, fb = os.path.join(base, "a"), os.path.join(base, "b")
    open(fa, "wb").write(os.urandom(3 << 20)); open(fb, "wb").write(os.urandom(3 << 20))

    def put(key, f):
        return subprocess.Popen(["curl", "-s", "--aws-sigv4", "aws:amz:us-east-1:s3", "--user", f"{distlib.S3_KEY}:{distlib.S3_SECRET}",
                                 "-X", "PUT", "-H", "If-None-Match: *", "--data-binary", "@" + f, "-o", "/dev/null", "-w", "%{http_code}",
                                 f"{mi.url}/bkt/{key}"], stdout=subprocess.PIPE)
    s1 = put("seq", fa).communicate()[0].decode(); s2 = put("seq", fb).communicate()[0].decode()
    R.check((s1, s2) == ("200", "412"), f"sequential create-only: {s1}, {s2}")
    outcomes = collections.Counter(); both = 0
    for i in range(40):
        pa, pb = put(f"k{i}", fa), put(f"k{i}", fb)
        a, b = pa.communicate()[0].decode(), pb.communicate()[0].decode()
        outcomes[(a, b)] += 1
        if a == "200" and b == "200": both += 1
    R.note(f"concurrent create-only outcomes (A,B): {dict(outcomes)}")
    R.check(both == 0, f"no round where both concurrent create-only PUTs returned 200 ({both}/40)")
finally:
    mi.stop(); shutil.rmtree(base, ignore_errors=True)
R.done()
