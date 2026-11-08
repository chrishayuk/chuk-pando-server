#!/bin/sh
# PANDO_ROLE=meta|frontend. Secrets and endpoints come from Fly secrets/env (docs/FLY-DEPLOYMENT-PLAN.md).
set -eu
case "${PANDO_ROLE:-frontend}" in
  meta)
    # Private network only, by two independent means: the app has no public services (no public IP is allocated),
    # and it binds only to the Machine's 6PN address (Fly maps `fly-local-6pn` to it), so even an accidentally
    # added public service could not reach it. PANDO_META_SECRET must be set (refuse to start otherwise).
    [ -n "${PANDO_META_SECRET:-}" ] || { echo "PANDO_META_SECRET is not set: refusing to start"; exit 2; }
    if getent hosts fly-local-6pn >/dev/null 2>&1; then
      META_LISTEN="tcp:[$(getent hosts fly-local-6pn | awk '{print $1}')]:7000"
    else
      echo "fly-local-6pn does not resolve: refusing to bind a public interface"; exit 2
    fi
    exec pando meta-serve --dir /data/meta --listen "$META_LISTEN" ;;
  frontend)
    mkdir -p /data
    # Tigris credentials as set by `fly storage create` (AWS_* secrets), mapped to the server's variables, unless
    # PANDO_S3 is set explicitly. Nothing is printed.
    if [ -z "${PANDO_S3:-}" ] && [ -n "${BUCKET_NAME:-}" ] && [ -n "${AWS_ENDPOINT_URL_S3:-}" ]; then
      export PANDO_S3="${AWS_ENDPOINT_URL_S3%/}/$BUCKET_NAME" PANDO_S3_KEY="$AWS_ACCESS_KEY_ID" PANDO_S3_SECRET="$AWS_SECRET_ACCESS_KEY"
      export PANDO_S3_REGION="${AWS_REGION:-auto}"
    fi
    [ -n "${PANDO_S3:-}" ] || { echo "no object store configured (PANDO_S3 or a Tigris bucket): refusing to start"; exit 2; }
    # TEST ONLY (acceptance test T7, docs/FLY-DEPLOYMENT-PROPOSAL.md): put the fault relay in front of the object store
    # on this Machine. TLS still terminates at the store (PANDO_S3_CONNECT only changes where the TCP connection goes).
    # Control from inside the Machine: python3 -c "import urllib.request as u;print(u.urlopen('http://127.0.0.1:9101/stall').read())"
    if [ "${PANDO_TEST_RELAY:-}" = "1" ]; then
      up=$(echo "$PANDO_S3" | sed -E 's#^https?://([^/]+)/.*#\1#'); case "$up" in *:*) ;; *) up="$up:443" ;; esac
      python3 /usr/local/bin/s3relay.py 127.0.0.1:9100 "$up" 9101 > /tmp/s3relay.log 2>&1 &
      export PANDO_S3_CONNECT=127.0.0.1:9100
      echo "TEST RELAY ACTIVE: object store reached through 127.0.0.1:9100 -> $up (control port 9101)"
    fi
    for v in PANDO_AUTH PANDO_URI_SECRET PANDO_META_SECRET; do
      eval "[ -n \"\${$v:-}\" ]" || { echo "$v is not set: refusing to start a public frontend"; exit 2; }
    done
    # Private cache and event log on the Machine's own disk (the store is authoritative; the cache is wiped at start).
    export PANDO_BIND="${PANDO_BIND:-[::]}"
    exec pando serve --port 8080 --data /data ;;
  *) echo "unknown PANDO_ROLE"; exit 2 ;;
esac
