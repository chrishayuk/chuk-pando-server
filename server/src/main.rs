// a few small helper APIs (offline tools, simulator) are intentionally kept
#![allow(dead_code)]

mod blob;
mod cache;
mod compact;
mod events;
mod http;
mod ingest;
mod meta;
mod metrics;
mod pack;
mod packout;
mod proto;
mod recompress;
mod repo;
mod s3;
mod auth;
mod cli;
mod diag;
mod shallow;
mod sim;
mod sys;
mod v0;

use anyhow::{anyhow, bail, Context, Result};
use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};

fn env_cfg() -> Result<(u16, PathBuf)> {
    let port: u16 = std::env::var("G_PORT").context("G_PORT not set")?.parse().context("bad G_PORT")?;
    let dir = PathBuf::from(std::env::var("G_DATA_DIR").context("G_DATA_DIR not set")?);
    std::fs::create_dir_all(&dir)?;
    let dir = dir.canonicalize()?;
    Ok((port, dir))
}

fn pid_alive(pid: i32) -> bool {
    sys::pid_alive(pid)
}

fn read_pid(dir: &Path) -> Option<i32> {
    std::fs::read_to_string(dir.join("server.pid")).ok()?.trim().parse().ok()
}

fn port_open(port: u16) -> bool {
    TcpStream::connect_timeout(&format!("127.0.0.1:{}", port).parse().unwrap(), Duration::from_millis(300)).is_ok()
}

fn http_req(port: u16, method: &str, path: &str, timeout: Duration) -> Result<(u16, String)> {
    let mut s = TcpStream::connect_timeout(&format!("127.0.0.1:{}", port).parse().unwrap(), Duration::from_secs(2))?;
    s.set_read_timeout(Some(timeout))?;
    // admin calls carry the deployment's Basic credentials when access control is configured (docs/FLY-DEPLOYMENT-PLAN.md)
    let authz = std::env::var("PANDO_AUTH").ok().filter(|v| !v.is_empty()).map(|a| format!("Authorization: Basic {}\r\n", auth::b64encode(a.as_bytes()))).unwrap_or_default();
    write!(s, "{} {} HTTP/1.1\r\nHost: 127.0.0.1\r\n{}Content-Length: 0\r\nConnection: close\r\n\r\n", method, path, authz)?;
    let mut b = Vec::new();
    s.read_to_end(&mut b)?;
    let t = String::from_utf8_lossy(&b).to_string();
    let code: u16 = t.split(' ').nth(1).and_then(|c| c.parse().ok()).ok_or_else(|| anyhow!("bad http response"))?;
    let body = t.splitn(2, "\r\n\r\n").nth(1).unwrap_or("").to_string();
    Ok((code, body))
}

