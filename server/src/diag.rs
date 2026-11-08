//! Read-only diagnostics for `pando status`, `pando explore fetch` and `pando explain` (docs/CLI-DESIGN.md).
//!
//! Every response says what it is: `recorded` (what a past operation did), `state` (committed metadata now) or
//! `hypothetical` (a fetch plan computed by the real planner, with nothing served or written). Nothing here commits
//! repository state; the only writes are operation records (`record_op`), made after a push was acknowledged.
//!
//! Access boundary:
//! - Clients get pack ids (content hashes, already public via packfile URIs), ref names and versions of the repository
//!   they asked about, and their own operations. Never metadata keys, LSNs, object-store endpoints or credentials,
//!   frontend addresses, or other repositories' names.
//! - Operation records are bound to an **unguessable token** returned to the pushing client (a `remote:` line), and,
//!   when the server authenticates, to the **principal** that pushed. `ops/<token>` needs the token (and, if the
//!   record has a principal, the same principal); `ops/last` needs an authenticated principal, or a loopback client
//!   of an unauthenticated (local) server. Knowing a commit or ref name never grants access to an operation.
//! - There is one deployment-wide credential today (`PANDO_AUTH`), so one principal and no per-repository
//!   authorisation. Diagnostics therefore expose nothing that the same credential could not already fetch.
use crate::proto;
use crate::repo::*;
use anyhow::{anyhow, Result};
use serde_json::{json, Value};
use std::cell::RefCell;
use std::sync::Arc;

pub const SCHEMA: &str = "pando.explain/1";

thread_local! {
    static PRINCIPAL: RefCell<Option<String>> = const { RefCell::new(None) };
}

/// The authenticated principal of the request being handled on this thread ("local" for a loopback client of an
/// unauthenticated server; None otherwise). Set by http.rs before a push is processed.
pub fn set_principal(p: Option<String>) {
    PRINCIPAL.with(|c| *c.borrow_mut() = p);
}
pub fn principal() -> Option<String> {
    PRINCIPAL.with(|c| c.borrow().clone())
}

/// 128 random bits, hex: the operation token handed to the pushing client.
pub fn new_token() -> String {
    // the operating system's CSPRNG, through rustls's crypto provider (ring), already a dependency
    let mut b = [0u8; 16];
    rustls::crypto::ring::default_provider().secure_random.fill(&mut b).expect("system random");
    crate::s3::hex(&b)
}

fn token_hash(token: &str) -> String {
    crate::s3::hex(&crate::s3::sha256(token.as_bytes()))
}

fn k_op(repo: &str, h: &str) -> String {
    format!("optok{SEP}{repo}{SEP}{h}")
}
fn k_opby_prefix(repo: &str, principal: &str) -> String {
    format!("opby{SEP}{repo}{SEP}{principal}{SEP}")
}

pub struct PushRecord {
    pub repo: String,
    pub token: String,
    pub principal: Option<String>,
    pub cmds: Vec<(String, String, String)>, // ref, old, new
    pub results: Vec<CmdResult>,
    pub pack: Option<(String, u64, u64, usize)>, // id, objects, bytes, external delta bases
    pub server_ms: f64,
}

/// Record a push for later explanation, after it was acknowledged, off the push path: two unique keys (by token hash;
/// by principal and time), so concurrent pushes never contend on them. Best effort: a crash right after the
/// acknowledgement can lose the record (never the push), and it becomes visible milliseconds after the push returns.
pub fn record_op(core: &Arc<Core>, r: PushRecord) {
    if crate::events::deterministic() {
        return; // the simulator replays pushes deterministically; no background writes there
    }
    let core = core.clone();
    std::thread::spawn(move || {
        let h = token_hash(&r.token);
        let now = crate::repo::now_ms();
        let rec = json!({
            "at_ms": now, "principal": r.principal,
            "refs": r.cmds.iter().map(|(name, old, new)| {
                let res = r.results.iter().find(|x| &x.name == name);
                json!({"ref": name, "old": old, "new": new, "ok": res.map(|x| x.ok).unwrap_or(false), "msg": res.map(|x| x.msg.clone()).unwrap_or_default()})
            }).collect::<Vec<_>>(),
            "pack": r.pack.as_ref().map(|(id, o, b, t)| json!({"id": id, "objects": o, "bytes": b, "external_bases": t})),
            "server_ms": r.server_ms,
        });
        let res = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| -> Result<()> {
            let mut tx = core.store.txn();
            tx.put(&k_op(&r.repo, &h), serde_json::to_vec(&rec)?);
            if let Some(p) = &r.principal {
                tx.put(&format!("{}{:016x}{SEP}{}", k_opby_prefix(&r.repo, p), now, h), h.as_bytes().to_vec());
            }
            tx.commit().map(|_| ())
        }));
        if !matches!(res, Ok(Ok(()))) {
            core.events.emit("op_record_failed", json!({"repo": r.repo}));
        }
    });
}

