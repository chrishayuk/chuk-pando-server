//! Minimal S3-compatible client for immutable pack storage (docs/NETWORK-STORAGE.md).
//!
//! Path-style requests over plain HTTP/1.1 (one connection per request), signed with AWS Signature Version 4
//! including the payload's SHA-256, so the store verifies every uploaded body. SHA-256 and HMAC are implemented
//! here because the offline build has no crypto crates; both are tested against published vectors (FIPS 180-4,
//! RFC 4231) and the signer against the AWS SigV4 documentation example.
//!
//! Operations: create-only PUT (`If-None-Match: *`; 412 means the key already exists), GET (optionally a byte
//! range), HEAD. Nothing here can overwrite an object.
use anyhow::{anyhow, bail, Context, Result};
use std::io::{Read, Write};

// ---------------------------------------------------------------- SHA-256 (FIPS 180-4)
const K: [u32; 64] = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
];

pub fn sha256(data: &[u8]) -> [u8; 32] {
    let mut h: [u32; 8] = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
    let bitlen = (data.len() as u64).wrapping_mul(8);
    let mut tail = Vec::with_capacity(128);
    let full = data.len() / 64 * 64;
    tail.extend_from_slice(&data[full..]);
    tail.push(0x80);
    while tail.len() % 64 != 56 {
        tail.push(0);
    }
    tail.extend_from_slice(&bitlen.to_be_bytes());
    let mut block = |c: &[u8]| {
        let mut w = [0u32; 64];
        for i in 0..16 {
            w[i] = u32::from_be_bytes([c[4 * i], c[4 * i + 1], c[4 * i + 2], c[4 * i + 3]]);
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16].wrapping_add(s0).wrapping_add(w[i - 7]).wrapping_add(s1);
        }
        let mut v = h;
        for i in 0..64 {
            let s1 = v[4].rotate_right(6) ^ v[4].rotate_right(11) ^ v[4].rotate_right(25);
            let ch = (v[4] & v[5]) ^ (!v[4] & v[6]);
            let t1 = v[7].wrapping_add(s1).wrapping_add(ch).wrapping_add(K[i]).wrapping_add(w[i]);
            let s0 = v[0].rotate_right(2) ^ v[0].rotate_right(13) ^ v[0].rotate_right(22);
            let maj = (v[0] & v[1]) ^ (v[0] & v[2]) ^ (v[1] & v[2]);
            let t2 = s0.wrapping_add(maj);
            v = [t1.wrapping_add(t2), v[0], v[1], v[2], v[3].wrapping_add(t1), v[4], v[5], v[6]];
        }
        for i in 0..8 {
            h[i] = h[i].wrapping_add(v[i]);
        }
    };
    for c in data[..full].chunks(64) {
        block(c);
    }
    for c in tail.chunks(64) {
        block(c);
    }
    let mut out = [0u8; 32];
    for i in 0..8 {
        out[4 * i..4 * i + 4].copy_from_slice(&h[i].to_be_bytes());
    }
    out
}

pub fn hmac_sha256(key: &[u8], msg: &[u8]) -> [u8; 32] {
    let mut k = [0u8; 64];
    if key.len() > 64 {
        k[..32].copy_from_slice(&sha256(key));
    } else {
        k[..key.len()].copy_from_slice(key);
    }
    let mut inner: Vec<u8> = k.iter().map(|b| b ^ 0x36).collect();
    inner.extend_from_slice(msg);
    let ih = sha256(&inner);
    let mut outer: Vec<u8> = k.iter().map(|b| b ^ 0x5c).collect();
    outer.extend_from_slice(&ih);
    sha256(&outer)
}

pub fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{:02x}", x)).collect()
}

// ---------------------------------------------------------------- SigV4
/// URI-encode per SigV4: unreserved characters kept; '/' kept when `slash` (paths).
fn uri_encode(s: &str, slash: bool) -> String {
    let mut o = String::new();
    for b in s.bytes() {
        let c = b as char;
        if c.is_ascii_alphanumeric() || "-_.~".contains(c) || (slash && c == '/') {
            o.push(c);
        } else {
            o.push_str(&format!("%{:02X}", b));
        }
    }
    o
}

