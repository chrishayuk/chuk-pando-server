#!/usr/bin/env python3
"""Harden the installers cargo-dist 0.34 generates (docs/RELEASING.md, installer integrity). Run on the
release's distrib directory after `dist build --artifacts=global`, before anything is uploaded:

  patch_installers.py DISTRIB_DIR

- pando-installer.ps1: dist's PowerShell installer checks no checksum. This embeds each archive's SHA-256 (from
  sha256.sum in the same directory, written by the same build) in the script's platform table, and verifies the
  downloaded archive with Get-FileHash before unpacking it; a mismatch, or a missing digest, refuses the install.
- pando-installer.sh: dist's shell installer already verifies the embedded SHA-256, but fails OPEN: with no
  `sha256sum` command (or no digest) it prints "skipping ..." and installs anyway. This replaces its
  verify_checksum with one that tries sha256sum, then `shasum -a 256`, then `openssl dgst -sha256`, and refuses to
  install if none exists, if no digest is known, or if the digests differ.

What this gives is INTEGRITY against the digest published in the same release (a corrupted or swapped archive is
refused). It is not independent authentication of the publisher: whoever can replace the archive in the release can
replace the installer and its digests too. Independent verification is `gh attestation verify` (README).

Idempotent (a patched file is recognised and left alone). Exits non-zero if the generated scripts do not have the
shape this patch expects (a cargo-dist upgrade must be reviewed, not silently half-patched).
"""
import os, re, sys

MARK = "pando release patch (verify/release/patch_installers.py)"

SH_VERIFY = r'''verify_checksum() {
    # MARK: fail closed. The archive must match the SHA-256 embedded in this installer, checked
    # with sha256sum, `shasum -a 256` or `openssl dgst -sha256`; with none of them, or no digest, nothing is installed.
    local _file="$1"
    local _checksum_style="$2"
    local _checksum_value="$3"
    local _calculated_checksum

    if [ "$_checksum_style" != "sha256" ] || [ -z "$_checksum_value" ]; then
        err "no SHA-256 is known for this archive; refusing to install"
    fi
    if check_cmd sha256sum; then
        _calculated_checksum="$(sha256sum -b "$_file" | awk '{printf $1}')"
    elif check_cmd shasum; then
        _calculated_checksum="$(shasum -a 256 -b "$_file" | awk '{printf $1}')"
    elif check_cmd openssl; then
        _calculated_checksum="$(openssl dgst -sha256 "$_file" | awk '{printf $NF}')"
    else
        err "cannot verify the download: none of sha256sum, shasum or openssl is installed; refusing to install"
    fi
    if [ "$_calculated_checksum" != "$_checksum_value" ]; then
        err "checksum mismatch; refusing to install
            want: $_checksum_value
            got:  $_calculated_checksum"
    fi
}'''.replace("MARK", MARK)

PS_VERIFY = '''  Invoke-DownloadFile -client $wc -url $url -path $dir_path

  # MARK: verify the archive's SHA-256 before unpacking it; refuse a mismatch or a missing digest
  $expected = $info["sha256"]
  if (-not $expected) {
    throw "ERROR: no SHA-256 is known for $artifact_name; refusing to install"
  }
  $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $dir_path).Hash.ToLowerInvariant()
  if ($actual -ne $expected.ToLowerInvariant()) {
    throw "ERROR: checksum mismatch for ${artifact_name}; refusing to install (want $expected, got $actual)"
  }
  Write-Verbose "  sha256 $actual verified"
'''.replace("MARK", MARK)


def digests(d):
    out = {}
    for line in open(os.path.join(d, "sha256.sum"), encoding="utf-8"):
        m = re.match(r"^([0-9a-f]{64}) [ *](\S+)\s*$", line.strip())
        if m:
            out[m.group(2)] = m.group(1)
    if not out:
        sys.exit("sha256.sum has no entries")
    return out


def patch_sh(path):
    t = open(path, encoding="utf-8").read()
    if MARK in t:
        return "already patched"
    m = re.search(r"^verify_checksum\(\) \{\n.*?^\}", t, re.S | re.M)
    if not m:
        sys.exit(f"{path}: verify_checksum() not found: review the generated installer")
    t = t[:m.start()] + SH_VERIFY + t[m.end():]
    old = '''            say "no checksums to verify" 1>&2'''
    if t.count(old) != 1:
        sys.exit(f"{path}: the 'no checksums to verify' branch not found exactly once")
    t = t.replace(old, '''            err "no checksum is embedded for this archive; refusing to install"''')
    if "skipping" in t[t.index("verify_checksum() {"):t.index("verify_checksum() {") + len(SH_VERIFY)]:
        sys.exit("patched verify_checksum still contains a skip path")
    open(path, "w", encoding="utf-8", newline="\n").write(t)
    return "patched"


def patch_ps1(path, dig):
    t = open(path, encoding="utf-8").read()
    if MARK in t:
        return "already patched"
    names = re.findall(r'^(\s*)"artifact_name" = "([^"]+)"$', t, re.M)
    if not names:
        sys.exit(f"{path}: no artifact_name entries found")
    for indent, name in set(names):
        if name not in dig:
            sys.exit(f"{path}: {name} has no digest in sha256.sum")
        t = re.sub(rf'^{re.escape(indent)}"artifact_name" = "{re.escape(name)}"$',
                   f'{indent}"artifact_name" = "{name}"\n{indent}"sha256" = "{dig[name]}"', t, flags=re.M)
    old = '''  Invoke-DownloadFile -client $wc -url $url -path $dir_path
'''
    if t.count(old) != 1:
        sys.exit(f"{path}: the archive download call was not found exactly once")
    t = t.replace(old, PS_VERIFY)
    open(path, "w", encoding="utf-8", newline="\n").write(t)
    return f"patched ({len(names)} platform entries)"


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    d = sys.argv[1]
    dig = digests(d)
    print("pando-installer.sh:", patch_sh(os.path.join(d, "pando-installer.sh")))
    print("pando-installer.ps1:", patch_ps1(os.path.join(d, "pando-installer.ps1"), dig))


if __name__ == "__main__":
    main()
