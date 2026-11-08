# Pando — a Git backend built for agent-scale development

An independent, experimental reproduction of the Git backend architecture described in OpenAI's
[**Scaling Git at OpenAI**](https://openai.com/index/scaling-git-at-openai/).
Use ordinary Git commands while exploring immutable packs, versioned branches, concurrent updates and reusable fetches.

## Quickstart
**1. Install** (one binary; no root or administrator rights; [details and integrity](#install-details)).

macOS and Linux:
<!-- readme-test: install unix -->
```sh
curl --proto '=https' --tlsv1.2 -LsSf https://github.com/chrishayuk/chuk-pando-server/releases/latest/download/pando-installer.sh | sh
. "$HOME/.config/pando/env.sh"    # or open a new terminal
pando version
```

Windows (PowerShell):
<!-- readme-test: install windows -->
```powershell
powershell -ExecutionPolicy Bypass -c "irm https://github.com/chrishayuk/chuk-pando-server/releases/latest/download/pando-installer.ps1 | iex"
$env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + $env:Path    # or open a new terminal
pando version
```
> **What the installers check:**
> - Both installers check the archive's SHA-256 against the digest published in the same GitHub Release, and refuse
>   to install on a mismatch. The macOS and Linux installer also refuses if it finds no SHA-256 tool.
> - This is an **integrity check, not independent proof of who published the file**. Anyone able to replace files
>   in the release could replace the digest as well.
> - To verify the publisher independently, check the archive's GitHub attestation with `gh attestation verify`. See
>   [Verifying a download](#verifying-a-download).

**2. Run a server** (terminal 1). It keeps its data in `./pando-data`, listens on `localhost:8080` only, and stops
with Ctrl-C:
<!-- readme-test: serve all -->
```sh
pando serve
```

**3. Push a repository to it with ordinary Git** (terminal 2). If you have never used Git on this machine, first tell
it who you are:
<!-- readme-test: identity all -->
```sh
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```
Then:
<!-- readme-test: create unix -->
```sh
mkdir hello && cd hello
git init -b main
echo "# hello" > README.md
git add README.md && git commit -m "hello"
git remote add origin http://localhost:8080/hello
git push -u origin main
pando explain last-push
```
<details><summary>Windows PowerShell</summary>

<!-- readme-test: create windows -->
```powershell
mkdir hello; cd hello
git init -b main
Set-Content README.md "# hello"
git add README.md; git commit -m "hello"
git remote add origin http://localhost:8080/hello
git push -u origin main
pando explain last-push
```
</details>

The server creates the repository on its first push. Its trunk is `main`, so use `main` as your branch name
(`git init -b main`, or `git branch -M main` in an existing repository).

**4. Push again, and explore.**
<!-- readme-test: second unix -->
```sh
echo "print('hi')" > app.py
git add app.py && git commit -m "second"
git push
pando explain last-push
pando explore history
pando explore fetch --from empty
```
<details><summary>Windows PowerShell</summary>

<!-- readme-test: second windows -->
```powershell
Set-Content app.py "print('hi')"
git add app.py; git commit -m "second"
git push
pando explain last-push
pando explore history
pando explore fetch --from empty
```
</details>

This is the output of a real run (abbreviated ids are from that run):
```
$ pando explain last-push
RECORDED by the server for your push to hello:

  refs/heads/main  bd420223f8 -> 88dcbad2f0   now version 2
      depends on: refs/heads/main@1
      LOCAL: 3 object(s) between the old and new tip in your repository

  stored as one immutable pack ac58b1dd7b: 3 object(s), 278 bytes
  server time 19.8 ms

  STATE now: self-contained copy for packfile-URI clients: not needed (self-contained)

$ pando explore history
STATE: history of refs/heads/main (2 versions)

  v2 tip 88dcbad2f0  pack ac58b1dd7b  depends on refs/heads/main@1  (extends the previous version)
  v1 tip bd420223f8  pack e898b0daf2  depends on nothing

$ pando explore fetch --from empty
HYPOTHETICAL: the plan the server would use now for this fetch. Nothing was sent or changed.

  want  refs/heads/main (version 2)
  have  nothing (a fresh clone)

   1. push pack (refs/heads/main@1)                3 objects 214 bytes   uri
   2. push pack (refs/heads/main@2)                3 objects 278 bytes   uri

  2 pack(s), 2 by packfile URI; 0 cover(s); 2 planning step(s); 3 metadata read(s).
  Without covers this fetch would need one pack per push it depends on: 2 pack(s).
```

**5. Clone it back.** A plain clone has the pack data re-sent inline. With packfile URIs, stock Git downloads the
stored push packs directly, but only when asked to (`fetch.uriprotocols`):
<!-- readme-test: clone all -->
```sh
cd ..
git clone http://localhost:8080/hello hello-copy
git -c fetch.uriprotocols=http clone http://localhost:8080/hello hello-uri
```
Both `hello-copy` and `hello-uri` contain `README.md` and `app.py`.

## What can I explore?
Run these inside a checkout whose remote is a Pando server. Each view is labelled with what it shows.
| Command | Label | Shows |
|---|---|---|
| `pando explain last-push` | RECORDED | what the server recorded for your last push: ref version n → n+1, its dependencies, the immutable pack it stored, timings |
| `pando explain --op TOKEN` | RECORDED | the same for the push that printed `pando: operation TOKEN` |
| `pando explore history [REF]` | STATE | the ref's versions, their packs, and the versions each one depends on |
| `pando explore covers [REF]` | STATE | the compaction hierarchy (covers L1–L4) built over the push packs, which stay unchanged |
| `pando explore fetch --from empty` | HYPOTHETICAL | the plan a fresh clone would get: covers, tail packs, inline or packfile URI, metadata reads. Nothing is sent. |
| `pando explore fetch --from COMMIT` | HYPOTHETICAL | the same for an incremental fetch from `COMMIT` |
| `pando status` | STATE | the remote, whether it is a Pando server, and your branches' versions |

- **RECORDED** is what the server did, from its event log.
- **STATE** is what it holds now.
- **HYPOTHETICAL** is what it would do, computed read-only.
- `--json` gives machine-readable output (schema `pando.explain/1`). `pando --help` lists every command.

**What you can do with it:**
- explore OpenAI's published design with your own repositories and your own Git;
- watch concurrent updates: clone the repository several times, push a branch from each at once (as several agents
  would), and see every push become its own ref version (`pando explore history <branch>`);
- see fetch planning and covers at work: push a few more commits (covers are built every 4 versions), then compare
  `pando explore covers` and `pando explore fetch --from empty`;
- run it across machines: several frontends sharing a metadata service and an S3-compatible store
  (`docs/DISTRIBUTED.md`, `docs/NETWORK-STORAGE.md`; advanced).

## What this is, and isn't
> - This is **not OpenAI's code**, and it is not affiliated with or endorsed by OpenAI. It was built from the
>   published article. Most of it was written by AI coding agents under the author's direction
>   ([PROVENANCE.md](PROVENANCE.md)).
> - **OpenAI's design** is what the article describes. **This reproduction's numbers** are our own measurements on
>   our own machines (`docs/`). Nothing here claims equivalence with OpenAI's production system or its figures.
> - OpenAI also published a modified Git **client**, [github.com/openai/git](https://github.com/openai/git). It is
>   a client, not the server. This server works with stock Git, and is also tested against that client's features
>   (explicit haves, parallel packfile-URI downloads).
> - It was independently audited; every finding was fixed or explicitly accepted (`verify/audit/`,
>   `docs/AUDIT-NET-REMEDIATION.md`).
> - It is experimental software. Don't make it the only copy of anything you care about.

## How it works
- **A push** streams into one new pack file, published write-once under its content hash. The ref then moves in a
  single metadata transaction that creates *version n+1* of the ref, recording its tip, its pack and the versions
  it depends on. Nothing is ever unpacked into a repository or rewritten.
- **Compaction** runs in the background and builds covers: new packs that span many pushes, at levels L1 to L4.
  The packs they cover stay where they are.
- **A fetch** is planned from ref versions and covers: the largest aligned covers, then the remaining push packs.
  Clients that ask for packfile URIs download those packs directly instead of having them re-encoded.

The details are in `MODEL.md`, `DECISIONS.md` and `docs/`. `docs/record/01-claims-ledger.md` maps the article's
claims to this implementation.

## Install details
**Requirements:** Git 2.28 or newer (for `git init -b`; Git for Windows on Windows). Packfile-URI clones were
tested with Git 2.54 (Apple Git-157), 2.55 and 2.56 (Git for Windows); the minimum Git version for
`fetch.uriprotocols` has not been established here.

**What the installer does:**
- Downloads the archive for your platform from this repository's GitHub Release, over HTTPS.
- Checks the archive's SHA-256 against the digest written into the installer script at release time, and refuses to
  install on a mismatch:
  - on macOS and Linux, with `sha256sum`, `shasum -a 256` or `openssl dgst -sha256`; if none of them exists, it
    refuses rather than installing unverified;
  - on Windows, with `Get-FileHash`.

  The digest and the archive come from the same release. This is integrity, not independent authentication (see
  [Verifying a download](#verifying-a-download)).
- Puts `pando` in `~/.pando/bin` (`%USERPROFILE%\.pando\bin` on Windows).
- Adds that directory to your `PATH`:
  - on macOS and Linux, it writes `~/.config/pando/env.sh` and appends `. "$HOME/.config/pando/env.sh"` to the
    shell profiles that exist (`~/.profile`, `~/.bashrc`, `~/.zshrc`, ...); for fish,
    `~/.config/fish/conf.d/pando.env.fish`;
  - on Windows, it changes your user `PATH` (registry, `HKCU\Environment`).
- Writes an install receipt: `~/.config/pando/pando-receipt.json`, or `%LOCALAPPDATA%\pando` on Windows.

To install **without** changing your profile or `PATH`, set `PANDO_NO_MODIFY_PATH=1` (PowerShell:
`$env:PANDO_NO_MODIFY_PATH = "1"`) before running the installer, then run `~/.pando/bin/pando` directly.

**Without the installer:** download the archive from the
[Releases page](https://github.com/chrishayuk/chuk-pando-server/releases), verify it (below), and put `pando` anywhere
on your `PATH`.

**Upgrading:** run the installer again. There is no self-updater in 0.1.0.

Code signing is not available for 0.1.0 (no Apple notarisation, no Windows Authenticode):
- **macOS:** an archive downloaded with a browser may be blocked by Gatekeeper. The curl install avoids that.
- **Windows:** SmartScreen may warn on first run.

### Verifying a download
- **Integrity (catches a corrupted or swapped file, but not a compromised release):** compare the archive's SHA-256
  with `sha256.sum` from the same release:
  - `shasum -a 256 pando-*.tar.xz` on macOS;
  - `sha256sum pando-*.tar.xz` on Linux;
  - `Get-FileHash pando-x86_64-pc-windows-msvc.zip` in PowerShell.

  The digest comes from the same place as the archive, so anyone able to replace the archive there can replace the
  digest too.
- **Authenticity (independent of where you downloaded it):** each release archive has a GitHub artifact
  attestation, a signed statement that the file was built by this repository's release workflow. With the
  [GitHub CLI](https://cli.github.com/):
  ```sh
  gh attestation verify pando-x86_64-pc-windows-msvc.zip --repo chrishayuk/chuk-pando-server
  ```
  It works the same for every platform's archive.

## Serving beyond your machine
`pando serve` listens on loopback (`localhost`) only. To accept other machines, give `--bind` **and** credentials:
```sh
PANDO_AUTH=alice:long-random-password pando serve --bind 0.0.0.0 --port 8080
```
- With `PANDO_AUTH` set, every Git, diagnostic and admin request needs HTTP Basic authentication with that user and
  password. Git asks for them, or uses its credential helper.
- Basic authentication sends the password with every request, so put the server behind TLS (a reverse proxy)
  anywhere but a trusted network.
- **The server enforces this.** `--bind` (or `PANDO_BIND`) to a non-loopback address without `PANDO_AUTH` refuses to
  start, and the error says how to set credentials. Loopback needs none.
- Without authentication, anyone who could reach the server could push to it and read every repository, including
  the operation records behind `pando explain`.

## Stop, reset, uninstall
- **Stop:** Ctrl-C in the terminal running `pando serve`.
- **Reset:** stop the server and delete its data directory (`./pando-data`, or the `--data` directory you gave).
- **Uninstall:** stop the server, then:

<!-- readme-test: uninstall unix -->
```sh
rm -rf "$HOME/.pando" "$HOME/.config/pando" "$HOME/.config/fish/conf.d/pando.env.fish"
for f in "$HOME/.profile" "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.zshrc" "$HOME/.zshenv"; do
  if [ -f "$f" ]; then sed -i.bak '/\.config\/pando\/env\.sh/d' "$f"; rm -f "$f.bak"; fi
done
```
<!-- readme-test: uninstall windows -->
```powershell
Remove-Item -Recurse -Force "$env:USERPROFILE\.pando", "$env:LOCALAPPDATA\pando" -ErrorAction SilentlyContinue
$p = ([Environment]::GetEnvironmentVariable("Path", "User") -split ";") | Where-Object { $_ -and $_ -notlike "*\.pando\bin" }
[Environment]::SetEnvironmentVariable("Path", ($p -join ";"), "User")
```

## Troubleshooting
- **"refusing to listen on 0.0.0.0:8080 without credentials":** you gave `--bind` (or `PANDO_BIND`) with a
  non-loopback address. Set `PANDO_AUTH=user:password` (see [Serving beyond your machine](#serving-beyond-your-machine)),
  or drop `--bind` to stay on loopback.
- **"address in use" on start:** another program has port 8080. Use `pando serve --port 9000`, and the same port in
  the remote URL.
- **`git clone` warns "remote HEAD refers to nonexistent ref" and checks nothing out:** the repository has no `main`
  branch. The server's trunk (HEAD) is always `main`. Push a `main` branch (`git branch -M main && git push -u origin
  main`).
- **`pando explain` says the remote is not a Pando server:** the checkout's remote (its upstream, else `origin`)
  points somewhere else. Use `pando explain last-push --remote http://localhost:8080/hello`.
- **Password prompts or a macOS Keychain dialog:** a server without `PANDO_AUTH` never asks for credentials. `pando
  explain` asks Git's credential helper (`git credential fill`) for any stored credentials for the remote, without
  prompting.
- **macOS Gatekeeper or Windows SmartScreen warnings:** see [Install details](#install-details).

## Status and evidence
| What | Where |
|---|---|
| Git features supported, and not supported | `FEATURES.md` |
| Design decisions, with alternatives | `DECISIONS.md`, `MODEL.md`, `LAYOUT.md` |
| Durability argument and crash tests | `docs/DURABILITY.md`, `verify/p0/` |
| Regression gate (every test, and every known failure with its reason) | `verify/gate/expectations.json`, `verify/run_suites.sh` |
| Independent audit and its reproducers | `verify/audit/`, `docs/AUDIT-NET-REMEDIATION.md` |
| Windows: the same guarantees, and the documented differences | `docs/WINDOWS.md` |
| How releases are built, checked and published; installer integrity | `docs/RELEASING.md` |
| What this public source contains, what was withheld, how to verify it | `docs/PUBLIC-SOURCE.md`, `docs/VERIFY.md` |

## Licence
Apache License 2.0 ([LICENSE](LICENSE), [NOTICE](NOTICE)).
- Release archives also include `THIRD-PARTY-LICENSES.txt`, covering the Rust crates compiled into the binary.
- This project contains no code from Git. It implements Git's documented protocols and pack format, and runs your
  installed `git` as a separate program for optional recompression
  ([docs/PROVENANCE-REVIEW.md](docs/PROVENANCE-REVIEW.md)).
- "Git" is a trademark of the Software Freedom Conservancy.

## For contributors
**Build from source:**
```sh
cd server && cargo build --release    # Rust 1.89 or newer; the binary is server/target/release/pando
```
- The test suites are under `verify/`, in Python and shell. macOS is the reference platform.
- `verify/run_suites.sh` runs them, and `verify/gate/gate.py` judges the results.
- `verify/release/readme_test.py` runs this README's commands as a new user would (`docs/RELEASING.md`).
- How to run the test suites and the regression gate from this repository: `docs/VERIFY.md`.
- The seed build's original README, with its harness notes, is `docs/SEED-README.md`.

**Code map (`server/src`):**
| File | Contents |
|---|---|
| `pack.rs` | object and pack codec, plus the sidecar index |
| `ingest.rs` | single-pass streaming decoder |
| `meta.rs` | metadata store: OCC, per-key commit locks, WAL group commit, checkpoints |
| `blob.rs` | blob store |
| `repo.rs` | ref versions, push commit, fetch planner |
| `packout.rs` | inline pack splicing |
| `proto.rs` | receive-pack and upload-pack v2 |
| `v0.rs` | upload-pack v0/v1 |
| `shallow.rs` | depth fetch |
| `compact.rs` | covers, L1 to L4 |
| `recompress.rs` | cover recompression with `git pack-objects` |
| `cache.rs` | cache nodes and consistent hashing |
| `http.rs` | the hyper front end |
| `diag.rs`, `cli.rs` | the read-only diagnostic API, and the `pando explain`, `explore` and `status` client |
| `events.rs` | trace log |
| `sim.rs` | deterministic simulator |
| `sys.rs` | operating-system specifics |
| `main.rs` | commands and process management |