/// UTC date (YYYYMMDD) and timestamp (YYYYMMDDTHHMMSSZ) from Unix seconds (civil-from-days, H. Hinnant).
fn amz_dates(secs: u64) -> (String, String) {
    let days = (secs / 86400) as i64;
    let rem = secs % 86400;
    let z = days + 719468;
    let era = z.div_euclid(146097);
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + if m <= 2 { 1 } else { 0 };
    let date = format!("{:04}{:02}{:02}", y, m, d);
    let ts = format!("{}T{:02}{:02}{:02}Z", date, rem / 3600, rem % 3600 / 60, rem % 60);
    (date, ts)
}

/// The SigV4 `Authorization` value. `headers` are the signed headers (lower-case names), which must include host,
/// x-amz-content-sha256 and x-amz-date.
fn authorization(key: &str, secret: &str, region: &str, method: &str, path: &str, query: &str, headers: &[(String, String)], payload_hash: &str, date: &str, ts: &str) -> String {
    let mut hs: Vec<(String, String)> = headers.iter().map(|(k, v)| (k.to_lowercase(), v.trim().to_string())).collect();
    hs.sort();
    let canon_headers: String = hs.iter().map(|(k, v)| format!("{}:{}\n", k, v)).collect();
    let signed: Vec<&str> = hs.iter().map(|(k, _)| k.as_str()).collect();
    let signed = signed.join(";");
    let creq = format!("{}\n{}\n{}\n{}\n{}\n{}", method, path, query, canon_headers, signed, payload_hash);
    let scope = format!("{}/{}/s3/aws4_request", date, region);
    let sts = format!("AWS4-HMAC-SHA256\n{}\n{}\n{}", ts, scope, hex(&sha256(creq.as_bytes())));
    let kd = hmac_sha256(format!("AWS4{}", secret).as_bytes(), date.as_bytes());
    let kr = hmac_sha256(&kd, region.as_bytes());
    let ks = hmac_sha256(&kr, b"s3");
    let kk = hmac_sha256(&ks, b"aws4_request");
    let sig = hex(&hmac_sha256(&kk, sts.as_bytes()));
    format!("AWS4-HMAC-SHA256 Credential={}/{}, SignedHeaders={}, Signature={}", key, scope, signed, sig)
}

// ---------------------------------------------------------------- profiling (docs/S3-PROFILE.md)
#[derive(Default)]
struct Prof {
    n: u64,
    bytes: u64,
    connect_us: u64,
    sign_us: u64,
    wire_us: u64,
    by_op: std::collections::BTreeMap<&'static str, (u64, u64)>, // op -> (count, total us)
    /// cache fill (blob.rs fetch): whole fill, verification, local write + fsync
    fill_n: u64,
    fill_us: u64,
    verify_us: u64,
    local_write_us: u64,
}
thread_local! {
    static PROF: std::cell::RefCell<Prof> = std::cell::RefCell::new(Prof::default());
}

/// Object-store work done by this thread since the last call (then reset): attributed to the current push.
pub fn prof_take() -> serde_json::Value {
    PROF.with(|p| {
        let p = std::mem::take(&mut *p.borrow_mut());
        serde_json::json!({"requests": p.n, "bytes": p.bytes, "connect_ms": p.connect_us as f64 / 1e3, "hash_sign_ms": p.sign_us as f64 / 1e3,
                           "wire_and_server_ms": p.wire_us as f64 / 1e3,
                           "cache_fills": p.fill_n, "cache_fill_ms": p.fill_us as f64 / 1e3, "verify_ms": p.verify_us as f64 / 1e3, "cache_write_fsync_ms": p.local_write_us as f64 / 1e3,
                           "by_op": p.by_op.iter().map(|(k, (c, us))| (k.to_string(), serde_json::json!({"n": c, "ms": *us as f64 / 1e3}))).collect::<serde_json::Map<_, _>>()})
    })
}

