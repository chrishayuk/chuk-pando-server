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
mod shallow;
mod sim;
mod v0;

use anyhow::{anyhow, bail, Context, Result};
use std::io::{Read, Write};
use std::net::TcpStream;
use std::os::unix::process::CommandExt;
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
    pid > 0 && unsafe { libc::kill(pid, 0) } == 0
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
    cmd.arg("serve").arg("--port").arg(port.to_string()).arg("--data").arg(&dir);
    cmd.stdin(std::process::Stdio::null()).stdout(log.try_clone()?).stderr(log);
    unsafe {
        cmd.pre_exec(|| {
            libc::setsid();
            Ok(())
        });
    }
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
            unsafe { libc::kill(pid, libc::SIGTERM) };
            let t0 = Instant::now();
            while pid_alive(pid) && t0.elapsed() < Duration::from_secs(15) {
                std::thread::sleep(Duration::from_millis(50));
            }
            if pid_alive(pid) {
                unsafe { libc::kill(-pid, libc::SIGKILL) };
                unsafe { libc::kill(pid, libc::SIGKILL) };
            }
        }
        let _ = std::fs::remove_file(dir.join("server.pid"));
    }
    Ok(())
}

fn valid_repo_name(n: &str) -> bool {
    !n.is_empty() && !n.contains("..") && !n.starts_with('/') && !n.starts_with('_') && n.chars().all(|c| c.is_ascii_alphanumeric() || "._-/".contains(c))
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
    use std::os::unix::io::AsRawFd;
    if unsafe { libc::flock(f.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
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

/// Many concurrent pushes need many sockets + pack files: lift the soft fd limit
/// (shells often start with 256) as far as the hard limit allows.
fn raise_nofile() {
    unsafe {
        let mut rl: libc::rlimit = std::mem::zeroed();
        if libc::getrlimit(libc::RLIMIT_NOFILE, &mut rl) != 0 {
            return;
        }
        for want in [rl.rlim_max, 1 << 20, 65536, 10240] {
            if want <= rl.rlim_cur || want > rl.rlim_max {
                continue;
            }
            let n = libc::rlimit { rlim_cur: want, rlim_max: rl.rlim_max };
            if libc::setrlimit(libc::RLIMIT_NOFILE, &n) == 0 {
                return;
            }
        }
    }
}

fn cmd_serve(args: &[String]) -> Result<()> {
    let mut port = 0u16;
    let mut dir = PathBuf::new();
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--port" => {
                port = args[i + 1].parse()?;
                i += 1;
            }
            "--data" => {
                dir = PathBuf::from(&args[i + 1]);
                i += 1;
            }
            _ => {}
        }
        i += 1;
    }
    unsafe {
        libc::setsid();
    }
    raise_nofile();
    let _lock = lock_data_dir(&dir)?;
    let (store, blobs) = open_storage(&dir)?;
    let events = Arc::new(events::Events::open(&dir.join("events"), &format!("server-{}", std::process::id())));
    events.emit("server_start", serde_json::json!({"pid": std::process::id(), "port": port}));
    let core = Arc::new(repo::Core::new(store, blobs, events));
    let rt = tokio::runtime::Builder::new_multi_thread().enable_all().max_blocking_threads(2048).thread_stack_size(4 << 20).build()?;
    rt.block_on(async move {
        let sock = tokio::net::TcpSocket::new_v4()?;
        sock.set_reuseaddr(true)?;
        // PANDO_BIND: listen address (default loopback; e.g. "[::]" on Fly.io). Exposure beyond loopback should be
        // paired with PANDO_AUTH (docs/FLY-DEPLOYMENT-PLAN.md).
        let bind = std::env::var("PANDO_BIND").ok().filter(|v| !v.is_empty()).unwrap_or_else(|| "127.0.0.1".into());
        let sa: std::net::SocketAddr = format!("{}:{}", bind, port).parse()?;
        let sock = if sa.is_ipv6() { tokio::net::TcpSocket::new_v6()? } else { sock };
        sock.bind(sa)?;
        let listener = sock.listen(4096)?;
        let cache = cache::start_ring(core.clone(), &dir).await.ok().map(Arc::new);
        let app = Arc::new(http::App {
            core: core.clone(),
            bytes_in: Default::default(),
            bytes_out: Default::default(),
            cache,
            started: Instant::now(),
            auth: auth::Auth::from_env(),
        });
        compact::start_background(core.clone());
        // pid file written only once the socket accepts connections
        let tmp = dir.join("server.pid.tmp");
        std::fs::write(&tmp, format!("{}\n", std::process::id()))?;
        std::fs::rename(&tmp, dir.join("server.pid"))?;
        let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
        let mut intr = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt())?;
        loop {
            tokio::select! {
                acc = listener.accept() => {
                    let (stream, _) = match acc { Ok(x) => x, Err(_) => continue };
                    let _ = stream.set_nodelay(true);
                    let app = app.clone();
                    tokio::spawn(async move {
                        let svc = hyper::service::service_fn(move |req| http::handle(req, app.clone()));
                        let _ = hyper::server::conn::http1::Builder::new()
                            .keep_alive(true)
                            .serve_connection(hyper_util::rt::TokioIo::new(stream), svc)
                            .await;
                    });
                }
                _ = term.recv() => break,
                _ = intr.recv() => break,
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
        "meta-serve" => cmd_meta_serve(&args[2..]),
        "sim" => sim::main(&args[2..]),
        "fuzz" => sim::fuzz_main(&args[2..]),
        "version" => {
            println!("pando 0.1");
            Ok(())
        }
        _ => Err(anyhow!("usage: pando start|stop|create-repo <name>|maintain|serve|sim")),
    };
    if let Err(e) = r {
        eprintln!("pando: {:#}", e);
        std::process::exit(1);
    }
}
