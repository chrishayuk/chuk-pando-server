#!/usr/bin/env python3
"""Privacy and provenance scan of a source tree that is meant to be published (docs/PUBLIC-SOURCE.md).

  privacy_scan.py TREE [--allow FILE] [--names FILE] [--private FILE]   exit 1 if any finding is not covered by the allow-list

It reads every regular file under TREE (except .git) and reports, per category, every match with file:line:
  secret      credentials or tokens: private keys, GitHub/AWS/Fly/Slack tokens, Bearer/Basic values, password= ...
  email       e-mail addresses
  name        personal names: one regex per line of --names FILE (kept outside the published tree)
  home-path   personal home-directory paths (/Users/<name>, /home/<name>, C:\\Users\\<name>)
  private-dir references to private local directories: one regex per line of --private FILE
  host        internal host names and addresses (*.internal, *.flycast, private and public IPv4 outside the
              documentation ranges)
  fly-id      Fly.io Machine ids, volume ids, and app-internal names
  transcript  raw agent-session transcripts (JSON lines with tool_use / tool_result records)
  binary      files that are not UTF-8 text

The allow-list (--allow) holds lines `category<TAB>path-glob<TAB>regex<TAB>reason`; a finding is allowed when all
three match. Every allowed finding is still printed, with its reason, so the review sees all of them.
"""
import fnmatch, json, os, re, sys

PATTERNS = {
    "secret": [
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----", r"\bgh[pousr]_[A-Za-z0-9]{30,}", r"\bgithub_pat_[A-Za-z0-9_]{30,}",
        r"\bAKIA[0-9A-Z]{16}\b", r"\bASIA[0-9A-Z]{16}\b", r"\bFlyV1 [A-Za-z0-9+/=_-]{20,}", r"\bfo1_[A-Za-z0-9_-]{20,}",
        r"\bxox[abprs]-[A-Za-z0-9-]{10,}", r"(?i)\bauthorization:\s*(bearer|basic)\s+[A-Za-z0-9+/=._-]{12,}",
        r"(?i)\b(password|passwd|secret|token|api[_-]?key)\s*[=:]\s*['\"]?[A-Za-z0-9+/=._-]{12,}",
        r"(?i)\baws_secret_access_key\b\s*[=:]\s*\S{20,}",
    ],
    "email": [r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"],
    "name": [],
    "home-path": [r"/Users/(?!Shared\b)[A-Za-z][A-Za-z0-9._-]*", r"/home/(?!runner\b)[a-z][a-z0-9._-]*", r"(?i)C:\\\\?Users\\\\?(?!runneradmin|Public)[A-Za-z][A-Za-z0-9._-]*"],
    "private-dir": [],   # supplied with --private FILE (one regex per line), kept outside the published tree
    "host": [r"\b[a-z0-9-]+\.internal\b", r"\b[a-z0-9-]+\.flycast\b",
             r"\b(?!127\.)(?!0\.0\.0\.0)(?!10\.255\.255\.255)(?:\d{1,3}\.){3}\d{1,3}\b"],
    "fly-id": [r"\b[0-9a-f]{14}\b(?=.*(?i:machine|fly))", r"\bvol_[0-9a-z]{10,}\b", r"(?i)\bmachine[ _-]?id\b"],
}
TRANSCRIPT = re.compile(r'"type"\s*:\s*"(tool_use|tool_result)"')
DOC_IPS = re.compile(r"^(192\.0\.2\.|198\.51\.100\.|203\.0\.113\.|0\.|255\.)")


def load_allow(path):
    out = []
    if path:
        for line in open(path, encoding="utf-8"):
            if line.strip() and not line.startswith("#"):
                cat, glob, rx, why = line.rstrip("\n").split("\t", 3)
                out.append((cat, glob, re.compile(rx), why))
    return out


def main():
    tree = sys.argv[1]
    allow = load_allow(sys.argv[sys.argv.index("--allow") + 1] if "--allow" in sys.argv else None)
    if "--names" in sys.argv:
        PATTERNS["name"] = [l.strip() for l in open(sys.argv[sys.argv.index("--names") + 1], encoding="utf-8") if l.strip() and not l.startswith("#")]
    if "--private" in sys.argv:
        PATTERNS["private-dir"] = [l.strip() for l in open(sys.argv[sys.argv.index("--private") + 1], encoding="utf-8") if l.strip() and not l.startswith("#")]
    comp = {c: [re.compile(p) for p in ps] for c, ps in PATTERNS.items()}
    findings = []
    nfiles = 0
    for root, dirs, files in os.walk(tree):
        dirs[:] = [d for d in dirs if d not in (".git", "target", ".cargo-home")]
        for f in sorted(files):
            p = os.path.join(root, f); rel = os.path.relpath(p, tree)
            if os.path.islink(p):
                continue
            nfiles += 1
            raw = open(p, "rb").read()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                findings.append(("binary", rel, 0, "not UTF-8 text")); continue
            if rel.endswith(".jsonl") and TRANSCRIPT.search(text):
                findings.append(("transcript", rel, 0, "JSON lines with tool_use/tool_result records"))
            for i, line in enumerate(text.splitlines(), 1):
                for cat, rxs in comp.items():
                    for rx in rxs:
                        for m in rx.finditer(line):
                            s = m.group(0)
                            if cat == "host" and re.match(r"(?:\d{1,3}\.){3}\d{1,3}$", s):
                                parts = [int(x) for x in s.split(".")]
                                if max(parts) > 255 or DOC_IPS.match(s):
                                    continue
                            findings.append((cat, rel, i, line.strip()[:200]))
    bad = 0
    by = {}
    for cat, rel, ln, ctx in findings:
        why = next((w for c, g, rx, w in allow if c == cat and fnmatch.fnmatch(rel, g) and rx.search(ctx)), None)
        by.setdefault(cat, []).append((rel, ln, ctx, why))
        bad += why is None
    print(f"privacy scan of {tree}: {nfiles} files")
    for cat in list(PATTERNS) + ["transcript", "binary"]:
        items = by.get(cat, [])
        print(f"\n== {cat}: {len(items)} finding(s), {sum(1 for x in items if x[3] is None)} not allowed")
        for rel, ln, ctx, why in items:
            print(f"  {'ALLOWED' if why else 'FINDING'} {rel}:{ln}: {ctx}" + (f"    [{why}]" if why else ""))
    print(f"\n{bad} finding(s) not covered by the allow-list")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
