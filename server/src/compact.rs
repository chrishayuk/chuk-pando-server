//! LSM-style compaction into covers (MODEL.md §4.3).
//!
//! For ref r, a level-L cover spans the aligned version range (hi-4^L, hi]
//! (hi % 4^L == 0). Its pack contains the union of the objects of the member
//! versions' L0 packs (at every level), with
//! duplicates removed, deltas kept when their base is inside the cover and
//! expanded otherwise, so a cover is self-contained (no REF_DELTA, no external
//! bases) and therefore servable as a packfile URI. Its dependencies are the
//! union of the members' dependencies minus the members themselves. Covers are
//! additional packs: nothing is ever deleted.
use crate::ingest;
use crate::meta::Conflict;
use crate::pack::*;
use crate::packout::{self, OutOpts};
use crate::repo::*;
use anyhow::{anyhow, bail, Result};
use serde_json::json;
use std::collections::{BTreeSet, HashSet};
use std::sync::mpsc;
use std::sync::{Arc, Mutex, OnceLock};

enum Job {
    Compact(String, String, u64),
    Twin(String, String),
}

static TX: OnceLock<Mutex<mpsc::Sender<Job>>> = OnceLock::new();

/// Notify the background worker that `r` reached version `seq`; `twin_pack` is
/// a newly committed L0 pack that is not self-contained (needs a thick twin).
pub fn notify(repo: &str, r: &str, seq: u64, twin_pack: Option<&str>) {
    if twin_pack.is_some() && hold_copies() {
        // test-only (PANDO_TEST_HOLD_COPIES=1): leave copies unbuilt to exercise the fetch fallback window
        return notify(repo, r, seq, None);
    }
    if let Some(tx) = TX.get() {
        let tx = tx.lock().unwrap();
        if let Some(p) = twin_pack {
            let _ = tx.send(Job::Twin(repo.to_string(), p.to_string()));
        }
        if seq % FANOUT == 0 {
            let _ = tx.send(Job::Compact(repo.to_string(), r.to_string(), seq));
        }
    }
}

/// Build the self-contained "thick twin" of a thin / REF_DELTA L0 pack: same
/// objects, external delta bases expanded, only OFS deltas. The original pack is
/// untouched; the twin lets packfile-URI clients receive every planned pack by
/// URI (empty inline pack). Recorded as `twin/R/<pack>` (write-once).
pub fn build_twin(core: &Core, repo: &str, pack: &str) -> Result<bool> {
    let t0 = std::time::Instant::now();
    let rs = core.repo(repo).ok_or_else(|| anyhow!("no repo"))?;
    if core.twin(&rs, pack).is_some() {
        return Ok(false);
    }
    let orig = core.blobs.index(repo, pack)?;
    if !orig.is_thin() && !orig.has_ref_delta() {
        return Ok(false);
    }
    let mut bytes = Vec::new();
    let opts = OutOpts { ofs: true, thin: false, no_ref_delta: true };
    let none = |_: &Oid| false;
    {
        let mut w = |b: &[u8]| -> Result<()> {
            bytes.extend_from_slice(b);
            Ok(())
        };
        packout::write_spliced(&core.blobs, repo, &[pack.to_string()], &HashSet::new(), &opts, &none, &mut w)?;
    }
    let tmp = core.blobs.tmp_path();
    let mut nolookup = |_: &Oid| -> Result<Option<(u8, Arc<Vec<u8>>, String)>> { Ok(None) };
    let res = ingest::ingest(std::io::Cursor::new(&bytes), &tmp, &mut nolookup)?;
    core.blobs.install(repo, &res.tmp_path, &res.idx)?;
    let twin_id = res.idx.id.clone();
    loop {
        let mut tx = core.store.txn();
        let tk = k_twin(repo, pack);
        if tx.get(&tk).is_some() {
            return Ok(false);
        }
        let orig_rec: Option<PackRec> = tx.get(&k_pack(repo, pack)).and_then(|v| serde_json::from_slice(&v).ok());
        tx.put(&tk, twin_id.as_bytes().to_vec());
        let pk = k_pack(repo, &twin_id);
        if tx.get(&pk).is_none() {
            let rec = PackRec {
                id: twin_id.clone(),
                level: 0,
                objects: res.count as u64,
                bytes: bytes.len() as u64,
                thin: false,
                ref_delta: false,
                rvs: orig_rec.map(|r| r.rvs).unwrap_or_default(),
                cover: None,
                twin_of: Some(pack.to_string()),
            };
            tx.put(&pk, serde_json::to_vec(&rec)?);
        }
        match tx.commit() {
            Ok(lsn) => {
                core.packs_derived.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                core.events.emit("twin_committed", json!({"repo": repo, "pack": pack, "twin": twin_id, "bytes": bytes.len(), "orig_bytes": orig.pack_size, "lsn": lsn, "build_ms": t0.elapsed().as_millis() as u64}));
                return Ok(true);
            }
            Err(e) if e.is::<Conflict>() => continue,
            Err(e) => return Err(e),
        }
    }
}

