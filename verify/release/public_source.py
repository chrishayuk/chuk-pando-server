#!/usr/bin/env python3
"""Build the public source tree from a private commit (docs/PUBLIC-SOURCE.md). Nothing here publishes anything.

  public_source.py archive-manifest
      (re)writes verify/audit/PUBLIC-ARCHIVE.sha256: the lines of each frozen audit archive's own SHA256SUMS for the
      files the public source keeps (verbatim lines, paths prefixed with the archive name).
  public_source.py export COMMIT OUT_DIR [--author "Name <email>"]
      exports the tree of COMMIT to OUT_DIR minus the WITHHELD paths below, adds three generated files, and makes a
      fresh git repository there with ONE commit by the given (neutral) identity:
        WITHHELD.tsv           path, sha256 at COMMIT (a directory: its git tree id and file count), reason, where kept
        PUBLIC-SOURCE-ORIGIN   the private commit this tree derives from, and how
        PUBLIC-SOURCE.sha256   SHA-256 of every other file in the tree
      Every other file is byte-identical to COMMIT's (checked here against git's own blob ids).
"""
import hashlib, os, shutil, subprocess, sys, tarfile, io

KEPT_AT = "the private archive repository (chuk-pando-server, to be renamed chuk-pando-server-archive; not public)"
TRANSCRIPT = "raw AI-agent session transcript (unreviewed)"
HOMEPATH = "frozen audit output containing a personal home-directory path (frozen: cannot be edited)"
WITHHELD = [
    ("verify/audit/archive/net/session.jsonl", TRANSCRIPT),
    ("verify/audit/archive/rev/session.jsonl", TRANSCRIPT),
    ("verify/audit/archive/net/builders-p1.txt", HOMEPATH),
    ("verify/audit/archive/net/coverage-inventory.md", HOMEPATH),
    ("verify/audit/archive/net/unit-run.txt", HOMEPATH),
    ("verify/audit/archive/rev/work/repro/r23_keylocks_writer_starvation.out", HOMEPATH),
    ("verify/audit/archive/rev/work/gate-review/run_suites.sh", HOMEPATH),
    ("verify/audit/archive/rev/work/gate-review/run_repro.sh", HOMEPATH),
    ("verify/audit/archive/rev/work/gate-review/demo_rejects.sh", HOMEPATH),
    ("verify/results", "raw internal test records (machine paths, timings); summarised in docs/VERIFY.md and reproducible with verify/run_suites.sh"),
    ("docs/RELEASE-READINESS.md", "internal release checkpoint (owner decisions, private CI run ids)"),
    ("docs/RELEASE-DRY-RUN.md", "internal dry-run record (includes an incident on the maintainer's machine)"),
    ("docs/DRAFT-RELEASE-PLAN.md", "operator copy of the release procedure with local paths; the public procedure is docs/RELEASING.md"),
    ("docs/record/RUNLOG.md", "experiment run log (private workspace layout, costs, isolation set-up)"),
    ("docs/proposals/README.md", "superseded draft README (an old URL, commands that do not exist)"),
]
GENERATED = ("WITHHELD.tsv", "PUBLIC-SOURCE-ORIGIN", "PUBLIC-SOURCE.sha256")
ROOT = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=os.path.dirname(os.path.abspath(__file__))).decode().strip()


def git(*a, cwd=ROOT, env=None):
    return subprocess.check_output(["git", *a], cwd=cwd, env=env)


def withheld(path):
    return any(path == w or path.startswith(w + "/") for w, _ in WITHHELD)


def archive_manifest():
    out = []
    for a in ("net", "rev"):
        for line in open(os.path.join(ROOT, "verify/audit/archive", a, "SHA256SUMS"), encoding="utf-8"):
            h, p = line.rstrip("\n").split("  ", 1)
            rel = f"verify/audit/archive/{a}/" + p[2:]
            if not withheld(rel):
                out.append(f"{h}  {a}/{p[2:]}")
    dst = os.path.join(ROOT, "verify/audit/PUBLIC-ARCHIVE.sha256")
    open(dst, "w").write("\n".join(out) + "\n")
    print(f"{dst}: {len(out)} files")


