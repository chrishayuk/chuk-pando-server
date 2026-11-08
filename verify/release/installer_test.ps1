# Install pando with the generated PowerShell installer from LOCAL artifacts (never a public URL), into a clean,
# disposable profile directory, then smoke-test the installed binary. Negative cases: a corrupted archive and a
# well-formed archive with the wrong content are both refused for their SHA-256, and nothing is installed.
#   installer_test.ps1 -Dist DISTRIB_DIR [-Port 18741]
# Safety: USERPROFILE/HOME point into a fresh temporary directory, PANDO_NO_MODIFY_PATH=1 (the installer must not
# touch the user's PATH in the registry), and PANDO_INSTALL_DIR is inside that directory.
# The installer under test must be the one patched by verify/release/patch_installers.py (cargo-dist 0.34's own
# PowerShell installer checks no checksum at all).
param([Parameter(Mandatory = $true)][string]$Dist, [int]$Port = 18741)
$ErrorActionPreference = "Stop"
$Dist = (Resolve-Path $Dist).Path
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$T = Join-Path ([IO.Path]::GetTempPath()) ("pando-inst-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Path $T | Out-Null
$fail = 0

function Start-Http($dir) {
  $p = Start-Process python -ArgumentList "-m", "http.server", "$Port", "--bind", "127.0.0.1", "--directory", "`"$dir`"" -PassThru -WindowStyle Hidden
  for ($i = 0; $i -lt 100; $i++) {
    try { Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$Port/" -TimeoutSec 2 | Out-Null; return $p } catch { Start-Sleep -Milliseconds 100 }
  }
  throw "http server did not start"
}

function Install-Into($hdir, $log) {
  $env:USERPROFILE = $hdir; $env:HOME = $hdir; $env:LOCALAPPDATA = Join-Path $hdir "AppData\Local"
  $env:PANDO_NO_MODIFY_PATH = "1"
  $env:PANDO_INSTALL_DIR = Join-Path $hdir ".pando"
  $env:PANDO_DOWNLOAD_URL = "http://127.0.0.1:$Port"
  $script = Join-Path $T "pando-installer.ps1"
  $out = & powershell -NoProfile -ExecutionPolicy Bypass -File $script *>&1 | Out-String
  $code = $LASTEXITCODE
  Set-Content -Path $log -Value $out
  return $code
}

# the raw registry value (unexpanded): the expanded one changes with USERPROFILE, which this test redirects
function Get-UserPathRaw { (Get-Item -LiteralPath 'registry::HKEY_CURRENT_USER\Environment').GetValue('Path', '', 'DoNotExpandEnvironmentNames') }
$userPathBefore = Get-UserPathRaw
$realProfile = $env:USERPROFILE
$realLocal = $env:LOCALAPPDATA
$http = Start-Http $Dist
try {
  Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$Port/pando-installer.ps1" -OutFile (Join-Path $T "pando-installer.ps1")
  # 1. normal install
  $H = Join-Path $T "home"; New-Item -ItemType Directory -Path $H | Out-Null
  $code = Install-Into $H (Join-Path $T "install.log")
  $log = Get-Content (Join-Path $T "install.log") -Raw
  if ($code -eq 0) { "ok    install" } else { "FAIL  install (exit $code)"; $log; $fail = 1 }
  $bin = Get-ChildItem -Path $H -Recurse -Filter pando.exe | Select-Object -First 1
  if ($bin) { "ok    installed at $($bin.FullName): $(& $bin.FullName version)" } else { "FAIL  no pando.exe under $H"; $log; $fail = 1 }
  if ($log -match "checksum") { "      installer log mentions: " + (($log -split "`n") | Select-String -Pattern "checksum" | Select-Object -First 1) }
  $userPathAfter = Get-UserPathRaw
  if ($userPathAfter -ne $userPathBefore) { "FAIL  the installer changed the user PATH despite PANDO_NO_MODIFY_PATH=1"; "      before: $userPathBefore"; "      after:  $userPathAfter"; $fail = 1 } else { "ok    user PATH unchanged" }
  ($log -split "`n") | ForEach-Object { "      | $_" }
  "      files created: " + ((Get-ChildItem -Path $H -Recurse -File | ForEach-Object { $_.FullName.Substring($H.Length) }) -join " ")
  # 2. smoke test of the installed binary (with the real profile restored for git and python)
  $env:USERPROFILE = $realProfile; $env:HOME = $realProfile; $env:LOCALAPPDATA = $realLocal
  if ($bin) { & python (Join-Path $Here "smoke.py") --bin $bin.FullName --port ($Port + 1); if ($LASTEXITCODE -ne 0) { $fail = 1 } }
  # 3-4. negative cases: each from its own loopback host, fresh profile; the install must fail for its checksum
  $zip = "pando-x86_64-pc-windows-msvc.zip"
  $corrupt = Join-Path $T "corrupt"; New-Item -ItemType Directory -Path $corrupt | Out-Null
  $junk = New-Object byte[] 100000; (New-Object Random 7).NextBytes($junk); [IO.File]::WriteAllBytes((Join-Path $corrupt $zip), $junk)
  $swap = Join-Path $T "swap"; New-Item -ItemType Directory -Path $swap | Out-Null
  $fake = Join-Path $T "fake"; New-Item -ItemType Directory -Path $fake | Out-Null
  Copy-Item $MyInvocation.MyCommand.Path (Join-Path $fake "pando.exe")   # a well-formed zip, wrong pando.exe
  Compress-Archive -Path (Join-Path $fake "pando.exe") -DestinationPath (Join-Path $swap $zip)
  foreach ($case in @(@("corrupted", $corrupt), @("substituted", $swap))) {
    $name = $case[0]
    Stop-Process -Id $http.Id -Force -ErrorAction SilentlyContinue
    $http = Start-Http $case[1]
    $H2 = Join-Path $T "home-$name"; New-Item -ItemType Directory -Path $H2 | Out-Null
    $code = Install-Into $H2 (Join-Path $T "$name.log")
    $env:USERPROFILE = $realProfile; $env:HOME = $realProfile; $env:LOCALAPPDATA = $realLocal
    $blog = Get-Content (Join-Path $T "$name.log") -Raw
    $installed = Get-ChildItem -Path $H2 -Recurse -Filter pando.exe -ErrorAction SilentlyContinue
    if ($code -eq 0 -or $installed) { "FAIL  ${name}: installed (exit $code)"; $blog; $fail = 1 }
    elseif ($blog -match "checksum mismatch") { "ok    $name archive refused: " + (($blog -split "`n") | Select-String -Pattern "checksum mismatch" | Select-Object -First 1) }
    else { "FAIL  ${name}: refused, but not for its checksum:"; $blog; $fail = 1 }
  }
} finally {
  $env:USERPROFILE = $realProfile; $env:HOME = $realProfile; $env:LOCALAPPDATA = $realLocal
  Stop-Process -Id $http.Id -Force -ErrorAction SilentlyContinue
  Remove-Item -Recurse -Force $T -ErrorAction SilentlyContinue
}
exit $fail
