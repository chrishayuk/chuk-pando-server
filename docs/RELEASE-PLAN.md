# First public release: plan (2026-11-07; 0.1.0 scope amended 2026-11-08, §6.0; nothing published or tagged)

**Goal:** someone who has watched the video installs a prebuilt binary, runs a local Pando server, pushes and clones
with their own Git, and explores the architecture (`pando demo`). They don't clone the source or install Rust,
Docker, MinIO or anything else. The same first five minutes on macOS, Linux and Windows.

**Out of scope for the quickstart:** distributed mode (metadata service, S3/Tigris, Fly.io). It stays in the
binary, documented under `docs/` as an advanced option.

## 0. Decisions needed before anything is published (owner)
| # | Decision | Why it blocks | Recommendation |
|---|---|---|---|
| D1 | **Licence**, plus the provenance and licence review required by the standing rule ("not open source before the review") | A public binary without a licence isn't usable by anyone; the review was required before any open-source claim. MinIO (AGPLv3) is only a test dependency; it's not shipped. | Apache-2.0 OR MIT (the Rust norm). Review the crate licences with `cargo deny check licenses`; ring and webpki-roots are ISC/MIT/Apache-style. |
| D2 | **Public GitHub repository** for the source and Releases. The checkout currently has no remote. | GitHub Releases, the installer URL, native Windows/Linux CI and attestations all need it. | `github.com/<owner>/<name>` (decided: chuk-pando-server). Free Actions minutes on public repositories cover the macOS and Windows runners. |
| D3 | **What the public tree contains** | The repository holds research artefacts and test results; the experiment record lives elsewhere. | Publish `chuk-pando-server` as is: docs move under `docs/`, results stay in the repository, `PROVENANCE.md` reviewed. |
| D4 | **The signing key** for update signatures (Ed25519) | `pando update` must verify authenticity, not only a checksum from the same server. | Generated offline, private key in a password manager plus a GitHub Actions secret; the public key compiled into the binary. |
| D5 | **The installer URL** | It appears in the video and the README, so it must be stable. | GitHub Releases' `latest/download/pando-installer.sh` (no domain needed). Or a short domain later, redirecting to it. |
| D6 | **Code signing** (Apple notarisation; Windows Authenticode) | Without it, macOS Gatekeeper and Windows SmartScreen warn on first run. The curl and `irm` installs avoid most Gatekeeper prompts, but Windows SmartScreen may still warn. | First release unsigned, with the warning and workaround documented. Apple Developer ID ($99/yr) and a Windows certificate (from about $200/yr) as a follow-up you approve. |

## 1. Platform matrix (all five are mandatory; the release is blocked if any fails)
| Platform | Target | Build | Native test runner | Current status |
|---|---|---|---|---|
| macOS, Apple Silicon | `aarch64-apple-darwin` | CI (`macos-15`) and locally | `macos-15` | builds; smoke test passes in CI; all suites run here (local reference platform) |
| macOS, Intel | `x86_64-apple-darwin` | CI (`macos-15-intel`) | `macos-15-intel` | builds; smoke test passes natively in CI (`release-1`) |
| Linux x86-64 | `x86_64-unknown-linux-gnu` (glibc 2.31+; or musl static, see §5) | CI (`ubuntu-22.04`) | `ubuntu-22.04` | builds; unit tests and smoke test pass natively in CI; full suites not run on Linux |
| Linux ARM64 | `aarch64-unknown-linux-gnu` | CI (`ubuntu-22.04-arm`) | same, native ARM | builds; unit tests and smoke test pass natively in CI; full suites not run on Linux |
| Windows x86-64 | `x86_64-pc-windows-msvc` | CI (`windows-2022`) | `windows-2022` with Git for Windows | builds and passes the release smoke test (`release-1`; `docs/WINDOWS.md`) |

"Supported" means the platform passes the release gate (§6) from the published artefact. Compiling is not enough.

