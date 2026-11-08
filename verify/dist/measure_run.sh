#!/bin/sh
HERE="$(cd "$(dirname "$0")/../.." && pwd)"; SRV="${SERVER_DIR:-$HERE}"
export G_CLIENTS="${G_CLIENTS:?set G_CLIENTS, e.g. stock=/path/to/git-2.55.0,apple=/usr/bin/git (docs/VERIFY.md)}"   # the Git clients the suites drive; the gate runs use a pinned stock Git 2.55.0 build
export PYTHONPATH="$HERE/verify/heldout" G_RUN_SH="$SRV/server/run.sh"
export CARGO_HOME="${CARGO_HOME:-$(git -C "$HERE" rev-parse --path-format=absolute --git-common-dir)/../.cargo-home}"
. "$HERE/verify/ensure_built.sh"   # build the tree under test; never run against a stale binary
exec python3 "$HERE/verify/dist/measure.py" "$@"