def export(commit, out, author):
    commit = git("rev-parse", commit).decode().strip()
    if os.path.exists(out):
        sys.exit(f"{out} exists: refusing")
    os.makedirs(out)
    tar = tarfile.open(fileobj=io.BytesIO(git("archive", "--format=tar", commit)))
    kept, rows = 0, []
    for m in tar.getmembers():
        if withheld(m.name):
            continue
        tar.extract(m, out, filter="tar") if sys.version_info >= (3, 12) else tar.extract(m, out)
        kept += m.isfile()
    for w, why in WITHHELD:
        typ = git("cat-file", "-t", f"{commit}:{w}").decode().strip()
        if typ == "blob":
            h = hashlib.sha256(git("show", f"{commit}:{w}")).hexdigest()
        else:
            n = len(git("ls-tree", "-r", "--name-only", commit, "--", w).decode().splitlines())
            h = f"git-tree:{git('rev-parse', f'{commit}:{w}').decode().strip()} ({n} files)"
        rows.append(f"{w}\t{h}\t{why}\t{KEPT_AT}")
    open(os.path.join(out, "WITHHELD.tsv"), "w").write("path\tsha256 at the origin commit\treason\twhere kept\n" + "\n".join(rows) + "\n")
    open(os.path.join(out, "PUBLIC-SOURCE-ORIGIN"), "w").write(
        f"origin_commit={commit}\norigin_repository=private (see WITHHELD.tsv, where kept)\n"
        f"derivation=git archive of origin_commit, minus the paths in WITHHELD.tsv, plus WITHHELD.tsv, PUBLIC-SOURCE-ORIGIN "
        f"and PUBLIC-SOURCE.sha256 (generated by verify/release/public_source.py)\n")
    # every exported file must be byte-identical to the origin commit's blob
    for root, dirs, files in os.walk(out):
        dirs[:] = [d for d in dirs if d != ".git"]
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), out)
            if rel in GENERATED or os.path.islink(os.path.join(root, f)):
                continue
            blob = git("rev-parse", f"{commit}:{rel}").decode().strip()
            got = subprocess.check_output(["git", "hash-object", os.path.join(root, f)]).decode().strip()
            if blob != got:
                sys.exit(f"{rel}: differs from the origin commit")
    lines = []
    for root, dirs, files in os.walk(out):
        dirs[:] = [d for d in dirs if d != ".git"]
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), out)
            if rel == "PUBLIC-SOURCE.sha256" or os.path.islink(os.path.join(root, f)):
                continue
            lines.append(f"{hashlib.sha256(open(os.path.join(root, f), 'rb').read()).hexdigest()}  ./{rel}")
    open(os.path.join(out, "PUBLIC-SOURCE.sha256"), "w").write("\n".join(sorted(lines, key=lambda l: l[66:])) + "\n")
    name, email = author.rsplit(" <", 1)
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email.rstrip(">"),
               GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email.rstrip(">"))
    git("init", "-q", "-b", "main", cwd=out, env=env)
    git("add", "-A", cwd=out, env=env)
    git("commit", "-q", "-m", "pando 0.1.0: public source\n\nDerived from a private, fully audited history; see docs/PUBLIC-SOURCE.md and PUBLIC-SOURCE-ORIGIN.", cwd=out, env=env)
    print(f"{out}: {kept} files from {commit[:12]}, {len(WITHHELD)} withheld entries, commit {git('rev-parse', 'HEAD', cwd=out).decode().strip()}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["archive-manifest"]:
        archive_manifest()
    elif sys.argv[1:2] == ["export"] and len(sys.argv) >= 4:
        a = sys.argv
        export(a[2], a[3], a[a.index("--author") + 1] if "--author" in a else "chuk-pando-server contributors <noreply@chuk-pando-server.invalid>")
    else:
        sys.exit(__doc__)
