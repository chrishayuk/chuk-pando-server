# Windows: what is the same, what differs (release 0.1.0)

Every operating-system call the server makes is in `server/src/sys.rs`. On Unix each function there is exactly the
call the code made before the port, so Unix behaviour is unchanged. On Windows each one has the equivalent that keeps
the same guarantee, or the server refuses. A refusal is a clear error, never silently weaker behaviour.

## Guarantees kept, and how
| Guarantee | Unix | Windows |
|---|---|---|
| One server per data directory; a dead server's lock is released | `flock(LOCK_EX\|LOCK_NB)` on `meta/LOCK` | `LockFileEx` (exclusive, fail immediately) through `std::fs::File::try_lock`; released when the handle closes or the process dies |
| A pack, index or substitution map is published once and never replaced | `link(2)` from the staging file (fails if the name exists) | `CreateHardLinkW` through `std::fs::hard_link` (fails if the name exists; NTFS) |
| A published file's data is durable before any acknowledgement | `fsync` (`F_FULLFSYNC` on Apple) of the file | `FlushFileBuffers` of the file (`sync_all`) |
| A new directory entry is durable before any acknowledgement | `fsync` of the directory | The directory is opened with `FILE_FLAG_BACKUP_SEMANTICS` and flushed with `FlushFileBuffers`. If the file system refuses (FAT, exFAT, some network shares), the error propagates and the push **fails**. Durability is never skipped. |
| Immutable packs are read-only | mode bits | the read-only attribute (same std API) |
| Concurrent readers of one pack handle | `pread` | `ReadFile` with an explicit offset (`seek_read`), looped; the file cursor is never used |
| The listening port is not shared with another process | `SO_REUSEADDR` (Unix semantics: reuse after `TIME_WAIT` only) | `SO_REUSEADDR` is **not** set. On Windows it would let a second server bind a port that is already in use. A taken port fails with "address in use". |
| Operation tokens and the metadata-service nonce are unguessable | `/dev/urandom` for the nonce; the system RNG through ring for tokens | the system RNG through ring for both |
| Repository names map to distinct directories | every name `valid_repo_name` accepts | Names Windows cannot store are refused: device names (`CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`, `LPT1`–`LPT9`, with or without an extension) and names ending in `.` (Windows strips the dot, so `a.` and `a` would collide). |

## Behaviour that differs
- **Stopping.** `pando serve` stops cleanly on Ctrl-C, Ctrl-Break, or when its console window closes.
  - `pando stop` (the test-harness command for a server started by `pando start`) has no signal it can send to a
    detached process on Windows, so it terminates the process (`TerminateProcess`), the equivalent of SIGKILL.
  - That is safe by design: every acknowledged push is already durable, and the crash tests kill the server this
    way.
  - The difference: the server's own shutdown steps don't run. Its `server_stop` event isn't logged, and the pid
    file is removed by `pando stop` rather than by the server.
- **`pando start`** detaches the server into a new process group with no console (`CREATE_NEW_PROCESS_GROUP |
  DETACHED_PROCESS`) instead of `setsid`.
- **File-descriptor limit.** Unix raises `RLIMIT_NOFILE`. Windows has no per-process handle limit of that kind, so
  there is nothing to raise.
- **Power loss.** On Windows, durability after power loss rests on NTFS's metadata journal and on `FlushFileBuffers`
  of the file and its directory. That is stated here from the documented semantics. Like on the other platforms, it
  is not tested by cutting power. Process-kill crash safety *is* tested on Windows: the release smoke test kills
  the server with `TerminateProcess` and checks every acknowledged ref after a restart.

## Not supported on Windows in this release (refused at start, with a message)
- **The metadata service over a Unix socket** (`PANDO_META=/path`, `pando meta-serve --sock`). Use
  `tcp:HOST:PORT`.
- **A shared pack store** (`PANDO_BLOBS`, several frontends on one directory). Each instance publishes its staging
  area by renaming a directory while that directory's lock file is open, and Windows refuses that rename. Without
  the rename, the area could be seen unlocked and swept by another frontend (audit F3). The local store and the
  S3-compatible store work.

Both are part of the advanced distributed mode, not the local quickstart.

## Line endings and paths
- The Git protocol and every file the server writes are binary or LF-only, and are read back by the server itself.
  `core.autocrlf` on the client changes the client's working tree only, never what is pushed.
- Paths are built with `Path::join`. A repository's directory is a single, escaped path component (`blob::enc`), so
  `/` in a repository name never becomes a path separator.
- Recompression runs the user's `git` (`git.exe` from Git for Windows on `PATH`, or `PANDO_COMPACT_GIT`) with
  `GIT_CONFIG_GLOBAL=NUL`.

## How Windows was tested
On the GitHub Actions `windows-2022` runner (Windows Server 2022, NTFS, Git for Windows), from the packaged `.zip`:
- the unit tests, including `sys::tests`: lock exclusion and release, positioned reads, directory flush on NTFS,
  process liveness, RNG, and reserved names;
- `verify/release/smoke.py`: serve, port-in-use, data lock, push, clone, fsck, `explain`, `explore`, incremental
  fetch, 8 concurrent branch pushes, and a hard kill with restart;
- the PowerShell installer test;
- the README-first acceptance test (`verify/release/readme_test.py`), which runs the README's PowerShell blocks
  literally in Windows PowerShell 5.1: install, serve, push, explain, explore, both clones, uninstall.

How these are run: `docs/RELEASING.md`. The Python suites that drive `run.sh` (hidden CG1–4, CG5/6, P0, P1,
dist, net, CLI) are bash-based and were run on macOS only. Running them on Windows needs the platform adapter
described in `docs/RELEASE-PLAN.md` §5, which this release does not include.
