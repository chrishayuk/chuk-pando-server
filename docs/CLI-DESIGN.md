# Pando CLI: client, server and remote exploration (design, 2026-11-07)

**Implemented in 0.1.0:** `pando status`, `pando explain last-push`, `pando explain --op`, `pando explore fetch`,
`pando explore history`, `pando explore covers`. **Not implemented (design only):** `pando demo`, `pando explore
pushes`, `pando explore distributed`, the interactive `pando explore` menu. The release scope is
`docs/RELEASE-PLAN.md` §6.

One binary, three contexts. Git keeps working unchanged: `pando` never replaces `git` or needs a forked client.
Exploration explains what the server **did** (recorded events), what it **is** (current committed state), and what it
**would do** (a hypothetical fetch plan, computed read-only). Every value shown carries one of those three labels.

## 1. Context discovery
| Context | How it's detected | What `pando` does |
|---|---|---|
| Inside a Git checkout | `git rev-parse --show-toplevel`; remote from `--remote`, else the current branch's upstream, else `origin` | client: asks that remote's diagnostic API (§3) |
| A local server's data directory | `--data DIR`, else `./pando-data` if it holds a Pando data lock or metadata | server: reads committed metadata, pack indexes and the event log directly, read-only |
| Explicit `--remote URL` | flag | remote: the diagnostic API at that URL |
| None of the above | — | explains and offers `pando demo` |

**Precedence:** explicit flags (`--remote`, `--data`, `--repo`) override discovery. When a remote isn't a Pando
server (no `pando` capability, §3), the CLI says so, then shows only local Git facts (refs, packs in the checkout),
labelled as local.

## 2. The shared diagnostic model (one representation; every view is built from it)
It is built only from what already exists: the metadata store (`ref/`, `rv/`, `pack/`, `cover/`, `twin/`, `tip/`
keys), pack indexes, `/_stats` counters, and the event log (`events.jsonl`: `push_received`, `pack_ingested`,
`pack_stored`, `ref_version_committed`, `push_reported`, `object_index_built`, `cover_committed`, `twin_committed`,
`fetch_served`, …), joined causally as in `verify/trace/timeline.py` (push id, pack id, LSN).

| Object | Fields | Source | Label |
|---|---|---|---|
| `RefVersion` | ref, version, tip, pack, deps [ref@version], linked, ls, lsn | `rv/` key | state |
| `Pack` | id, level (L0–L4), objects, bytes, thin, has-copy, cover range | `pack/` key + index | state |
| `Cover` | ref, level, lo..hi, pack, members, bytes before and after recompression | `cover/` key + `cover_recompress` event | state (+ recorded) |
| `Push` | push id, refs and versions, pack, objects, bytes, timings, result per ref | events `push_*`, `pack_*`, `ref_version_committed` | recorded |
| `Fetch` | wants, haves, plan (covers and tail packs, steps, metadata reads), delivery (inline or URI per pack), bytes | `fetch_served` event | recorded |
| `FetchPlan` | the same as `Fetch.plan`, for a chosen from/to version | the planner run read-only (§4) | **hypothetical** |
| `Discovery` | frontend B learning frontend A's packs: hwm_from → hwm, discovered packs and their LSNs | `object_index_built` events on each frontend | recorded |

**JSON:**
- Every command takes `--json`, which emits `{"schema": "pando.explain/1", "kind": ..., "label":
  "recorded|state|hypothetical", "data": ...}`.
- The schema is versioned and documented, and it is stable within a major version.
- CreatorOS visuals consume this output directly.

## 3. Access boundary: the diagnostic API on the Git frontend
Clients never talk to the metadata service or the object store, and never receive credentials, internal keys,
endpoints, other repositories' names or topology.

