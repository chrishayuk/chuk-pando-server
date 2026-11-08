//! Deterministic simulation mode: `pando sim --seed N [--steps S] [--clients C]
//! [--refs R] [--crash P] [--fault P] [--dir D]`.
//!
//! A seeded single-threaded scheduler interleaves protocol-level actions of
//! several synthetic clients (receive-pack advertisement, push, ls-refs, fetch)
//! with compaction runs, injected metadata-commit faults (before/after the WAL
//! fsync) and crash/restart (all in-memory state dropped, optional torn WAL
//! tail). Every action goes through the real protocol handlers, pack ingest,
//! metadata store, blob store, planner and pack writer. Invariants (MODEL.md §5)
//! are checked continuously; the event trace is written with logical
//! timestamps so the same seed reproduces the same trace byte-for-byte.
use crate::blob::Blobs;
use crate::events::{Events, DETERMINISTIC};
use crate::meta::Store;
use crate::pack::*;
use crate::proto;
use crate::repo::*;
use anyhow::{anyhow, bail, Result};
use flate2::{Decompress, FlushDecompress, Status};
use serde_json::json;
use sha1::{Digest, Sha1};
use std::collections::{BTreeMap, BTreeSet};
use std::io::Cursor;
use std::path::{Path, PathBuf};
use std::sync::atomic::Ordering;
use std::sync::Arc;

pub struct Rng(u64);
impl Rng {
    pub fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E3779B97F4A7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58476D1CE4E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D049BB133111EB);
        z ^ (z >> 31)
    }
    pub fn below(&mut self, n: u64) -> u64 {
        if n == 0 {
            0
        } else {
            self.next() % n
        }
    }
    pub fn chance(&mut self, p: f64) -> bool {
        (self.next() % 1_000_000) as f64 / 1_000_000.0 < p
    }
}

#[derive(Default)]
struct ClientRepo {
    objs: BTreeMap<Oid, (u8, Vec<u8>)>,
    delta_hint: BTreeMap<Oid, Oid>,
}

impl ClientRepo {
    fn put(&mut self, t: u8, d: Vec<u8>) -> Oid {
        let o = hash_object(t, &d);
        self.objs.entry(o).or_insert((t, d));
        o
    }
    fn refs_of(&self, o: &Oid) -> Vec<Oid> {
        match self.objs.get(o) {
            Some((t, d)) => {
                let mut v = Vec::new();
                let mut tt = None;
                let _ = parse_refs(*t, d, &mut v, &mut tt);
                v
            }
            None => vec![],
        }
    }
    /// reachable set; returns Err(missing oid) when the closure is incomplete
    fn reachable(&self, tips: &[Oid]) -> std::result::Result<BTreeSet<Oid>, Oid> {
        let mut seen = BTreeSet::new();
        let mut st: Vec<Oid> = tips.iter().filter(|o| !o.is_zero()).cloned().collect();
        while let Some(o) = st.pop() {
            if !seen.insert(o) {
                continue;
            }
            if !self.objs.contains_key(&o) {
                return Err(o);
            }
            for r in self.refs_of(&o) {
                if !seen.contains(&r) {
                    st.push(r);
                }
            }
        }
        Ok(seen)
    }
    fn tree_entries(&self, tree: &Oid) -> BTreeMap<String, Oid> {
        let mut m = BTreeMap::new();
        if let Some((_, d)) = self.objs.get(tree) {
            let mut p = 0;
            while p < d.len() {
                let sp = d[p..].iter().position(|&c| c == b' ').unwrap() + p;
                let nul = d[sp..].iter().position(|&c| c == 0).unwrap() + sp;
                m.insert(String::from_utf8_lossy(&d[sp + 1..nul]).to_string(), Oid::from_slice(&d[nul + 1..nul + 21]));
                p = nul + 21;
            }
        }
        m
    }
    fn commit_tree(&self, c: &Oid) -> Option<Oid> {
        let (_, d) = self.objs.get(c)?;
        Oid::from_hex(std::str::from_utf8(&d[5..45]).ok()?).ok()
    }
    fn make_commit(&mut self, rng: &mut Rng, parent: Option<Oid>, tag: &str, cherry: Option<Oid>) -> Oid {
        let mut files = parent.and_then(|p| self.commit_tree(&p)).map(|t| self.tree_entries(&t)).unwrap_or_default();
        let nmod = 1 + rng.below(2);
        for _ in 0..nmod {
            let name = format!("f{}", rng.below(6));
            let old = files.get(&name).cloned();
            let mut content = match old.and_then(|o| self.objs.get(&o).map(|x| x.1.clone())) {
                Some(c) => c,
                None => Vec::new(),
            };
            for _ in 0..(3 + rng.below(20)) {
                content.extend_from_slice(format!("{} line {} {}\n", tag, rng.below(1000), rng.next()).as_bytes());
            }
            let b = self.put(T_BLOB, content);
            if let Some(o) = old {
                self.delta_hint.insert(b, o);
            }
            files.insert(name, b);
        }
        if let Some(c) = cherry {
            files.insert("cherry".into(), c);
        }
        let mut tree = Vec::new();
        for (n, o) in &files {
            tree.extend_from_slice(format!("100644 {}\0", n).as_bytes());
            tree.extend_from_slice(&o.0);
        }
        let t = self.put(T_TREE, tree);
        let mut c = format!("tree {}\n", t);
        if let Some(p) = parent {
            c.push_str(&format!("parent {}\n", p));
        }
        c.push_str(&format!("author s <s@x> 1700000000 +0000\ncommitter s <s@x> 1700000000 +0000\n\n{}\n", tag));
        self.put(T_COMMIT, c.into_bytes())
    }