/// Test-only switch (docs/BACKGROUND-COPIES.md, criterion 3): copy jobs are not run, so a test can observe the
/// window between push acknowledgment and copy publication. Never set in normal operation.
pub fn hold_copies() -> bool {
    std::env::var("PANDO_TEST_HOLD_COPIES").map(|v| v == "1").unwrap_or(false)
}

/// Background preparation of a self-contained copy (D-13; docs/BACKGROUND-COPIES.md). Every thin / REF_DELTA L0
/// pack gets one: a reader positioned inside a cover's range still needs the individual L0 packs.
pub fn prepare_copy(core: &Core, repo: &str, pack: &str) -> Result<bool> {
    build_twin(core, repo, pack)
}

/// Build every missing self-contained copy (startup catch-up and `maintain`): reconstructed from committed state,
/// so no job record is needed. Returns the number built.
pub fn prepare_missing_copies(core: &Core, repo: &str) -> Result<u64> {
    if hold_copies() {
        return Ok(0);
    }
    let mut n = 0;
    let p = k_pack_prefix(repo);
    let pending: Vec<String> = core
        .store
        .scan(&p)
        .into_iter()
        .filter_map(|(k, v)| serde_json::from_slice::<PackRec>(&v).ok().map(|r| (k, r)))
        .filter(|(_, r)| r.level == 0 && r.twin_of.is_none() && r.cover.is_none() && (r.thin || r.ref_delta))
        .map(|(k, _)| k[p.len()..].to_string())
        .collect();
    for pack in pending {
        if prepare_copy(core, repo, &pack)? {
            n += 1;
        }
    }
    Ok(n)
}