## 2. CLI plan (one binary, the same commands everywhere)
| Command | Today | Proposed |
|---|---|---|
| `pando` (no arguments) | `usage: pando start\|stop\|...` | a short welcome: what Pando is, `pando demo`, `pando serve`, `pando --help` |
| `pando --help`, `pando <cmd> --help` | none | proper help (hand-written; no new argument-parsing crate needed) |
| `pando serve` | requires `--data` and `--port`; port 0 by default; repositories must be created first | Defaults: `--data ./pando-data` (created if missing) and `--port 8080` (a clear error if taken, suggesting `--port`). Repositories are **created on first push** (local mode; `--no-autocreate` to disable). It prints the clone and push URLs, runs in the foreground, stops cleanly on Ctrl-C, and binds to loopback unless `--bind` and `PANDO_AUTH` are given. |
| `pando demo` | none | §3 |
| `pando stats [--url]` | `/_stats` exists (JSON) | human-readable counters from a running server: packs, covers, copies, metadata reads, pushes, fetches |
| `pando trace [--data DIR] [--push ID]` | `verify/trace/timeline.py` (Python, needs the source) | built in: the causal timeline (push → pack → commit → index refresh → fetch) from the event log, in the terminal |
| `pando update`, `pando update --check` | none | §4. An update check (cached for 24 h, non-blocking, 1 s timeout) on interactive commands only. It is off when `CI` is set, when stdout is not a terminal, or with `PANDO_NO_UPDATE_CHECK=1`. It is never run by `serve` in the background. |
| `pando version` | prints "pando 0.1" | version, commit, target, build date, and the update channel |
| `pando doctor` | none | checks: Git present and its version; port free; data directory writable; file-lock support; clock; whether an update is available |
| `start`, `stop`, `create-repo`, `maintain`, `meta-serve`, `sim`, `fuzz` | exist | **kept unchanged.** The test harness (INTERFACE.md) and the distributed mode use them; they are listed under "advanced" in the help |

## 3. `pando demo`: real server, real Git protocol
1. Start a disposable server in a temporary directory, on a free loopback port, with the real `serve` code path
   in-process.
2. Using the user's own `git`, which is required and checked:
   - create a sample repository and make about 20 commits to a few files;
   - push them one at a time, so each push stores one immutable pack and one ref version;
   - push a second branch built on top of `main`.
3. Run `maintain`, so covers (L1/L2) are built and self-contained copies prepared.
4. Clone and fetch, with and without packfile URIs:
   - show how many packs were planned;
   - show which were reused by URI rather than rebuilt;
   - show the metadata reads per fetch.
5. Print a narrated summary taken from the **real event log**, the same causal timeline as `pando trace`. Example
   lines:
   - "push 7 stored pack 3f2a… (2 objects, 412 B) and committed main@7 at LSN 9";
   - "fetch planned 3 packs: L2 cover 1–4, L1 cover 5–8, pack 9";
   - "no pack was rewritten: covers are new files".
6. End with the next steps: `pando serve`, `git clone http://localhost:8080/demo`, `pando trace`, and the docs link.
   The temporary directory is deleted unless `--keep` is given.

The demo is a thin driver over existing code: `serve`, the git client and the event log. It adds no special demo
logic to the server. It is tested in CI on all five platforms.

## 4. Installation and update: security design
**Build and packaging:** cargo-dist 0.34 (maintained; last release 2026-11-02). It generates:
- the GitHub Actions release workflow for the five targets;
- archives (`.tar.xz` and `.zip`) with `SHA256SUMS`;
- `pando-installer.sh` (macOS/Linux) and `pando-installer.ps1` (Windows), which detect the OS and architecture and
  install to `~/.pando/bin` (or `%USERPROFILE%\.pando\bin`) without root or admin;
- GitHub artifact attestations (SLSA build provenance, verifiable with `gh attestation verify`).

**Updater: not axoupdater.** It would add an HTTP stack (reqwest and its dependencies) to a binary that today has 14
direct dependencies, and it verifies by checksum from the same release, which you ruled insufficient on its own.
Proposed instead: `pando update` built from parts already in the binary:
- **rustls** HTTPS (as in the S3 client);
- **ring's Ed25519 verification** (already a dependency).

`pando update` does the following:
1. Fetch the latest `manifest.json` (version, minimum compatible storage format, and per-target archive URLs and
   SHA-256s) and `manifest.json.sig` from GitHub Releases.
2. Verify the signature against the **public key compiled into the running binary**. The private key signs in the
   release workflow (D4). A compromised download host can't produce a valid manifest.