/// Who is asking: the authenticated principal, or "local" for a loopback client of an unauthenticated server.
pub struct Caller {
    pub principal: Option<String>,
}

fn envelope(kind: &str, label: &str, data: Value) -> Value {
    json!({"schema": SCHEMA, "kind": kind, "label": label, "data": data})
}

/// Dispatch `/<repo>.git/pando/v1/<sub>`. Returns (HTTP status, JSON body).
pub fn handle(core: &Arc<Core>, repo: &str, sub: &str, query: &str, caller: &Caller, uri_for: &dyn Fn(&str, &str) -> Option<String>) -> (u16, Value) {
    let _bg = crate::metrics::background(); // explanations never count as foreground (client-serving) work
    let r = match sub {
        "capabilities" => Ok((200, capabilities(caller))),
        "refs" => refs(core, repo),
        "plan" => plan(core, repo, query, uri_for),
        "history" => history(core, repo, query),
        "covers" => covers(core, repo, query),
        "ops/last" => match &caller.principal {
            Some(p) => last_op(core, repo, p),
            None => Ok((403, json!({"error": "operation history needs an authenticated principal (or a loopback client of a local server); use the token printed by the push: pando explain --op <token>"}))),
        },
        s if s.starts_with("ops/") => op_by_token(core, repo, &s[4..], caller),
        _ => Ok((404, json!({"error": "unknown diagnostic"}))),
    };
    r.unwrap_or_else(|e| (500, json!({"error": format!("{:#}", e)})))
}

fn capabilities(caller: &Caller) -> Value {
    envelope("capabilities", "state", json!({
        "server": "pando", "version": env!("CARGO_PKG_VERSION"),
        "features": ["immutable-push-packs", "ref-versions", "covers", "self-contained-copies", "packfile-uris", "operation-tokens"],
        "your_operations": if caller.principal.is_some() { "by your credentials and by token" } else { "by token only" },
    }))
}

fn refs(core: &Arc<Core>, repo: &str) -> Result<(u16, Value)> {
    if core.repo(repo).is_none() {
        return Ok((404, json!({"error": "no such repository"})));
    }
    let v: Vec<Value> = core.list_refs(repo).into_iter().map(|(r, p)| json!({"ref": r, "version": p.seq, "tip": p.tip})).collect();
    Ok((200, envelope("refs", "state", json!({"repo": repo, "refs": v}))))
}

fn qparam<'a>(query: &'a str, k: &str) -> Vec<&'a str> {
    query.split('&').filter_map(|kv| kv.strip_prefix(k).and_then(|r| r.strip_prefix('='))).collect()
}

/// The version of `oid` on `r`, if `oid` is a tip that ref had (tip index).
fn version_of(core: &Arc<Core>, repo: &str, oid: &str) -> Option<RvId> {
    core.store.get(&k_tip(repo, oid)).and_then(|v| serde_json::from_slice::<RvId>(&v).ok())
}

fn describe_pack(core: &Arc<Core>, repo: &str, id: &str) -> Value {
    match core.store.get(&k_pack(repo, id)).and_then(|v| serde_json::from_slice::<PackRec>(&v).ok()) {
        Some(p) => json!({
            "id": id, "level": p.level, "objects": p.objects, "bytes": p.bytes, "thin": p.thin,
            "cover": p.cover.as_ref().map(|c| json!({"ref": c.r, "level": c.level, "versions": format!("{}..{}", c.lo + 1, c.hi)})),
            "versions": p.rvs.iter().map(|v| format!("{}@{}", v.r, v.s)).collect::<Vec<_>>(),
        }),
        None => json!({"id": id}),
    }
}

/// All ref versions `start` transitively depends on, itself included (bounded walk over recorded dependencies).
fn closure(core: &Arc<Core>, rs: &RepoState, start: &RvId) -> std::collections::HashSet<(String, u64)> {
    let mut seen = std::collections::HashSet::new();
    let mut todo = vec![(start.r.clone(), start.s)];
    while let Some((r, v)) = todo.pop() {
        if v == 0 || seen.len() > 1_000_000 || !seen.insert((r.clone(), v)) {
            continue;
        }
        if let Ok(rv) = core.rv(rs, &r, v) {
            todo.extend(rv.deps.iter().map(|d| (d.r.clone(), d.s)));
        }
    }
    seen
}

