# Durability guarantees (as of the P0 changes)

## What "durable" means here
Every file and directory sync goes through Rust's `File::sync_all` / `File::sync_data`.

- **Apple platforms:** both are implemented as `fcntl(fd, F_FULLFSYNC)`, which flushes the drive's write cache, not just the OS buffers. Source: Rust std `library/std/src/sys/fs/unix.rs`, `os_fsync` / `os_datasync` under `target_vendor = "apple"`. This was checked in the installed rust-src (1.96); the server is built with 1.98, and the same implementation is assumed.
- **Directories on APFS:** `F_FULLFSYNC` on a directory descriptor succeeds; checked on this host on 2026-11-06.
- **Other Unix:** `sync_all` is `fsync(2)`, and `sync_data` is `fdatasync(2)` where available.
- **Errors:** `fsync_dir` errors are returned to the caller, never ignored. A push whose directory entries cannot be made durable is not acknowledged.

## Order of operations for an acknowledged push
1. The incoming pack is streamed to a temporary file under `blobs/tmp/`, validated by a single-pass decode, then flushed (`ingest.rs`, `sync_all` after the last byte).
2. The pack's index sidecar (`.pidx`) is written to a temporary file and flushed.
3. The first time this process writes to a repository, `blobs/repos/<repo>/packs` is created. Each directory from there up to the blob root is synced into its parent (`Blobs::ensure_durable_dir`). The blob root, `repos/`, `tmp/` and the metadata directory are synced into their parents at startup.
4. The pack, then the index, are published **write-once** with `hard_link`, which fails if the name exists. They are made read-only, and the pack directory is synced. If the pack id was already published, the existing files are kept. The directory is still synced before continuing, so a concurrent installer's entry is also durable.
5. One metadata transaction records the pack, the ref version (tip, pack, dependency versions), the tip index and the ref pointer. It is committed through the write-ahead log with `sync_data` (group commit).
6. Only then is `ok <ref>` sent to the client.

**Compaction covers:** each cover's pack and index are published by the same write-once path, and synced, before the transaction that makes the cover visible to the planner.

## What is verified, and how
| Property | Evidence |
|---|---|
| Acknowledged pushes survive SIGKILL and restart | Hidden CG2 suite; CG6 crash-after-ack test; the P0 repush-under-maintain test |
| No published pack or index is ever replaced | `blob::tests::install_never_replaces_existing_index` (fails on the seed code, passes now); `install_twice_keeps_first_copy`; P0 black-box guard |
| No pack is ever deleted | By code: there is no deletion path for packs. Orphan collection is not implemented (gap list item 12) |
| Many packs under a low soft fd limit | P0 guard (`ulimit -S -n 256`, 400 pushes, restart) |

## What is NOT verified
- **Power loss** has not been tested. The ordering above is established by reading the code and by the platform semantics of `F_FULLFSYNC`. SIGKILL tests only show the process-crash case.
- Only APFS on macOS has been exercised. Linux and other filesystems are untested.
- Drive firmware that ignores cache flushes is outside what software can guarantee.
