# On-disk layout (everything under `$G_DATA_DIR`)

```
$G_DATA_DIR/
  server.pid            PID of the process holding the listening socket (also the session /
                        process-group leader; written only after the socket is listening)
  server.log            stderr/stdout of the server
  meta/                 metadata store (embedded, see DECISIONS D1, MODEL §2)
    LOCK                flock()ed by the running server (one server per data dir)
    wal.<g>             write-ahead log, generation g: records [len u32][crc32 u32][payload],
                        payload = list of (lsn u64, key, put|delete, value). Torn tail truncated on recovery.
    snapshot.<g>        checkpoint (all keys) that wal.<g> continues from (absent until first checkpoint)
  blobs/                blob store (immutable)
    tmp/                staging for incoming/compaction packs; emptied at start (crash leftovers)
    repos/<enc(repo)>/packs/
      <id>.pack         a pack EXACTLY as received from the client (L0) or as written by compaction
                        (covers) / twinning (thick twins); <id> = SHA-1 trailer of the pack; file
                        mode read-only. NOTE: L0 packs from stock `git push` are usually THIN
                        (REF_DELTA against objects stored in other packs), exactly as git sent
                        them, so `git index-pack` on such a file alone needs `--fix-thin` and the
                        bases; covers and thick twins are self-contained.
      <id>.pidx         sidecar index (binary, checksummed): per entry oid/offset/type/delta base,
                        the pack's exact prerequisites (graph refs not contained), thin-pack bases
                        with the pack each was resolved from, annotated tags (tag -> target)
  events/events.jsonl   structured event log (MODEL §6), one JSON object per line
```

`enc(repo)`: `[A-Za-z0-9._-]` kept, every other byte as `%XX` (so `a/b` -> `a%2Fb`).

There is no Git repository on the server side: no `objects/`, no loose objects, no `refs/`,
no `HEAD`/`config`. Pushed packs are never unpacked or rewritten; the decode keeps only the
sidecar index. Packs are never deleted (L0 and covers at every level are kept). Packs whose
metadata transaction never committed (crash between blob write and commit) remain as
unreferenced files and are ignored.

## Metadata keys (values are JSON)

| key | value |
|---|---|
| `repo\x01<R>` | `{name, trunk:"refs/heads/main", created}` |
| `ref\x01<R>\x01<refname>` | `{seq, tip, ls, peeled}` — current pointer (tip = 40 zeros when deleted) |
| `rv\x01<R>\x01<refname>\x01<seq:016x>` | ref version `{r, s, tip, pack, deps:[{r,s}], linked, ls, peeled, push, ts}` |
| `tip\x01<R>\x01<oid>` | `{r, s}` first ref version whose tip is `<oid>` |
| `pack\x01<R>\x01<id>` | `{id, level, objects, bytes, thin, ref_delta, rvs:[{r,s,ls,tip}], cover}` |
| `twin\x01<R>\x01<packid>` | id of the thick twin (self-contained copy) of a thin L0 pack |
| `cover\x01<R>\x01<refname>\x01<level>\x01<hi:016x>` | `{r, level, lo, hi, pack, deps, tips, linked, objects, bytes}` |

Inspecting: the WAL is plain framed binary with JSON values; `strings meta/wal.0` shows keys and
values. The `.pidx` format is defined in `server/src/pack.rs` (`PackIndex::serialize`).
