#!/bin/sh
# Sourced by every verify/*/run.sh before it runs anything (after SRV and CARGO_HOME are set): build the server of
# the tree under test (no-op when up to date) and print its identity, so a standalone test script can never run
# against a stale binary from an earlier build. verify/run_suites.sh builds too and records the hash in meta.txt.
( cd "$SRV/server" && cargo build --release --offline -q ) || { echo "verify: server build failed for $SRV" >&2; exit 2; }
echo "verify: server $(git -C "$SRV" rev-parse --short HEAD 2>/dev/null)$( [ -n "$(git -C "$SRV" status --porcelain -- server 2>/dev/null)" ] && echo "+dirty" ) binary $(shasum -a 256 "$SRV/server/target/release/pando" | cut -c1-12)" >&2
