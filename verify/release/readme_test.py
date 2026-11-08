#!/usr/bin/env python3
"""README-first acceptance test (docs/RELEASING.md): run the README's own commands, top to bottom, as a new
user would, in a clean profile, and check what the README says should happen.

  readme_test.py --artifacts DIR [--readme README.md] [--port 8080] [--keep] [--junit OUT.xml]
  readme_test.py --public [--port N] ...   after publication: the README's URLs exactly as written, no substitution

It executes the fenced code blocks marked `<!-- readme-test: STEP PLATFORM -->` (PLATFORM: unix, windows or all), in
README order, as ONE shell session (sh on macOS/Linux, Windows PowerShell 5.1 on Windows), so `cd` and variables
carry over exactly as for a person typing them. Nothing else is run between the README's commands except:
  - the substitutions a pre-release test needs, each printed at the start: the release download URL becomes a
    loopback HTTP server over --artifacts (never a public URL), and PANDO_DOWNLOAD_URL points the installer there;
    with --port, the README's port 8080 becomes that port;
  - `pando serve` (README "terminal 1") is started in the background, and stopped before the uninstall step (the
    README's "Ctrl-C");
  - after the run, the checks below read the transcript and the files.
Isolation: on macOS/Linux the session runs under `env -i` with a fresh HOME (no real profile, Git config or
Keychain is reachable) and GIT_CONFIG_NOSYSTEM=1. On Windows it is meant for a disposable CI runner (it uses that
runner's own profile, as a new user would).
"""
import json, os, re, shutil, socket, subprocess, sys, tempfile, threading, time, http.server, functools

WIN = os.name == "nt"
PLAT = "windows" if WIN else "unix"
RELEASE_URL = "https://github.com/chrishayuk/chuk-pando-server/releases/latest/download"