pub fn prof_fill(total_us: u64, verify_us: u64, write_us: u64) {
    PROF.with(|p| {
        let mut p = p.borrow_mut();
        p.fill_n += 1;
        p.fill_us += total_us;
        p.verify_us += verify_us;
        p.local_write_us += write_us;
    });
}

fn prof_add(op: &'static str, bytes: u64, connect: u64, sign: u64, wire: u64) {
    PROF.with(|p| {
        let mut p = p.borrow_mut();
        p.n += 1;
        p.bytes += bytes;
        p.connect_us += connect;
        p.sign_us += sign;
        p.wire_us += wire;
        let e = p.by_op.entry(op).or_default();
        e.0 += 1;
        e.1 += connect + sign + wire;
    });
}

// ---------------------------------------------------------------- client
pub struct S3 {
    host: String, // the Host header value (host[:port] as given)
    addr: String, // host:port to connect to
    tls: Option<(std::sync::Arc<rustls::ClientConfig>, rustls::pki_types::ServerName<'static>)>,
    bucket: String,
    key: String,
    secret: String,
    region: String,
}

pub struct Resp {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl Resp {
    pub fn header(&self, n: &str) -> Option<&str> {
        self.headers.iter().find(|(k, _)| k.eq_ignore_ascii_case(n)).map(|(_, v)| v.as_str())
    }
}

impl S3 {
    /// From `PANDO_S3=http://HOST:PORT/BUCKET`, `PANDO_S3_KEY`, `PANDO_S3_SECRET`, optional `PANDO_S3_REGION`.
    pub fn from_env() -> Result<Option<S3>> {
        let url = match std::env::var("PANDO_S3") {
            Ok(u) if !u.is_empty() => u,
            _ => return Ok(None),
        };
        let (https, rest) = match (url.strip_prefix("https://"), url.strip_prefix("http://")) {
            (Some(r), _) => (true, r),
            (None, Some(r)) => (false, r),
            _ => bail!("PANDO_S3 must be http(s)://HOST[:PORT]/BUCKET"),
        };
        let (host, bucket) = rest.split_once('/').ok_or_else(|| anyhow!("PANDO_S3 needs a bucket"))?;
        let hostname = host.rsplit_once(':').map(|(h, _)| h).unwrap_or(host).to_string();
        let addr = if host.contains(':') { host.to_string() } else { format!("{}:{}", host, if https { 443 } else { 80 }) };
        let tls = if https { Some((tls_config()?, rustls::pki_types::ServerName::try_from(hostname)?)) } else { None };
        Ok(Some(S3 {
            host: host.to_string(),
            addr,
            tls,
            bucket: bucket.trim_end_matches('/').to_string(),
            key: std::env::var("PANDO_S3_KEY").context("PANDO_S3_KEY")?,
            secret: std::env::var("PANDO_S3_SECRET").context("PANDO_S3_SECRET")?,
            region: std::env::var("PANDO_S3_REGION").unwrap_or_else(|_| "us-east-1".into()),
        }))
    }

    /// Every operation here is safe to retry: GET and HEAD read; a create-only PUT whose first attempt succeeded
    /// unseen gets 412 on retry and is then verified by the caller. Transport errors (refused, reset, short
    /// response) and 5xx are retried with backoff for up to ~10 s; the last error is returned.
    fn request(&self, method: &str, key: &str, extra: &[(&str, String)], body: &[u8]) -> Result<Resp> {
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
        let mut wait = 20u64;
        let mut attempt = 0u32;
        loop {
            attempt += 1;
            let r = self.request_once(method, key, extra, body).with_context(|| format!("object store {} {} (attempt {})", method, key, attempt));
            let retry = match &r {
                Ok(resp) => resp.status >= 500,
                Err(_) => true,
            };
            if !retry || std::time::Instant::now() > deadline {
                if retry {
                    crate::metrics::s3_op("gave_up");
                }
                return r;
            }
            crate::metrics::s3_op("retry");
            std::thread::sleep(std::time::Duration::from_millis(wait));
            wait = (wait * 2).min(1000);
        }
    }

