#!/usr/bin/env python3
"""Cover compression vs Git repack on registered workloads S, C, U (docs/COMPRESSION-WORKLOADS.md).
Usage: verify/recompress/run_workloads.sh OUT.json [S C U]"""
import json, os, random, shutil, subprocess, sys, tempfile
from harness.lib import Server, Work, git

N = 260
OUT = sys.argv[1]; WANT = sys.argv[2:] or ["S", "C", "U"]
GIT = os.environ.get("G_GIT", "git")
ENV = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
           # no detached auto-gc/maintenance racing our repack (known pitfall, blind author STATUS item 8)
           GIT_CONFIG_COUNT="3", GIT_CONFIG_KEY_0="gc.auto", GIT_CONFIG_VALUE_0="0",
           GIT_CONFIG_KEY_1="maintenance.auto", GIT_CONFIG_VALUE_1="false", GIT_CONFIG_KEY_2="gc.autoDetach", GIT_CONFIG_VALUE_2="false")


def sh(args, cwd=None, inp=None):
    p = subprocess.run([GIT, *args], cwd=cwd, env=ENV, input=inp, capture_output=True)
    if p.returncode:
        raise RuntimeError(f"git {args}: {p.stderr.decode()[-400:]}")
    return p.stdout


def rline(rnd, n=60, alpha="abcdefghijklmnopqrstuvwxyz "):
    return "".join(rnd.choice(alpha) for _ in range(n)) + "\n"