3. Download the archive for this target into a temporary file **in the install directory** (same filesystem), and
   check its SHA-256 against the signed manifest.
4. Replace atomically:
   - Unix: `rename(new, pando)`, after keeping `pando.previous`.
   - Windows: the running executable can't be overwritten, but it can be renamed. Rename `pando.exe` to
     `pando.previous.exe`, then move the new file into place (`MoveFileEx` with write-through).
5. **Running servers.** If a `pando serve` is running from this executable, it is detected through the data
   directory lock and pid files. The update still installs; the server keeps the old code until restarted, and the
   user is told so. Nothing restarts it automatically.
6. `pando update --rollback` restores `pando.previous`. `pando update --check` only reports.
7. **The update never touches repository data.** A binary refuses to open a data directory whose storage-format
   version it doesn't know (a format marker file is written from this release onward). Format migrations, if ever
   needed, would be a separate explicit command.

**First-install trust:** the installer is fetched over HTTPS from GitHub and checks the archive's SHA-256. For
stronger assurance, the README documents `gh attestation verify`. Every later update is verified by signature.

## 5. Native Windows port (inventory from the current code; 56 Unix-specific sites in 5 files)
| Area | Unix today | Windows design | Durability argument to test |
|---|---|---|---|
| Data-directory and instance locks | `libc::flock` | `std::fs::File::try_lock` (stable since Rust 1.89; `LockFileEx` on Windows, `flock` on Unix) | lock exclusion across processes; released on process death |
| Positioned reads (`pread`) | `FileExt::read_exact_at` | `std::os::windows::fs::FileExt::seek_read`, in a loop | concurrent readers on one handle |
| Durable publication (tmp → hard link without replace; fsync dir) | `link(2)` + `fsync(dir)` | `CreateHardLinkW` (fails if the target exists; NTFS) + `FlushFileBuffers` on the file. NTFS journals metadata, so the directory needs no flush, and a link is atomic. Where links aren't supported (FAT, exFAT, some network shares), **refuse to start** rather than weaken the guarantee. | SIGKILL-equivalent (`TerminateProcess`) during publication; power-loss semantics stated as NTFS journal semantics, not tested |
| Read-only immutable packs | `set_readonly` | the same API (the read-only attribute) | P0's "never replaced" test |
| Deleting and renaming open files | allowed on Unix | open handles must use `FILE_SHARE_DELETE`; temporary files are cleaned up at start, not while open | P1Recompress, tmp sweep |
| Daemon `start`/`stop` (`setsid`, `kill`) | POSIX signals | `CREATE_NEW_PROCESS_GROUP`, `GenerateConsoleCtrlEvent`, then `TerminateProcess` | harness start/stop, crash restart |
| `serve` shutdown | SIGTERM | Ctrl-C and Ctrl-Break handlers (tokio `signal::windows`) | clean stop leaves no lock |
| File-descriptor limit, `getrusage` | `setrlimit`, `getrusage` | no-op; `GetProcessTimes` | — |
| Metadata service over a Unix socket | `UnixListener` | TCP only on Windows (distributed mode is advanced) | — |
| Paths | `/` assumptions in the URL ↔ path mapping | repository names are validated; paths built with `Path::join` only; reserved names (`CON`, `NUL`, …) rejected | path-traversal and reserved-name tests |
| Recompression shells out to `git` | `git` on `PATH` | Git for Windows; the binary is found as `git.exe` | P1Recompress |

**The suites must run on Windows too.** Today they are Python + bash with `run.sh`, and the harness uses POSIX
signals.
- The Python suites (hidden CG1–4, CG5/6, p0, p1, dist and net) run with Python on Windows. `run.sh` gains a
  `run.ps1` twin, and the harness's kill and start calls get Windows equivalents. That changes harness code shared
  with the frozen suites, so it would be **added as a platform adapter**, leaving the frozen test files unchanged.
- **Not portable as is:** `tests/*.sh` (inherited, not gated) and MinIO-based tests on Windows (MinIO has a Windows
  build; to be checked).

## 6. Release gate

### 6.0 Scope of release 0.1.0 (owner decision, 2026-11-08)
These items are **deferred**. Nothing in the README, the help text or the release notes may claim they exist:
- **`pando doctor`**
- **`pando demo`**
- **`pando update`**: it waits for signed-update infrastructure (D4: the key, a signed manifest, rollback). 0.1.0
  ships no self-updater (`install-updater = false`). To upgrade, run the installer again.
