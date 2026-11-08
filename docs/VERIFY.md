# Verifying pando from this repository

Everything needed to build pando and run its functional, durability and security tests is in this repository.
Run them on any machine, with no access to the private engineering record. Two inputs are not here, and the
suites that need them are named below.

## Build
```sh
cd server && cargo build --release        # Rust 1.89+; the binary is server/target/release/pando
cargo test --release                      # unit tests (26)
```

## What the suites need
| Input | Used by | How to get it |
|---|---|---|
| `G_CLIENTS=stock=/path/to/git[,apple=/usr/bin/git]` | every suite | The gate runs use a **stock Git 2.55.0** built from source (`git clone https://github.com/git/git && git checkout v2.55.0 && make`). Any recent Git works; results were recorded with 2.55.0. The second client is optional. |
| `FORK_GIT=/path/to/git` | `dist`, `fly`, `p1` (the packfile-URI and explicit-haves client tests) | OpenAI's client fork, built from <https://github.com/openai/git>. **Required:** without it these suites stop with an error rather than quietly testing fewer clients. |
| MinIO (`G_MINIO=/path/to/minio`; default `/opt/homebrew/bin/minio`) | `dist-tcp-s3`, `net`, `fault`, `cli`, `repro`, `repro-rev` | <https://min.io> (an external test process; AGPLv3, never linked or shipped) |
| Python 3.10+ with `pytest` | all Python suites | `pip install pytest` |
| `CG56=<dir>` | `cg56`, `par` | **Not public.** The frozen blind CG5/CG6 suite from the experiment that produced this server. It is kept privately. |

## Running the gate
```sh
export G_CLIENTS=stock=$HOME/opt/git-2.55.0/bin/git,apple=/usr/bin/git FORK_GIT=$HOME/opt/openai-git/git
verify/run_suites.sh mycheck public     # every suite whose inputs are public
verify/run_suites.sh mycheck all        # also cg56 and par: needs CG56 (private)
```
`verify/gate/gate.py` decides the verdict from the per-test records, never from a suite's exit status.
- **Failures:** it fails on any unexpected failure or skip, any missing required test, and any known failure that
  fails differently from its registered signature.
- **Registered exceptions:** every known failure and allowed skip is in `verify/gate/expectations.json`, with its
  reason.
- **`public`** checks every suite except `cg56` and `par`, and reports **PARTIAL**, naming those two as not checked.
  Nothing else is relaxed:
  - the expectations are the same file;
  - a missing `FORK_GIT` or MinIO fails the run;
  - nothing is skipped silently.

## The audit archive: public package and full archive
`verify/audit/archive/{net,rev}` holds the independent audit and the remediation review, copied from the experiment
record. Each has a `SHA256SUMS`. The gated suites `repro` and `repro-rev` run the auditors' original reproducers,
unmodified.
- **The full archive** (kept privately) includes two raw AI-agent session transcripts, and seven output files and
  copies of scripts that contain a personal home-directory path. They are listed in `WITHHELD.tsv` with their
  original SHA-256.
- **This repository carries the public package:** every other archive file, byte-identical. The reproducers need
  nothing that was withheld.
  - `verify/audit/PUBLIC-ARCHIVE.sha256` lists the public files. Each line is a verbatim line of the archive's own
    `SHA256SUMS`, which is kept unchanged.
- **`run_repro.sh` has two explicit modes:**
  - `G_ARCHIVE_CHECK=full` (the default) requires every file in `SHA256SUMS`, so here it refuses to run;
  - `G_ARCHIVE_CHECK=public` (set by `run_suites.sh … public`) checks the public manifest strictly, and checks that
    each of its lines appears in `SHA256SUMS`.

  Neither mode tolerates a missing file the other doesn't name.

## What can and cannot be reproduced here
| Evidence | Reproducible from this repository? |
|---|---|
| Unit tests, smoke test, P0 durability (SIGKILL), P1 fidelity, distributed mode (dist, dist-tcp-s3, net), object-store faults, access control (fly: Basic auth, signed packfile URIs, metadata handshake), the diagnostic CLI and its boundary (cli), hidden CG1–4 | **yes** |
| The auditors' reproducers (`repro` r01–r12b; `repro-rev` r20–r22) | **yes** (public archive package) |
| Frozen blind CG5/CG6 suite (`cg56`, `par`) | **no**: the suite is private (`CG56`) |
| The auditors' raw sessions, `coverage-inventory.md`, `unit-run.txt`, `builders-p1.txt`, the reviewer's gate-review script copies, one reproducer output | **no**: withheld (`WITHHELD.tsv`). `AUDIT.md` and `REVIEW.md` (the findings) are here. |
| Historical test records (`verify/results/`) | **no**: withheld. Re-run the suites instead. |

## Release-level tests
`verify/release/` holds:
- `smoke.py`: one packaged archive;
- `installer_test.sh` and `installer_test.ps1`: the installers, including corrupted, substituted and fail-closed
  cases;
- `readme_test.py`: the README, run literally;
- `patch_installers.py`: the installer hardening;
- `third_party_licenses.py`: the licence file;
- `privacy_scan.py`: the pre-publication scan;
- `public_source.py`: how this tree was derived.

`.github/workflows/build-test.yml` runs them natively on the five platforms. See `docs/RELEASING.md`.