    /// Build a pack like `git pack-objects --revs --thin`: new ^negatives.
    fn build_pack(&self, rng: &mut Rng, tips: &[Oid], negatives: &[Oid], thin: bool) -> Vec<u8> {
        let negs: Vec<Oid> = negatives.iter().filter(|o| self.objs.contains_key(o)).cloned().collect();
        let excl = self.reachable(&negs).unwrap_or_default();
        let all = self.reachable(tips).unwrap_or_default();
        let send: Vec<Oid> = all.difference(&excl).cloned().collect();
        let mut out = Vec::new();
        out.extend_from_slice(b"PACK");
        out.extend_from_slice(&2u32.to_be_bytes());
        out.extend_from_slice(&(send.len() as u32).to_be_bytes());
        for o in &send {
            let (t, d) = &self.objs[o];
            let base = self.delta_hint.get(o).filter(|b| (thin && excl.contains(b)) || send.contains(b));
            if let (Some(b), true) = (base, rng.chance(0.7)) {
                let bd = &self.objs[b].1;
                let delta = make_delta(bd, d);
                encode_entry_header(T_REF, delta.len() as u64, &mut out);
                out.extend_from_slice(&b.0);
                out.extend_from_slice(&crate::packout::compress(&delta));
            } else {
                encode_entry_header(*t, d.len() as u64, &mut out);
                out.extend_from_slice(&crate::packout::compress(d));
            }
        }
        let mut h = Sha1::new();
        h.update(&out);
        let s = h.finalize();
        out.extend_from_slice(&s);
        out
    }
}

fn varint(mut n: usize, out: &mut Vec<u8>) {
    loop {
        let mut c = (n & 0x7f) as u8;
        n >>= 7;
        if n > 0 {
            c |= 0x80;
        }
        out.push(c);
        if n == 0 {
            break;
        }
    }
}

/// delta = copy(common prefix of base) + inserts
fn make_delta(base: &[u8], target: &[u8]) -> Vec<u8> {
    let mut d = Vec::new();
    varint(base.len(), &mut d);
    varint(target.len(), &mut d);
    let k = base.iter().zip(target.iter()).take_while(|(a, b)| a == b).count().min(0xffff);
    if k > 0 {
        d.push(0x80 | 0x10 | 0x20);
        d.push((k & 0xff) as u8);
        d.push(((k >> 8) & 0xff) as u8);
    }
    for ch in target[k..].chunks(127) {
        d.push(ch.len() as u8);
        d.extend_from_slice(ch);
    }
    d
}

fn decode_pack(b: &[u8], store: &ClientRepo) -> Result<Vec<(u8, Vec<u8>)>> {
    decode_pack_opts(b, store, false)
}

/// Client-side full pack decoder (like index-pack --fix-thin into a store);
/// `forbid_ref`: the client asked for no-ref-delta.
fn decode_pack_opts(b: &[u8], store: &ClientRepo, forbid_ref: bool) -> Result<Vec<(u8, Vec<u8>)>> {
    if b.len() < 32 || &b[..4] != b"PACK" {
        bail!("bad pack");
    }
    let n = u32::from_be_bytes(b[8..12].try_into().unwrap()) as usize;
    let mut p = 12usize;
    // like index-pack: read every entry first, then resolve deltas in any order
    let mut raw: Vec<(usize, u8, usize, Oid, Vec<u8>)> = Vec::new();
    for _ in 0..n {
        let off = p;
        let (t, size, hl) = decode_entry_header(&b[p..])?;
        p += hl;
        let mut base_off = 0usize;
        let mut base_oid = Oid::ZERO;
        if t == T_OFS {
            let mut c = b[p];
            p += 1;
            let mut o = (c & 0x7f) as usize;
            while c & 0x80 != 0 {
                c = b[p];
                p += 1;
                o = ((o + 1) << 7) | (c & 0x7f) as usize;
            }
            base_off = off - o;
        } else if t == T_REF {
            if forbid_ref {
                bail!("REF_DELTA entry although no-ref-delta was requested");
            }
            base_oid = Oid::from_slice(&b[p..p + 20]);
            p += 20;
        }
        let mut d = Decompress::new(true);
        let mut data = Vec::with_capacity(size as usize + 16);
        loop {
            if data.capacity() - data.len() < 16 {
                data.reserve(4096);
            }
            let before = d.total_in() as usize;
            let st = d.decompress_vec(&b[p + before..], &mut data, FlushDecompress::None).map_err(|e| anyhow!("{}", e))?;
            if st == Status::StreamEnd {
                break;
            }
            if d.total_in() as usize == before && data.len() < data.capacity() {
                bail!("stuck inflate");
            }
        }
        p += d.total_in() as usize;
        raw.push((off, t, base_off, base_oid, data));
    }
    let mut by_off: BTreeMap<usize, (u8, Vec<u8>)> = BTreeMap::new();
    let mut by_oid: BTreeMap<Oid, (u8, Vec<u8>)> = BTreeMap::new();
    let mut done = vec![false; raw.len()];
    let mut out = Vec::new();
    loop {
        let mut progress = false;
        for (i, (off, t, base_off, base_oid, data)) in raw.iter().enumerate() {
            if done[i] {
                continue;
            }
            let obj = match *t {
                T_OFS => by_off.get(base_off).map(|(bt, bd)| (*bt, apply_delta(bd, data))),
                T_REF => by_oid.get(base_oid).or_else(|| store.objs.get(base_oid)).map(|(bt, bd)| (*bt, apply_delta(bd, data))),
                _ => Some((*t, Ok(data.clone()))),
            };
            if let Some((t, d)) = obj {
                let d = d?;
                by_oid.insert(hash_object(t, &d), (t, d.clone()));
                by_off.insert(*off, (t, d.clone()));
                out.push((t, d));
                done[i] = true;
                progress = true;
            }
        }
        if !progress {
            break;
        }
    }
    if let Some(i) = done.iter().position(|d| !d) {
        let (_, t, _, base_oid, _) = &raw[i];
        if *t == T_REF {
            bail!("thin base {} not available", base_oid);
        }
        bail!("ofs base missing");
    }
    Ok(out)
}

