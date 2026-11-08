# Server interface contract (public to builders; hidden tests drive your server only through this)

Provide an executable `server/run.sh` in your workspace. The harness sets environment variables and calls it with one subcommand. Do not rely on anything else.

Environment (set by harness): `G_PORT` (TCP port to listen on, 127.0.0.1), `G_DATA_DIR` (empty or previously used directory; ALL persistent state, including metadata store files and blob store, MUST live under it; services you need, e.g. a database, must be started by `run.sh` and keep their data under it).

| Subcommand | Behaviour |
|---|---|
| `start` | Start everything needed; return only when the server accepts requests (≤ 60 s). Idempotent. Write to `$G_DATA_DIR/server.pid` the PID, **as seen by the process that runs the tests**, of the process that holds the listening socket (and, ideally, leads a process group containing all your processes; use `setsid` where available). The harness `kill -9`s that PID (and its group) and then **verifies** the process is gone and the port no longer accepts connections; a wrong, wrapper, or foreign-namespace PID makes the crash tests FAIL. Restarting on an existing `G_DATA_DIR` after SIGKILL MUST recover: acknowledged data intact. |
| `stop` | Graceful shutdown. |
| `create-repo <name>` | Create an empty repository reachable at `http://127.0.0.1:$G_PORT/<name>.git` (HTTP, Git smart protocol; protocol v2 for fetch/clone). No authentication. Default branch `main` is the trunk. |
| `maintain` (optional) | Run compaction/garbage collection/retention work to completion, then return. If not implemented, exit 0 without doing anything. |

HTTP: `GET http://127.0.0.1:$G_PORT/_stats` MUST return JSON (instrumentation, audited in CG7):
`{"metadata_reads": int, "metadata_writes": int, "packs_stored": int, "bytes_in": int, "bytes_out": int, "cpu_seconds": float}`, counters monotonic since process start (reset on restart is fine).

Clients: real `git` binaries (2.54–2.55, and for extension gates a modified Git client), HTTP only, no custom client code.
Hidden tests start your server fresh per test (new `G_DATA_DIR`, new port), may SIGKILL it at arbitrary times, and may run several tests concurrently on different ports.
Structural checks read `G_DATA_DIR` from outside (e.g. whether the server unpacked pushed objects into a server-side repository). Describe your on-disk layout in `LAYOUT.md`.
