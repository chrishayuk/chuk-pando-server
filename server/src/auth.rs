//! Access control for a deployment reachable from outside one host (docs/FLY-DEPLOYMENT-PLAN.md, G2–G4). Every part
//! is off unless configured, so the local test deployments are unchanged.
//!
//! - `PANDO_AUTH=user:password`: HTTP Basic is required on every Git and admin endpoint, and on `/_stats`.
//! - `PANDO_PUBLIC_URL=https://host`: packfile URIs are advertised under this base URL (served on the frontend's own
//!   port at `/_cache/...`) instead of the loopback cache nodes.
//! - `PANDO_URI_SECRET`: packfile URIs carry an expiry and an HMAC-SHA256 signature over (path, expiry). `/_cache/` then
//!   requires a valid, unexpired signature and nothing else, so clients fetch URIs without credentials, but the URIs
//!   cannot be forged or reused after expiry. `PANDO_URI_TTL` sets the lifetime in seconds (default 3600).
//! - `PANDO_META_SECRET`: the metadata-service connection handshake (see meta.rs).
use crate::s3::{hex, hmac_sha256};

pub struct Auth {
    basic: Option<String>, // expected "user:password"
    pub public_url: Option<String>,
    uri_secret: Option<Vec<u8>>,
    uri_ttl: u64,
}

fn env(k: &str) -> Option<String> {
    std::env::var(k).ok().filter(|v| !v.is_empty())
}

impl Auth {
    pub fn from_env() -> Auth {
        Auth {
            basic: env("PANDO_AUTH"),
            public_url: env("PANDO_PUBLIC_URL").map(|u| u.trim_end_matches('/').to_string()),
            uri_secret: env("PANDO_URI_SECRET").map(|s| s.into_bytes()),
            uri_ttl: env("PANDO_URI_TTL").and_then(|v| v.parse().ok()).unwrap_or(3600),
        }
    }

    /// Does this request carry the configured Basic credentials? (true when Basic auth is not configured)
    pub fn basic_ok(&self, authorization: Option<&str>) -> bool {
        let want = match &self.basic {
            None => return true,
            Some(w) => w,
        };
        let got = authorization.and_then(|h| h.strip_prefix("Basic ")).and_then(|b| b64decode(b.trim()));
        match got {
            Some(g) => ct_eq(&g, want.as_bytes()),
            None => false,
        }
    }

    /// The authenticated principal (the Basic user name) when Basic auth is configured and the credentials are valid.
    pub fn principal(&self, authorization: Option<&str>) -> Option<String> {
        if self.basic.is_none() || !self.basic_ok(authorization) {
            return None;
        }
        let raw = authorization?.strip_prefix("Basic ").and_then(|b| b64decode(b.trim()))?;
        let s = String::from_utf8(raw).ok()?;
        Some(s.split_once(':').map(|(u, _)| u.to_string()).unwrap_or(s))
    }

    pub fn basic_required(&self) -> bool {
        self.basic.is_some()
    }

    fn sign(&self, path: &str, exp: u64) -> Option<String> {
        self.uri_secret.as_ref().map(|k| hex(&hmac_sha256(k, format!("{}\n{}", path, exp).as_bytes())))
    }

    /// Packfile URI for `rest` = "packs/<enc repo>/<id>.pack", under the public base URL.
    pub fn public_uri(&self, rest: &str) -> Option<String> {
        let base = self.public_url.as_ref()?;
        match self.sign(rest, now() + self.uri_ttl) {
            Some(sig) => Some(format!("{}/_cache/{}?exp={}&sig={}", base, rest, now() + self.uri_ttl, sig)),
            None => Some(format!("{}/_cache/{}", base, rest)),
        }
    }

    /// Access decision for `/_cache/<rest>`: with a URI secret, a valid unexpired signature is required and
    /// sufficient; without one, the Basic rule applies.
    pub fn cache_ok(&self, rest: &str, query: &str, authorization: Option<&str>) -> bool {
        if self.uri_secret.is_none() {
            return self.basic_ok(authorization);
        }
        let q = |k: &str| query.split('&').find_map(|kv| kv.strip_prefix(&format!("{}=", k)).map(|v| v.to_string()));
        let (exp, sig) = match (q("exp").and_then(|e| e.parse::<u64>().ok()), q("sig")) {
            (Some(e), Some(s)) => (e, s),
            _ => return false,
        };
        if exp < now() {
            return false;
        }
        match self.sign(rest, exp) {
            Some(want) => ct_eq(want.as_bytes(), sig.as_bytes()),
            None => false,
        }
    }
}

fn now() -> u64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

/// Constant-time comparison (length is not secret).
pub fn ct_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

pub fn b64decode(s: &str) -> Option<Vec<u8>> {
    let val = |c: u8| -> Option<u32> {
        Some(match c {
            b'A'..=b'Z' => c - b'A',
            b'a'..=b'z' => c - b'a' + 26,
            b'0'..=b'9' => c - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            _ => return None,
        } as u32)
    };
    let s = s.trim_end_matches('=').as_bytes();
    let mut out = Vec::new();
    let (mut buf, mut bits) = (0u32, 0u32);
    for &c in s {
        buf = (buf << 6) | val(c)?;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((buf >> bits) as u8);
            buf &= (1 << bits) - 1;
        }
    }
    Some(out)
}

pub fn b64encode(b: &[u8]) -> String {
    const A: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut o = String::new();
    for c in b.chunks(3) {
        let n = (c[0] as u32) << 16 | (*c.get(1).unwrap_or(&0) as u32) << 8 | *c.get(2).unwrap_or(&0) as u32;
        for i in 0..4 {
            if i <= c.len() {
                o.push(A[(n >> (18 - 6 * i) & 63) as usize] as char);
            } else {
                o.push('=');
            }
        }
    }
    o
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn base64_roundtrip_and_rfc4648_vectors() {
        for (p, e) in [("", ""), ("f", "Zg=="), ("fo", "Zm8="), ("foo", "Zm9v"), ("foob", "Zm9vYg=="), ("fooba", "Zm9vYmE="), ("foobar", "Zm9vYmFy")] {
            assert_eq!(b64encode(p.as_bytes()), e);
            assert_eq!(b64decode(e).unwrap(), p.as_bytes());
        }
    }
    #[test]
    fn signed_uris_verify_and_reject_tampering_and_expiry() {
        let a = Auth { basic: Some("u:p".into()), public_url: Some("https://x".into()), uri_secret: Some(b"k".to_vec()), uri_ttl: 60 };
        let u = a.public_uri("packs/r/abc.pack").unwrap();
        let q = u.split_once('?').unwrap().1;
        assert!(a.cache_ok("packs/r/abc.pack", q, None));
        assert!(!a.cache_ok("packs/r/abd.pack", q, None), "signature must bind the path");
        assert!(!a.cache_ok("packs/r/abc.pack", &q.replace("sig=", "sig=0"), None));
        let exp = now() - 1;
        let stale = format!("exp={}&sig={}", exp, a.sign("packs/r/abc.pack", exp).unwrap());
        assert!(!a.cache_ok("packs/r/abc.pack", &stale, None), "expired URI accepted");
        assert!(a.basic_ok(Some(&format!("Basic {}", b64encode(b"u:p")))));
        assert!(!a.basic_ok(Some(&format!("Basic {}", b64encode(b"u:q")))));
        assert!(!a.basic_ok(None));
    }
}
