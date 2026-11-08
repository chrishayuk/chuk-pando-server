#!/bin/sh
# Run the P0 regression tests against SERVER_DIR (default: this checkout).
HERE="$(cd "$(dirname "$0")/../.." && pwd)"; SRV="${SERVER_DIR:-$HERE}"
export G_CLIENTS="${G_CLIENTS:?set G_CLIENTS, e.g. stock=/path/to/git-2.55.0,apple=/usr/bin/git (docs/VERIFY.md)}"   # the Git clients the suites drive; the gate runs use a pinned stock Git 2.55.0 build
export PYTHONPATH="$HERE/verify/heldout" G_RUN_SH="$SRV/server/run.sh" CARGO_HOME="$HERE/.cargo-home"
. "$HERE/verify/ensure_built.sh"   # build the tree under test; never run against a stale binary
cd "$(dirname "$0")" && exec python3 -m pytest test_p0.py -v --tb=short -rfEs -p no:cacheprovider "$@"