/// A hypothetical fetch plan: the real planner and the real delivery decision, for a client holding `have` (zero or
/// more commit ids) and wanting `want` (a ref name or commit id). Nothing is served and no state is written.
fn plan(core: &Arc<Core>, repo: &str, query: &str, uri_for: &dyn Fn(&str, &str) -> Option<String>) -> Result<(u16, Value)> {
    let rs = match core.repo(repo) {
        Some(r) => r,
        None => return Ok((404, json!({"error": "no such repository"}))),
    };
    let want_s = qparam(query, "want").first().map(|s| s.to_string()).unwrap_or_else(|| "refs/heads/main".into());
    let refs = core.list_refs(repo);
    let (want_ref, want_tip) = match refs.iter().find(|(r, _)| *r == want_s || *r == format!("refs/heads/{}", want_s)) {
        Some((r, p)) => (Some(r.clone()), p.tip.clone()),
        None => (None, want_s.clone()),
    };
    let want = crate::pack::Oid::from_hex(&want_tip).map_err(|_| anyhow!("want: not a ref or commit id: {}", want_s))?;
    let haves: Vec<crate::pack::Oid> = qparam(query, "have").iter().filter_map(|h| crate::pack::Oid::from_hex(h).ok()).collect();
    let p = core.plan_fetch(repo, &[want], &haves)?;
    let deliveries = proto::delivery(core, repo, &p, true, true, true, uri_for)?;
    let packs: Vec<Value> = deliveries.iter().map(|d| {
        let mut v = describe_pack(core, repo, &d.pack);
        v["delivery"] = json!(if d.uri.is_some() { if d.served != d.pack { "uri (self-contained copy)" } else { "uri" } } else { "inline" });
        v
    }).collect();
    let to = version_of(core, repo, &want_tip);
    let from = haves.first().and_then(|h| version_of(core, repo, &h.hex()));
    // Push packs a fetch would need without covers: every ref version in the wanted version's dependency closure
    // (its own predecessors and, for a branch, the trunk versions it was built on), minus those the client's version
    // already depends on. Earlier this counted only the wanted ref's own versions, which understated branch fetches.
    let span = match (&from, &to) {
        (_, Some(t)) => {
            let want_set = closure(core, &rs, t);
            match (&from, haves.is_empty()) {
                (Some(f), _) => Some(want_set.difference(&closure(core, &rs, f)).count() as u64),
                (None, true) => Some(want_set.len() as u64),
                (None, false) => None,
            }
        }
        _ => None,
    };
    Ok((200, envelope("fetch-plan", "hypothetical", json!({
        "repo": repo, "want": {"ref": want_ref, "tip": want_tip, "version": to.as_ref().map(|v| v.s)},
        "have": {"tips": haves.iter().map(|h| h.hex()).collect::<Vec<_>>(), "version": from.as_ref().map(|v| v.s)},
        "versions_needed": span,
        "packs": packs,
        "summary": {"packs": deliveries.len(), "by_uri": deliveries.iter().filter(|d| d.uri.is_some()).count(),
                    "covers": p.covers_used, "plan_steps": p.steps, "metadata_reads": p.metadata_reads,
                    "pushes_without_covers": span},
    }))))
}

fn ref_param(core: &Arc<Core>, repo: &str, query: &str) -> Option<(String, u64)> {
    let want = qparam(query, "ref").first().map(|s| s.to_string()).unwrap_or_else(|| "refs/heads/main".into());
    core.list_refs(repo).into_iter().find(|(r, _)| *r == want || *r == format!("refs/heads/{}", want)).map(|(r, p)| (r, p.seq))
}

/// Ref versions of one ref, newest first (default the last 50): what each version points at, the pack it introduced,
/// and the versions it depends on.
fn history(core: &Arc<Core>, repo: &str, query: &str) -> Result<(u16, Value)> {
    let rs = match core.repo(repo) {
        Some(r) => r,
        None => return Ok((404, json!({"error": "no such repository"}))),
    };
    let (r, seq) = match ref_param(core, repo, query) {
        Some(x) => x,
        None => return Ok((404, json!({"error": "no such ref"}))),
    };
    let limit: u64 = qparam(query, "limit").first().and_then(|v| v.parse().ok()).unwrap_or(50);
    let mut out = Vec::new();
    for s in (1..=seq).rev().take(limit as usize) {
        if let Ok(v) = core.rv(&rs, &r, s) {
            out.push(json!({"version": s, "tip": v.tip, "pack": v.pack,
                            "depends_on": v.deps.iter().map(|d| format!("{}@{}", d.r, d.s)).collect::<Vec<_>>(),
                            "linked_to_previous": v.linked}));
        }
    }
    Ok((200, envelope("history", "state", json!({"repo": repo, "ref": r, "versions": seq, "history": out}))))
}