    fn request_once(&self, method: &str, key: &str, extra: &[(&str, String)], body: &[u8]) -> Result<Resp> {
        let t_sign = std::time::Instant::now();
        let path = format!("/{}/{}", uri_encode(&self.bucket, false), uri_encode(key, true));
        let ph = hex(&sha256(body));
        let secs = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)?.as_secs();
        let (date, ts) = amz_dates(secs);
        let signed = vec![("host".to_string(), self.host.clone()), ("x-amz-content-sha256".to_string(), ph.clone()), ("x-amz-date".to_string(), ts.clone())];
        let auth = authorization(&self.key, &self.secret, &self.region, method, &path, "", &signed, &ph, &date, &ts);
        let mut req = format!(
            "{} {} HTTP/1.1\r\nHost: {}\r\nx-amz-content-sha256: {}\r\nx-amz-date: {}\r\nAuthorization: {}\r\nContent-Length: {}\r\nConnection: close\r\n",
            method,
            path,
            self.host,
            ph,
            ts,
            auth,
            body.len()
        );
        for (k, v) in extra {
            req.push_str(&format!("{}: {}\r\n", k, v));
        }
        req.push_str("\r\n");
        let sign_us = t_sign.elapsed().as_micros() as u64;
        let t_conn = std::time::Instant::now();
        // Bounded waits (audit F6): connect 3 s; each read or write may wait PANDO_S3_TIMEOUT (default 15 s) without
        // progress. Before: no connect timeout and a 120 s read timeout, so a stalled store held a push for minutes.
        let tcp = crate::meta::connect_timeout(&self.addr, std::time::Duration::from_secs(3)).with_context(|| format!("object store {}", self.addr))?;
        tcp.set_nodelay(true)?;
        let io = Some(std::time::Duration::from_secs(crate::meta::env_secs("PANDO_S3_TIMEOUT", 15)));
        tcp.set_read_timeout(io)?;
        tcp.set_write_timeout(io)?;
        let mut c: Box<dyn ReadWrite> = match &self.tls {
            None => Box::new(tcp),
            Some((cfg, name)) => {
                let conn = rustls::ClientConnection::new(cfg.clone(), name.clone())?;
                Box::new(rustls::StreamOwned::new(conn, tcp))
            }
        };
        let conn_us = t_conn.elapsed().as_micros() as u64;
        let t_wire = std::time::Instant::now();
        // A server may answer before reading the whole body (e.g. 412 for a create-only PUT of an existing key) and
        // close; the body write then fails with a reset. Read the response anyway: it is the real outcome.
        let wrote = c.write_all(req.as_bytes()).and_then(|_| c.write_all(body));
        let mut raw = Vec::new();
        let read = match c.read_to_end(&mut raw) {
            // TLS peers may close without close_notify after a complete response; parse_response checks completeness
            Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof && !raw.is_empty() => Ok(raw.len()),
            r => r,
        };
        let op: &'static str = match method {
            "PUT" => "put",
            "GET" => "get",
            "HEAD" => "head",
            _ => "other",
        };
        prof_add(op, (body.len() + raw.len()) as u64, conn_us, sign_us, t_wire.elapsed().as_micros() as u64);
        match (parse_response(&raw, method == "HEAD"), wrote, read) {
            (Ok(r), _, _) => Ok(r),
            (Err(_), Err(e), _) | (Err(_), Ok(()), Err(e)) => Err(e.into()),
            (Err(e), Ok(()), Ok(_)) => Err(e),
        }
    }

    /// Create-only upload. Ok(true): created; Ok(false): the key already existed (412) and was not touched.
    pub fn put_if_absent(&self, key: &str, body: &[u8]) -> Result<bool> {
        let r = self.request("PUT", key, &[("If-None-Match", "*".into())], body)?;
        match r.status {
            200 => Ok(true),
            412 => Ok(false),
            s => bail!("object store PUT {} -> {}: {}", key, s, String::from_utf8_lossy(&r.body).chars().take(300).collect::<String>()),
        }
    }

    pub fn get(&self, key: &str) -> Result<Option<Vec<u8>>> {
        let r = self.request("GET", key, &[], b"")?;
        match r.status {
            200 => Ok(Some(r.body)),
            404 => Ok(None),
            s => bail!("object store GET {} -> {}", key, s),
        }
    }