- **The full regression suites on Linux and Windows.** They need the platform adapter (§5); they run on macOS only.
  On every platform, the release is gated by:
  - the unit tests;
  - the packaged smoke test;
  - the installer tests;
  - the README-first test.
- **Code signing** (D6: Apple notarisation, Windows Authenticode) is unavailable for 0.1.0. The binaries are
  unsigned, and the README documents Gatekeeper and SmartScreen.

**Fixed decisions for 0.1.0:**
- The server's trunk is **`main`**: every repository's `HEAD` is `refs/heads/main` (FEATURES.md). Users push a
  `main` branch, and the README says so.
- A **non-loopback `--bind` requires `PANDO_AUTH`**. Without it the server refuses to start.
- **Both installers verify the archive's SHA-256 and fail closed.** This checks integrity against the digest in
  the same release. It does not authenticate the publisher independently; GitHub attestations do that, verified
  with `gh attestation verify`.
- **The release is first published as a draft** (`docs/RELEASING.md`).

### 6.1 The gate for 0.1.0 (a release is published only if every row passes)
1. **Strict regression gate** (`verify/run_suites.sh`, `--strict`) on macOS ARM64.
2. **All five targets built** by `build-test.yml` on native runners. A missing artifact fails the release.
3. **Native packaged tests on each of the five runners:**
   - unit tests;
   - `verify/release/smoke.py` on the archive: version, serve, port and data locks, bind refusal and Basic auth,
     push, clone, fsck, explain, explore, incremental fetch, 8 concurrent branches, hard kill and restart;
   - the installer tests: valid install; corrupted and substituted archives refused; fail closed without a SHA-256
     tool on macOS and Linux;
   - the README-first test.
4. **Draft-release checks** on the artifacts actually uploaded to the draft (`docs/RELEASING.md`):
   checksums, `gh attestation verify` for every archive, native installers and the README-first test from the
   draft's assets.
5. **Publication** (`gh release edit --draft=false`) only on the owner's explicit approval.

### 6.2 The original gate (kept for later releases)
1. **Strict regression gate** (`verify/run_suites.sh`, `--strict`) on macOS ARM64, as today. Plus the same suites on
   Linux x86-64, ARM64 and Windows, against **their own platform's expectations file**. Any platform-specific known
   failure is listed with its reason; there are no silent omissions.
2. **Build all five targets.** A missing artefact fails the release.
3. **Clean-machine smoke test from the published artefacts**, per platform (fresh runner, no Rust, no source
   checkout). It follows the README exactly:
   - install with the installer one-liner;
   - `pando version`, and `pando doctor` *(deferred)*;
   - `pando demo` *(deferred)*;
   - `pando serve` in the background, then with stock Git: push, clone, fsck, incremental fetch with packfile
     URIs;
   - kill the server hard, restart it, and check that the acknowledged refs are intact;
   - concurrent pushes to 8 branches.
4. **Update test** *(deferred with `pando update`)*. Install the previous release (or a staged `v0.0.0-test`
   pre-release), then `pando update` to the candidate:
   - the signature is verified and the binary replaced;
   - `pando update --rollback` restores the previous one;
   - repository data is untouched across both;
   - **negative cases:** a tampered manifest, a wrong key and a wrong checksum are all refused, and the existing
     binary is left intact.
5. **Release.** Only then: tag, publish the GitHub Release, sign the manifest and attach the attestations.

**Order of work:**
1. Windows port and suite adapter.
2. CLI defaults and help.
3. `demo`, `trace` and `stats`.
4. `update`.
5. cargo-dist CI.
6. Clean-machine tests.
7. Dry-run release on a pre-release tag in a private fork.
8. The first public release.

The **dry-run artefacts** for the platforms buildable on this Mac are produced locally (§7). The rest need the CI
runners, which need D2.

## 7. Status
- Release procedure and checks: `docs/RELEASING.md`.
- The public source, and what it withholds: `docs/PUBLIC-SOURCE.md`.
- Internal release records (dry run, checkpoints, owner decisions) are kept in the private repository.
