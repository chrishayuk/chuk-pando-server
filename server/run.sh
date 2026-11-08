#!/bin/sh
# Harness entry point. Usage: run.sh start|stop|create-repo <name>|maintain
# Env: G_PORT, G_DATA_DIR (all state lives under G_DATA_DIR).
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
BIN="$HERE/target/release/pando"
if ! "$BIN" version >/dev/null 2>&1; then
  # missing or built for another platform: (re)build. The crate sources used by this build are
  # cached in ../.cargo-home, so an offline build works without network access.
  if [ -z "${CARGO_HOME:-}" ] && [ -d "$HERE/../.cargo-home/registry" ]; then
    export CARGO_HOME="$HERE/../.cargo-home"
  fi
  (cd "$HERE" && cargo build --release --offline >&2) || (cd "$HERE" && cargo build --release >&2)
fi
exec "$BIN" "$@"