fn pkt_lines(b: &[u8]) -> Result<Vec<Option<Vec<u8>>>> {
    let mut c = Cursor::new(b);
    let mut v = Vec::new();
    while let Some(p) = proto::read_pkt(&mut c)? {
        match p {
            proto::Pkt::Data(d) => v.push(Some(d)),
            _ => v.push(None),
        }
    }
    Ok(v)
}

struct Client {
    id: usize,
    repo: ClientRepo,
    tips: BTreeMap<String, Oid>,
    snapshot: Option<Vec<(String, Oid)>>,
    last_pos: BTreeMap<String, usize>,
    ctr: u64,
    /// Some(.have oids) when the last advertisement was an explicit-haves one
    explicit: Option<Vec<Oid>>,
}

pub struct Sim {
    rng: Rng,
    dir: PathBuf,
    core: Option<Arc<Core>>,
    events: Arc<Events>,
    clients: Vec<Client>,
    refs: Vec<String>,
    /// model: acknowledged history of each ref (tips in commit order; ZERO = deleted)
    model: BTreeMap<String, Vec<Oid>>,
    /// pushes that failed with an injected fault after the WAL fsync: may have applied
    maybe: Vec<(String, Oid, Oid)>,
    pub violations: Vec<String>,
    stats: BTreeMap<&'static str, u64>,
    pack_sums: BTreeMap<PathBuf, String>,
    crash_now: Arc<std::sync::atomic::AtomicBool>,
}

const REPO: &str = "sim";

impl Sim {
    fn open_core(dir: &Path, events: Arc<Events>) -> Result<Arc<Core>> {
        let store = Store::open(&dir.join("meta"))?;
        let blobs = Arc::new(Blobs::new(&dir.join("blobs"))?);
        Ok(Arc::new(Core::new(store, blobs, events)))
    }
    fn core(&self) -> Arc<Core> {
        self.core.clone().unwrap()
    }
    fn bump(&mut self, k: &'static str) {
        *self.stats.entry(k).or_default() += 1;
    }
    fn violation(&mut self, s: String) {
        self.events.emit("sim_violation", json!({"what": s}));
        self.violations.push(s);
    }

    fn advertise(&mut self, ci: usize) -> Result<()> {
        let explicit = self.rng.chance(0.3);
        let adv = proto::receive_advertisement(&self.core(), REPO, explicit);
        let mut refs = Vec::new();
        let mut haves = Vec::new();
        for l in pkt_lines(&adv)?.into_iter().flatten() {
            let s = String::from_utf8_lossy(&l).to_string();
            let s = s.split('\0').next().unwrap().trim_end().to_string();
            let mut it = s.splitn(2, ' ');
            let (o, n) = (it.next().unwrap(), it.next().unwrap_or(""));
            if n.starts_with("refs/") {
                refs.push((n.to_string(), Oid::from_hex(o)?));
            } else if n == ".have" {
                haves.push(Oid::from_hex(o)?);
            }
        }
        self.clients[ci].snapshot = Some(refs);
        // explicit-haves: only .have objects may be pack prerequisites (named refs are for old-ids)
        self.clients[ci].explicit = if explicit { Some(haves) } else { None };
        Ok(())
    }