fn cmd_start() -> Result<()> {
    let (port, dir) = env_cfg()?;
    if let Some(pid) = read_pid(&dir) {
        if pid_alive(pid) && port_open(port) {
            return Ok(());
        }
    }
    let exe = std::env::current_exe()?;
    let log = std::fs::OpenOptions::new().create(true).append(true).open(dir.join("server.log"))?;
    let _ = std::fs::remove_file(dir.join("server.pid"));
    let mut cmd = std::process::Command::new(exe);
    // the harness interface (INTERFACE.md) creates repositories explicitly: no creation on first push here
    cmd.arg("serve").arg("--port").arg(port.to_string()).arg("--data").arg(&dir).arg("--no-autocreate");
    cmd.stdin(std::process::Stdio::null()).stdout(log.try_clone()?).stderr(log);
    sys::detach(&mut cmd);
    let mut child = cmd.spawn()?;
    let t0 = Instant::now();
    while t0.elapsed() < Duration::from_secs(60) {
        if let Ok(Some(st)) = child.try_wait() {
            bail!("server exited during startup: {} (see {:?})", st, dir.join("server.log"));
        }
        if read_pid(&dir) == Some(child.id() as i32) && port_open(port) {
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    bail!("server did not become ready within 60s")
}

fn cmd_stop() -> Result<()> {
    let (_port, dir) = env_cfg()?;
    if let Some(pid) = read_pid(&dir) {
        if pid_alive(pid) {
            sys::request_stop(pid);
            let t0 = Instant::now();
            while pid_alive(pid) && t0.elapsed() < Duration::from_secs(15) {
                std::thread::sleep(Duration::from_millis(50));
            }
            if pid_alive(pid) {
                sys::kill(pid);
            }
        }
        let _ = std::fs::remove_file(dir.join("server.pid"));
    }
    Ok(())
}

pub fn valid_repo_name(n: &str) -> bool {
    !n.is_empty() && !n.contains("..") && !n.starts_with('/') && !n.starts_with('_') && n.chars().all(|c| c.is_ascii_alphanumeric() || "._-/".contains(c))
        && sys::repo_name_portable(n)
}

/// Storage for this process (docs/DISTRIBUTED.md). Default: everything under the data directory (single process).
/// `PANDO_META=<socket>`: the shared metadata service; `PANDO_BLOBS=<dir>`: a pack store shared with other frontends.
fn open_storage(dir: &Path) -> Result<(Arc<meta::Store>, Arc<blob::Blobs>)> {
    let store = match std::env::var("PANDO_META") {
        Ok(a) if !a.is_empty() => meta::Store::connect(&meta::Addr::parse(&a))?,
        _ => meta::Store::open(&dir.join("meta"))?,
    };
    let blobs = match (s3::S3::from_env()?, std::env::var("PANDO_BLOBS")) {
        (Some(s3), _) => blob::Blobs::new_s3(&dir.join("blobs"), s3)?,
        (None, Ok(b)) if !b.is_empty() => blob::Blobs::new_shared(Path::new(&b))?,
        _ => blob::Blobs::new(&dir.join("blobs"))?,
    };
    Ok((store, Arc::new(blobs)))
}

fn cmd_meta_serve(args: &[String]) -> Result<()> {
    let (mut dir, mut sock) = (None, None);
    let mut i = 0;
    while i + 1 < args.len() {
        match args[i].as_str() {
            "--dir" => dir = Some(PathBuf::from(&args[i + 1])),
            "--sock" => sock = Some(meta::Addr::Unix(PathBuf::from(&args[i + 1]))),
            "--listen" => sock = Some(meta::Addr::parse(&args[i + 1])),
            _ => {}
        }
        i += 2;
    }
    meta::serve(&dir.ok_or_else(|| anyhow!("--dir"))?, &sock.ok_or_else(|| anyhow!("--sock or --listen"))?)
}

fn offline_core(dir: &Path) -> Result<Arc<repo::Core>> {
    let (store, blobs) = open_storage(dir)?;
    let events = Arc::new(events::Events::open(&dir.join("events"), "offline"));
    Ok(Arc::new(repo::Core::new(store, blobs, events)))
}

fn lock_data_dir(dir: &Path) -> Result<std::fs::File> {
    std::fs::create_dir_all(dir.join("meta"))?;
    let f = std::fs::OpenOptions::new().create(true).write(true).open(dir.join("meta").join("LOCK"))?;
    if !sys::try_lock_exclusive(&f) {
        bail!("data directory is locked by another server process");
    }
    Ok(f)
}

fn cmd_create_repo(name: &str) -> Result<()> {
    let name = name.strip_suffix(".git").unwrap_or(name);
    if !valid_repo_name(name) {
        bail!("invalid repository name {:?}", name);
    }
    let (port, dir) = env_cfg()?;
    match http_req(port, "POST", &format!("/_admin/create-repo?name={}", name), Duration::from_secs(30)) {
        Ok((200, _)) => return Ok(()),
        Ok((c, b)) => bail!("create-repo failed: {} {}", c, b),
        Err(_) => {}
    }
    let _lock = lock_data_dir(&dir)?;
    let core = offline_core(&dir)?;
    core.create_repo(name)?;
    Ok(())
}

fn cmd_maintain() -> Result<()> {
    let (port, dir) = env_cfg()?;
    match http_req(port, "POST", "/_admin/maintain", Duration::from_secs(3600)) {
        Ok((200, b)) => {
            print!("{}", b);
            return Ok(());
        }
        Ok((c, b)) => bail!("maintain failed: {} {}", c, b),
        Err(_) => {}
    }
    let _lock = lock_data_dir(&dir)?;
    let core = offline_core(&dir)?;
    println!("{}", compact::maintain_all(&core)?);
    Ok(())
}

fn raise_nofile() {
    sys::raise_nofile()
}

const HELP: &str = "pando: a Git server built on immutable packs and versioned refs, after the architecture OpenAI described
in \"Scaling Git at OpenAI\". Use it with ordinary Git.

  pando serve [--port 8080] [--data ./pando-data]   run a local server (see pando serve --help)
  pando explain last-push                           what the server recorded for your last push (inside a checkout)
  pando explore fetch|history|covers                what a fetch would send; ref versions; compaction (inside a checkout)
  pando status                                      the remote and your branches' versions
  pando version                                     version, commit and target

Advanced (test harness and distributed mode): start, stop, create-repo, maintain, meta-serve, sim, fuzz.
Source and documentation: https://github.com/chrishayuk/chuk-pando-server";

const SERVE_HELP: &str = "pando serve [--port 8080] [--data ./pando-data] [--bind 127.0.0.1] [--no-autocreate]

Runs a complete local Pando server: metadata, pack storage, compaction and background maintenance in one process.
Push any repository to it with ordinary Git; a repository is created on its first push (unless --no-autocreate):
    git remote add origin http://localhost:8080/<name>
    git push -u origin main
Listens on loopback only unless --bind is given; a non-loopback --bind requires PANDO_AUTH=user:password (HTTP
Basic) and is refused without it. Stop with Ctrl-C.";

/// A server reachable from other machines must authenticate (docs/FLY-DEPLOYMENT-PLAN.md): a non-loopback listen
/// address is refused unless PANDO_AUTH is set. Loopback needs no credentials. PANDO_ALLOW_UNAUTHENTICATED_BIND=1
/// overrides the refusal, for isolated test networks only (the test suites use it to check what an unauthenticated
/// non-loopback caller can see).
fn check_bind(sa: std::net::SocketAddr, has_auth: bool, override_ok: bool) -> Result<()> {
    if sa.ip().is_loopback() || has_auth || override_ok {
        return Ok(());
    }
    bail!("refusing to listen on {} without credentials: a server reachable from other machines must require \
authentication.\n  Set PANDO_AUTH=user:password (HTTP Basic; put the server behind TLS beyond a trusted network), e.g.\n    \
PANDO_AUTH=alice:long-random-password pando serve --bind {}\n  Loopback (the default, 127.0.0.1) needs no credentials: \
pando serve", sa, sa.ip())
}

/// What stops a foreground `pando serve` cleanly. Unix: SIGTERM or SIGINT. Windows: Ctrl-C, Ctrl-Break, or the
/// console window closing. (A hard kill is also safe: every acknowledged push is already durable.)
struct StopSignals {
    #[cfg(unix)]
    term: tokio::signal::unix::Signal,
    #[cfg(unix)]
    intr: tokio::signal::unix::Signal,
    #[cfg(windows)]
    c: tokio::signal::windows::CtrlC,
    #[cfg(windows)]
    brk: tokio::signal::windows::CtrlBreak,
    #[cfg(windows)]
    close: tokio::signal::windows::CtrlClose,
}

fn stop_signals() -> std::io::Result<StopSignals> {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{signal, SignalKind};
        Ok(StopSignals { term: signal(SignalKind::terminate())?, intr: signal(SignalKind::interrupt())? })
    }
    #[cfg(windows)]
    {
        use tokio::signal::windows::{ctrl_break, ctrl_c, ctrl_close};
        Ok(StopSignals { c: ctrl_c()?, brk: ctrl_break()?, close: ctrl_close()? })
    }
}

impl StopSignals {
    async fn recv(&mut self) {
        #[cfg(unix)]
        tokio::select! {
            _ = self.term.recv() => {}
            _ = self.intr.recv() => {}
        }
        #[cfg(windows)]
        tokio::select! {
            _ = self.c.recv() => {}
            _ = self.brk.recv() => {}
            _ = self.close.recv() => {}
        }
    }
}

fn cmd_serve(args: &[String]) -> Result<()> {
    let (mut port, mut dir, mut autocreate, mut bind_flag) = (8080u16, PathBuf::from("pando-data"), true, None::<String>);
    let mut i = 0;
    let val = |i: usize| args.get(i + 1).cloned().ok_or_else(|| anyhow!("{} needs a value (see pando serve --help)", args[i]));
    while i < args.len() {
        match args[i].as_str() {
            "--port" => {
                port = val(i)?.parse().map_err(|_| anyhow!("--port: not a port number"))?;
                i += 1;
            }
            "--data" => {
                dir = PathBuf::from(val(i)?);
                i += 1;
            }
            "--bind" => {
                bind_flag = Some(val(i)?);
                i += 1;
            }
            "--no-autocreate" => autocreate = false,
            "--autocreate" => autocreate = true,
            "-h" | "--help" => {
                println!("{}", SERVE_HELP);
                return Ok(());
            }
            a => bail!("pando serve: unknown option {} (see pando serve --help)", a),
        }
        i += 1;
    }
    // Listen address: --bind, else PANDO_BIND (e.g. "[::]" on Fly.io), else loopback. Checked before anything is created.
    let bind = bind_flag.clone().or_else(|| std::env::var("PANDO_BIND").ok().filter(|v| !v.is_empty())).unwrap_or_else(|| "127.0.0.1".into());
    let sa: std::net::SocketAddr = format!("{}:{}", bind, port).parse()
        .or_else(|_| format!("[{}]:{}", bind, port).parse())
        .map_err(|_| anyhow!("--bind {:?}: not an IP address (examples: 127.0.0.1, 0.0.0.0, ::)", bind))?;
    let has_auth = std::env::var("PANDO_AUTH").map(|v| !v.is_empty()).unwrap_or(false);
    let override_ok = std::env::var("PANDO_ALLOW_UNAUTHENTICATED_BIND").map(|v| v == "1").unwrap_or(false);
    check_bind(sa, has_auth, override_ok)?;
    std::fs::create_dir_all(&dir).with_context(|| format!("create data directory {}", dir.display()))?;
    sys::new_session();
    raise_nofile();
    let _lock = lock_data_dir(&dir)?;
    let (store, blobs) = open_storage(&dir)?;
    let events = Arc::new(events::Events::open(&dir.join("events"), &format!("server-{}", std::process::id())));
    events.emit("server_start", serde_json::json!({"pid": std::process::id(), "port": port}));
    let core = Arc::new(repo::Core::new(store, blobs, events));
    let rt = tokio::runtime::Builder::new_multi_thread().enable_all().max_blocking_threads(2048).thread_stack_size(4 << 20).build()?;
    rt.block_on(async move {
        let sock = tokio::net::TcpSocket::new_v4()?;
        // SO_REUSEADDR on Unix only: on Windows it would let this server bind a port another process is already
        // listening on (port sharing), instead of failing with "address in use" (docs/WINDOWS.md).
        #[cfg(unix)]
        sock.set_reuseaddr(true)?;
        let sock = if sa.is_ipv6() {
            let s6 = tokio::net::TcpSocket::new_v6()?;
            #[cfg(unix)]
            s6.set_reuseaddr(true)?;
            s6
        } else {
            sock
        };
        sock.bind(sa)?;
        let listener = sock.listen(4096)?;
        let shown = if sa.ip().is_loopback() { format!("localhost:{}", port) } else { format!("{}:{}", sa.ip(), port) };
        println!("pando {} serving http://{}  (data: {}{})", env!("CARGO_PKG_VERSION"), shown, dir.display(),
                 if autocreate { "; repositories are created on first push" } else { "" });
        println!("  push:     git remote add origin http://{}/<name> && git push -u origin main", shown);
        println!("  explore:  pando explain last-push | pando explore fetch   (inside the repository)");
        let cache = cache::start_ring(core.clone(), &dir).await.ok().map(Arc::new);
        let app = Arc::new(http::App {
            core: core.clone(),
            bytes_in: Default::default(),
            bytes_out: Default::default(),
            cache,
            started: Instant::now(),
            auth: auth::Auth::from_env(),
            autocreate,
        });
        compact::start_background(core.clone());
        // pid file written only once the socket accepts connections
        let tmp = dir.join("server.pid.tmp");
        std::fs::write(&tmp, format!("{}\n", std::process::id()))?;
        std::fs::rename(&tmp, dir.join("server.pid"))?;
        let mut stop = stop_signals()?;
        loop {
            tokio::select! {
                acc = listener.accept() => {
                    let (stream, peer) = match acc { Ok(x) => x, Err(_) => continue };
                    let _ = stream.set_nodelay(true);
                    let app = app.clone();
                    tokio::spawn(async move {
                        let svc = hyper::service::service_fn(move |req| http::handle(req, app.clone(), peer));
                        let _ = hyper::server::conn::http1::Builder::new()
                            .keep_alive(true)
                            .serve_connection(hyper_util::rt::TokioIo::new(stream), svc)
                            .await;
                    });
                }
                _ = stop.recv() => break,
            }
        }
        core.events.emit("server_stop", serde_json::json!({}));
        let _ = std::fs::remove_file(dir.join("server.pid"));
        Ok::<(), anyhow::Error>(())
    })?;
    std::process::exit(0);
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let sub = args.get(1).map(|s| s.as_str()).unwrap_or("");
    let r = match sub {
        "start" => cmd_start(),
        "stop" => cmd_stop(),
        "create-repo" => match args.get(2) {
            Some(n) => cmd_create_repo(n),
            None => Err(anyhow!("usage: create-repo <name>")),
        },
        "maintain" => cmd_maintain(),
        "serve" => cmd_serve(&args[2..]),
        "status" => cli::main("status", &args[2..]),
        "explain" => cli::main("explain", &args[2..]),
        "explore" => match args.get(2).map(|s| s.as_str()) {
            Some("fetch") => cli::main("explore-fetch", &args[3..]),
            Some("history") => cli::main("explore-history", &args[3..]),
            Some("covers") => cli::main("explore-covers", &args[3..]),
            _ => { println!("{}", cli::HELP); Ok(()) }
        },
        "meta-serve" => cmd_meta_serve(&args[2..]),
        "sim" => sim::main(&args[2..]),
        "fuzz" => sim::fuzz_main(&args[2..]),
        "version" | "--version" | "-V" => {
            println!("pando {} (commit {}, {})", env!("CARGO_PKG_VERSION"), env!("PANDO_COMMIT"), env!("PANDO_TARGET"));
            Ok(())
        }
        "" | "help" | "-h" | "--help" => {
            println!("{}", HELP);
            Ok(())
        }
        other => Err(anyhow!("unknown command {:?} (see pando --help)", other)),
    };
    if let Err(e) = r {
        eprintln!("pando: {:#}", e);
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn non_loopback_bind_needs_credentials() {
        let sa = |s: &str| s.parse::<std::net::SocketAddr>().unwrap();
        for ok in ["127.0.0.1:8080", "[::1]:8080"] {
            assert!(check_bind(sa(ok), false, false).is_ok(), "{} is loopback", ok);
        }
        for open in ["0.0.0.0:8080", "[::]:8080", "192.168.1.5:8080"] {
            let e = check_bind(sa(open), false, false).unwrap_err().to_string();
            assert!(e.contains("PANDO_AUTH") && e.contains("refusing"), "{}: {}", open, e);
            assert!(check_bind(sa(open), true, false).is_ok(), "{} with PANDO_AUTH", open);
            assert!(check_bind(sa(open), false, true).is_ok(), "{} with the explicit test override", open);
        }
    }
}