pub fn start_background(core: Arc<Core>) {
    let (tx, rx) = mpsc::channel::<Job>();
    let _ = TX.set(Mutex::new(tx));
    // WAL checkpointing keeps restart (replay) time bounded
    let core_ck = core.clone();
    std::thread::Builder::new()
        .name("checkpointer".into())
        .spawn(move || loop {
            let _bg = crate::metrics::background();
            std::thread::sleep(std::time::Duration::from_secs(5));
            if core_ck.store.wal_size() > 64 << 20 {
                match core_ck.store.checkpoint() {
                    Ok(()) => core_ck.events.emit("metadata_checkpoint", json!({})),
                    Err(e) => core_ck.events.emit("metadata_checkpoint_error", json!({"error": format!("{:#}", e)})),
                }
            }
        })
        .ok();
    std::thread::Builder::new()
        .name("compactor".into())
        .spawn(move || {
            let _bg = crate::metrics::background();
            // A background job can fail or panic (an unreachable metadata service panics the reading thread, see
            // meta.rs Remote::get). Neither may stop this worker (audit F1): each job runs under catch_unwind, and a
            // repository whose job failed is retried, by a full per-repository pass, until one succeeds.
            let run = |what: &str, f: &mut dyn FnMut() -> Result<()>| -> bool {
                match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    inject_job_panic(what);
                    f()
                })) {
                    Ok(Ok(())) => true,
                    Ok(Err(e)) => {
                        core.events.emit("compaction_error", json!({"job": what, "error": format!("{:#}", e)}));
                        false
                    }
                    Err(p) => {
                        let msg = p.downcast_ref::<String>().cloned().or_else(|| p.downcast_ref::<&str>().map(|s| s.to_string()));
                        core.events.emit("compaction_error", json!({"job": what, "panic": msg.unwrap_or_default()}));
                        false
                    }
                }
            };
            let mut retry: HashSet<String> = HashSet::new();
            // catch up on anything missed before a crash/restart
            // catch-up per repository, so only the ones that fail are retried (review V2)
            match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| list_repos(&core))) {
                Ok(repos) => {
                    for repo in repos {
                        if !run("catch-up", &mut || maintain_repo(&core, &repo).map(|_| ())) {
                            retry.insert(repo);
                        }
                    }
                }
                Err(_) => {
                    retry.insert(String::new()); // "" = list the repositories again, then all of them
                }
            }
            loop {
                let job = match rx.recv_timeout(std::time::Duration::from_secs(retry_secs())) {
                    Ok(j) => Some(j),
                    Err(mpsc::RecvTimeoutError::Timeout) => None,
                    Err(mpsc::RecvTimeoutError::Disconnected) => break,
                };
                let idle = job.is_none();
                if let Some(job) = job {
                    let (repo, ok) = match &job {
                        Job::Compact(repo, r, seq) => (repo.clone(), run("compact", &mut || compact_ref(&core, repo, r, *seq).map(|_| ()))),
                        Job::Twin(repo, p) => (
                            repo.clone(),
                            run("copy", &mut || {
                                let _g = maint_lock();
                                prepare_copy(&core, repo, p).map(|_| ())
                            }),
                        ),
                    };
                    if !ok {
                        retry.insert(repo);
                    }
                }
                if !retry.is_empty() && retry_due(idle) {
                    let pending: Vec<String> = retry.iter().cloned().collect();
                    for repo in pending {
                        let ok = if repo.is_empty() {
                            run("retry", &mut || maintain_all(&core).map(|_| ()))
                        } else {
                            run("retry", &mut || maintain_repo(&core, &repo).map(|_| ()))
                        };
                        if ok {
                            retry.remove(&repo);
                            core.events.emit("compaction_recovered", json!({"repo": repo}));
                        }
                    }
                }
            }
        })
        .ok();
}

/// Test-only (PANDO_TEST_PANIC_JOBS=n): the first n background jobs panic, as a metadata outage does, to test that
/// the worker survives and the work is retried. Never set in normal operation.
fn inject_job_panic(what: &str) {
    static LEFT: OnceLock<std::sync::atomic::AtomicI64> = OnceLock::new();
    let left = LEFT.get_or_init(|| {
        std::sync::atomic::AtomicI64::new(std::env::var("PANDO_TEST_PANIC_JOBS").ok().and_then(|v| v.parse().ok()).unwrap_or(0))
    });
    if what != "catch-up" && left.fetch_sub(1, std::sync::atomic::Ordering::SeqCst) > 0 {
        panic!("PANDO_TEST_PANIC_JOBS: injected panic in background job '{}'", what);
    }
}

/// Seconds between retries of failed background work (test override: PANDO_TEST_RETRY_SECS).
const RETRY_SECS: u64 = 30;

/// Retry after a quiet period (no job arrived for RETRY_SECS), or at most every RETRY_SECS while jobs keep coming.
fn retry_secs() -> u64 {
    std::env::var("PANDO_TEST_RETRY_SECS").ok().and_then(|v| v.parse().ok()).unwrap_or(RETRY_SECS)
}

fn retry_due(idle: bool) -> bool {
    static LAST: Mutex<Option<std::time::Instant>> = Mutex::new(None);
    let secs = retry_secs();
    let mut l = LAST.lock().unwrap_or_else(|e| e.into_inner());
    let due = idle || l.map(|t| t.elapsed().as_secs() >= secs).unwrap_or(true);
    if due {
        *l = Some(std::time::Instant::now());
    }
    due
}

/// MAINT_LOCK, tolerating poison: a panic in a previous holder (e.g. a metadata outage) must not disable
/// maintenance for the life of the process (audit F1). The state it protects is in the metadata store, written
/// transactionally and write-once, so a holder that died part-way leaves nothing inconsistent behind.
fn maint_lock() -> std::sync::MutexGuard<'static, ()> {
    MAINT_LOCK.lock().unwrap_or_else(|e| e.into_inner())
}