def blocks(readme):
    """[(step, code)] for this platform, in README order."""
    out, lines, i = [], open(readme, encoding="utf-8").read().splitlines(), 0
    while i < len(lines):
        m = re.match(r"\s*<!-- readme-test: (\w+) (unix|windows|all) -->", lines[i])
        if m and m.group(2) in (PLAT, "all"):
            j = i + 1
            while not lines[j].lstrip().startswith("```"):
                j += 1
            k = j + 1
            code = []
            while not lines[k].lstrip().startswith("```"):
                code.append(lines[k]); k += 1
            out.append((m.group(1), "\n".join(code)))
            i = k
        i += 1
    return out


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def main(a):
    readme = a.get("readme") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "README.md")
    public = bool(a.get("public"))
    art = os.path.abspath(a.get("artifacts") or ".")
    port = int(a.get("port") or 8080)
    T = tempfile.mkdtemp(prefix="pando-readme-")
    home = os.path.join(T, "home"); os.makedirs(home)
    hport = free_port()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", hport), functools.partial(http.server.SimpleHTTPRequestHandler, directory=art))
    httpd.RequestHandlerClass.log_message = lambda *x: None
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    local = RELEASE_URL if public else f"http://127.0.0.1:{hport}"
    steps = blocks(readme)
    print(f"README: {os.path.abspath(readme)}; platform {PLAT}; steps: {[s for s, _ in steps]}")
    if public:
        print("public mode: the README's release URL is used as written (no artifact substitution)")
    else:
        print(f"substitution: {RELEASE_URL} -> {local} (and PANDO_DOWNLOAD_URL={local}); artifacts from {art}")
    if port != 8080:
        print(f"substitution: port 8080 -> {port}")
    # the README's curl only accepts https (--proto '=https'); the loopback artifact host is plain http
    if not public:
        print("substitution: curl --proto '=https' --tlsv1.2 -> curl (loopback http artifact host)")
    sub = (lambda c: c.replace("localhost:8080", f"localhost:{port}")) if public else \
        (lambda c: (c.replace(RELEASE_URL, local).replace("localhost:8080", f"localhost:{port}")
                    .replace("--proto '=https' --tlsv1.2 ", "")))
    serve_log = os.path.join(T, "serve.log")
    if WIN:
        script = ["$ErrorActionPreference = 'Continue'"] + ([] if public else [f"$env:PANDO_DOWNLOAD_URL = '{local}'"]) + ["Set-Location $env:USERPROFILE"]
        for step, code in steps:
            script.append(f"Write-Output '@@STEP {step}'")
            if step == "serve":
                script.append(f"$srv = Start-Process pando -ArgumentList 'serve','--port','{port}' -PassThru -NoNewWindow "
                              f"-RedirectStandardOutput '{serve_log}' -RedirectStandardError '{serve_log}.err'")
                script.append(f"for ($i = 0; $i -lt 100; $i++) {{ try {{ (New-Object Net.Sockets.TcpClient('127.0.0.1', {port})).Close(); break }} catch {{ Start-Sleep -Milliseconds 100 }} }}")
                continue
            if step == "uninstall":
                script.append("Stop-Process -Id $srv.Id -Force; Start-Sleep 1")
            for line in sub(code).splitlines():
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                script.append("$global:LASTEXITCODE = 0")
                script.append(line)
                script.append(f"if ($LASTEXITCODE -ne 0) {{ Write-Output '@@FAIL {step} exit ' + $LASTEXITCODE; exit 1 }}")
            script.append(f"Write-Output '@@DONE {step}'")
        path = os.path.join(T, "readme.ps1")
        open(path, "w", encoding="utf-8-sig").write("\r\n".join(script) + "\r\n")
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path]
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1")
        workdir = os.environ["USERPROFILE"]
    else:
        script = ["set -e"] + ([] if public else [f'export PANDO_DOWNLOAD_URL="{local}"']) + ['cd "$HOME"']
        for step, code in steps:
            script.append(f"echo '@@STEP {step}'")
            if step == "serve":
                script.append(f'pando serve --port {port} > "{serve_log}" 2>&1 & SRV=$!')
                script.append(f"i=0; while ! (exec 3<>/dev/tcp/127.0.0.1/{port}) 2>/dev/null; do i=$((i+1)); [ $i -gt 100 ] && break; sleep 0.1; done")
                continue
            if step == "uninstall":
                script.append("kill $SRV; wait $SRV || true")
            script.append(sub(code))
            script.append(f"echo '@@DONE {step}'")
        path = os.path.join(T, "readme.sh")
        open(path, "w").write("\n".join(script) + "\n")
        sysp = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        env = {"PATH": sysp, "HOME": home, "SHELL": "/bin/bash", "TMPDIR": tempfile.gettempdir(), "GIT_CONFIG_NOSYSTEM": "1", "LANG": "C.UTF-8"}
        cmd = ["bash", path]
        workdir = home
    print(f"session script: {path}\n" + "-" * 60, flush=True)
    p = subprocess.run(cmd, cwd=workdir, env=env, capture_output=True, text=True, timeout=900)
    out = p.stdout + p.stderr
    print(out)
    print("-" * 60)
    httpd.shutdown()
    base = os.environ["USERPROFILE"] if WIN else home
    res = []

    def check(name, ok, why=""):
        res.append((name, ok, why)); print(("ok    " if ok else "FAIL  ") + name + ("" if ok else f": {why}"), flush=True)

    def section(step):
        m = re.search(rf"@@STEP {step}\n(.*?)(?=@@STEP |\Z)", out.replace("\r\n", "\n"), re.S)
        return m.group(1) if m else ""

    def read(*parts):
        try:
            return open(os.path.join(base, *parts), encoding="utf-8", errors="replace").read()
        except OSError:
            return None
    for step, _ in steps:
        check(f"step {step} ran to completion", f"@@DONE {step}" in out or (step == "serve" and "@@STEP create" in out), section(step)[-400:])
    inst = section("install")
    check("pando version names the release", re.search(r"pando \d+\.\d+\.\d+ \(commit \w+, [\w-]+\)", inst) is not None, inst[-300:])
    if not WIN:
        check("installer did not skip the checksum", "skipping sha256" not in inst and "no checksums to verify" not in inst, inst[-300:])
    cr, se = section("create"), section("second")
    check("first push: version 1 recorded", "now version 1" in cr and "refs/heads/main" in cr, cr[-400:])
    check("second push: version 2 depends on main@1", "now version 2" in se and "depends on: refs/heads/main@1" in se, se[-600:])
    check("explore history lists 2 versions", "history of refs/heads/main (2 versions)" in se, se[-600:])
    check("explore fetch is HYPOTHETICAL", "HYPOTHETICAL" in se, se[-300:])
    for d in ("hello-copy", "hello-uri"):
        r1, r2 = read(d, "README.md"), read(d, "app.py")
        check(f"{d} checked out README.md and app.py with the pushed content",
              r1 is not None and "# hello" in r1 and r2 is not None and "print('hi')" in r2, f"README.md={r1!r} app.py={r2!r}")
    ev = read("pando-data", "events", "events.jsonl") or ""
    fetches = [json.loads(l) for l in ev.splitlines() if '"fetch_served"' in l]
    check("the plain clone got its packs inline", any(not f.get("uri_packs") and f.get("packs") for f in fetches), str(fetches)[:300])
    check("the fetch.uriprotocols clone got its packs by packfile URI", any(f.get("uri_packs") for f in fetches), str(fetches)[:300])
    if "@@DONE uninstall" in out and "@@DONE install" in out:
        check("uninstall removed ~/.pando", not os.path.exists(os.path.join(base, ".pando")))
        if WIN:
            up = subprocess.run(["powershell", "-NoProfile", "-c", "[Environment]::GetEnvironmentVariable('Path','User')"], capture_output=True, text=True).stdout
            check("uninstall removed .pando from the user PATH", ".pando" not in up, up)
            check("uninstall removed the receipt", not os.path.exists(os.path.join(os.environ.get("LOCALAPPDATA", ""), "pando")))
        else:
            left = [f for f in (".profile", ".bashrc", ".bash_profile", ".zshrc", ".zshenv") if "pando" in (read(f) or "")]
            check("uninstall removed the profile lines", not left, str(left))
            check("uninstall removed ~/.config/pando", not os.path.exists(os.path.join(base, ".config", "pando")))
    check("session exit status 0", p.returncode == 0, f"exit {p.returncode}")
    fails = [n for n, ok, _ in res if not ok]
    print(f"readme-test: {len(res) - len(fails)} of {len(res)} checks passed" + (f"; FAILED: {fails}" if fails else ""))
    if a.get("junit"):
        import xml.sax.saxutils as sx
        cases = "\n".join(f'<testcase classname="release.readme" name="{sx.escape(n)}">'
                          + ("" if ok else f'<failure message="failed">{sx.escape(w[:2000])}</failure>') + "</testcase>" for n, ok, w in res)
        open(a["junit"], "w").write(f'<?xml version="1.0"?>\n<testsuite name="readme" tests="{len(res)}" failures="{len(fails)}">\n{cases}\n</testsuite>\n')
    if not a.get("keep"):
        shutil.rmtree(T, ignore_errors=True)
    return 1 if fails else 0


if __name__ == "__main__":
    args, i, v = {}, 1, sys.argv
    while i < len(v):
        if v[i] in ("--keep", "--public"):
            args[v[i][2:]] = True; i += 1
        else:
            args[v[i][2:]] = v[i + 1]; i += 2
    sys.exit(main(args))