    fn push(&mut self, ci: usize) -> Result<()> {
        let snap = match self.clients[ci].snapshot.take() {
            Some(s) => s,
            None => return self.advertise(ci),
        };
        let r = self.refs[self.rng.below(self.refs.len() as u64) as usize].clone();
        let cur = snap.iter().find(|(n, _)| *n == r).map(|x| x.1).unwrap_or(Oid::ZERO);
        let action = self.rng.below(10);
        let c = &mut self.clients[ci];
        c.ctr += 1;
        let tag = format!("c{}-{}", c.id, c.ctr);
        let have_cur = !cur.is_zero() && c.repo.reachable(&[cur]).is_ok();
        // pick a parent
        let new = if action == 0 && !cur.is_zero() {
            Oid::ZERO // delete
        } else {
            let parent = if action <= 1 {
                // force: unrelated or older base
                c.tips.values().next().cloned().filter(|_| self.rng.chance(0.5))
            } else if have_cur {
                Some(cur)
            } else {
                c.tips.get(&r).cloned().or_else(|| c.tips.values().next().cloned())
            };
            let cherry = if self.rng.chance(0.2) {
                // reuse a blob from some other known commit (cross-branch dependency)
                let blobs: Vec<Oid> = c.repo.objs.iter().filter(|(_, v)| v.0 == T_BLOB).map(|(k, _)| *k).collect();
                if blobs.is_empty() { None } else { Some(blobs[self.rng.below(blobs.len() as u64) as usize]) }
            } else {
                None
            };
            let parent = parent.filter(|p| c.repo.reachable(&[*p]).is_ok());
            c.repo.make_commit(&mut self.rng, parent, &tag, cherry)
        };
        // old-ids come from the (possibly stale) advertisement => real CAS races
        let old = cur;
        let mut cmds: Vec<(String, Oid, Oid)> = vec![(r.clone(), old, new)];
        // sometimes a second ref in the same push (one pack introducing two refs)
        if !new.is_zero() && self.rng.chance(0.25) {
            let r2 = self.refs[self.rng.below(self.refs.len() as u64) as usize].clone();
            if r2 != r {
                let cur2 = snap.iter().find(|(n, _)| *n == r2).map(|x| x.1).unwrap_or(Oid::ZERO);
                let c = &mut self.clients[ci];
                let parent2 = if !cur2.is_zero() && c.repo.reachable(&[cur2]).is_ok() { Some(cur2) } else { None };
                let new2 = c.repo.make_commit(&mut self.rng, parent2, &format!("{}-b", tag), None);
                cmds.push((r2, cur2, new2));
            }
        }
        let c = &mut self.clients[ci];
        let explicit = c.explicit.take();
        let negatives: Vec<Oid> = match &explicit {
            Some(h) => h.clone(),
            None => snap.iter().map(|x| x.1).collect(),
        };
        let atomic = self.rng.chance(0.3);
        let mut body = Vec::new();
        let base_caps = if atomic { "report-status side-band-64k atomic ofs-delta" } else { "report-status side-band-64k ofs-delta" };
        let caps_s = if explicit.is_some() { format!("{} explicit-haves", base_caps) } else { base_caps.to_string() };
        let caps = caps_s.as_str();
        for (i, (rr, o, n)) in cmds.iter().enumerate() {
            if i == 0 {
                proto::pkt_str(&mut body, &format!("{} {} {}\0{}\n", o, n, rr, caps));
            } else {
                proto::pkt_str(&mut body, &format!("{} {} {}\n", o, n, rr));
            }
        }
        proto::flush(&mut body);
        let news: Vec<Oid> = cmds.iter().map(|x| x.2).filter(|o| !o.is_zero()).collect();
        if !news.is_empty() {
            let thin = self.rng.chance(0.8);
            let pack = c.repo.build_pack(&mut self.rng, &news, &negatives, thin);
            body.extend_from_slice(&pack);
        }
        if cmds.len() > 1 {
            self.bump("multi_ref_push");
            return self.finish_multi(body, cmds, atomic, ci);
        }
        let mut resp = Vec::new();
        let core = self.core();
        let res = proto::handle_receive(&core, REPO, Cursor::new(body), &mut |v| {
            resp.extend(v);
            Ok(())
        });
        let mut status: Option<(bool, String)> = None;
        let mut unpack_ok = false;
        if res.is_ok() {
            let mut inner = Vec::new();
            for l in pkt_lines(&resp)?.into_iter().flatten() {
                if l.first() == Some(&1) {
                    inner.extend_from_slice(&l[1..]);
                }
            }
            for l in pkt_lines(&inner)?.into_iter().flatten() {
                let s = String::from_utf8_lossy(&l).trim_end().to_string();
                if s == "unpack ok" {
                    unpack_ok = true;
                } else if let Some(x) = s.strip_prefix("ok ") {
                    if x == r {
                        status = Some((true, String::new()));
                    }
                } else if let Some(x) = s.strip_prefix("ng ") {
                    if x.starts_with(&r) {
                        status = Some((false, x[r.len()..].trim().to_string()));
                    }
                }
            }
        }
        let model_tip = self.model.get(&r).and_then(|h| h.last().cloned()).unwrap_or(Oid::ZERO);
        let expect_ok = model_tip == old;
        match status {
            Some((true, _)) => {
                self.bump("push_ok");
                if !expect_ok {
                    self.violation(format!("push to {} accepted with old {} but model tip {}", r, old, model_tip));
                }
                self.model.entry(r.clone()).or_default().push(new);
                let c = &mut self.clients[ci];
                if new.is_zero() {
                    c.tips.remove(&r);
                } else {
                    c.tips.insert(r.clone(), new);
                }
            }
            Some((false, msg)) => {
                self.bump("push_rejected");
                if msg.contains("injected fault") {
                    self.maybe.push((r.clone(), old, new));
                    self.bump("push_fault");
                } else if expect_ok && unpack_ok {
                    self.violation(format!("push to {} rejected ({}) although old matched model", r, msg));
                }
            }
            None => {
                self.bump("push_noreport");
                if !unpack_ok {
                    self.violation(format!("push to {} produced no report: {:?}", r, res.err()));
                }
            }
        }
        Ok(())
    }

    fn finish_multi(&mut self, body: Vec<u8>, cmds: Vec<(String, Oid, Oid)>, atomic: bool, ci: usize) -> Result<()> {
        let mut resp = Vec::new();
        let core = self.core();
        let res = proto::handle_receive(&core, REPO, Cursor::new(body), &mut |v| {
            resp.extend(v);
            Ok(())
        });
        let mut st: BTreeMap<String, (bool, String)> = BTreeMap::new();
        let mut unpack_ok = false;
        if res.is_ok() {
            let mut inner = Vec::new();
            for l in pkt_lines(&resp)?.into_iter().flatten() {
                if l.first() == Some(&1) {
                    inner.extend_from_slice(&l[1..]);
                }
            }
            for l in pkt_lines(&inner)?.into_iter().flatten() {
                let s = String::from_utf8_lossy(&l).trim_end().to_string();
                if s == "unpack ok" {
                    unpack_ok = true;
                } else if let Some(x) = s.strip_prefix("ok ") {
                    st.insert(x.to_string(), (true, String::new()));
                } else if let Some(x) = s.strip_prefix("ng ") {
                    let (n, m) = x.split_once(' ').unwrap_or((x, ""));
                    st.insert(n.to_string(), (false, m.to_string()));
                }
            }
        }
        let expect: Vec<bool> = cmds.iter().map(|(r, o, _)| self.model.get(r).and_then(|h| h.last().cloned()).unwrap_or(Oid::ZERO) == *o).collect();
        let all = expect.iter().all(|x| *x);
        let fault = st.values().any(|(_, m)| m.contains("injected fault"));
        for (i, (r, o, n)) in cmds.iter().enumerate() {
            let exp = if atomic { all } else { expect[i] };
            match st.get(r) {
                Some((true, _)) => {
                    self.bump("push_ok");
                    if !exp {
                        self.violation(format!("multi push: {} accepted but expectation failed (atomic={})", r, atomic));
                    }
                    self.model.entry(r.clone()).or_default().push(*n);
                    self.clients[ci].tips.insert(r.clone(), *n);
                }
                Some((false, m)) => {
                    self.bump("push_rejected");
                    if fault {
                        self.maybe.push((r.clone(), *o, *n));
                    } else if exp && unpack_ok {
                        self.violation(format!("multi push: {} rejected ({}) although expected ok", r, m));
                    }
                }
                None => {
                    if unpack_ok {
                        self.violation(format!("multi push: no status for {}", r));
                    }
                }
            }
        }
        Ok(())
    }

