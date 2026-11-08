//! Ref versions, dependencies, push commit protocol and fetch planning.
//! The precise model is documented in MODEL.md; event names used here are the
//! trace vocabulary listed there.
use crate::blob::Blobs;
use crate::events::Events;
use crate::meta::{Conflict, Store};
use crate::pack::*;
use anyhow::{anyhow, bail, Result};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex, RwLock};

pub const SEP: char = '\x01';

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct RvId {
    pub r: String,
    pub s: u64,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct RefPtr {
    pub seq: u64,
    /// hex tip; 40 zeros when the ref is deleted
    pub tip: String,
    pub ls: u64,
    #[serde(default)]
    pub peeled: Option<String>,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct RefVersion {
    pub r: String,
    pub s: u64,
    pub tip: String,
    pub pack: Option<String>,
    pub deps: Vec<RvId>,
    /// depends on r@(s-1) and r@(s-1)'s tip is an ancestor of this tip
    pub linked: bool,
    /// smallest k such that r@k..r@s are all in this version's closure via links
    pub ls: u64,
    #[serde(default)]
    pub peeled: Option<String>,
    pub push: String,
    pub ts: u64,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct RvInfo {
    pub r: String,
    pub s: u64,
    pub ls: u64,
    pub tip: String,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct PackRec {
    pub id: String,
    pub level: u8,
    pub objects: u64,
    pub bytes: u64,
    pub thin: bool,
    pub ref_delta: bool,
    /// ref versions introduced together with this pack (L0) or covered (cover)
    pub rvs: Vec<RvInfo>,
    #[serde(default)]
    pub cover: Option<CoverRec>,
    /// set on a "thick twin": a self-contained copy of the named thin L0 pack
    #[serde(default)]
    pub twin_of: Option<String>,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct CoverRec {
    pub r: String,
    pub level: u8,
    pub lo: u64, // exclusive
    pub hi: u64, // inclusive
    pub pack: String,
    pub deps: Vec<RvId>,
    pub tips: Vec<String>,
    /// all versions lo+1..hi are linked to their predecessor
    pub linked: bool,
    /// all versions lo+2..hi are linked to their predecessor: the cover contains no discarded history even if
    /// version lo+1 itself is a creation or force-push. `None` (covers written before this field): unknown,
    /// treated as unusable when `linked` is false.
    #[serde(default)]
    pub linked_inner: Option<bool>,
    #[serde(default)]
    pub objects: u64,
    #[serde(default)]
    pub bytes: u64,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct RepoRec {
    pub name: String,
    pub trunk: String,
    pub created: u64,
}

/// A non-atomic push that failed after committing some of its commands: `done` holds the results of the committed
/// (and any already rejected) commands; the others failed with `msg`.
#[derive(Debug)]
pub struct PartialPush {
    pub done: Vec<CmdResult>,
    pub msg: String,
}
impl std::fmt::Display for PartialPush {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.msg)
    }
}
impl std::error::Error for PartialPush {}

fn is_outcome_unknown(e: &anyhow::Error) -> bool {
    e.downcast_ref::<crate::meta::Unavailable>().map(|u| u.0.starts_with("commit outcome unknown")).unwrap_or(false)
}

pub fn k_repo(repo: &str) -> String {
    format!("repo{SEP}{repo}")
}
pub fn k_ref(repo: &str, r: &str) -> String {
    format!("ref{SEP}{repo}{SEP}{r}")
}
pub fn k_ref_prefix(repo: &str) -> String {
    format!("ref{SEP}{repo}{SEP}")
}
pub fn k_rv(repo: &str, r: &str, s: u64) -> String {
    format!("rv{SEP}{repo}{SEP}{r}{SEP}{s:016x}")
}
pub fn k_tip(repo: &str, oid: &str) -> String {
    format!("tip{SEP}{repo}{SEP}{oid}")
}
thread_local! {
    /// commits visited by `links_to` during the current push (push_timing instrumentation)
    static LINK_VISITS: std::cell::Cell<u64> = const { std::cell::Cell::new(0) };
}

pub fn k_pack(repo: &str, id: &str) -> String {
    format!("pack{SEP}{repo}{SEP}{id}")
}
pub fn k_pack_prefix(repo: &str) -> String {
    format!("pack{SEP}{repo}{SEP}")
}
pub fn k_twin(repo: &str, pack: &str) -> String {
    format!("twin{SEP}{repo}{SEP}{pack}")
}
pub fn k_cover(repo: &str, r: &str, level: u8, hi: u64) -> String {
    format!("cover{SEP}{repo}{SEP}{r}{SEP}{level}{SEP}{hi:016x}")
}

pub const ZERO_HEX: &str = "0000000000000000000000000000000000000000";

pub fn now_ms() -> u64 {
    if crate::events::deterministic() {
        return 0;
    }
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_millis() as u64
}

struct Slot {
    id: String,
    rvs: Vec<RvInfo>,
}

#[derive(Default)]
struct ObjIndex {
    loaded: bool,
    /// high-water mark: every pack record committed at an LSN <= hwm is registered (docs/INCREMENTAL-INDEX.md)
    hwm: u64,
    slots: Vec<Slot>,
    slot_of: HashMap<String, u32>,
    map: HashMap<Oid, u32>,
}

pub struct RepoState {
    pub name: String,
    pub trunk: String,
    idx: RwLock<ObjIndex>,
    rv_cache: Mutex<HashMap<(String, u64), Arc<RefVersion>>>,
    cover_cache: Mutex<HashMap<String, Option<Arc<CoverRec>>>>,
    twin_cache: Mutex<HashMap<String, String>>,
}

pub struct Core {
    pub store: Arc<Store>,
    pub blobs: Arc<Blobs>,
    pub events: Arc<Events>,
    repos: Mutex<HashMap<String, Arc<RepoState>>>,
    pub cache_hits: std::sync::atomic::AtomicU64,
    pub packs_stored: std::sync::atomic::AtomicU64,
    pub packs_derived: std::sync::atomic::AtomicU64,
}

#[derive(Clone, Debug)]
pub struct Command {
    pub old: Oid,
    pub new: Oid,
    pub name: String,
}

#[derive(Clone, Debug)]
pub struct CmdResult {
    pub name: String,
    pub ok: bool,
    pub msg: String,
}

pub struct PushInput<'a> {
    pub repo: &'a str,
    pub cmds: Vec<Command>,
    pub pack: Option<(Arc<PackIndex>, u32)>,
    pub atomic: bool,
    pub push_id: String,
}

pub fn valid_refname(n: &str) -> bool {
    if !n.starts_with("refs/") || n.ends_with('/') || n.ends_with(".lock") || n.contains("..") || n.contains("//") || n.contains("@{") {
        return false;
    }
    !n.bytes().any(|c| c < 0x20 || c == 0x7f || b" ~^:?*[\\".contains(&c))
}

impl Core {
    pub fn new(store: Arc<Store>, blobs: Arc<Blobs>, events: Arc<Events>) -> Core {
        Core {
            store,
            blobs,
            events,
            repos: Mutex::new(HashMap::new()),
            cache_hits: Default::default(),
            packs_stored: Default::default(),
            packs_derived: Default::default(),
        }
    }

    pub fn create_repo(&self, name: &str) -> Result<bool> {
        loop {
            let mut tx = self.store.txn();
            if tx.get(&k_repo(name)).is_some() {
                return Ok(false);
            }
            let rec = RepoRec { name: name.to_string(), trunk: "refs/heads/main".into(), created: now_ms() };
            tx.put(&k_repo(name), serde_json::to_vec(&rec)?);
            match tx.commit() {
                Ok(_) => {
                    self.events.emit("repo_created", json!({"repo": name}));
                    return Ok(true);
                }
                Err(e) if e.is::<Conflict>() => continue,
                Err(e) => return Err(e),
            }
        }
    }

    pub fn repo(&self, name: &str) -> Option<Arc<RepoState>> {
        if let Some(r) = self.repos.lock().unwrap().get(name) {
            return Some(r.clone());
        }
        let rec: RepoRec = serde_json::from_slice(&self.store.get(&k_repo(name))?).ok()?;
        let st = Arc::new(RepoState {
            name: name.to_string(),
            trunk: rec.trunk,
            idx: RwLock::new(ObjIndex::default()),
            rv_cache: Mutex::new(HashMap::new()),
            cover_cache: Mutex::new(HashMap::new()),
            twin_cache: Mutex::new(HashMap::new()),
        });
        let mut m = self.repos.lock().unwrap();
        Some(m.entry(name.to_string()).or_insert(st).clone())
    }

    pub fn list_refs(&self, repo: &str) -> Vec<(String, RefPtr)> {
        let p = k_ref_prefix(repo);
        self.store
            .scan(&p)
            .into_iter()
            .filter_map(|(k, v)| {
                let r = k[p.len()..].to_string();
                let ptr: RefPtr = serde_json::from_slice(&v).ok()?;
                if ptr.tip == ZERO_HEX {
                    None
                } else {
                    Some((r, ptr))
                }
            })
            .collect()
    }

    pub fn get_ref(&self, repo: &str, r: &str) -> Option<RefPtr> {
        let v = self.store.get(&k_ref(repo, r))?;
        serde_json::from_slice(&v).ok()
    }

    /// Immutable ref-version record (cached; immutable so never stale).
    pub fn rv(&self, rs: &RepoState, r: &str, s: u64) -> Result<Arc<RefVersion>> {
        if let Some(v) = rs.rv_cache.lock().unwrap().get(&(r.to_string(), s)) {
            self.cache_hits.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            crate::metrics::cache_hit();
            return Ok(v.clone());
        }
        let v = self.store.get(&k_rv(&rs.name, r, s)).ok_or_else(|| anyhow!("missing ref version {}@{}", r, s))?;
        let rv: Arc<RefVersion> = Arc::new(serde_json::from_slice(&v)?);
        let mut c = rs.rv_cache.lock().unwrap();
        if c.len() > 200_000 {
            c.clear();
        }
        c.insert((r.to_string(), s), rv.clone());
        Ok(rv)
    }

    pub fn cover(&self, rs: &RepoState, r: &str, level: u8, hi: u64) -> Option<Arc<CoverRec>> {
        let k = k_cover(&rs.name, r, level, hi);
        if let Some(v) = rs.cover_cache.lock().unwrap().get(&k) {
            if v.is_some() {
                self.cache_hits.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            crate::metrics::cache_hit();
                return v.clone();
            }
        }
        let c: Option<Arc<CoverRec>> = self.store.get(&k).and_then(|v| serde_json::from_slice(&v).ok()).map(Arc::new);
        // only positive results are cached (covers appear over time)
        if c.is_some() {
            rs.cover_cache.lock().unwrap().insert(k, c.clone());
        }
        c
    }

    /// Thick twin of a thin L0 pack, if built (write-once; positive results cached).
    pub fn twin(&self, rs: &RepoState, pack: &str) -> Option<String> {
        if let Some(t) = rs.twin_cache.lock().unwrap().get(pack) {
            self.cache_hits.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            crate::metrics::cache_hit();
            return Some(t.clone());
        }
        let v = self.store.get(&k_twin(&rs.name, pack))?;
        let t = String::from_utf8(v.to_vec()).ok()?;
        rs.twin_cache.lock().unwrap().insert(pack.to_string(), t.clone());
        Some(t)
    }

    pub fn tip_rv(&self, rs: &RepoState, oid: &str) -> Option<RvId> {
        let v = self.store.get(&k_tip(&rs.name, oid))?;
        serde_json::from_slice(&v).ok()
    }

    fn ensure_index(&self, rs: &RepoState, force_refresh: bool) -> Result<()> {
        let (loaded, hwm) = {
            let i = rs.idx.read().unwrap();
            (i.loaded, i.hwm)
        };
        if loaded && !force_refresh {
            return Ok(());
        }
        let t0 = std::time::Instant::now();
        let (recs, new_hwm, incremental): (Vec<(u64, Arc<Vec<u8>>)>, u64, bool) = if loaded {
            // incremental: only pack records committed since the high-water mark
            let (st, ch) = self.store.changes_since(&k_pack_prefix(&rs.name), hwm);
            (ch.into_iter().map(|(lsn, _, v)| (lsn, v)).collect(), st, true)
        } else {
            // first build: capture the stable LSN BEFORE the scan, so nothing at or below it can be missed
            let st = self.store.stable_lsn();
            (self.store.scan(&k_pack_prefix(&rs.name)).into_iter().map(|(_, v)| (0, v)).collect(), st, false)
        };
        let nkeys = recs.len();
        let mut add = Vec::new();
        // trace (docs/TRACING.md): which pack records an incremental refresh delivered, with their commit LSNs
        let mut discovered: Vec<serde_json::Value> = Vec::new();
        for (lsn, v) in recs {
            if let Ok(p) = serde_json::from_slice::<PackRec>(&v) {
                if incremental && discovered.len() < 64 {
                    discovered.push(json!({"pack": p.id, "lsn": lsn, "level": p.level, "copy_of": p.twin_of}));
                }
                if p.level == 0 && p.twin_of.is_none() {
                    add.push(p);
                }
            }
        }
        let (npacks, mut nobj) = (add.len(), 0usize);
        for p in add {
            let pidx = self.blobs.index(&rs.name, &p.id)?;
            nobj += pidx.entries.len();
            // idempotent: an already-known pack only gains any newly appended ref versions
            self.register_pack(rs, &pidx, p.rvs);
        }
        {
            let mut i = rs.idx.write().unwrap();
            i.loaded = true;
            i.hwm = i.hwm.max(new_hwm);
        }
        self.events.emit("object_index_built", json!({"repo": rs.name, "packs": npacks, "objects": nobj, "refresh": force_refresh,
                                                    "incremental": incremental, "keys_read": nkeys, "hwm_from": hwm, "hwm": new_hwm,
                                                    "discovered": discovered,
                                                    "ms": t0.elapsed().as_secs_f64() * 1e3}));
        Ok(())
    }

    fn register_pack(&self, rs: &RepoState, pidx: &PackIndex, rvs: Vec<RvInfo>) {
        let mut i = rs.idx.write().unwrap();
        if let Some(&s) = i.slot_of.get(&pidx.id) {
            let slot = &mut i.slots[s as usize];
            for r in rvs {
                if !slot.rvs.iter().any(|x| x.r == r.r && x.s == r.s) {
                    slot.rvs.push(r);
                }
            }
            return;
        }
        let n = i.slots.len() as u32;
        i.slots.push(Slot { id: pidx.id.clone(), rvs });
        i.slot_of.insert(pidx.id.clone(), n);
        for e in &pidx.entries {
            i.map.entry(e.oid).or_insert(n);
        }
    }

    /// All ref versions that introduced the (first) committed L0 pack containing `oid`.
    pub fn locate_rvs(&self, rs: &RepoState, oid: &Oid) -> Vec<RvInfo> {
        crate::metrics::index_lookup();
        let i = rs.idx.read().unwrap();
        i.map.get(oid).map(|s| i.slots[*s as usize].rvs.clone()).unwrap_or_default()
    }

    /// Locate a committed object: (pack id, entry index, introducing RV).
    pub fn locate(&self, rs: &RepoState, oid: &Oid) -> Result<Option<(String, usize, RvInfo)>> {
        self.ensure_index(rs, false)?;
        for attempt in 0..2 {
            crate::metrics::index_lookup();
            let hit = {
                let i = rs.idx.read().unwrap();
                i.map.get(oid).map(|s| {
                    let sl = &i.slots[*s as usize];
                    (sl.id.clone(), sl.rvs[0].clone())
                })
            };
            if let Some((id, rv)) = hit {
                let pidx = self.blobs.index(&rs.name, &id)?;
                let ei = pidx.find(oid).ok_or_else(|| anyhow!("index inconsistency"))?;
                return Ok(Some((id, ei, rv)));
            }
            if attempt == 0 {
                // another server/thread may have committed it: refresh from metadata
                self.ensure_index(rs, true)?;
            }
        }
        Ok(None)
    }

    pub fn read_obj(&self, rs: &RepoState, oid: &Oid) -> Result<Option<(u8, Arc<Vec<u8>>, String)>> {
        match self.locate(rs, oid)? {
            Some((id, ei, _)) => {
                let (t, d) = self.blobs.read_object(&rs.name, &id, ei)?;
                Ok(Some((t, d, id)))
            }
            None => Ok(None),
        }
    }

    fn obj_type(&self, rs: &RepoState, pack: Option<&PackIndex>, oid: &Oid) -> Result<Option<u8>> {
        if let Some(p) = pack {
            if let Some(i) = p.find(oid) {
                return Ok(Some(p.entries[i].otype));
            }
        }
        match self.locate(rs, oid)? {
            Some((id, ei, _)) => Ok(Some(self.blobs.index(&rs.name, &id)?.entries[ei].otype)),
            None => Ok(None),
        }
    }

    /// Peel a (possibly annotated tag) object to its non-tag target.
    fn peel(&self, rs: &RepoState, pack: Option<&PackIndex>, oid: &Oid) -> Result<Option<Oid>> {
        let mut cur = *oid;
        let mut peeled = None;
        for _ in 0..32 {
            let t = self.obj_type(rs, pack, &cur)?;
            if t != Some(T_TAG) {
                return Ok(peeled);
            }
            let mut target = None;
            if let Some(p) = pack {
                if let Some((_, tg, _)) = p.tags.iter().find(|(a, _, _)| *a == cur) {
                    target = Some(*tg);
                }
            }
            if target.is_none() {
                if let Some((_, d, _)) = self.read_obj(rs, &cur)? {
                    let mut refs = Vec::new();
                    let mut tt = None;
                    parse_refs(T_TAG, &d, &mut refs, &mut tt)?;
                    target = tt.map(|x| x.0);
                }
            }
            match target {
                Some(t) => {
                    cur = t;
                    peeled = Some(t);
                }
                None => return Ok(peeled),
            }
        }
        Ok(peeled)
    }

    /// Is `cur` a parent (outside the pack) of a commit reachable from `new` through
    /// commits of the pack? (Per ref: in a multi-ref pack a boundary commit may belong
    /// to another ref's history — found by the exhaustive model checker.)
    fn links_to(&self, repo: &str, pidx: &PackIndex, new: &Oid, cur: &Oid) -> Result<bool> {
        let mut stack = vec![*new];
        let mut seen: HashSet<Oid> = HashSet::new();
        while let Some(o) = stack.pop() {
            if !seen.insert(o) {
                continue;
            }
            LINK_VISITS.with(|c| c.set(c.get() + 1));
            let ei = match pidx.find(&o) {
                Some(i) => i,
                None => continue,
            };
            if pidx.entries[ei].otype != T_COMMIT {
                continue;
            }
            let (_, d) = self.blobs.read_object(repo, &pidx.id, ei)?;
            let mut refs = Vec::new();
            let mut tt = None;
            parse_refs(T_COMMIT, &d, &mut refs, &mut tt)?;
            for p in refs.iter().skip(1) {
                if p == cur {
                    return Ok(true);
                }
                if pidx.find(p).is_some() {
                    stack.push(*p);
                }
            }
        }
        Ok(false)
    }

    /// The push commit protocol (MODEL.md §3).
    pub fn push(&self, inp: PushInput) -> Result<Vec<CmdResult>> {
        let t_start = std::time::Instant::now();
        let mut tm: Vec<(&str, f64)> = Vec::new();
        let mut lap = {
            let mut last = t_start;
            move |name: &'static str, tm: &mut Vec<(&'static str, f64)>| {
                let now = std::time::Instant::now();
                tm.push((name, (now - last).as_secs_f64() * 1e3));
                last = now;
            }
        };
        LINK_VISITS.with(|c| c.set(0));
        let (push_id, repo_name) = (inp.push_id.clone(), inp.repo.to_string());
        // Test-only mutant (verify/dist D2 validation): a repository-wide lock held across the push, which the D2
        // test must detect. Never set in normal operation. Per process, so it serialises one frontend's pushes.
        let _mutant = if std::env::var("PANDO_TEST_MUTANT_GLOBAL_LOCK").map(|v| v == "1").unwrap_or(false) {
            static G: std::sync::Mutex<()> = std::sync::Mutex::new(());
            Some(G.lock().unwrap_or_else(|e| e.into_inner()))
        } else {
            None
        };
        let r = self.push_inner(inp, &mut tm, &mut lap);
        let mut f = serde_json::Map::new();
        f.insert("repo".into(), json!(repo_name));
        f.insert("push".into(), json!(push_id));
        for (k, v) in &tm {
            // a retried transaction laps the same phase again: sum them
            let key = format!("{}_ms", k);
            let prev = f.get(&key).and_then(|x| x.as_f64()).unwrap_or(0.0);
            f.insert(key, json!(prev + v));
        }
        f.insert("link_commits_visited".into(), json!(LINK_VISITS.with(|c| c.get())));
        f.insert("total_ms".into(), json!(t_start.elapsed().as_secs_f64() * 1e3));
        self.events.emit("push_timing", serde_json::Value::Object(f));
        r
    }

    /// Were all of `rvs` written by push `push`? Reads may panic while the metadata service is unreachable
    /// (Remote::get); that counts as "not established".
    fn applied_by(&self, repo: &str, rvs: &[RefVersion], push: &str) -> bool {
        std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            rvs.iter().all(|rv| {
                self.store
                    .get(&k_rv(repo, &rv.r, rv.s))
                    .and_then(|v| serde_json::from_slice::<RefVersion>(&v).ok())
                    .map(|x| x.push == push && x.tip == rv.tip)
                    .unwrap_or(false)
            })
        }))
        .unwrap_or(false)
    }

    fn push_inner(&self, inp: PushInput, tm: &mut Vec<(&'static str, f64)>, lap: &mut dyn FnMut(&'static str, &mut Vec<(&'static str, f64)>)) -> Result<Vec<CmdResult>> {
        let rs = self.repo(inp.repo).ok_or_else(|| anyhow!("no such repository"))?;
        let pidx = inp.pack.as_ref().map(|(i, _)| i.clone());
        let pack_nonempty = pidx.as_ref().map(|p| !p.entries.is_empty()).unwrap_or(false);

        // 1. exact prerequisites -> dependency ref versions (no metadata reads)
        let mut prereqs: Vec<Oid> = pidx.as_ref().map(|p| p.prereqs.clone()).unwrap_or_default();
        for c in &inp.cmds {
            if !c.new.is_zero() && pidx.as_ref().and_then(|p| p.find(&c.new)).is_none() {
                prereqs.push(c.new);
            }
        }
        prereqs.sort();
        prereqs.dedup();
        let mut missing = Vec::new();
        let mut dep_infos: Vec<RvInfo> = Vec::new();
        let mut boundary: HashSet<String> = HashSet::new();
        for p in &prereqs {
            match self.locate(&rs, p)? {
                Some((id, ei, rv)) => {
                    let t = self.blobs.index(&rs.name, &id)?.entries[ei].otype;
                    if t == T_COMMIT {
                        boundary.insert(p.hex());
                    }
                    // A pack that introduced several refs at once: depend on all of them, so a
                    // client holding only one of those refs still receives the pack (MODEL A1).
                    let all = self.locate_rvs(&rs, p);
                    if all.len() > 1 {
                        dep_infos.extend(all);
                    } else {
                        dep_infos.push(rv);
                    }
                }
                None => missing.push(*p),
            }
        }
        if !missing.is_empty() {
            self.events.emit(
                "push_rejected",
                json!({"repo": inp.repo, "push": inp.push_id, "reason": "missing_prereqs", "missing": missing.iter().take(20).map(|o| o.hex()).collect::<Vec<_>>()}),
            );
            return Ok(inp
                .cmds
                .iter()
                .map(|c| CmdResult { name: c.name.clone(), ok: false, msg: format!("missing necessary objects ({})", missing[0]) })
                .collect());
        }
        // subsume deps: (r, j) is in closure of (r, k) when ls(k) <= j <= k
        let mut best: HashMap<String, Vec<RvInfo>> = HashMap::new();
        for d in dep_infos {
            let v = best.entry(d.r.clone()).or_default();
            if v.iter().any(|x| x.ls <= d.s && d.s <= x.s) {
                continue;
            }
            v.retain(|x| !(d.ls <= x.s && x.s <= d.s));
            v.push(d);
        }
        let mut deps: Vec<RvId> = best.into_values().flatten().map(|d| RvId { r: d.r, s: d.s }).collect();
        deps.sort();
        lap("prereqs", tm);

        // 2. durable blob first (already installed by caller); pack record written in txn
        let mut results = Vec::new();
        let new_rvinfos: Vec<RvInfo> = Vec::new();
        let _ = new_rvinfos;

        // peeled values for tags
        let mut peeled: HashMap<String, Option<String>> = HashMap::new();
        for c in &inp.cmds {
            if !c.new.is_zero() && c.name.starts_with("refs/tags/") {
                let p = self.peel(&rs, pidx.as_deref(), &c.new)?;
                peeled.insert(c.name.clone(), p.map(|o| o.hex()));
            }
        }

        // Branches must point at commits, as in stock Git (receive-pack/refs.c refuse a non-commit on
        // refs/heads/*). CG7 audit B D-12.
        let mut non_commit_branch: HashMap<String, String> = HashMap::new();
        for c in &inp.cmds {
            if !c.new.is_zero() && c.name.starts_with("refs/heads/") && self.obj_type(&rs, pidx.as_deref(), &c.new)? != Some(T_COMMIT) {
                non_commit_branch.insert(
                    c.name.clone(),
                    format!("failed to update ref: trying to write non-commit object {} to branch '{}'", c.new.hex(), c.name),
                );
            }
        }

        lap("ref_checks", tm);
        let groups: Vec<Vec<&Command>> = if inp.atomic { vec![inp.cmds.iter().collect()] } else { inp.cmds.iter().map(|c| vec![c]).collect() };
        let mut introduced: Vec<RvInfo> = Vec::new();
        for g in groups {
            // Non-atomic pushes commit group by group. An error in a later group must not be reported for groups
            // already committed (audit F4: every command was reported "server error", including committed refs).
            // also under catch_unwind: a metadata read panics when the service stays unreachable (Remote::get), and
            // that must not discard the results of groups already committed (review V3)
            let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| -> Result<()> {
            let mut attempt = 0;
            loop {
                attempt += 1;
                let mut tx = self.store.txn();
                let mut fail: Option<(String, String)> = None;
                let mut rvs_written: Vec<RefVersion> = Vec::new();
                let mut infos = Vec::new();
                for c in &g {
                    if let Some(msg) = non_commit_branch.get(&c.name) {
                        fail = Some((c.name.clone(), msg.clone()));
                        break;
                    }
                    let cur: Option<RefPtr> = tx.get(&k_ref(inp.repo, &c.name)).and_then(|v| serde_json::from_slice(&v).ok());
                    let cur_tip = cur.as_ref().map(|p| p.tip.clone()).unwrap_or_else(|| ZERO_HEX.to_string());
                    if cur_tip != c.old.hex() {
                        let msg = if c.old.is_zero() {
                            format!("cannot lock ref '{}': reference already exists", c.name)
                        } else if cur_tip == ZERO_HEX {
                            format!("cannot lock ref '{}': unable to resolve reference", c.name)
                        } else {
                            format!("cannot lock ref '{}': is at {} but expected {}", c.name, cur_tip, c.old.hex())
                        };
                        fail = Some((c.name.clone(), msg));
                        break;
                    }
                    if cur_tip == ZERO_HEX && !c.new.is_zero() {
                        // directory/file conflicts, as git: refs/heads/a vs refs/heads/a/b.
                        // Deliberately NOT part of the transaction's read set: otherwise every
                        // branch creation would lock the stripe of a shared parent key (e.g.
                        // refs/heads/feature) through the fsync. Residual race: concurrent
                        // creation of a and a/b can both succeed (DECISIONS D15).
                        let mut conflict = None;
                        let parts: Vec<usize> = c.name.match_indices('/').map(|(i, _)| i).collect();
                        for &i in parts.iter().skip(2) {
                            let parent = &c.name[..i];
                            let p: Option<RefPtr> = self.store.get(&k_ref(inp.repo, parent)).and_then(|v| serde_json::from_slice(&v).ok());
                            if p.map(|p| p.tip != ZERO_HEX).unwrap_or(false) {
                                conflict = Some(parent.to_string());
                                break;
                            }
                        }
                        if conflict.is_none() {
                            let pre = k_ref(inp.repo, &format!("{}/", c.name));
                            if let Some((k, _)) = self.store.scan(&pre).into_iter().find(|(_, v)| serde_json::from_slice::<RefPtr>(v).map(|p| p.tip != ZERO_HEX).unwrap_or(false)) {
                                conflict = Some(k[k_ref_prefix(inp.repo).len()..].to_string());
                            }
                        }
                        if let Some(other) = conflict {
                            fail = Some((c.name.clone(), format!("cannot lock ref '{}': '{}' exists; cannot create '{}'", c.name, other, c.name)));
                            break;
                        }
                    }
                    let seq = cur.as_ref().map(|p| p.seq).unwrap_or(0) + 1;
                    let rv = if c.new.is_zero() {
                        RefVersion {
                            r: c.name.clone(),
                            s: seq,
                            tip: ZERO_HEX.into(),
                            pack: None,
                            deps: vec![],
                            linked: false,
                            ls: seq,
                            peeled: None,
                            push: inp.push_id.clone(),
                            ts: now_ms(),
                        }
                    } else {
                        let linked = cur_tip != ZERO_HEX
                            && pack_nonempty
                            && boundary.contains(&cur_tip)
                            && self.links_to(inp.repo, pidx.as_ref().unwrap(), &c.new, &Oid::from_hex(&cur_tip)?)?;
                        let mut d = deps.clone();
                        if linked {
                            let prev = cur.as_ref().unwrap();
                            let pid = RvId { r: c.name.clone(), s: prev.seq };
                            // drop older versions of this ref subsumed by the link
                            d.retain(|x| !(x.r == c.name && x.s >= prev.ls && x.s <= prev.seq));
                            d.push(pid);
                            d.sort();
                        }
                        RefVersion {
                            r: c.name.clone(),
                            s: seq,
                            tip: c.new.hex(),
                            pack: if pack_nonempty { pidx.as_ref().map(|p| p.id.clone()) } else { None },
                            deps: d,
                            linked,
                            ls: if linked { cur.as_ref().unwrap().ls } else { seq },
                            peeled: peeled.get(&c.name).cloned().flatten(),
                            push: inp.push_id.clone(),
                            ts: now_ms(),
                        }
                    };
                    let ptr = RefPtr { seq, tip: rv.tip.clone(), ls: rv.ls, peeled: rv.peeled.clone() };
                    tx.put(&k_rv(inp.repo, &c.name, seq), serde_json::to_vec(&rv)?);
                    tx.put(&k_ref(inp.repo, &c.name), serde_json::to_vec(&ptr)?);
                    if !c.new.is_zero() {
                        let tk = k_tip(inp.repo, &rv.tip);
                        // first writer wins: the earliest version with this tip has closure == reach(tip)
                        if tx.get(&tk).is_none() {
                            tx.put(&tk, serde_json::to_vec(&RvId { r: c.name.clone(), s: seq })?);
                        }
                        infos.push(RvInfo { r: c.name.clone(), s: seq, ls: rv.ls, tip: rv.tip.clone() });
                    }
                    rvs_written.push(rv);
                }
                if let Some((name, msg)) = fail {
                    self.events.emit("ref_cas_failed", json!({"repo": inp.repo, "push": inp.push_id, "ref": name, "msg": msg}));
                    for c in &g {
                        if c.name == name {
                            results.push(CmdResult { name: c.name.clone(), ok: false, msg: msg.clone() });
                        } else {
                            results.push(CmdResult { name: c.name.clone(), ok: false, msg: "atomic push failed".into() });
                        }
                    }
                    break;
                }
                lap("txn_build_and_links", tm);
                let mut pack_new = false;
                if pack_nonempty && !infos.is_empty() {
                    let p = pidx.as_ref().unwrap();
                    let pk = k_pack(inp.repo, &p.id);
                    let existing: Option<PackRec> = tx.get(&pk).and_then(|v| serde_json::from_slice(&v).ok());
                    if let Some(mut rec) = existing {
                        // append-only: record every ref version introduced with this pack
                        let before = rec.rvs.len();
                        for i in &infos {
                            if !rec.rvs.iter().any(|x| x.r == i.r && x.s == i.s) {
                                rec.rvs.push(i.clone());
                            }
                        }
                        if rec.rvs.len() != before && rec.level == 0 && rec.twin_of.is_none() {
                            tx.put(&pk, serde_json::to_vec(&rec)?);
                        }
                    } else {
                        pack_new = true;
                        let rec = PackRec {
                            id: p.id.clone(),
                            level: 0,
                            objects: p.entries.len() as u64,
                            bytes: p.pack_size,
                            thin: p.is_thin(),
                            ref_delta: p.has_ref_delta(),
                            rvs: infos.iter().cloned().chain(introduced.iter().cloned()).collect(),
                            cover: None,
                            twin_of: None,
                        };
                        tx.put(&pk, serde_json::to_vec(&rec)?);
                    }
                }
                let committed = match tx.commit() {
                    // A lost reply (audit F5): the commit may or may not have been applied. Version keys are written
                    // once, by the transaction that won that version, and record its push id; so if every version
                    // this transaction wrote now carries this push's id, it was applied and the update is reported as
                    // done. Otherwise "outcome unknown" stands (a commit still in flight could yet apply).
                    Err(e) if is_outcome_unknown(&e) && !rvs_written.is_empty() => {
                        let applied = self.applied_by(inp.repo, &rvs_written, &inp.push_id);
                        self.events.emit("commit_outcome_resolved", json!({"repo": inp.repo, "push": inp.push_id, "applied": applied}));
                        if applied { Ok(0) } else { Err(e) }
                    }
                    r => r,
                };
                lap("txn_commit", tm);
                match committed {
                    Ok(lsn) => {
                        for rv in &rvs_written {
                            self.events.emit(
                                "ref_version_committed",
                                json!({"repo": inp.repo, "push": inp.push_id, "lsn": lsn, "ref": rv.r, "version": rv.s, "tip": rv.tip,
                                       "pack": rv.pack, "deps": rv.deps, "linked": rv.linked, "ls": rv.ls,
                                       "old": g.iter().find(|c| c.name == rv.r).map(|c| c.old.hex())}),
                            );
                            rs.rv_cache.lock().unwrap().insert((rv.r.clone(), rv.s), Arc::new(rv.clone()));
                            crate::compact::notify(inp.repo, &rv.r, rv.s, None);
                        }
                        if pack_new {
                            self.packs_stored.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                            // committed and durable; queue its self-contained copy (not awaited: D-13)
                            let p = pidx.as_ref().unwrap();
                            if p.is_thin() || p.has_ref_delta() {
                                if let Some(rv) = rvs_written.last() {
                                    crate::compact::notify(inp.repo, &rv.r, rv.s, Some(&p.id));
                                }
                            }
                        }
                        if pack_nonempty && !infos.is_empty() {
                            let p = pidx.as_ref().unwrap();
                            if rs.idx.read().unwrap().loaded {
                                self.register_pack(&rs, p, infos.clone());
                            }
                            lap("register", tm);
                        }
                        introduced.extend(infos);
                        for c in &g {
                            results.push(CmdResult { name: c.name.clone(), ok: true, msg: String::new() });
                        }
                        break;
                    }
                    Err(e) if e.is::<Conflict>() && attempt < 1000 => continue,
                    Err(e) => return Err(e),
                }
            }
            Ok(())
            }))
            .unwrap_or_else(|p| {
                let m = p.downcast_ref::<String>().cloned().or_else(|| p.downcast_ref::<&str>().map(|s| s.to_string()));
                Err(anyhow!("{}", m.unwrap_or_else(|| "internal error".into())))
            });
            if let Err(e) = r {
                if inp.atomic || results.is_empty() {
                    return Err(e);
                }
                return Err(anyhow::Error::new(PartialPush { done: std::mem::take(&mut results), msg: format!("{:#}", e) }));
            }
        }
        Ok(results)
    }

    // ---------------- fetch planning ----------------

    /// Plan a fetch: returns (packs to send in dependency order, acked haves,
    /// held-check context). MODEL.md §4.
    pub fn plan_fetch(&self, repo: &str, wants: &[Oid], haves: &[Oid]) -> Result<FetchPlan> {
        self.plan_fetch_opts(repo, wants, haves, false)
    }

    /// `shallow_client`: the client has truncated history, so a have only proves the
    /// have's own version is held (no linked range) and nothing older may be assumed.
    pub fn plan_fetch_opts(&self, repo: &str, wants: &[Oid], haves: &[Oid], shallow_client: bool) -> Result<FetchPlan> {
        let rs = self.repo(repo).ok_or_else(|| anyhow!("no such repository"))?;
        let reads0 = crate::metrics::local_reads();
        let have_set: HashSet<String> = haves.iter().map(|h| h.hex()).collect();
        // held ranges per ref: [lo, hi]
        let mut held: HashMap<String, Vec<(u64, u64)>> = HashMap::new();
        let mut acks = Vec::new();
        for h in haves.iter().take(256) {
            if let Some(id) = self.tip_rv(&rs, &h.hex()) {
                if let Ok(v) = self.rv(&rs, &id.r, id.s) {
                    let lo = if shallow_client { v.s } else { v.ls };
                    held.entry(id.r.clone()).or_default().push((lo, v.s));
                    acks.push(*h);
                }
            }
        }
        let mut want_rvs = Vec::new();
        for w in wants {
            match self.tip_rv(&rs, &w.hex()) {
                Some(id) => want_rvs.push(id),
                // v2 allows wanting any object: the version that introduced a pack
                // containing it has a closure containing everything reachable from it
                None => match self.locate(&rs, w)? {
                    Some((_, _, rv)) => want_rvs.push(RvId { r: rv.r, s: rv.s }),
                    None => bail!("not our ref {}", w),
                },
            }
        }
        let is_held = |held: &HashMap<String, Vec<(u64, u64)>>, r: &str, s: u64| -> bool { held.get(r).map(|v| v.iter().any(|(lo, hi)| *lo <= s && s <= *hi)).unwrap_or(false) };

        // Walk the ref-version DAG from the wants, stopping at held versions.
        // Uses covers (skip list) to jump over aligned linked ranges.
        let mut packs: Vec<String> = Vec::new();
        let mut seen_pack: HashSet<String> = HashSet::new();
        let mut visited: HashSet<(String, u64)> = HashSet::new();
        let mut stack: Vec<RvId> = want_rvs.clone();
        let mut steps = 0u64;
        let mut covers_used = 0u64;
        while let Some(id) = stack.pop() {
            if !visited.insert((id.r.clone(), id.s)) {
                continue;
            }
            if is_held(&held, &id.r, id.s) {
                continue;
            }
            steps += 1;
            // try the largest cover ending at id.s that does not reach into held range
            let mut jumped = false;
            for level in (1..=MAX_LEVEL).rev() {
                let span = FANOUT.pow(level as u32);
                if id.s % span != 0 {
                    continue;
                }
                let lo = id.s - span;
                // do not jump over a held version of this ref
                if held.get(&id.r).map(|v| v.iter().any(|(_, hi)| *hi > lo && *hi <= id.s)).unwrap_or(false) {
                    continue;
                }
                if let Some(c) = self.cover(&rs, &id.r, level, id.s) {
                    if c.tips.iter().any(|t| have_set.contains(t)) {
                        continue;
                    }
                    // A cover whose range contains a force-push or delete (a version not linked to its
                    // predecessor) also contains the discarded history's packs: using it would send objects the
                    // client cannot reach. Fall back to a smaller cover or the version walk (CG7 audit B D-9).
                    // Only a break AFTER the cover's first version (lo+1) means discarded history inside the
                    // cover; a cover whose only unlinked version is lo+1 (e.g. the branch creation) is safe.
                    if !c.linked && c.linked_inner != Some(true) {
                        continue;
                    }
                    if !c.pack.is_empty() && seen_pack.insert(c.pack.clone()) {
                        packs.push(c.pack.clone());
                    }
                    covers_used += 1;
                    for d in &c.deps {
                        stack.push(d.clone());
                    }
                    jumped = true;
                    break;
                }
            }
            if jumped {
                continue;
            }
            let v = self.rv(&rs, &id.r, id.s)?;
            if have_set.contains(&v.tip) {
                continue;
            }
            if let Some(p) = &v.pack {
                if seen_pack.insert(p.clone()) {
                    packs.push(p.clone());
                }
            }
            for d in &v.deps {
                stack.push(d.clone());
            }
        }
        // dependency order: walk produced newest-first; reverse so bases come first
        packs.reverse();
        // exact logical reads of THIS plan (thread-local; planning does not await)
        let reads = crate::metrics::local_reads() - reads0;
        crate::metrics::plan_steps(steps);
        Ok(FetchPlan { packs, acks, held, steps, covers_used, metadata_reads: reads, rs, shallow_client })
    }

    /// Does the client certainly hold `oid` (an external delta base), given the plan?
    pub fn client_holds(&self, plan: &FetchPlan, oid: &Oid) -> bool {
        // a shallow client may lack any object older than its shallow tips
        if plan.shallow_client || !matches!(self.locate(&plan.rs, oid), Ok(Some(_))) {
            return false;
        }
        // The object is reachable from the tip of at least one version that introduced its
        // pack, but not necessarily from a given one (multi-ref packs): require all held.
        let rvs = self.locate_rvs(&plan.rs, oid);
        !rvs.is_empty() && rvs.iter().all(|rv| plan.held.get(&rv.r).map(|v| v.iter().any(|(lo, hi)| *lo <= rv.s && rv.s <= *hi)).unwrap_or(false))
    }
}

pub const FANOUT: u64 = 4;
pub const MAX_LEVEL: u8 = 4;

pub struct FetchPlan {
    pub packs: Vec<String>,
    pub acks: Vec<Oid>,
    pub held: HashMap<String, Vec<(u64, u64)>>,
    pub steps: u64,
    pub covers_used: u64,
    pub metadata_reads: u64,
    pub rs: Arc<RepoState>,
    pub shallow_client: bool,
}