class WL:
    def __init__(self, kind, seed=11):
        self.kind, self.rnd, self.files = kind, random.Random(seed), {}
        r = self.rnd
        if kind == "S":
            templates = [[rline(r) for _ in range(270)] for _ in range(6)]   # ~16 KiB each
            for i in range(120):
                t = list(templates[i % 6])
                for _ in range(max(1, len(t) // 20)):
                    t[r.randrange(len(t))] = rline(r)
                self.files[f"s/{i:03d}.txt"] = t
        else:
            for i in range(60):
                self.files[f"f/{i:03d}.txt"] = [rline(r) for _ in range(135)]   # ~8 KiB

    def step(self, v):
        r = self.rnd
        names = sorted(self.files)
        if self.kind == "S":
            for f in r.sample(names, r.randint(1, 4)):
                for _ in range(r.randint(1, 3)):
                    self.files[f][r.randrange(len(self.files[f]))] = rline(r)
        elif self.kind == "U":
            for f in r.sample(names, r.randint(1, 3)):
                for _ in range(r.randint(1, 4)):
                    self.files[f][r.randrange(len(self.files[f]))] = rline(r)
        else:  # C
            x = r.random()
            if x < 1 / 3:
                src = r.choice(names); new = list(self.files[src])
                for _ in range(3):
                    new[r.randrange(len(new))] = rline(r)
                self.files[f"c/{v:04d}.txt"] = new
            elif x < 0.45:
                src = r.choice(names); self.files[f"m/{v:04d}.txt"] = self.files.pop(src)
            else:
                f = r.choice(names); self.files[f][r.randrange(len(self.files[f]))] = rline(r)

    def write(self, d):
        for root, dirs, fs in os.walk(d):
            if ".git" in root.split(os.sep):
                continue
            for f in fs:
                if not os.path.join(root, f).startswith(os.path.join(d, ".git")):
                    os.remove(os.path.join(root, f))
        for name, lines in self.files.items():
            p = os.path.join(d, name); os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").writelines(lines)


def objects_of_pack(pack, scratch, strict=False):
    """Index the pack alone. `strict` (full connectivity) only for packs that start from the empty repository:
    a range pack (e.g. versions 17..32) legitimately references objects of earlier versions."""
    d = tempfile.mkdtemp(dir=scratch); sh(["init", "-q", "--bare", d])
    dst = os.path.join(d, "objects", "pack", "p.pack"); shutil.copy(pack, dst)
    sh(["index-pack", *(["--strict"] if strict else []), dst], cwd=d)
    idx = sh(["show-index"], inp=open(dst[:-5] + ".idx", "rb").read()).decode()
    return {l.split()[1] for l in idx.strip().split("\n") if l.strip()}


def git_pack(src, revs, scratch, window, depth, strict=False):
    """Self-contained pack of exactly the objects of `revs`, deltas computed from scratch; verified by index-pack."""
    data = sh(["pack-objects", "--revs", "--stdout", "-q", "--threads=1", "--delta-base-offset", "--no-reuse-delta",
               "--no-reuse-object", f"--window={window}", f"--depth={depth}"], cwd=src, inp=("\n".join(revs) + "\n").encode())
    p = os.path.join(tempfile.mkdtemp(dir=scratch), "g.pack"); open(p, "wb").write(data)
    objs = objects_of_pack(p, scratch, strict)
    return len(data), objs


def rev_objects(src, revs):
    out = sh(["rev-list", "--objects", *revs], cwd=src).decode()
    return {l.split()[0] for l in out.strip().split("\n") if l.strip()}


def self_check(scratch):
    """Workload R method check: aggressive Git pack of R's v256 must be close to the 195,106 B measured by git repack."""
    rnd = random.Random(7); d = tempfile.mkdtemp(dir=scratch); sh(["init", "-q", "-b", "main", d])
    lines = [f"{i:06d} " + "".join(rnd.choice("abcdefghij") for _ in range(56)) + "\n" for i in range(4096)]
    for v in range(256):
        lines[rnd.randrange(len(lines))] = f"edit {v} " + "".join(rnd.choice("klmnopqrst") for _ in range(50)) + "\n"
        open(os.path.join(d, "big.txt"), "w").writelines(lines); sh(["add", "-A"], cwd=d); sh(["commit", "-q", "-m", f"v{v}"], cwd=d)
    sh(["repack", "-adq"], cwd=d)
    n, _ = git_pack(d, ["HEAD"], scratch, 250, 4095)
    return n


def run(kind, scratch):
    srv = Server(); srv.start(); srv.create("r"); url = srv.url("r")
    w = Work(scratch, f"w{kind}"); wl = WL(kind); tips = []
    for v in range(1, N + 1):
        if v > 1:
            wl.step(v)
        wl.write(w.dir); w.git("add", "-A"); w.git("commit", "-q", "--allow-empty", "-m", f"v{v}")
        w.git("push", "-q", url, "main"); tips.append(w.head())
    srv.maintain()
    p = os.path.join(srv.data, "events", "events.jsonl")
    if not os.path.exists(p):
        p = os.path.join(srv.data, "events.jsonl")
    covers = [json.loads(l) for l in open(p) if "cover_committed" in l]
    covers = [c.get("data", c) for c in covers]
    packfiles = {}
    for root, _, fs in os.walk(srv.data):
        for f in fs:
            if f.endswith(".pack"):
                packfiles[f[:-5]] = os.path.join(root, f)
    src = os.path.join(scratch, f"src{kind}"); sh(["clone", "-q", "--no-local", w.dir, src]); sh(["repack", "-adq"], cwd=src)
    res = {}
    for label, level, lo, hi in (("L4_1_256", 4, 0, 256), ("L2_17_32", 2, 16, 32)):
        c = next((c for c in covers if c["level"] == level and c["lo"] == lo and c["hi"] == hi), None)
        if not c:
            res[label] = {"error": "cover not found"}; continue
        revs = [tips[hi - 1]] + ([f"^{tips[lo - 1]}"] if lo else [])
        need = rev_objects(src, revs)
        st = lo == 0
        b_objs = objects_of_pack(packfiles[c["pack"]], scratch, st)
        conv, conv_objs = git_pack(src, revs, scratch, 10, 50, st)
        aggr, aggr_objs = git_pack(src, revs, scratch, 250, 4095, st)
        res[label] = {"objects_needed": len(need), "b_cover_bytes": os.path.getsize(packfiles[c["pack"]]),
                      "git_conventional_bytes": conv, "git_aggressive_bytes": aggr,
                      "complete": {"b": b_objs == need, "conventional": conv_objs == need, "aggressive": aggr_objs == need},
                      "b_extra_objects": len(b_objs - need), "b_missing_objects": len(need - b_objs)}
    l0 = sum(os.path.getsize(packfiles[k]) for k in packfiles if k not in {c["pack"] for c in covers})
    srv.cleanup()
    return {"versions": N, "files_at_end": len(wl.files), "all_non_cover_pack_bytes": l0, "covers": res}


def main():
    scratch = tempfile.mkdtemp(prefix="wl-")
    out = {"self_check_R_aggressive_bytes": self_check(scratch), "self_check_expected": 195106}
    print("self-check:", out["self_check_R_aggressive_bytes"], flush=True)
    for k in WANT:
        out[k] = run(k, scratch); print(k, json.dumps(out[k]["covers"]), flush=True)
    json.dump(out, open(OUT, "w"), indent=1)
    shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    main()