    fn resolve_maybe(&mut self) -> Result<()> {
        if self.maybe.is_empty() {
            return Ok(());
        }
        let server: BTreeMap<String, Oid> = self.core().list_refs(REPO).into_iter().map(|(n, p)| (n, Oid::from_hex(&p.tip).unwrap())).collect();
        for (r, old, new) in std::mem::take(&mut self.maybe) {
            let st = server.get(&r).cloned().unwrap_or(Oid::ZERO);
            let mt = self.model.get(&r).and_then(|h| h.last().cloned()).unwrap_or(Oid::ZERO);
            if st == new && mt == old && st != mt {
                self.model.entry(r.clone()).or_default().push(new);
                self.events.emit("sim_unacked_applied", json!({"ref": r, "tip": new.hex()}));
            }
        }
        Ok(())
    }

    fn fetch(&mut self, ci: usize) -> Result<()> {
        if self.rng.chance(0.15) {
            // client `gc --prune=now`: drop everything not reachable from its refs
            self.bump("client_gc");
            let c = &mut self.clients[ci];
            let tips: Vec<Oid> = c.tips.values().cloned().collect();
            let keep = c.repo.reachable(&tips).unwrap_or_default();
            c.repo.objs.retain(|o, _| keep.contains(o));
        }
        let core = self.core();
        // ls-refs
        let mut body = Vec::new();
        proto::pkt_str(&mut body, "command=ls-refs\n");
        proto::delim(&mut body);
        proto::pkt_str(&mut body, "peel\n");
        proto::pkt_str(&mut body, "symrefs\n");
        proto::flush(&mut body);
        let up = proto::UriProvider { uri_for: &|_: &str, _: &str| None };
        let mut resp = Vec::new();
        proto::handle_upload_v2(&core, REPO, Cursor::new(body), &up, &mut |v| {
            resp.extend(v);
            Ok(())
        })?;
        let mut listed = Vec::new();
        for l in pkt_lines(&resp)?.into_iter().flatten() {
            let s = String::from_utf8_lossy(&l).trim_end().to_string();
            let mut it = s.split(' ');
            let o = it.next().unwrap_or("");
            let n = it.next().unwrap_or("");
            if n.starts_with("refs/") {
                listed.push((n.to_string(), Oid::from_hex(o)?));
            }
        }
        // monotonic reads per client
        for (n, o) in &listed {
            if let Some(h) = self.model.get(n) {
                if let Some(pos) = h.iter().rposition(|x| x == o) {
                    let last = self.clients[ci].last_pos.get(n).cloned().unwrap_or(0);
                    if pos < last {
                        self.violation(format!("client {} saw {} go backwards ({} -> {})", ci, n, last, pos));
                    }
                    self.clients[ci].last_pos.insert(n.clone(), pos);
                } else {
                    self.violation(format!("ls-refs shows {} at {} which was never acknowledged", n, o));
                }
            }
        }
        if listed.is_empty() {
            return Ok(());
        }
        let k = 1 + self.rng.below(listed.len() as u64) as usize;
        let wants: Vec<(String, Oid)> = listed.iter().take(k).cloned().collect();
        let c = &self.clients[ci];
        let haves: Vec<Oid> = c.tips.values().filter(|t| c.repo.reachable(&[**t]).is_ok()).cloned().collect();
        let thin = self.rng.chance(0.8);
        let use_uris = self.rng.chance(0.4);
        let nrd = use_uris && self.rng.chance(0.5);
        let mut body = Vec::new();
        proto::pkt_str(&mut body, "command=fetch\n");
        proto::delim(&mut body);
        if thin {
            proto::pkt_str(&mut body, "thin-pack\n");
        }
        if use_uris {
            proto::pkt_str(&mut body, "packfile-uris http,https\n");
            if nrd {
                proto::pkt_str(&mut body, "no-ref-delta\n");
            }
        }
        proto::pkt_str(&mut body, "ofs-delta\n");
        proto::pkt_str(&mut body, "no-progress\n");
        for (_, w) in &wants {
            proto::pkt_str(&mut body, &format!("want {}\n", w));
        }
        for h in &haves {
            proto::pkt_str(&mut body, &format!("have {}\n", h));
        }
        proto::pkt_str(&mut body, "done\n");
        proto::flush(&mut body);
        let mut resp = Vec::new();
        let up_uri = proto::UriProvider { uri_for: &|_: &str, p: &str| Some(format!("sim://{}", p)) };
        proto::handle_upload_v2(&core, REPO, Cursor::new(body), if use_uris { &up_uri } else { &up }, &mut |v| {
            resp.extend(v);
            Ok(())
        })?;
        let mut pack = Vec::new();
        let mut uris: Vec<(String, String)> = Vec::new();
        let mut section = String::new();
        for l in pkt_lines(&resp)?.into_iter().flatten() {
            if section == "packfile" {
                if l.first() == Some(&1) {
                    pack.extend_from_slice(&l[1..]);
                }
            } else if l == b"packfile\n" || l == b"packfile-uris\n" {
                section = String::from_utf8_lossy(&l).trim_end().to_string();
            } else if l.starts_with(b"ERR") {
                self.violation(format!("fetch error: {}", String::from_utf8_lossy(&l)));
                return Ok(());
            } else if section == "packfile-uris" {
                let s = String::from_utf8_lossy(&l).trim_end().to_string();
                if let Some((h, u)) = s.split_once(' ') {
                    uris.push((h.to_string(), u.trim_start_matches("sim://").to_string()));
                }
            }
        }
        self.bump("fetch");
        if !uris.is_empty() {
            self.bump("fetch_with_uris");
        }
        // inline pack is indexed first (thin bases from the local store allowed if requested)
        let objs = match decode_pack_opts(&pack, &self.clients[ci].repo, nrd) {
            Ok(o) => o,
            Err(e) => {
                self.violation(format!("client {} could not index fetched pack (uris={}, nrd={}): {}", ci, uris.len(), nrd, e));
                return Ok(());
            }
        };
        for (t, d) in objs {
            self.clients[ci].repo.put(t, d);
        }
        // then each packfile URI: hash must match, and the pack must index without
        // thickening (git verifies the hash); with no-ref-delta, standalone (parallel)
        let empty = ClientRepo::default();
        for (h, id) in &uris {
            let b = std::fs::read(core.blobs.pack_path(REPO, id))?;
            let trailer = Oid::from_slice(&b[b.len() - 20..]).hex();
            if trailer != *h || *h != *id {
                self.violation(format!("packfile-uri hash mismatch {} vs {}", h, trailer));
            }
            match decode_pack_opts(&b, &empty, nrd) {
                Ok(objs) => {
                    for (t, d) in objs {
                        self.clients[ci].repo.put(t, d);
                    }
                }
                Err(e) => self.violation(format!("uri pack {} not self-contained (nrd={}): {}", id, nrd, e)),
            }
        }
        for (n, w) in &wants {
            if let Err(m) = self.clients[ci].repo.reachable(&[*w]) {
                let msg = format!("client {} fetch of {} incomplete: missing {}", ci, n, m);
                self.violation(msg);
                return Ok(());
            }
            self.clients[ci].tips.insert(n.clone(), *w);
        }
        Ok(())
    }

