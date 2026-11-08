# Shared helpers for the local gate tests. Source from a test script.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUN="$ROOT/server/run.sh"
: "${TMPDIR:=/tmp}"
W="$(mktemp -d "$TMPDIR/pando-test.XXXXXX")"
export G_DATA_DIR="$W/data"
export G_PORT="${G_PORT:-$(( 20000 + ($$ % 20000) ))}"
URL_BASE="http://127.0.0.1:$G_PORT"
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@x GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@x
export GIT_TERMINAL_PROMPT=0
# client-side detached auto-gc/maintenance races with rapid local commits in these scripts
# (observed: a local ref pointing at a pruned object); it has nothing to do with the server.
export GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_0=gc.auto GIT_CONFIG_VALUE_0=0 GIT_CONFIG_KEY_1=maintenance.auto GIT_CONFIG_VALUE_1=false
FAILS=0
PASSES=0

ok()   { PASSES=$((PASSES+1)); echo "ok   - $*"; }
fail() { FAILS=$((FAILS+1)); echo "FAIL - $*"; }
check() { # check "desc" cmd...
  d="$1"; shift
  if "$@" >"$W/last.out" 2>&1; then ok "$d"; else fail "$d"; sed 's/^/     | /' "$W/last.out" | tail -20; fi
}
check_fail() {
  d="$1"; shift
  if "$@" >"$W/last.out" 2>&1; then fail "$d (unexpected success)"; sed 's/^/     | /' "$W/last.out" | tail -10; else ok "$d"; fi
}
start_server() { "$RUN" start; }
stop_server() { "$RUN" stop || true; }
kill_server() {
  pid=$(cat "$G_DATA_DIR/server.pid")
  kill -9 -- "-$pid" 2>/dev/null || kill -9 "$pid"
  for i in $(seq 1 50); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
}
finish() {
  stop_server
  echo "# passes=$PASSES fails=$FAILS (work dir $W)"
  [ "$FAILS" = 0 ] || exit 1
  exit 0
}
commit_file() { # repo file content msg
  (cd "$1" && printf '%s\n' "$3" > "$2" && git add "$2" && git commit -qm "$4")
}
fsck_clone() { # url dir  -> fresh clone + fsck
  rm -rf "$2" && git clone -q "$1" "$2" && (cd "$2" && git fsck --full --strict >/dev/null)
}
