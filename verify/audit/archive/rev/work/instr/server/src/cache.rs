//! Pack cache service: N cache nodes (separate HTTP listeners on ephemeral
//! 127.0.0.1 ports, same process here; in production separate machines), each
//! with an in-memory LRU of immutable pack bytes and a fall-back read from the
//! blob store. Packs are assigned to nodes by consistent hashing (64 virtual
//! nodes per cache node). Packfile URIs handed to clients point at the owner.
use crate::blob::enc;
use crate::http::{App, RespBody};
use crate::repo::Core;
use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::{Response, StatusCode};
use sha1::{Digest, Sha1};
use std::collections::{HashMap, VecDeque};
use std::convert::Infallible;
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

pub const NODES: usize = 3;
const VNODES: usize = 64;
const NODE_CACHE_BYTES: usize = 128 << 20;

pub struct Node {
    pub port: u16,
    lru: Mutex<(HashMap<String, Bytes>, VecDeque<String>, usize)>,
    pub hits: AtomicU64,
    pub misses: AtomicU64,
    pub bytes_out: AtomicU64,
}

pub struct CacheRing {
    pub nodes: Vec<Arc<Node>>,
    ring: Vec<(u64, usize)>,
    core: Arc<Core>,
}

fn h64(s: &str) -> u64 {
    let mut h = Sha1::new();
    h.update(s.as_bytes());
    u64::from_be_bytes(h.finalize()[..8].try_into().unwrap())
}

impl CacheRing {
    pub fn owner(&self, key: &str) -> usize {
        let h = h64(key);
        let i = self.ring.partition_point(|(p, _)| *p < h);
        self.ring[i % self.ring.len()].1
    }
    pub fn uri_for(&self, repo: &str, pack: &str) -> String {
        let n = &self.nodes[self.owner(&format!("{}/{}", repo, pack))];
        format!("http://127.0.0.1:{}/packs/{}/{}.pack", n.port, enc(repo), pack)
    }
    pub fn bytes_out(&self) -> u64 {
        self.nodes.iter().map(|n| n.bytes_out.load(Ordering::Relaxed)).sum()
    }
}

impl Node {
    fn get(&self, key: &str) -> Option<Bytes> {
        let l = self.lru.lock().unwrap();
        l.0.get(key).cloned()
    }
    fn put(&self, key: &str, b: Bytes) {
        if b.len() > NODE_CACHE_BYTES / 4 {
            return;
        }
        let mut l = self.lru.lock().unwrap();
        if l.0.contains_key(key) {
            return;
        }
        l.2 += b.len();
        l.0.insert(key.to_string(), b);
        l.1.push_back(key.to_string());
        while l.2 > NODE_CACHE_BYTES {
            if let Some(k) = l.1.pop_front() {
                if let Some(v) = l.0.remove(&k) {
                    l.2 -= v.len();
                }
            } else {
                break;
            }
        }
    }
}

fn resp(status: StatusCode, b: Bytes) -> Response<RespBody> {
    Response::builder()
        .status(status)
        .header("Content-Type", "application/x-git-packfile")
        .header("Cache-Control", "public, max-age=31536000, immutable")
        .body(Full::new(b).map_err(|e: Infallible| match e {}).boxed())
        .unwrap()
}

/// path: "packs/<enc repo>/<id>.pack"
async fn serve_pack(core: &Arc<Core>, node: &Arc<Node>, path: &str) -> Response<RespBody> {
    let p = path.trim_start_matches('/');
    let rest = match p.strip_prefix("packs/") {
        Some(r) => r,
        None => return resp(StatusCode::NOT_FOUND, Bytes::new()),
    };
    let (erepo, file) = match rest.rsplit_once('/') {
        Some(x) if !x.0.is_empty() && !x.0.contains('/') && x.0 != "." && x.0 != ".." => x,
        _ => return resp(StatusCode::NOT_FOUND, Bytes::new()),
    };
    let id = match file.strip_suffix(".pack") {
        Some(i) if i.len() == 40 && i.bytes().all(|c| c.is_ascii_hexdigit()) => i.to_string(),
        _ => return resp(StatusCode::NOT_FOUND, Bytes::new()),
    };
    let key = format!("{}/{}", erepo, id);
    if let Some(b) = node.get(&key) {
        node.hits.fetch_add(1, Ordering::Relaxed);
        node.bytes_out.fetch_add(b.len() as u64, Ordering::Relaxed);
        return resp(StatusCode::OK, b);
    }
    node.misses.fetch_add(1, Ordering::Relaxed);
    if core.blobs.is_s3() {
        // S3 mode: the cache is private and filled on demand (verified download)
        let (c2, e2, i2) = (core.clone(), erepo.to_string(), id.clone());
        if !matches!(tokio::task::spawn_blocking(move || c2.blobs.ensure_local_pack(&e2, &i2)).await, Ok(Ok(()))) {
            return resp(StatusCode::NOT_FOUND, Bytes::new());
        }
    }
    let path = core.blobs.root.join("repos").join(erepo).join("packs").join(format!("{}.pack", id));
    match tokio::fs::read(&path).await {
        Ok(v) => {
            let b = Bytes::from(v);
            node.put(&key, b.clone());
            node.bytes_out.fetch_add(b.len() as u64, Ordering::Relaxed);
            resp(StatusCode::OK, b)
        }
        Err(_) => resp(StatusCode::NOT_FOUND, Bytes::new()),
    }
}

pub async fn start_ring(core: Arc<Core>, _dir: &Path) -> anyhow::Result<CacheRing> {
    let mut nodes = Vec::new();
    for _ in 0..NODES {
        let l = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let port = l.local_addr()?.port();
        let node = Arc::new(Node { port, lru: Mutex::new((HashMap::new(), VecDeque::new(), 0)), hits: Default::default(), misses: Default::default(), bytes_out: Default::default() });
        let (core2, node2) = (core.clone(), node.clone());
        tokio::spawn(async move {
            loop {
                let (stream, _) = match l.accept().await {
                    Ok(x) => x,
                    Err(_) => continue,
                };
                let (core3, node3) = (core2.clone(), node2.clone());
                tokio::spawn(async move {
                    let svc = hyper::service::service_fn(move |req: hyper::Request<hyper::body::Incoming>| {
                        let (c, n) = (core3.clone(), node3.clone());
                        async move { Ok::<_, Infallible>(serve_pack(&c, &n, req.uri().path()).await) }
                    });
                    let _ = hyper::server::conn::http1::Builder::new().serve_connection(hyper_util::rt::TokioIo::new(stream), svc).await;
                });
            }
        });
        nodes.push(node);
    }
    let mut ring = Vec::new();
    for (i, _) in nodes.iter().enumerate() {
        for v in 0..VNODES {
            ring.push((h64(&format!("cache-node-{}#{}", i, v)), i));
        }
    }
    ring.sort();
    Ok(CacheRing { nodes, ring, core })
}

/// Fallback: the main port also serves `/_cache/packs/...` (same content).
pub async fn serve_local(c: &CacheRing, rest: &str, _app: &App) -> Response<RespBody> {
    let node = c.nodes[0].clone();
    serve_pack(&c.core, &node, rest).await
}
