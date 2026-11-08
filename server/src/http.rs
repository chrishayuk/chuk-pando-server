//! HTTP front end (hyper 1). Git smart HTTP endpoints, /_stats, admin and the
//! pack cache service (separate listeners, consistent hashing).
use crate::proto;
use crate::repo::Core;
use anyhow::Result;
use bytes::Bytes;
use http_body_util::{combinators::BoxBody, BodyExt, Full};
use hyper::body::{Body, Frame, Incoming};
use hyper::{Request, Response, StatusCode};
use serde_json::json;
use std::convert::Infallible;
use std::io::{BufReader, Read};
use std::pin::Pin;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::task::{Context, Poll};

pub type RespBody = BoxBody<Bytes, std::io::Error>;

pub struct App {
    pub core: Arc<Core>,
    pub bytes_in: AtomicU64,
    pub bytes_out: Arc<AtomicU64>,
    pub cache: Option<Arc<crate::cache::CacheRing>>,
    pub started: std::time::Instant,
    pub auth: crate::auth::Auth,
    /// create a repository on its first push (`pando serve` default; off for the harness and with --no-autocreate)
    pub autocreate: bool,
}

struct ChanBody {
    rx: tokio::sync::mpsc::Receiver<Bytes>,
    ctr: Arc<AtomicU64>,
}
impl Body for ChanBody {
    type Data = Bytes;
    type Error = std::io::Error;
    fn poll_frame(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Result<Frame<Bytes>, std::io::Error>>> {
        match self.rx.poll_recv(cx) {
            Poll::Ready(Some(b)) => {
                self.ctr.fetch_add(b.len() as u64, Ordering::Relaxed);
                Poll::Ready(Some(Ok(Frame::data(b))))
            }
            Poll::Ready(None) => Poll::Ready(None),
            Poll::Pending => Poll::Pending,
        }
    }
}

/// Blocking reader over an async request body.
pub struct ChanReader {
    rx: tokio::sync::mpsc::Receiver<Bytes>,
    cur: Bytes,
}
impl Read for ChanReader {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        while self.cur.is_empty() {
            match self.rx.blocking_recv() {
                Some(b) => self.cur = b,
                None => return Ok(0),
            }
        }
        let n = buf.len().min(self.cur.len());
        buf[..n].copy_from_slice(&self.cur[..n]);
        let _ = self.cur.split_to(n);
        Ok(n)
    }
}

fn full(status: StatusCode, ctype: &str, body: Vec<u8>, ctr: &AtomicU64) -> Response<RespBody> {
    ctr.fetch_add(body.len() as u64, Ordering::Relaxed);
    Response::builder()
        .status(status)
        .header("Content-Type", ctype)
        .header("Cache-Control", "no-cache, max-age=0, must-revalidate")
        .body(Full::new(Bytes::from(body)).map_err(|e: Infallible| match e {}).boxed())
        .unwrap()
}

fn text(status: StatusCode, s: &str, app: &App) -> Response<RespBody> {
    full(status, "text/plain", s.as_bytes().to_vec(), &app.bytes_out)
}

fn query_param<'a>(q: &'a str, k: &str) -> Option<&'a str> {
    q.split('&').find_map(|kv| {
        let mut it = kv.splitn(2, '=');
        if it.next() == Some(k) {
            it.next()
        } else {
            None
        }
    })
}

pub fn url_decode(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' && i + 2 < b.len() + 0 && i + 2 <= b.len() - 1 {
            if let Ok(v) = u8::from_str_radix(&s[i + 1..i + 3], 16) {
                out.push(v);
                i += 3;
                continue;
            }
        }
        out.push(if b[i] == b'+' { b' ' } else { b[i] });
        i += 1;
    }
    String::from_utf8_lossy(&out).to_string()
}

/// Split "/<repo>.git/<rest>" into (repo, rest)
fn split_repo(path: &str) -> Option<(String, String)> {
    let p = path.trim_start_matches('/');
    for suffix in ["/info/refs", "/git-upload-pack", "/git-receive-pack", "/HEAD"] {
        if let Some(r) = p.strip_suffix(suffix) {
            let repo = r.strip_suffix(".git").unwrap_or(r);
            return Some((url_decode(repo), suffix[1..].to_string()));
        }
    }
    None
}

pub fn cpu_seconds() -> f64 {
    crate::sys::cpu_seconds()
}