    /// I1: every servable ref version has a complete object closure in stored packs.
    fn check_closures(&mut self) -> Result<()> {
        let core = self.core();
        for (n, p) in core.list_refs(REPO) {
            let tip = Oid::from_hex(&p.tip)?;
            let plan = core.plan_fetch(REPO, &[tip], &[])?;
            let mut objs = ClientRepo::default();
            for pk in &plan.packs {
                let idx = core.blobs.index(REPO, pk)?;
                for (i, _) in idx.entries.iter().enumerate() {
                    let (t, d) = core.blobs.read_object(REPO, pk, i)?;
                    objs.put(t, d.to_vec());
                }
            }
            if let Err(m) = objs.reachable(&[tip]) {
                self.violation(format!("closure of {}@{} incomplete: missing {}", n, p.seq, m));
            }
        }
        // I7': a thick twin holds exactly the objects of its original, and is self-contained
        let tp = format!("twin{}{}{}", SEP, REPO, SEP);
        for (k, v) in core.store.scan(&tp) {
            let orig = &k[tp.len()..];
            let twin = String::from_utf8_lossy(&v).to_string();
            let a: BTreeSet<Oid> = core.blobs.index(REPO, orig)?.entries.iter().map(|e| e.oid).collect();
            let ti = core.blobs.index(REPO, &twin)?;
            let b: BTreeSet<Oid> = ti.entries.iter().map(|e| e.oid).collect();
            if a != b || ti.is_thin() || ti.has_ref_delta() {
                self.violation(format!("twin {} of {} differs or is not self-contained", twin, orig));
            }
        }
        // I6: packs immutable
        let mut sums = Vec::new();
        for id in core.blobs.list_packs(REPO) {
            let path = core.blobs.pack_path(REPO, &id);
            let b = std::fs::read(&path)?;
            let mut h = Sha1::new();
            h.update(&b);
            sums.push((path, Oid::from_slice(&h.finalize()).hex()));
        }
        for (p, s) in sums {
            if let Some(old) = self.pack_sums.get(&p) {
                if *old != s {
                    self.violation(format!("pack {:?} changed", p));
                }
            }
            self.pack_sums.insert(p, s);
        }
        Ok(())
    }

