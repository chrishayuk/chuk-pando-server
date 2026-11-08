#!/usr/bin/env bash
# Reference/baseline implementation of campaign/INTERFACE.md. Env: G_PORT G_DATA_DIR; optional G_PROFILE (b0|b1), G_MUTANT, G_SERVER_GIT.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
: "${G_PORT:?}" "${G_DATA_DIR:?}"; PROFILE="${G_PROFILE:-b0}"; GITB="${G_SERVER_GIT:-git}"
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
mkdir -p "$G_DATA_DIR/repos"
tune() { # B1: documented tuned conventional configuration (see baselines-sealed/B1.md)
  $GITB -C "$1" config receive.unpackLimit 1          # keep every push as a pack
  $GITB -C "$1" config gc.auto 0                      # maintenance is explicit
  $GITB -C "$1" config repack.writeBitmaps true
  $GITB -C "$1" config pack.writeReverseIndex true
  $GITB -C "$1" config core.commitGraph true
  $GITB -C "$1" config uploadpack.allowFilter true
  $GITB -C "$1" config pack.threads 0
}
case "${1:-}" in
  start)
    if [ -f "$G_DATA_DIR/server.pid" ] && kill -0 "$(cat "$G_DATA_DIR/server.pid")" 2>/dev/null; then exit 0; fi
    G_SERVER_GIT="$GITB" python3 -I "$HERE/gitbridge.py" --data "$G_DATA_DIR" --port "$G_PORT" --profile "$PROFILE" --mutant "${G_MUTANT:-}" --git "$GITB" \
      >"$G_DATA_DIR/server.log" 2>&1 < /dev/null &
    echo $! > "$G_DATA_DIR/server.pid"
    for i in $(seq 1 120); do curl -sf "http://127.0.0.1:$G_PORT/_stats" >/dev/null 2>&1 && exit 0; sleep 0.25; done
    echo "server did not start" >&2; exit 1 ;;
  stop) [ -f "$G_DATA_DIR/server.pid" ] && kill "$(cat "$G_DATA_DIR/server.pid")" 2>/dev/null || true; rm -f "$G_DATA_DIR/server.pid" ;;
  create-repo)
    R="$G_DATA_DIR/repos/$2.git"; $GITB init -q --bare -b main "$R"
    $GITB -C "$R" config http.receivepack true
    $GITB -C "$R" config receive.denyNonFastForwards false
    $GITB -C "$R" config receive.denyDeletes false
    $GITB -C "$R" config receive.advertiseAtomic true
    [ "$PROFILE" = b1 ] && tune "$R"
    [ -n "${G_MUTANT:-}" ] && [ "${G_MUTANT}" = nodurable ] && { rm -rf "$G_DATA_DIR/snapshot"; cp -R "$G_DATA_DIR/repos" "$G_DATA_DIR/snapshot"; }
    exit 0 ;;
  maintain)
    if [ "$PROFILE" = b1 ]; then for R in "$G_DATA_DIR"/repos/*.git; do
      $GITB -C "$R" repack -Adb -q; $GITB -C "$R" commit-graph write --reachable -q 2>/dev/null || true; done
    fi; exit 0 ;;
  *) echo "usage: run.sh start|stop|create-repo NAME|maintain" >&2; exit 2 ;;
esac
