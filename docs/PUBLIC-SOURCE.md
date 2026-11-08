# This public source and how it relates to the private engineering history

pando was built in a private repository whose history includes the engineering record: the experiment that
produced the first version, two independent audits and their raw sessions, and every test run. This repository
publishes the **source tree of one tested commit** of that history, as **one commit**, without the material listed
below.

## How the tree was made, and how to check it
`verify/release/public_source.py export <commit>` performs these steps:
1. It takes `git archive` of the private commit named in **`PUBLIC-SOURCE-ORIGIN`**.
2. It removes the paths listed in **`WITHHELD.tsv`**.
3. It adds three generated files: `WITHHELD.tsv`, `PUBLIC-SOURCE-ORIGIN` and `PUBLIC-SOURCE.sha256`.

It also checks that every remaining file is byte-identical to the private commit's file, comparing against git's
own object ids. Nothing in the tree was edited on the way out. Any sanitising (placeholders for local paths in
`docs/evidence/keylock-perf/` and in the provenance-scan summary; Fly Machine ids in `docs/FLY-RESULTS.md`) was
committed in the private history before the export.

- **`PUBLIC-SOURCE.sha256`** lists the SHA-256 of every file in this tree except itself. Check with
  `shasum -a 256 -c PUBLIC-SOURCE.sha256`.
- **`WITHHELD.tsv`** gives, for each withheld path: its SHA-256 at the origin commit (for a directory, its git tree
  id and file count), the reason, and where it is kept. Anyone holding the private archive can check that a
  withheld file is the one named here.

## What is withheld, and why
| Withheld | Why | Is anything needed to build, run or test pando missing? |
|---|---|---|
| Two raw AI-agent session transcripts in the audit archive | unreviewed raw sessions | No. The audits' findings (`AUDIT.md`, `REVIEW.md`) and the reproducers are here. |
| Seven frozen audit-archive files (three outputs, a coverage inventory, one reproducer output, and the reviewer's copies of three gate scripts) | they contain a personal home-directory path, and frozen files cannot be edited | No. The reproducers don't read them (`docs/VERIFY.md`). |
| `verify/results/` | raw internal test records (machine paths, timings) | No. Re-run with `verify/run_suites.sh`. |
| Internal release records (`docs/RELEASE-READINESS.md`, `docs/RELEASE-DRY-RUN.md`, `docs/DRAFT-RELEASE-PLAN.md`) | maintainer decisions and private CI references | No. The procedure is `docs/RELEASING.md`. |
| `docs/record/RUNLOG.md` | the experiment's operational log (private workspace layout, costs) | No |
| `docs/proposals/README.md` | a superseded draft README | No |

What remains reproducible, and what isn't, is in `docs/VERIFY.md`.

## Not part of this repository
- The experiment record that produced the seed build, and its builders' workspaces.
- The frozen blind CG5/CG6 suite.
- The Git and OpenAI-fork sources the builder had available (`PROVENANCE.md`, `docs/PROVENANCE-REVIEW.md`).