    pub fn get_range(&self, key: &str, start: u64, end_incl: u64) -> Result<Option<Vec<u8>>> {
        let r = self.request("GET", key, &[("Range", format!("bytes={}-{}", start, end_incl))], b"")?;
        match r.status {
            200 | 206 => Ok(Some(r.body)),
            404 => Ok(None),
            s => bail!("object store GET range {} -> {}", key, s),
        }
    }

    /// Size of the object, or None.
    pub fn head(&self, key: &str) -> Result<Option<u64>> {
        let r = self.request("HEAD", key, &[], b"")?;
        match r.status {
            200 => Ok(Some(r.header("content-length").and_then(|v| v.parse().ok()).ok_or_else(|| anyhow!("HEAD without length"))?)),
            404 => Ok(None),
            s => bail!("object store HEAD {} -> {}", key, s),
        }
    }
}

trait ReadWrite: Read + Write {}
impl<T: Read + Write> ReadWrite for T {}

/// TLS client configuration: Mozilla's root store (webpki-roots), plus an optional extra CA from `PANDO_S3_CA` (PEM),
/// e.g. for a local store with a self-signed certificate. Certificates are always verified.
fn tls_config() -> Result<std::sync::Arc<rustls::ClientConfig>> {
    let mut roots = rustls::RootCertStore::empty();
    roots.extend(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());
    if let Ok(p) = std::env::var("PANDO_S3_CA") {
        use rustls::pki_types::pem::PemObject;
        for c in rustls::pki_types::CertificateDer::pem_file_iter(&p).with_context(|| format!("PANDO_S3_CA {}", p))? {
            roots.add(c?)?;
        }
    }
    let provider = std::sync::Arc::new(rustls::crypto::ring::default_provider());
    let cfg = rustls::ClientConfig::builder_with_provider(provider).with_safe_default_protocol_versions()?.with_root_certificates(roots).with_no_client_auth();
    Ok(std::sync::Arc::new(cfg))
}

fn parse_response(raw: &[u8], head: bool) -> Result<Resp> {
    let end = raw.windows(4).position(|w| w == b"\r\n\r\n").ok_or_else(|| anyhow!("object store: truncated response"))?;
    let hdr = std::str::from_utf8(&raw[..end])?;
    let mut lines = hdr.split("\r\n");
    let status: u16 = lines.next().and_then(|l| l.split_whitespace().nth(1)).and_then(|s| s.parse().ok()).ok_or_else(|| anyhow!("bad status line"))?;
    let headers: Vec<(String, String)> = lines.filter_map(|l| l.split_once(':')).map(|(k, v)| (k.trim().to_string(), v.trim().to_string())).collect();
    let mut body = raw[end + 4..].to_vec();
    if head {
        body.clear();
    } else if headers.iter().any(|(k, v)| k.eq_ignore_ascii_case("transfer-encoding") && v.eq_ignore_ascii_case("chunked")) {
        body = dechunk(&body)?;
    } else if let Some(n) = headers.iter().find(|(k, _)| k.eq_ignore_ascii_case("content-length")).and_then(|(_, v)| v.parse::<usize>().ok()) {
        if body.len() < n {
            bail!("object store: short body ({} of {} bytes)", body.len(), n);
        }
        body.truncate(n);
    }
    Ok(Resp { status, headers, body })
}