    fn crash(&mut self) -> Result<()> {
        self.bump("crash");
        self.events.emit("sim_crash", json!({}));
        self.core = None; // drop all in-memory state, no shutdown hooks
        if self.rng.chance(0.3) {
            // torn tail: garbage appended after the last complete record
            let meta = self.dir.join("meta");
            if let Ok(rd) = std::fs::read_dir(&meta) {
                let mut wals: Vec<PathBuf> = rd.flatten().map(|e| e.path()).filter(|p| p.file_name().unwrap().to_string_lossy().starts_with("wal.")).collect();
                wals.sort();
                if let Some(w) = wals.last() {
                    use std::io::Write;
                    let mut f = std::fs::OpenOptions::new().append(true).open(w)?;
                    let n = 1 + self.rng.below(40) as usize;
                    let junk: Vec<u8> = (0..n).map(|_| self.rng.next() as u8).collect();
                    f.write_all(&junk)?;
                    self.bump("torn_tail");
                }
            }
        }
        self.core = Some(Self::open_core(&self.dir, self.events.clone())?);
        self.events.emit("sim_restart", json!({}));
        self.resolve_maybe()?;
        // durability: every acknowledged update is present
        let server: BTreeMap<String, Oid> = self.core().list_refs(REPO).into_iter().map(|(n, p)| (n, Oid::from_hex(&p.tip).unwrap())).collect();
        for (r, h) in self.model.clone() {
            let mt = h.last().cloned().unwrap_or(Oid::ZERO);
            let st = server.get(&r).cloned().unwrap_or(Oid::ZERO);
            if mt != st {
                self.violation(format!("after restart {} is {} but last acknowledged {}", r, st, mt));
            }
        }
        self.check_closures()
    }

    fn arm_fault(&mut self) {
        let before = self.rng.chance(0.5);
        let point = if before { "before_wal" } else { "after_wal" };
        self.bump("fault_armed");
        self.events.emit("sim_fault_armed", json!({"point": point}));
        let fired = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let core = self.core();
        let crash_now = self.crash_now.clone();
        // "after_wal" models a process crash between WAL fsync and in-memory apply:
        // the scheduler crashes the server right after the step.
        *core.store.fail_hook.lock().unwrap() = Some(Arc::new(move |p: &str| {
            let fire = p == point && !fired.swap(true, Ordering::SeqCst);
            if fire && p == "after_wal" {
                crash_now.store(true, Ordering::SeqCst);
            }
            fire
        }));
    }
}

pub fn main(args: &[String]) -> Result<()> {
    let mut seed = 1u64;
    let mut steps = 400u64;
    let mut nclients = 3usize;
    let mut nrefs = 4usize;
    let mut crash = 0.03f64;
    let mut fault = 0.05f64;
    let mut dir: Option<PathBuf> = None;
    let mut i = 0;
    while i < args.len() {
        let v = args.get(i + 1).cloned().unwrap_or_default();
        match args[i].as_str() {
            "--seed" => seed = v.parse()?,
            "--steps" => steps = v.parse()?,
            "--clients" => nclients = v.parse()?,
            "--refs" => nrefs = v.parse()?,
            "--crash" => crash = v.parse()?,
            "--fault" => fault = v.parse()?,
            "--dir" => dir = Some(PathBuf::from(v)),
            _ => bail!("unknown sim option {}", args[i]),
        }
        i += 2;
    }
    let report = run(seed, steps, nclients, nrefs, crash, fault, dir)?;
    println!("{}", report);
    if report["violations"].as_array().map(|a| !a.is_empty()).unwrap_or(true) {
        std::process::exit(1);
    }
    Ok(())
}

pub fn run(seed: u64, steps: u64, nclients: usize, nrefs: usize, crash: f64, fault: f64, dir: Option<PathBuf>) -> Result<serde_json::Value> {
    DETERMINISTIC.store(true, Ordering::SeqCst);
    let dir = dir.unwrap_or_else(|| std::env::temp_dir().join(format!("pando-sim-{}", seed)));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir)?;
    let events = Arc::new(Events::open(&dir.join("events"), "sim"));
    events.emit("sim_start", json!({"seed": seed, "steps": steps, "clients": nclients, "refs": nrefs, "crash": crash, "fault": fault}));
    let core = Sim::open_core(&dir, events.clone())?;
    core.create_repo(REPO)?;
    let mut refs = vec!["refs/heads/main".to_string()];
    for k in 1..nrefs {
        refs.push(format!("refs/heads/b{}", k));
    }
    let mut sim = Sim {
        rng: Rng(seed),
        dir: dir.clone(),
        core: Some(core),
        events: events.clone(),
        clients: (0..nclients).map(|id| Client { id, repo: Default::default(), tips: BTreeMap::new(), snapshot: None, last_pos: BTreeMap::new(), ctr: 0, explicit: None }).collect(),
        refs,
        model: BTreeMap::new(),
        maybe: Vec::new(),
        violations: Vec::new(),
        stats: BTreeMap::new(),
        pack_sums: BTreeMap::new(),
        crash_now: Arc::new(std::sync::atomic::AtomicBool::new(false)),
    };
    for step in 0..steps {
        let ci = sim.rng.below(nclients as u64) as usize;
        let x = sim.rng.below(1000) as f64 / 1000.0;
        sim.events.emit("sim_step", json!({"step": step, "client": ci}));
        let r = if x < crash {
            sim.crash()
        } else if x < crash + fault {
            sim.arm_fault();
            Ok(())
        } else if x < crash + fault + 0.03 {
            sim.bump("maintain");
            let core = sim.core();
            let r = crate::compact::maintain_all(&core).map(|_| ());
            if sim.rng.chance(0.5) {
                sim.bump("checkpoint");
                core.store.checkpoint().and(r)
            } else {
                r
            }
        } else if x < 0.35 {
            sim.advertise(ci)
        } else if x < 0.70 {
            sim.push(ci)
        } else {
            sim.fetch(ci)
        };
        if let Err(e) = r {
            let msg = format!("step {} error: {:#}", step, e);
            if !msg.contains("injected fault") {
                sim.violation(msg);
            }
        }
        if sim.crash_now.swap(false, Ordering::SeqCst) {
            if let Err(e) = sim.crash() {
                sim.violation(format!("step {} restart error: {:#}", step, e));
            }
        }
        if step % 25 == 24 {
            sim.check_closures()?;
        }
    }
    sim.resolve_maybe()?;
    sim.check_closures()?;
    let core = sim.core();
    let report = json!({
        "seed": seed,
        "steps": steps,
        "stats": sim.stats,
        "refs": core.list_refs(REPO).len(),
        "packs": core.blobs.list_packs(REPO).len(),
        "metadata_reads": core.store.reads.load(Ordering::Relaxed),
        "metadata_writes": core.store.writes.load(Ordering::Relaxed),
        "violations": sim.violations,
        "trace": dir.join("events").join("events.jsonl"),
    });
    let mut traced = report.clone();
    traced.as_object_mut().unwrap().remove("trace");
    events.emit("sim_end", traced);
    Ok(report)
}