**Endpoints** (on the frontend's own port, same authentication as Git):

| Endpoint | Returns | Who |
|---|---|---|
| `GET /<repo>.git/pando/v1/capabilities` | server version, protocol features (packfile URIs, covers, copies), whether diagnostics are enabled. No topology. | anyone who may fetch the repository |
| `GET /<repo>.git/pando/v1/refs` | per ref: current version, tip, dependency summary | fetch permission |
| `GET /<repo>.git/pando/v1/history?ref=&from=&to=` | RefVersions and their packs (ids, levels, sizes) | fetch permission |
| `GET /<repo>.git/pando/v1/covers?ref=` | the cover hierarchy with ranges, sizes and recompression figures | fetch permission |
| `POST /<repo>.git/pando/v1/plan` `{have: oid or ref@version, want: ref@version}` | a hypothetical FetchPlan (§4), clearly labelled; nothing is served or written | fetch permission |
| `GET /<repo>.git/pando/v1/ops/<op-id>` | the recorded events of **one** push or fetch, correlated by an operation id the client holds (below) | the client that performed it |
| `GET /_admin/pando/v1/...` | the same for every repository, plus frontends, LSNs and change-log discovery (`explore distributed`) | administrator credential only (separate from Git credentials), or local access |

**Operation access (revised 2026-11-07 after owner review; implemented).** Knowing a commit or ref grants nothing:
- **Token.** Every push gets an unguessable 128-bit token from the server, shown only to the pushing client as a
  `remote: pando: operation <token>` line, which stock Git prints. The server stores only its SHA-256, inside the
  operation record. `pando explain --op <token>` presents it.
- **Principal.** The record is also bound to the pushing principal: the Basic user when access control is on, or
  `local` for a loopback client of an unauthenticated server.
  - `ops/<token>` additionally requires the same principal. Any mismatch returns 404, the same as for an unknown
    token, so there is no oracle.
  - `ops/last` (`pando explain last-push` after a plain `git push`) returns the caller's own latest push. It needs an
    authenticated principal (credentials from `git credential fill`, the ones Git used), or a loopback client of a
    local server. Anyone else gets 403.
- **One credential today.** The deployment has a single shared credential (`PANDO_AUTH`), so there is one principal
  and no per-repository authorisation. Diagnostics are gated by the same credential as Git, and expose nothing it
  could not already fetch. Per-user principals need per-user accounts, which don't exist yet.
- **Records** are two unique keys per push (by token hash; by principal and time), so pushes never contend on
  them. They are written just after the acknowledgement, off the push path: visible a few milliseconds later,
  and possibly lost on a crash right after the acknowledgement (never the push itself).

**Not exposed:**
- object-store keys and credentials;
- metadata keys, LSNs and other internal identifiers (administrator view only);
- frontend addresses and internal hostnames;
- names of repositories other than the one requested;
- other users' operations, beyond what the ref history already makes public.

**Implementation constraints:**
- Diagnostics can be switched off (`PANDO_DIAGNOSTICS=off`).
- Responses are rate-limited per credential.
- Endpoints are read-only by construction: their handlers get a read-only view of `Core` that has no commit, maintain
  or install methods. This is enforced by type, not by convention.

## 4. Read-only guarantees
- **The planner** used for hypothetical plans is the same code as the fetch path. It runs against a read-only
  `Store` snapshot, with no packout, no cache fill counted as a fetch, and no events except `explain_requested`.
- **Server-side exploration** opens the data directory read-only, using a shared lock, so it can't run alongside
  a startup sweep. It reads metadata from the WAL and snapshot without truncating or checkpointing.
- **Tests** for every command:
  - the data directory's byte-for-byte hash is unchanged afterwards;
  - no new key is written to the metadata store;
  - with a server running, the event log gains only `explain_requested`.

## 5. Commands
| Command | Context | Shows | Label |
|---|---|---|---|
| `pando status` | any | client: the remote, whether it is a Pando server, its version, and the current versions of your branches. Server: repositories, pack, cover and copy counts, last push, health. | state |
| `pando explain last-push` | client | for your last push to this remote: client side (objects and bytes Git sent, from `git` itself), server side (pack stored, ref version n→n+1, dependencies, copy or cover work scheduled), next fetch (URI-reusable or not) | recorded |
| `pando explore` | any | a small interactive menu over the views below (terminal UI; plain numbered prompts, so it works in every terminal) | — |
| `pando explore pushes [--ref]` | any | each push: immutable pack, objects, bytes, ref-version transaction (in administrator view: its LSN) | recorded + state |
| `pando explore history [--ref]` | any | the version dependency graph: trunk-relative pushes, `deps`, `linked`, packs | state |
| `pando explore covers [--ref]` | any | the L0–L4 hierarchy as a tree, with ranges, member packs, bytes and recompression savings | state |
| `pando explore fetch [--from VERSION or --have OID] [--to VERSION]` | any | the plan: covers chosen and why (largest aligned cover inside the needed range), tail packs, dependency closure, inline or URI per pack, metadata reads, bytes. Plus "packs avoided" against sending every L0 pack. | **hypothetical** |
| `pando explore distributed` | server or administrator | frontend A commits; frontend B discovers it through the change log (`hwm` advance, LSNs); the shared metadata in between | recorded |
| `pando demo` | none | creates the workload that makes all of the above interesting, then points to `explore` | — |

**Progressive detail:** the default output fits one screen. `-v` adds ids and sizes; `--json` gives everything.

## 6. Smallest end-to-end slice (first)
1. A read-only `Core` view, and the diagnostic endpoints `capabilities`, `refs`, `plan` and `ops`, with the access
   rules in §3.
2. `pando status`, `pando explore fetch` and `pando explain last-push`, client side and server side, with `--json`.
3. Tests:
   - two machines' worth of contexts on one host: client in a checkout, server in a data directory;
   - a non-Pando remote (plain `git http-backend`) handled gracefully;
   - read-only checks (§4);
   - no internal identifier in client-visible JSON (a test that scans responses for key prefixes, LSNs and S3
     endpoints).

Then `demo`, `explore covers`, `explore history`, `explore pushes`, `explore distributed`, and the interactive menu.
All of it is tested from the packaged binaries on the five platforms (RELEASE-PLAN.md §6).