static MAINT_LOCK: Mutex<()> = Mutex::new(());

/// Run compaction to completion for every repository and ref.
pub fn maintain_all(core: &Core) -> Result<String> {
    // Every repository is attempted, each independently: one that fails (or panics, as on a metadata outage) does not
    // stop the others (review V2: a single broken repository blocked covers and copies for all later ones).
    let (mut built, mut twins) = (0u64, 0u64);
    let repos = list_repos(core);
    let mut failed: Vec<String> = Vec::new();
    for repo in &repos {
        match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| maintain_repo(core, repo))) {
            Ok(Ok((b, t))) => {
                built += b;
                twins += t;
            }
            Ok(Err(e)) => failed.push(format!("{}: {:#}", repo, e)),
            Err(_) => failed.push(format!("{}: panicked (metadata service unreachable?)", repo)),
        }
    }
    if !failed.is_empty() {
        bail!("maintenance failed for {} of {} repositories ({} covers built, {} copies prepared elsewhere): {}",
              failed.len(), repos.len(), built, twins, failed.join("; "));
    }
    Ok(format!("compaction complete: {} repositories, {} covers built, {} copies prepared", repos.len(), built, twins))
}

pub fn list_repos(core: &Core) -> Vec<String> {
    core.store.scan(&format!("repo{SEP}")).into_iter().map(|(k, _)| k[5..].to_string()).collect()
}

/// Every cover of every ref of `repo`, and the missing self-contained copies: (covers built, copies prepared).
pub fn maintain_repo(core: &Core, repo: &str) -> Result<(u64, u64)> {
    let p = k_ref_prefix(repo);
    let refs: Vec<(String, u64)> = core
        .store
        .scan(&p)
        .into_iter()
        .filter_map(|(k, v)| serde_json::from_slice::<RefPtr>(&v).ok().map(|ptr| (k[p.len()..].to_string(), ptr.seq)))
        .collect();
    let mut built = 0;
    for (r, seq) in refs {
        built += compact_ref(core, repo, &r, seq)?;
    }
    // self-contained copies of thin pushes are prepared in the background (D-13)
    let _g = maint_lock();
    Ok((built, prepare_missing_copies(core, repo)?))
}

/// Ensure all covers ending at or below `seq` exist for ref `r`.
pub fn compact_ref(core: &Core, repo: &str, r: &str, seq: u64) -> Result<u64> {
    let _g = maint_lock();
    let rs = core.repo(repo).ok_or_else(|| anyhow!("no repo"))?;
    let mut built = 0;
    for level in 1..=MAX_LEVEL {
        let span = FANOUT.pow(level as u32);
        let mut hi = span;
        while hi <= seq {
            if core.cover(&rs, r, level, hi).is_none() {
                build_cover(core, &rs, r, level, hi)?;
                built += 1;
            }
            hi += span;
        }
    }
    Ok(built)
}