/// The cover hierarchy of one ref: every cover (level, versions lo+1..hi, its pack and size), plus the number of L0
/// push packs it stands for.
fn covers(core: &Arc<Core>, repo: &str, query: &str) -> Result<(u16, Value)> {
    if core.repo(repo).is_none() {
        return Ok((404, json!({"error": "no such repository"})));
    }
    let (r, seq) = match ref_param(core, repo, query) {
        Some(x) => x,
        None => return Ok((404, json!({"error": "no such ref"}))),
    };
    let prefix = format!("cover{SEP}{repo}{SEP}{r}{SEP}");
    let mut list: Vec<Value> = core.store.scan(&prefix).into_iter().filter_map(|(_, v)| serde_json::from_slice::<CoverRec>(&v).ok()).map(|c| {
        let p = core.store.get(&k_pack(repo, &c.pack)).and_then(|v| serde_json::from_slice::<PackRec>(&v).ok());
        json!({"level": c.level, "first_version": c.lo + 1, "last_version": c.hi, "pushes": c.hi - c.lo,
               "pack": c.pack, "objects": p.as_ref().map(|p| p.objects), "bytes": p.as_ref().map(|p| p.bytes)})
    }).collect();
    list.sort_by_key(|v| (std::cmp::Reverse(v["level"].as_u64().unwrap_or(0)), v["first_version"].as_u64().unwrap_or(0)));
    Ok((200, envelope("covers", "state", json!({"repo": repo, "ref": r, "versions": seq, "covers": list}))))
}

fn explain_op(core: &Arc<Core>, repo: &str, rec: Value) -> Value {
    let mut refs = Vec::new();
    for r in rec["refs"].as_array().cloned().unwrap_or_default() {
        let mut v = r.clone();
        if r["ok"].as_bool() == Some(true) {
            if let Some(id) = r["new"].as_str().and_then(|t| version_of(core, repo, t)) {
                v["version"] = json!(id.s);
                if let Some(rs) = core.repo(repo) {
                    if let Ok(rv) = core.rv(&rs, &id.r, id.s) {
                        v["depends_on"] = json!(rv.deps.iter().map(|d| format!("{}@{}", d.r, d.s)).collect::<Vec<_>>());
                        v["linked_to_previous"] = json!(rv.linked);
                    }
                }
            }
        }
        refs.push(v);
    }
    let pack = rec["pack"].clone();
    let copy = pack["id"].as_str().and_then(|id| {
        let rs = core.repo(repo)?;
        let thin = pack["external_bases"].as_u64().unwrap_or(0) > 0;
        Some(match (thin, core.twin(&rs, id)) {
            (false, _) => "not needed (self-contained)",
            (true, Some(_)) => "built",
            (true, None) => "pending (fetches send this pack inline until it is built)",
        })
    });
    envelope("push", "recorded", json!({
        "repo": repo, "at_ms": rec["at_ms"], "refs": refs, "pack": pack, "server_ms": rec["server_ms"],
        "now": {"label": "state", "self_contained_copy": copy},
    }))
}

fn op_by_token(core: &Arc<Core>, repo: &str, token: &str, caller: &Caller) -> Result<(u16, Value)> {
    let nf = (404, json!({"error": "no such operation"}));
    if token.len() != 32 || !token.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Ok(nf);
    }
    let rec: Value = match core.store.get(&k_op(repo, &token_hash(token))).and_then(|v| serde_json::from_slice(&v).ok()) {
        Some(r) => r,
        None => return Ok(nf),
    };
    // a record made under a principal is shown only to that principal (404, not 403: no oracle)
    if let Some(p) = rec["principal"].as_str() {
        if caller.principal.as_deref() != Some(p) {
            return Ok(nf);
        }
    }
    Ok((200, explain_op(core, repo, rec)))
}

fn last_op(core: &Arc<Core>, repo: &str, principal: &str) -> Result<(u16, Value)> {
    let latest = core.store.scan(&k_opby_prefix(repo, principal)).into_iter().map(|(_, v)| v).last();
    let h = match latest {
        Some(h) => String::from_utf8_lossy(&h).to_string(),
        None => return Ok((404, json!({"error": "no recorded push by you to this repository"}))),
    };
    match core.store.get(&k_op(repo, &h)).and_then(|v| serde_json::from_slice::<Value>(&v).ok()) {
        Some(rec) => Ok((200, explain_op(core, repo, rec))),
        None => Ok((404, json!({"error": "no recorded push by you to this repository"}))),
    }
}
