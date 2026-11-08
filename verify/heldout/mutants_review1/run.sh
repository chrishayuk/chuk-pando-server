#!/usr/bin/env bash
# REVIEW-1 copy of baselines-sealed/server/run.sh with extra mutants (see gitbridge.py docstring).
# Env: G_PORT G_DATA_DIR; optional G_PROFILE (b0|b1), G_MUTANT, G_SERVER_GIT.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
: "${G_PORT:?}" "${G_DATA_DIR:?}"; PROFILE="${G_PROFILE:-b0}"; GITB="${G_SERVER_GIT:-git}"
M="${G_MUTANT:-}"
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
mkdir -p "$G_DATA_DIR/repos"
tune() { # B1: documented tuned conventional configuration (see baselines-sealed/B1.md)
  $GITB -C "$1" config receive.unpackLimit 1
  $GITB -C "$1" config gc.auto 0
  $GITB -C "$1" config repack.writeBitmaps true
  $GITB -C "$1" config pack.writeReverseIndex true
  $GITB -C "$1" config core.commitGraph true
  $GITB -C "$1" config uploadpack.allowFilter true
  $GITB -C "$1" config pack.threads 0
}
case "${1:-}" in
  start)
    if [ -f "$G_DATA_DIR/server.pid" ] && kill -0 "$(cat "$G_DATA_DIR/server.pid")" 2>/dev/null; then exit 0; fi
    G_SERVER_GIT="$GITB" python3 -I "$HERE/gitbridge.py" --data "$G_DATA_DIR" --port "$G_PORT" --profile "$PROFILE" --mutant "$M" --git "$GITB" \
      >>"$G_DATA_DIR/server.log" 2>&1 < /dev/null &
    if [ "$M" = nodurable_fakepid ]; then echo $$ > "$G_DATA_DIR/server.pid"   # BUG: PID of this (exiting) shell, not the server
    else echo $! > "$G_DATA_DIR/server.pid"; fi
    for i in $(seq 1 120); do curl -sf "http://127.0.0.1:$G_PORT/_stats" >/dev/null 2>&1 && exit 0; sleep 0.25; done
    echo "server did not start" >&2; exit 1 ;;
  stop)
    # graceful stop uses the server's own pid record (so leaked servers are not left behind by the review runs)
    [ -f "$G_DATA_DIR/real.pid" ] && kill "$(cat "$G_DATA_DIR/real.pid")" 2>/dev/null || true
    [ -f "$G_DATA_DIR/server.pid" ] && kill "$(cat "$G_DATA_DIR/server.pid")" 2>/dev/null || true; rm -f "$G_DATA_DIR/server.pid" ;;
  create-repo)
    R="$G_DATA_DIR/repos/$2.git"; $GITB init -q --bare -b main "$R"
    $GITB -C "$R" config http.receivepack true
    $GITB -C "$R" config receive.denyNonFastForwards false
    $GITB -C "$R" config receive.denyDeletes false
    $GITB -C "$R" config receive.advertiseAtomic true
    [ "$PROFILE" = b1 ] && tune "$R"
    case "$M" in nodurable|nodurable_fakepid|nodurable_late) rm -rf "$G_DATA_DIR/snapshot"; cp -R "$G_DATA_DIR/repos" "$G_DATA_DIR/snapshot";; esac
    if [ "$M" = structgame ]; then mv "$R/objects" "$R/odb"; fi   # same bare repo, object dir renamed
    exit 0 ;;
  maintain)
    if [ "$PROFILE" = b1 ]; then for R in "$G_DATA_DIR"/repos/*.git; do
      $GITB -C "$R" repack -Adb -q; $GITB -C "$R" commit-graph write --reachable -q 2>/dev/null || true; done
    fi
    if [ "$M" = gcnotags ]; then for R in "$G_DATA_DIR"/repos/*.git; do
      # BUG: GC root set = branches only. Hide tags, repack + prune, then restore tag refs (now dangling).
      saved="$($GITB -C "$R" for-each-ref refs/tags --format='%(objectname) %(refname)')"
      echo "$saved" | while read -r oid ref; do if [ -n "$ref" ]; then $GITB -C "$R" update-ref -d "$ref"; fi; done
      $GITB -C "$R" repack -a -d -q; $GITB -C "$R" prune --expire=now
      echo "$saved" | while read -r oid ref; do if [ -n "$ref" ]; then mkdir -p "$R/$(dirname "$ref")"; echo "$oid" > "$R/$ref"; fi; done
    done; fi
    exit 0 ;;
  *) echo "usage: run.sh start|stop|create-repo NAME|maintain" >&2; exit 2 ;;
esac