pub async fn handle(req: Request<Incoming>, app: Arc<App>, peer: std::net::SocketAddr) -> Result<Response<RespBody>, Infallible> {
    let path = req.uri().path().to_string();
    let query = req.uri().query().unwrap_or("").to_string();
    let method = req.method().clone();
    // Access control (docs/FLY-DEPLOYMENT-PLAN.md): signed packfile URIs under /_cache/, Basic auth for everything else.
    let authz = req.headers().get("authorization").and_then(|v| v.to_str().ok()).map(|s| s.to_string());
    if let Some(rest) = path.strip_prefix("/_cache/") {
        if !app.auth.cache_ok(rest, &query, authz.as_deref()) {
            return Ok(text(StatusCode::FORBIDDEN, "forbidden\n", &app));
        }
    } else if !app.auth.basic_ok(authz.as_deref()) {
        let mut r = text(StatusCode::UNAUTHORIZED, "authentication required\n", &app);
        r.headers_mut().insert("www-authenticate", hyper::header::HeaderValue::from_static("Basic realm=\"pando\""));
        return Ok(r);
    }
    // Principal (docs/CLI-DESIGN.md): the Basic user when access control is on; "local" for a loopback client of an
    // unauthenticated server (the single-user local case); otherwise none.
    let principal = if app.auth.basic_required() {
        app.auth.principal(authz.as_deref())
    } else if peer.ip().is_loopback() {
        Some("local".to_string())
    } else {
        None
    };
    // Read-only diagnostics: /<repo>[.git]/pando/v1/<what>
    if let Some((repo_part, sub)) = path.trim_start_matches('/').split_once("/pando/v1/") {
        let repo = url_decode(repo_part.strip_suffix(".git").unwrap_or(repo_part));
        let (core, app2, sub, query) = (app.core.clone(), app.clone(), sub.to_string(), query.clone());
        let caller = crate::diag::Caller { principal };
        let r = tokio::task::spawn_blocking(move || {
            let uri_for = |repo: &str, pack: &str| -> Option<String> {
                match app2.auth.public_uri(&format!("packs/{}/{}.pack", crate::blob::enc(repo), pack)) {
                    Some(u) => Some(u),
                    None => app2.cache.as_ref().map(|c| c.uri_for(repo, pack)),
                }
            };
            crate::diag::handle(&core, &repo, &sub, &query, &caller, &uri_for)
        })
        .await
        .unwrap_or((500, json!({"error": "internal error"})));
        let mut resp = text(StatusCode::from_u16(r.0).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR), &format!("{}\n", r.1), &app);
        resp.headers_mut().insert("content-type", hyper::header::HeaderValue::from_static("application/json"));
        return Ok(resp);
    }
    if path == "/_stats" {
        let c = &app.core;
        let cache_out = app.cache.as_ref().map(|c| c.bytes_out()).unwrap_or(0);
        let (hits, misses) = app
            .cache
            .as_ref()
            .map(|c| c.nodes.iter().fold((0, 0), |a, n| (a.0 + n.hits.load(Ordering::Relaxed), a.1 + n.misses.load(Ordering::Relaxed))))
            .unwrap_or((0, 0));
        let mut body = json!({
            "store_read_ops": c.store.reads.load(Ordering::Relaxed),
            "store_write_ops": c.store.writes.load(Ordering::Relaxed),
            "packs_stored": c.packs_stored.load(Ordering::Relaxed) + c.packs_derived.load(Ordering::Relaxed),
            "packs_stored_l0": c.packs_stored.load(Ordering::Relaxed),
            "packs_stored_derived": c.packs_derived.load(Ordering::Relaxed),
            "bytes_in": app.bytes_in.load(Ordering::Relaxed),
            "bytes_out": app.bytes_out.load(Ordering::Relaxed) + cache_out,
            "bytes_out_git": app.bytes_out.load(Ordering::Relaxed),
            "bytes_out_cache": cache_out,
            "cache_hits": hits,
            "cache_misses": misses,
            "cache_nodes": app.cache.as_ref().map(|c| c.nodes.iter().map(|n| n.port).collect::<Vec<_>>()).unwrap_or_default(),
            "cpu_seconds": cpu_seconds(),
            "metadata_keys_scanned": c.store.keys_scanned.load(Ordering::Relaxed),
            "metadata_commits": c.store.commits.load(Ordering::Relaxed),
            "metadata_conflicts": c.store.conflicts.load(Ordering::Relaxed),
            "metadata_cache_hits": c.cache_hits.load(Ordering::Relaxed),
            "uptime_seconds": app.started.elapsed().as_secs_f64(),
        });
        if let (Some(o), serde_json::Value::Object(m)) = (body.as_object_mut(), crate::metrics::stats()) {
            o.extend(m);
        }
        return Ok(full(StatusCode::OK, "application/json", body.to_string().into_bytes(), &AtomicU64::new(0)));
    }
    if path == "/_admin/create-repo" {
        let name = url_decode(query_param(&query, "name").unwrap_or(""));
        if name.is_empty() {
            return Ok(text(StatusCode::BAD_REQUEST, "missing name\n", &app));
        }
        let core = app.core.clone();
        let r = tokio::task::spawn_blocking(move || core.create_repo(&name)).await;
        return Ok(match r {
            Ok(Ok(true)) => text(StatusCode::OK, "created\n", &app),
            Ok(Ok(false)) => text(StatusCode::OK, "exists\n", &app),
            Ok(Err(e)) => text(StatusCode::INTERNAL_SERVER_ERROR, &format!("{}\n", e), &app),
            Err(e) => text(StatusCode::INTERNAL_SERVER_ERROR, &format!("{}\n", e), &app),
        });
    }
    if path == "/_admin/maintain" {
        let core = app.core.clone();
        let r = tokio::task::spawn_blocking(move || {
            let _bg = crate::metrics::background();
            crate::compact::maintain_all(&core)
        }).await;
        return Ok(match r {
            Ok(Ok(s)) => text(StatusCode::OK, &format!("{}\n", s), &app),
            Ok(Err(e)) => text(StatusCode::INTERNAL_SERVER_ERROR, &format!("{:#}\n", e), &app),
            Err(e) => text(StatusCode::INTERNAL_SERVER_ERROR, &format!("{}\n", e), &app),
        });
    }
    if let Some(rest) = path.strip_prefix("/_cache/") {
        if let Some(c) = &app.cache {
            return Ok(crate::cache::serve_local(c, rest, &app).await);
        }
    }
    let (repo, rest) = match split_repo(&path) {
        Some(x) => x,
        None => return Ok(text(StatusCode::NOT_FOUND, "not found\n", &app)),
    };
    if app.core.repo(&repo).is_none() {
        // a push (never a fetch) to a missing repository creates it, when enabled, after the same access check as
        // any push, through the same metadata transaction as create-repo
        let push = rest == "git-receive-pack" || (rest == "info/refs" && query_param(&query, "service") == Some("git-receive-pack"));
        if !(push && app.autocreate && crate::valid_repo_name(&repo)) {
            return Ok(text(StatusCode::NOT_FOUND, "repository not found\n", &app));
        }
        let (core, name) = (app.core.clone(), repo.clone());
        let created = tokio::task::spawn_blocking(move || core.create_repo(&name)).await;
        if !matches!(created, Ok(Ok(_))) {
            return Ok(text(StatusCode::INTERNAL_SERVER_ERROR, "could not create repository\n", &app));
        }
    }
    let git_protocol = req.headers().get("git-protocol").and_then(|v| v.to_str().ok()).unwrap_or("").to_string();
    let gzip = req.headers().get("content-encoding").and_then(|v| v.to_str().ok()).map(|v| v.contains("gzip")).unwrap_or(false);
    match (method.as_str(), rest.as_str()) {
        ("GET", "info/refs") => {
            let service = query_param(&query, "service").unwrap_or("");
            let core = app.core.clone();
            match service {
                "git-upload-pack" => {
                    let mut out = Vec::new();
                    proto::pkt_str(&mut out, "# service=git-upload-pack\n");
                    proto::flush(&mut out);
                    if git_protocol.split(':').any(|p| p == "version=2") {
                        out.extend(proto::v2_advertisement());
                    } else {
                        let repo2 = repo.clone();
                        let adv = tokio::task::spawn_blocking(move || crate::v0::advertise(&core, &repo2)).await.unwrap_or_default();
                        out.extend(adv);
                    }
                    Ok(full(StatusCode::OK, "application/x-git-upload-pack-advertisement", out, &app.bytes_out))
                }
                "git-receive-pack" => {
                    let explicit = git_protocol.split(':').any(|p| p == "explicit-haves");
                    let repo2 = repo.clone();
                    let adv = tokio::task::spawn_blocking(move || proto::receive_advertisement(&core, &repo2, explicit)).await.unwrap_or_default();
                    let mut out = Vec::new();
                    proto::pkt_str(&mut out, "# service=git-receive-pack\n");
                    proto::flush(&mut out);
                    out.extend(adv);
                    Ok(full(StatusCode::OK, "application/x-git-receive-pack-advertisement", out, &app.bytes_out))
                }
                _ => Ok(text(StatusCode::FORBIDDEN, "dumb HTTP protocol is not supported\n", &app)),
            }
        }
        ("POST", "git-upload-pack") | ("POST", "git-receive-pack") => {
            let receive = rest == "git-receive-pack";
            let v2 = git_protocol.split(':').any(|p| p == "version=2");
            let (in_tx, in_rx) = tokio::sync::mpsc::channel::<Bytes>(32);
            let (out_tx, out_rx) = tokio::sync::mpsc::channel::<Bytes>(32);
            // pump request body
            let app2 = app.clone();
            let mut body = req.into_body();
            tokio::spawn(async move {
                while let Some(fr) = body.frame().await {
                    match fr {
                        Ok(f) => {
                            if let Ok(d) = f.into_data() {
                                app2.bytes_in.fetch_add(d.len() as u64, Ordering::Relaxed);
                                if in_tx.send(d).await.is_err() {
                                    break;
                                }
                            }
                        }
                        Err(_) => break,
                    }
                }
            });
            let core = app.core.clone();
            let app3 = app.clone();
            let principal_rx = principal.clone();
            tokio::task::spawn_blocking(move || {
                let reader = ChanReader { rx: in_rx, cur: Bytes::new() };
                let boxed: Box<dyn Read> = if gzip { Box::new(flate2::read::MultiGzDecoder::new(reader)) } else { Box::new(reader) };
                let br = BufReader::with_capacity(256 * 1024, boxed);
                let mut send = |v: Vec<u8>| -> Result<()> { out_tx.blocking_send(Bytes::from(v)).map_err(|_| anyhow::anyhow!("client went away")) };
                proto::set_sideband(false); // blocking threads are reused across requests
                crate::diag::set_principal(principal_rx);
                let r = if receive {
                    proto::handle_receive(&core, &repo, br, &mut send)
                } else if v2 {
                    let uri_for = |repo: &str, pack: &str| -> Option<String> {
                        // a public base URL (signed if configured) when reachable from outside; else the loopback cache nodes
                        match app3.auth.public_uri(&format!("packs/{}/{}.pack", crate::blob::enc(repo), pack)) {
                            Some(u) => Some(u),
                            None => app3.cache.as_ref().map(|c| c.uri_for(repo, pack)),
                        }
                    };
                    let up = proto::UriProvider { uri_for: &uri_for };
                    proto::handle_upload_v2(&core, &repo, br, &up, &mut send)
                } else {
                    crate::v0::handle_upload_v0(&core, &repo, br, &mut send)
                };
                if let Err(e) = r {
                    core.events.emit("request_error", json!({"repo": repo, "receive": receive, "error": format!("{:#}", e)}));
                    let mut o = Vec::new();
                    if receive {
                        // report as an unpack failure so the client prints the reason
                        let mut rep = Vec::new();
                        proto::pkt_str(&mut rep, &format!("unpack {}\n", format!("{:#}", e).replace('\n', " ")));
                        proto::flush(&mut rep);
                        let mut d = vec![1u8];
                        d.extend_from_slice(&rep);
                        proto::pkt(&mut o, &d);
                        proto::flush(&mut o);
                    } else if proto::in_sideband() {
                        // inside the multiplexed pack stream: band 3 is the fatal-error channel (audit F8)
                        let mut d = vec![3u8];
                        d.extend_from_slice(format!("{:#}\n", e).as_bytes());
                        proto::pkt(&mut o, &d);
                    } else {
                        proto::pkt_str(&mut o, &format!("ERR {:#}\n", e));
                    }
                    let _ = send(o);
                }
            });
            let ctype = if receive { "application/x-git-receive-pack-result" } else { "application/x-git-upload-pack-result" };
            Ok(Response::builder()
                .status(StatusCode::OK)
                .header("Content-Type", ctype)
                .header("Cache-Control", "no-cache, max-age=0, must-revalidate")
                .body(ChanBody { rx: out_rx, ctr: app.bytes_out.clone() }.boxed())
                .unwrap())
        }
        ("GET", "HEAD") => {
            let rs = app.core.repo(&repo).unwrap();
            Ok(text(StatusCode::OK, &format!("ref: {}\n", rs.trunk), &app))
        }
        _ => Ok(text(StatusCode::NOT_FOUND, "not found\n", &app)),
    }
}