fn build_cover(core: &Core, rs: &RepoState, r: &str, level: u8, hi: u64) -> Result<()> {
    let span = FANOUT.pow(level as u32);
    let lo = hi - span;
    let mut packs: Vec<String> = Vec::new();
    let mut deps: BTreeSet<RvId> = BTreeSet::new();
    let mut tips: Vec<String> = Vec::new();
    let mut linked = true;
    let mut linked_inner = true;
    // Every level is built from the member versions' L0 packs, not from the child covers:
    // a self-contained child holds the first object of every delta chain in full, and splicing
    // children would carry 4^(L-1) such full copies upward. From L0 members, deltas between
    // consecutive versions stay deltas and each cover holds one full copy per chain.
    if level >= 1 {
        for s in lo + 1..=hi {
            let v = core.rv(rs, r, s)?;
            if let Some(p) = &v.pack {
                if !packs.contains(p) {
                    packs.push(p.clone());
                }
            }
            for d in &v.deps {
                deps.insert(d.clone());
            }
            if v.tip != ZERO_HEX {
                tips.push(v.tip.clone());
            }
            linked &= v.linked;
            if s > lo + 1 {
                linked_inner &= v.linked;
            }
        }
    } else {
        let child_span = span / FANOUT;
        for k in (0..FANOUT).rev() {
            let chi = hi - k * child_span;
            let c = match core.cover(rs, r, level - 1, chi) {
                Some(c) => c,
                None => {
                    build_cover(core, rs, r, level - 1, chi)?;
                    core.cover(rs, r, level - 1, chi).ok_or_else(|| anyhow!("child cover missing"))?
                }
            };
            if !c.pack.is_empty() && !packs.contains(&c.pack) {
                packs.push(c.pack.clone());
            }
            for d in &c.deps {
                deps.insert(d.clone());
            }
            tips.extend(c.tips.iter().cloned());
            linked &= c.linked;
            linked_inner = false; // child-cover path is unused (level >= 1 always takes the L0 branch); stay conservative
        }
    }
    deps.retain(|d| !(d.r == r && d.s > lo && d.s <= hi));
    // write the cover pack (self-contained)
    let mut bytes = Vec::new();
    let (pack_id, objects) = if packs.is_empty() {
        (String::new(), 0)
    } else {
        let opts = OutOpts { ofs: true, thin: false, no_ref_delta: true };
        let none = |_: &Oid| false;
        {
            let mut w = |b: &[u8]| -> Result<()> {
                bytes.extend_from_slice(b);
                Ok(())
            };
            packout::write_spliced(&core.blobs, &rs.name, &packs, &HashSet::new(), &opts, &none, &mut w)?;
        }
        let tmp = core.blobs.tmp_path();
        let mut nolookup = |_: &Oid| -> Result<Option<(u8, Arc<Vec<u8>>, String)>> { Ok(None) };
        let mut res = ingest::ingest(std::io::Cursor::new(&bytes), &tmp, &mut nolookup)?;
        if crate::recompress::enabled() {
            // Re-delta the cover's objects (cross-path deltas splicing cannot create). Kept only if verified
            // and smaller; otherwise the spliced cover stands. See recompress.rs.
            let spliced_len = bytes.len();
            let out = crate::recompress::recompress(&bytes, &res.idx, &core.blobs.tmp_path());
            core.events.emit(
                "cover_recompress",
                json!({"repo": rs.name, "ref": r, "level": level, "lo": lo, "hi": hi, "spliced_bytes": spliced_len,
                       "recompressed_bytes": out.bytes.as_ref().map(|b| b.len()), "millis": out.millis as u64, "result": out.reason,
                       "window": crate::recompress::WINDOW, "depth": crate::recompress::DEPTH}),
            );
            if let Some(nb) = out.bytes {
                let _ = std::fs::remove_file(&res.tmp_path);
                let tmp2 = core.blobs.tmp_path();
                res = ingest::ingest(std::io::Cursor::new(&nb), &tmp2, &mut nolookup)?;
                bytes = nb;
            }
        }
        core.blobs.install(&rs.name, &res.tmp_path, &res.idx)?;
        // Substitution map (background work, counted as background metadata reads): for each whole object in
        // this cover, the L0 pack/entry storing it as a delta. The fetch path consults it instead of the global
        // object index, and still checks at fetch time that the delta's base is emitted or held by the client.
        let cover_id = res.idx.id.clone();
        let mut map: Vec<(Oid, String, u32)> = Vec::new();
        for e in &res.idx.entries {
            if !matches!(e.kind, T_COMMIT | T_TREE | T_BLOB | T_TAG) {
                continue;
            }
            if let Some((lp, lei, _)) = core.locate(rs, &e.oid)? {
                if lp != cover_id && matches!(core.blobs.index(&rs.name, &lp)?.entries[lei].kind, T_OFS | T_REF) {
                    map.push((e.oid, lp, lei as u32));
                }
            }
        }
        core.blobs.install_alt(&rs.name, &cover_id, &map)?;
        (res.idx.id.clone(), res.count as u64)
    };
    let rec = CoverRec {
        r: r.to_string(),
        level,
        lo,
        hi,
        pack: pack_id.clone(),
        deps: deps.into_iter().collect(),
        tips,
        linked,
        linked_inner: Some(linked_inner),
        objects,
        bytes: bytes.len() as u64,
    };
    loop {
        let mut tx = core.store.txn();
        let ck = k_cover(&rs.name, r, level, hi);
        if tx.get(&ck).is_some() {
            return Ok(());
        }
        tx.put(&ck, serde_json::to_vec(&rec)?);
        if !pack_id.is_empty() {
            let pk = k_pack(&rs.name, &pack_id);
            if tx.get(&pk).is_none() {
                let p = PackRec { id: pack_id.clone(), level, objects, bytes: bytes.len() as u64, thin: false, ref_delta: false, rvs: vec![], cover: Some(rec.clone()), twin_of: None };
                tx.put(&pk, serde_json::to_vec(&p)?);
            }
        }
        match tx.commit() {
            Ok(lsn) => {
                if !pack_id.is_empty() {
                    core.packs_derived.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                }
                core.events.emit(
                    "cover_committed",
                    json!({"repo": rs.name, "ref": r, "level": level, "lo": lo, "hi": hi, "pack": pack_id, "members": packs,
                           "deps": rec.deps, "linked": linked, "objects": objects, "bytes": bytes.len(), "lsn": lsn}),
                );
                return Ok(());
            }
            Err(e) if e.is::<Conflict>() => continue,
            Err(e) => return Err(e),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// docs/BACKGROUND-COPIES.md criterion 5: concurrent publication of one pack's copy is idempotent. A short
    /// simulator run leaves thin packs; their copies are then built by 4 threads at once without MAINT_LOCK.
    #[test]
    fn concurrent_copy_builds_publish_one_copy() {
        let dir = std::env::temp_dir().join(format!("pando-copy-race-{}", std::process::id()));
        // hold copies during the simulation so thin packs remain without one (no other test reads this variable)
        unsafe { std::env::set_var("PANDO_TEST_HOLD_COPIES", "1") };
        let rep = crate::sim::run(5, 300, 3, 2, 0.0, 0.0, Some(dir.clone())).unwrap();
        unsafe { std::env::remove_var("PANDO_TEST_HOLD_COPIES") };
        assert!(rep["violations"].as_array().unwrap().is_empty(), "{}", rep["violations"]);
        let store = crate::meta::Store::open(&dir.join("meta")).unwrap();
        let blobs = Arc::new(crate::blob::Blobs::new(&dir.join("blobs")).unwrap());
        let events = Arc::new(crate::events::Events::open(&dir.join("events2"), "t"));
        let core = Arc::new(Core::new(store, blobs, events));
        let repo = "sim";
        let p = k_pack_prefix(repo);
        let thin: Vec<String> = core
            .store
            .scan(&p)
            .into_iter()
            .filter_map(|(k, v)| serde_json::from_slice::<PackRec>(&v).ok().map(|r| (k, r)))
            .filter(|(_, r)| r.level == 0 && r.twin_of.is_none() && r.cover.is_none() && (r.thin || r.ref_delta))
            .map(|(k, _)| k[p.len()..].to_string())
            .filter(|id| core.store.get(&k_twin(repo, id)).is_none())
            .collect();
        assert!(!thin.is_empty(), "simulation left no thin pack without a copy");
        for pack in &thin {
            let ths: Vec<_> = (0..4)
                .map(|_| {
                    let (c, pk) = (core.clone(), pack.clone());
                    std::thread::spawn(move || build_twin(&c, repo, &pk).unwrap())
                })
                .collect();
            let won: usize = ths.into_iter().map(|t| t.join().unwrap() as usize).sum();
            assert_eq!(won, 1, "pack {}: {} publishers committed", pack, won);
            let t = String::from_utf8(core.store.get(&k_twin(repo, pack)).unwrap().to_vec()).unwrap();
            let idx = core.blobs.index(repo, &t).unwrap();
            assert!(!idx.is_thin() && !idx.has_ref_delta());
            let orig = core.blobs.index(repo, pack).unwrap();
            let a: std::collections::BTreeSet<_> = orig.entries.iter().map(|e| e.oid).collect();
            let b: std::collections::BTreeSet<_> = idx.entries.iter().map(|e| e.oid).collect();
            assert_eq!(a, b);
            let rec: PackRec = serde_json::from_slice(&core.store.get(&k_pack(repo, &t)).unwrap()).unwrap();
            assert_eq!(rec.twin_of.as_deref(), Some(pack.as_str()));
        }
        let _ = std::fs::remove_dir_all(&dir);
    }
}
