#!/bin/sh
# Start the server under a soft RLIMIT_NOFILE of 256 (macOS shell default); the hard limit is unchanged.
ulimit -S -n 256
exec "$(cd "$(dirname "$0")/../.." && pwd)/server/run.sh" "$@"