/// `pando fuzz --seed N --iters K`: mutate valid packs / request bodies and feed
/// them to the ingest + receive-pack path; any panic is a failure.
pub fn fuzz_main(args: &[String]) -> Result<()> {
    let mut seed = 1u64;
    let mut iters = 2000u64;
    let mut i = 0;
    while i < args.len() {
        let v = args.get(i + 1).cloned().unwrap_or_default();
        match args[i].as_str() {
            "--seed" => seed = v.parse()?,
            "--iters" => iters = v.parse()?,
            _ => bail!("unknown fuzz option {}", args[i]),
        }
        i += 2;
    }
    DETERMINISTIC.store(true, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("pando-fuzz-{}", seed));
    let _ = std::fs::remove_dir_all(&dir);
    let events = Arc::new(Events::open(&dir.join("events"), "fuzz"));
    let core = Sim::open_core(&dir, events)?;
    core.create_repo(REPO)?;
    let mut rng = Rng(seed);
    let mut client = ClientRepo::default();
    // a base history on the server so thin deltas have bases
    let c1 = client.make_commit(&mut rng, None, "base", None);
    let pack = client.build_pack(&mut rng, &[c1], &[], false);
    let mut body = Vec::new();
    proto::pkt_str(&mut body, &format!("{} {} refs/heads/main\0report-status\n", Oid::ZERO, c1));
    proto::flush(&mut body);
    body.extend_from_slice(&pack);
    proto::handle_receive(&core, REPO, Cursor::new(body), &mut |_| Ok(()))?;
    let (mut accepted, mut rejected, mut panics) = (0u64, 0u64, 0u64);
    for it in 0..iters {
        let c2 = client.make_commit(&mut rng, Some(c1), &format!("f{}", it), None);
        let mut pack = client.build_pack(&mut rng, &[c2], &[c1], true);
        let mut body = Vec::new();
        proto::pkt_str(&mut body, &format!("{} {} refs/heads/f{}\0report-status side-band-64k\n", Oid::ZERO, c2, it));
        proto::flush(&mut body);
        let hdr_len = body.len();
        // mutations
        let nmut = 1 + rng.below(4);
        for _ in 0..nmut {
            match rng.below(6) {
                0 if !pack.is_empty() => {
                    let p = rng.below(pack.len() as u64) as usize;
                    pack[p] ^= 1 << rng.below(8);
                }
                1 if pack.len() > 12 => {
                    let p = 12 + rng.below((pack.len() - 12) as u64) as usize;
                    pack.truncate(p);
                }
                2 => {
                    let p = rng.below(pack.len() as u64 + 1) as usize;
                    let junk: Vec<u8> = (0..rng.below(64)).map(|_| rng.next() as u8).collect();
                    pack.splice(p..p, junk);
                }
                3 if pack.len() >= 12 => {
                    // absurd object count
                    let n = rng.next() as u32;
                    pack[8..12].copy_from_slice(&n.to_be_bytes());
                }
                4 => {
                    // recompute trailer so corruption passes the checksum and reaches the decoder
                    if pack.len() > 20 {
                        let n = pack.len() - 20;
                        let mut h = Sha1::new();
                        h.update(&pack[..n]);
                        let s = h.finalize();
                        pack[n..].copy_from_slice(&s);
                    }
                }
                _ => {
                    // corrupt the pkt-line framing
                    if hdr_len > 4 {
                        let p = rng.below(hdr_len as u64) as usize;
                        body[p] = rng.next() as u8;
                    }
                }
            }
        }
        body.extend_from_slice(&pack);
        let core2 = core.clone();
        let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let mut resp = Vec::new();
            let r = proto::handle_receive(&core2, REPO, Cursor::new(body), &mut |v| {
                resp.extend(v);
                Ok(())
            });
            (r.is_ok(), String::from_utf8_lossy(&resp).contains("unpack ok"))
        }));
        match r {
            Ok((_, true)) => accepted += 1,
            Ok(_) => rejected += 1,
            Err(_) => panics += 1,
        }
    }
    // the store must still be usable and every ref servable
    let mut broken = 0;
    for (_, p) in core.list_refs(REPO) {
        let tip = Oid::from_hex(&p.tip)?;
        let plan = core.plan_fetch(REPO, &[tip], &[])?;
        let mut objs = ClientRepo::default();
        for pk in &plan.packs {
            let idx = core.blobs.index(REPO, pk)?;
            for (i, _) in idx.entries.iter().enumerate() {
                let (t, d) = core.blobs.read_object(REPO, pk, i)?;
                objs.put(t, d.to_vec());
            }
        }
        if objs.reachable(&[tip]).is_err() {
            broken += 1;
        }
    }
    let rep = json!({"seed": seed, "iters": iters, "accepted": accepted, "rejected": rejected, "panics": panics, "broken_refs": broken});
    println!("{}", rep);
    if panics > 0 || broken > 0 {
        std::process::exit(1);
    }
    Ok(())
}