fn dechunk(b: &[u8]) -> Result<Vec<u8>> {
    let mut out = Vec::new();
    let mut i = 0;
    loop {
        let nl = b[i..].windows(2).position(|w| w == b"\r\n").ok_or_else(|| anyhow!("bad chunk"))? + i;
        let n = usize::from_str_radix(std::str::from_utf8(&b[i..nl])?.split(';').next().unwrap().trim(), 16)?;
        if n == 0 {
            return Ok(out);
        }
        out.extend_from_slice(b.get(nl + 2..nl + 2 + n).ok_or_else(|| anyhow!("short chunk"))?);
        i = nl + 2 + n + 2;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn sha256_vectors() {
        assert_eq!(hex(&sha256(b"")), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
        assert_eq!(hex(&sha256(b"abc")), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
        assert_eq!(
            hex(&sha256(b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq")),
            "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1"
        );
        let m = vec![b'a'; 1_000_000];
        assert_eq!(hex(&sha256(&m)), "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0");
        // lengths around the padding boundary (55/56 and 63/64 bytes); expected values from Python hashlib
        let want = [
            "4738a3251bb63f069631865c9f13c8ae71dd9ae35d931aafe41d2a8450a0c2d3",
            "976c902bdd5d5e69fea580d001fb2f327ef54b14e2ef41ed0503dcc2dbe771b1",
            "ece61dffe174a9ca613ded7f5115064c7b82a58c749578b1c7e3f11e3cd1430f",
            "4c787b18fc81ef6bed1770d04ed815c376fb40086408cc53f8eb56e05024289c",
            "63ed63c47a810d314c2f8fb5d01f0a9daeea80871b1a3c98d112b0dfa1f2ea18",
            "943cc61860776ca8e78a365d988036ed2ade2ca4aca3236dd9501adc4fbbaa27",
            "849e6994b02d460680d1d4d9ed690aa35811c88056987e0b6cad0eb85cb7d98d",
            "d175981afaf25e685b62793092fada1a5c558abdbbcaa6b1fd079f9e63da6877",
            "7e4508ce173f6048bd59f51b5e19bf4e03fb658bc42c9ad10f19bd84772a1bf7",
            "cd1fe83cee4304baaddb3cb993e1c86959c66b31ca6668170704df63df70e9ec",
            "2e491c140ec361d0d2095b73c2da0e89622851b3dd49521ca4dae1d4209b09dd",
            "ceed7831cac7af05835437ae5f10140ff06223749ff2d8dd1554ee1ebe4f6afd",
            "dab79c13012357a863d73f8280c0c96864183460cf4b526fade88b45fa261b00",
            "4267755c91adbe48467358342d80bda027985cda840cea5aef936cdc0aa6138e",
            "6cfeeb3aa25d3f411dae5eec17d7369ca7153e72dcf54bcf4c3daec0f5b21fc7",
            "2fa7eeba09baebc8ecef6fcfd44a61d64619aefae0b51f724658aa6dc03e8424",
            "603494bff2b1eb821298a82832d9bfa23dd98b4b284997c660b50db3d2a71de6",
            "d6d16d1d6763046187abdba2c5bf8411b5d08738843bcd708a51e853d81ea49b",
            "21b3b79175786deab215c4582f17796e919e2a8426cf26c30610d142732afca2",
            "f401964b468203df7d9450b5716730d4ec22c26717f3231931c57a5fae2ac091",
        ];
        for (i, n) in (50..70).enumerate() {
            assert_eq!(hex(&sha256(&vec![7u8; n])), want[i], "SHA-256 of {} bytes", n);
        }
    }
    #[test]
    fn hmac_rfc4231_case2() {
        assert_eq!(hex(&hmac_sha256(b"Jefe", b"what do ya want for nothing?")), "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843");
    }
    #[test]
    fn sigv4_aws_example_get_object() {
        // AWS S3 documentation, "Example: GET Object" (Signature Version 4, header-based auth)
        let ph = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
        let h = vec![
            ("host".to_string(), "examplebucket.s3.amazonaws.com".to_string()),
            ("range".to_string(), "bytes=0-9".to_string()),
            ("x-amz-content-sha256".to_string(), ph.to_string()),
            ("x-amz-date".to_string(), "20130524T000000Z".to_string()),
        ];
        let a = authorization("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "us-east-1", "GET", "/test.txt", "", &h, ph, "20130524", "20130524T000000Z");
        assert!(a.ends_with("Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"), "{}", a);
    }
    #[test]
    fn dates() {
        assert_eq!(amz_dates(1369353600), ("20130524".to_string(), "20130524T000000Z".to_string()));
        assert_eq!(amz_dates(951782400).0, "20000229");
    }
}
