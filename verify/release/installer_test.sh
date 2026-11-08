#!/bin/sh
# Install pando with the generated shell installer from LOCAL artifacts (never a public URL), in a clean, disposable
# HOME, then smoke-test the installed binary. Negative cases: a corrupted and a substituted archive are refused, and
# so is a genuine one when no SHA-256 tool exists (fail closed); with only shasum it is verified and installed.
#   installer_test.sh DISTRIB_DIR [PORT]      (DISTRIB_DIR holds pando-installer.sh and the archives)
# Safety (an early test once edited a real shell profile): HOME and XDG_CONFIG_HOME are a fresh temporary directory, and
# PANDO_NO_MODIFY_PATH=1, so no real shell profile or config directory can be edited.
set -eu
DIST="$(cd "$1" && pwd)"; PORT="${2:-18741}"; HERE="$(cd "$(dirname "$0")" && pwd)"
T="$(mktemp -d "${TMPDIR:-/tmp}/pando-inst-XXXXXX")"
python3 -m http.server "$PORT" --bind 127.0.0.1 --directory "$DIST" > "$T/http.log" 2>&1 & HTTP=$!
trap 'kill $HTTP 2>/dev/null; rm -rf "$T"' EXIT
i=0; until curl -fsS "http://127.0.0.1:$PORT/" >/dev/null 2>&1; do i=$((i+1)); [ $i -gt 50 ] && { echo "http server failed"; exit 1; }; sleep 0.1; done
clean_env() { h="$1"; shift; env -i PATH="$PATH" HOME="$h" XDG_CONFIG_HOME="$h/.config" SHELL=/bin/sh TMPDIR="${TMPDIR:-/tmp}" \
                 PANDO_NO_MODIFY_PATH=1 PANDO_DOWNLOAD_URL="http://127.0.0.1:$PORT" "$@"; }
fail=0
# 1. normal install
H="$T/home"; mkdir -p "$H"
curl -fsS "http://127.0.0.1:$PORT/pando-installer.sh" -o "$T/pando-installer.sh"
if clean_env "$H" sh "$T/pando-installer.sh" > "$T/install.log" 2>&1; then echo "ok    install"; else echo "FAIL  install"; cat "$T/install.log"; fail=1; fi
if grep -qi 'no checksums to verify\|skipping sha256' "$T/install.log"; then echo "FAIL  the installer did not verify the checksum"; cat "$T/install.log"; fail=1
else echo "      (installer log has no 'no checksums'/'skipping' notice; the tampered-archive case below is the real check)"; fi
sed 's/^/      | /' "$T/install.log"
BIN="$H/.pando/bin/pando"
[ -x "$BIN" ] && echo "ok    installed at ~/.pando/bin/pando: $("$BIN" version)" || { echo "FAIL  no binary at $BIN"; fail=1; }
# nothing but the install dir and the receipt under the disposable HOME; no profile edited
for rc in .profile .bashrc .bash_profile .zshrc .zshenv .config/fish/conf.d/pando.env.fish; do
  [ -e "$H/$rc" ] && { echo "FAIL  installer created or edited $rc despite PANDO_NO_MODIFY_PATH=1"; fail=1; }
done
echo "      files created: $(cd "$H" && find . -type f | sort | tr '\n' ' ')"
# 2. the installed binary passes the smoke test
python3 "$HERE/smoke.py" --bin "$BIN" --port "$((PORT + 1))" || fail=1
# 3-5. negative cases, each from its own loopback host with a fresh disposable HOME; nothing may be installed
A=$(cd "$DIST" && ls pando-*.tar.xz | grep "$( "$BIN" version | sed 's/.*, \(.*\))/\1/' )")
OTHER=$(cd "$DIST" && ls pando-*.tar.xz | grep -v "^$A$" | head -1)
serve_dir() { kill $HTTP 2>/dev/null; python3 -m http.server "$PORT" --bind 127.0.0.1 --directory "$1" > "$T/http-$2.log" 2>&1 & HTTP=$!
  i=0; until curl -fsS "http://127.0.0.1:$PORT/" >/dev/null 2>&1; do i=$((i+1)); [ $i -gt 50 ] && break; sleep 0.1; done; }
# run_case NAME DIR EXPECT_REGEX [PATH]: the install must FAIL, for the expected reason, and install nothing
run_case() {
  h="$T/home-$1"; mkdir -p "$h"; serve_dir "$2" "$1"
  if env -i PATH="${4:-$PATH}" HOME="$h" XDG_CONFIG_HOME="$h/.config" SHELL=/bin/sh TMPDIR="${TMPDIR:-/tmp}" \
       PANDO_NO_MODIFY_PATH=1 PANDO_DOWNLOAD_URL="http://127.0.0.1:$PORT" sh "$T/pando-installer.sh" > "$T/$1.log" 2>&1; then
    echo "FAIL  $1: the installer succeeded"; sed 's/^/      | /' "$T/$1.log"; fail=1
  elif grep -Eqi "$3" "$T/$1.log"; then echo "ok    $1 refused: $(grep -Ei "$3" "$T/$1.log" | head -1)"
  else echo "FAIL  $1: refused, but not for the expected reason ($3):"; sed 's/^/      | /' "$T/$1.log"; fail=1; fi
  [ -e "$h/.pando/bin/pando" ] && { echo "FAIL  $1: a binary was installed"; fail=1; }
  return 0
}
# 3. corrupted archive: random bytes under this platform's archive name
mkdir -p "$T/corrupt"; head -c 100000 /dev/urandom > "$T/corrupt/$A"
run_case corrupted "$T/corrupt" 'checksum mismatch'
# 4. substituted archive: a well-formed archive with the wrong content (another platform's release archive)
mkdir -p "$T/swap"; cp "$DIST/$OTHER" "$T/swap/$A"
run_case substituted "$T/swap" 'checksum mismatch'
# 5. fail closed: the genuine archive, but no SHA-256 tool (sha256sum, shasum, openssl) on PATH
mkdir -p "$T/notool"; for d in /usr/local/bin /usr/bin /bin /usr/sbin /sbin; do [ -d "$d" ] || continue
  for f in "$d"/*; do n=$(basename "$f"); case "$n" in sha*sum|shasum*|openssl*) continue ;; esac
    [ -e "$T/notool/$n" ] || ln -s "$f" "$T/notool/$n"; done; done
run_case no_sha256_tool "$DIST" 'none of sha256sum, shasum or openssl' "$T/notool"
# 6. fallback: no sha256sum, but shasum present: the genuine archive installs and is verified
if command -v shasum >/dev/null 2>&1; then
  mkdir -p "$T/fallback"; for f in "$T"/notool/*; do ln -s "$(readlink "$f")" "$T/fallback/$(basename "$f")"; done
  ln -s "$(command -v shasum)" "$T/fallback/shasum"; h="$T/home-fallback"; mkdir -p "$h"; serve_dir "$DIST" fallback
  if env -i PATH="$T/fallback" HOME="$h" XDG_CONFIG_HOME="$h/.config" SHELL=/bin/sh TMPDIR="${TMPDIR:-/tmp}" PANDO_NO_MODIFY_PATH=1 \
       PANDO_DOWNLOAD_URL="http://127.0.0.1:$PORT" sh "$T/pando-installer.sh" > "$T/fallback.log" 2>&1 && [ -x "$h/.pando/bin/pando" ]; then
    echo "ok    no sha256sum: verified with shasum and installed"
  else echo "FAIL  fallback to shasum:"; sed 's/^/      | /' "$T/fallback.log"; fail=1; fi
fi
exit $fail
