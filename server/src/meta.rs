//! Embedded metadata store: ordered key/value map with multi-key optimistic
//! transactions (per-key version validation = compare-and-swap), exact per-key
//! commit locks (only transactions touching the same key wait for each other) and
//! a write-ahead log with group commit. See DECISIONS.md D1 and MODEL.md.
use anyhow::{bail, Context, Result};
use std::collections::{BTreeMap, HashMap};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, RwLock};

/// Commit locks on exact keys: written keys exclusively, keys only read shared, all of a transaction's keys
/// acquired at once (so no lock order is needed). Replaced 4096 hash-striped locks, under which an unrelated
/// transaction waited for another whenever one of its keys shared a stripe with one of the other's (audit F12:
/// a push to another branch waited behind a stalled commit; see docs/AUDIT-NET-REMEDIATION.md).
struct KeyLocks {
    state: Mutex<LockState>,
    cv: Condvar,
}

#[derive(Default)]
struct LockState {
    held: HashMap<String, i64>, // -1 = exclusive, n > 0 = n shared holders
    /// Tickets of transactions waiting to write each key. A waiting writer blocks *later* shared requests for that
    /// key, so overlapping readers cannot starve it (review V4). Only later ones: otherwise two writers that each
    /// need the other's key shared would block each other through their registrations. The oldest waiter is
    /// blocked only by actual holders, and holders never wait, so every waiter eventually proceeds.
    waiting: HashMap<String, std::collections::BTreeSet<u64>>,
    next_ticket: u64,
}

struct KeyGuard<'a> {
    l: &'a KeyLocks,
    keys: Vec<(String, bool)>,
}

impl KeyLocks {
    fn new() -> KeyLocks {
        KeyLocks { state: Mutex::new(LockState::default()), cv: Condvar::new() }
    }

    fn acquire(&self, keys: Vec<(String, bool)>) -> KeyGuard<'_> {
        let mut st = self.state.lock().unwrap_or_else(|e| e.into_inner());
        st.next_ticket += 1;
        let me = st.next_ticket;
        let mut registered = false;
        loop {
            let free = keys.iter().all(|(k, excl)| {
                let earlier_writer = st.waiting.get(k).and_then(|t| t.iter().next()).map(|&t| t < me).unwrap_or(false);
                match st.held.get(k) {
                    None => *excl || !earlier_writer,
                    Some(&n) => !*excl && n > 0 && !earlier_writer,
                }
            });
            if free {
                for (k, excl) in &keys {
                    if *excl {
                        if registered {
                            if let Some(t) = st.waiting.get_mut(k) {
                                t.remove(&me);
                                if t.is_empty() {
                                    st.waiting.remove(k);
                                }
                            }
                        }
                        st.held.insert(k.clone(), -1);
                    } else {
                        *st.held.entry(k.clone()).or_insert(0) += 1;
                    }
                }
                return KeyGuard { l: self, keys };
            }
            if !registered {
                for (k, excl) in &keys {
                    if *excl {
                        st.waiting.entry(k.clone()).or_default().insert(me);
                    }
                }
                registered = true;
            }
            st = self.cv.wait(st).unwrap_or_else(|e| e.into_inner());
        }
    }
}

impl Drop for KeyGuard<'_> {
    fn drop(&mut self) {
        let mut st = self.l.state.lock().unwrap_or_else(|e| e.into_inner());
        for (k, excl) in &self.keys {
            let gone = match st.held.get_mut(k) {
                Some(n) if !*excl && *n > 1 => {
                    *n -= 1;
                    false
                }
                _ => true,
            };
            if gone {
                st.held.remove(k);
            }
        }
        drop(st);
        self.l.cv.notify_all();
    }
}

#[derive(Clone)]
struct Val {
    ver: u64,
    data: Arc<Vec<u8>>,
}

/// Reserved key of the snapshot record that carries the highest allocated LSN (never a live key).
const CHECKPOINT_LSN_KEY: &str = "\x00checkpoint-lsn";

pub struct Store {
    dir: PathBuf,
    data: RwLock<BTreeMap<String, Val>>,
    locks: KeyLocks,
    gate: RwLock<()>,
    wal: Mutex<WalBuf>,
    sync: Mutex<SyncState>,
    sync_cv: Condvar,
    file: Mutex<File>,
    pub reads: AtomicU64,
    pub keys_scanned: AtomicU64,
    pub writes: AtomicU64,
    pub commits: AtomicU64,
    pub conflicts: AtomicU64,
    /// fault injection: fail the commit at a given point (simulation)
    pub fail_hook: Mutex<Option<Arc<dyn Fn(&str) -> bool + Send + Sync>>>,
    generation: Mutex<u64>,
    /// Set when this process is a frontend of a shared metadata service (docs/DISTRIBUTED.md): every operation is
    /// forwarded; the local map stays empty.
    remote: Option<Remote>,
    /// Committed publication order (docs/INCREMENTAL-INDEX.md): `stable` = largest LSN such that every LSN <= it has
    /// been applied or abandoned; `done` holds finished LSNs above it.
    applied: Mutex<Applied>,
    /// In-memory change log of tracked keys (prefix `pack<SEP>`), LSN -> keys; rebuilt at open.
    changes: RwLock<BTreeMap<u64, Vec<String>>>,
}

struct Applied {
    stable: u64,
    done: std::collections::BTreeSet<u64>,
}

/// Keys whose writes are recorded in the change log.
pub const TRACKED_PREFIX: &str = "pack\x01";

fn tracked(k: &str) -> bool {
    k.starts_with(TRACKED_PREFIX)
}

struct WalBuf {
    buf: Vec<u8>,
    next: u64,
    size: u64,
}
struct SyncState {
    durable: u64,
    syncing: bool,
}

#[derive(Debug)]
pub struct Conflict;
impl std::fmt::Display for Conflict {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "transaction conflict")
    }
}
impl std::error::Error for Conflict {}

pub struct Txn<'a> {
    store: &'a Store,
    reads: Vec<(String, u64)>,
    writes: BTreeMap<String, Option<Vec<u8>>>,
}


impl Store {
    pub fn open(dir: &Path) -> Result<Arc<Store>> {
        fs::create_dir_all(dir)?;
        // The store directory's own entry must be durable in its parent before anything is acknowledged.
        if let Some(parent) = dir.parent() {
            fsync_dir(parent)?;
        }
        // find latest generation
        let mut generation = 0u64;
        for e in fs::read_dir(dir)? {
            let n = e?.file_name().to_string_lossy().to_string();
            if let Some(g) = n.strip_prefix("snapshot.") {
                if let Ok(g) = g.parse::<u64>() {
                    generation = generation.max(g);
                }
            }
        }
        let mut map = BTreeMap::new();
        let mut maxver = 0u64;
        if generation > 0 {
            let p = dir.join(format!("snapshot.{}", generation));
            let mut b = Vec::new();
            File::open(&p)?.read_to_end(&mut b)?;
            let recs = parse_records(&b, true).context("corrupt snapshot")?;
            for (ver, k, v) in recs.0 {
                maxver = maxver.max(ver);
                if let Some(v) = v {
                    map.insert(k, Val { ver, data: Arc::new(v) });
                }
            }
        }
        let walp = dir.join(format!("wal.{}", generation));
        let mut b = Vec::new();
        if walp.exists() {
            File::open(&walp)?.read_to_end(&mut b)?;
        }
        let (recs, good) = parse_records(&b, false)?;
        for (ver, k, v) in recs {
            maxver = maxver.max(ver);
            match v {
                Some(v) => {
                    map.insert(k, Val { ver, data: Arc::new(v) });
                }
                None => {
                    map.remove(&k);
                }
            }
        }
        let mut changes: BTreeMap<u64, Vec<String>> = BTreeMap::new();
        for (k, v) in map.iter() {
            if tracked(k) {
                changes.entry(v.ver).or_default().push(k.clone());
            }
        }
        let file = OpenOptions::new().create(true).append(false).write(true).read(true).open(&walp)?;
        if (good as u64) < b.len() as u64 {
            // torn tail from a crash: truncate to last complete record
            file.set_len(good as u64)?;
        }
        file.sync_all()?;
        let mut file = file;
        use std::io::Seek;
        file.seek(std::io::SeekFrom::Start(good as u64))?;
        fsync_dir(dir)?;
        Ok(Arc::new(Store {
            dir: dir.to_path_buf(),
            data: RwLock::new(map),
            locks: KeyLocks::new(),
            gate: RwLock::new(()),
            wal: Mutex::new(WalBuf { buf: Vec::new(), next: maxver, size: good as u64 }),
            sync: Mutex::new(SyncState { durable: maxver, syncing: false }),
            sync_cv: Condvar::new(),
            file: Mutex::new(file),
            reads: AtomicU64::new(0),
            keys_scanned: AtomicU64::new(0),
            writes: AtomicU64::new(0),
            commits: AtomicU64::new(0),
            conflicts: AtomicU64::new(0),
            fail_hook: Mutex::new(None),
            generation: Mutex::new(generation),
            remote: None,
            applied: Mutex::new(Applied { stable: maxver, done: Default::default() }),
            changes: RwLock::new(changes),
        }))
    }

    /// A frontend's handle on the shared metadata service at `addr`.
    pub fn connect(addr: &Addr) -> Result<Arc<Store>> {
        let r = Remote { addr: addr.clone(), pool: Mutex::new(Vec::new()) };
        // fail fast if the service is unreachable, or speaks a different handshake (e.g. it expects PANDO_META_SECRET)
        if r.call(&[b'P'])? != b"K" {
            bail!("metadata service did not answer the ping (handshake mismatch: is PANDO_META_SECRET set on both sides?)");
        }
        Ok(Arc::new(Store {
            dir: PathBuf::new(),
            data: RwLock::new(BTreeMap::new()),
            locks: KeyLocks::new(),
            gate: RwLock::new(()),
            wal: Mutex::new(WalBuf { buf: Vec::new(), next: 0, size: 0 }),
            sync: Mutex::new(SyncState { durable: 0, syncing: false }),
            sync_cv: Condvar::new(),
            file: Mutex::new(File::open(crate::sys::NULL_DEVICE)?),
            reads: AtomicU64::new(0),
            keys_scanned: AtomicU64::new(0),
            writes: AtomicU64::new(0),
            commits: AtomicU64::new(0),
            conflicts: AtomicU64::new(0),
            fail_hook: Mutex::new(None),
            generation: Mutex::new(0),
            remote: Some(r),
            applied: Mutex::new(Applied { stable: 0, done: Default::default() }),
            changes: RwLock::new(BTreeMap::new()),
        }))
    }

    pub fn is_remote(&self) -> bool {
        self.remote.is_some()
    }

    /// Read with version (service side of a remote `Txn::get`).
    fn get_ver(&self, k: &str) -> Option<(u64, Arc<Vec<u8>>)> {
        self.data.read().unwrap().get(k).map(|v| (v.ver, v.data.clone()))
    }

    /// Commit a transaction whose reads were made remotely (service side): the same validation and durability path
    /// as a local transaction.
    pub fn commit_external(&self, reads: Vec<(String, u64)>, writes: BTreeMap<String, Option<Vec<u8>>>) -> Result<u64> {
        self.commit(Txn { store: self, reads, writes })
    }

    pub fn txn(&self) -> Txn<'_> {
        Txn { store: self, reads: Vec::new(), writes: BTreeMap::new() }
    }

    /// Non-transactional read (counts as a metadata read).
    pub fn get(&self, k: &str) -> Option<Arc<Vec<u8>>> {
        self.reads.fetch_add(1, Ordering::Relaxed);
        crate::metrics::store_point_read();
        if let Some(r) = &self.remote {
            return r.get(k).map(|(_, v)| v);
        }
        self.data.read().unwrap().get(k).map(|v| v.data.clone())
    }

    /// Prefix scan; counts one metadata read per returned key.
    pub fn scan(&self, prefix: &str) -> Vec<(String, Arc<Vec<u8>>)> {
        let v: Vec<_> = if let Some(r) = &self.remote {
            r.scan(prefix)
        } else {
            self.scan_local(prefix)
        };
        // one range read operation (as in an ordered KV store); keys reported separately
        self.reads.fetch_add(1, Ordering::Relaxed);
        self.keys_scanned.fetch_add(v.len() as u64, Ordering::Relaxed);
        crate::metrics::store_scan(v.len() as u64);
        v
    }

    fn scan_local(&self, prefix: &str) -> Vec<(String, Arc<Vec<u8>>)> {
        let d = self.data.read().unwrap();
        d.range(prefix.to_string()..).take_while(|(k, _)| k.starts_with(prefix)).map(|(k, v)| (k.clone(), v.data.clone())).collect()
    }

    fn version(&self, k: &str) -> u64 {
        self.data.read().unwrap().get(k).map(|v| v.ver).unwrap_or(0)
    }

    fn hook(&self, point: &str) -> bool {
        // the closure is called after the mutex is released, so a hook that blocks one commit cannot block others
        let h = self.fail_hook.lock().unwrap().clone();
        if let Some(h) = h {
            return h(point);
        }
        false
    }

    pub fn commit(&self, tx: Txn<'_>) -> Result<u64> {
        if tx.writes.is_empty() {
            // read-only: validate nothing, nothing to log
            return Ok(0);
        }
        if let Some(r) = &self.remote {
            let n = tx.writes.len() as u64;
            let lsn = r.commit(&tx.reads, &tx.writes)?;
            self.writes.fetch_add(n, Ordering::Relaxed);
            crate::metrics::writes(n);
            self.commits.fetch_add(1, Ordering::Relaxed);
            return Ok(lsn);
        }
        let _g = self.gate.read().unwrap();
        let mut keys: Vec<(String, bool)> = tx.writes.keys().map(|k| (k.clone(), true)).collect();
        for (k, _) in &tx.reads {
            if !tx.writes.contains_key(k) {
                keys.push((k.clone(), false));
            }
        }
        keys.sort();
        keys.dedup_by(|a, b| a.0 == b.0);
        let _locks = self.locks.acquire(keys);
        // Test-only (verify/dist D2): stall a commit that writes a key containing PANDO_TEST_PAUSE_KEY while it holds
        // its key locks, to show unrelated commits proceed. Never set in normal operation.
        if let Ok(pk) = std::env::var("PANDO_TEST_PAUSE_KEY") {
            if !pk.is_empty() && tx.writes.keys().any(|k| k.contains(&pk)) {
                std::thread::sleep(std::time::Duration::from_millis(3000));
            }
        }
        for (k, ver) in &tx.reads {
            if self.version(k) != *ver {
                self.conflicts.fetch_add(1, Ordering::Relaxed);
                return Err(Conflict.into());
            }
        }
        if self.hook("before_wal") {
            bail!("injected fault before wal");
        }
        // append to WAL buffer
        let lsn;
        {
            let mut w = self.wal.lock().unwrap();
            w.next += 1;
            lsn = w.next;
            let mut payload = Vec::new();
            for (k, v) in &tx.writes {
                encode_rec(&mut payload, lsn, k, v.as_deref());
            }
            let crc = crc32fast::hash(&payload);
            w.buf.extend_from_slice(&(payload.len() as u32).to_be_bytes());
            w.buf.extend_from_slice(&crc.to_be_bytes());
            w.buf.extend_from_slice(&payload);
        }
        self.wait_durable(lsn)?;
        if self.hook("after_wal") {
            self.finish_lsn(lsn); // abandoned in this process (recovery replays it)
            bail!("injected fault after wal (durable but not applied)");
        }
        // test hook point: a commit held between durability and application (I1); never fires in normal operation
        let _ = self.hook("before_apply");
        {
            let mut d = self.data.write().unwrap();
            let mut ch = self.changes.write().unwrap();
            for (k, v) in tx.writes.iter() {
                match v {
                    Some(v) => {
                        d.insert(k.clone(), Val { ver: lsn, data: Arc::new(v.clone()) });
                    }
                    None => {
                        d.remove(k);
                    }
                }
                if tracked(k) {
                    ch.entry(lsn).or_default().push(k.clone());
                }
            }
        }
        self.finish_lsn(lsn);
        self.writes.fetch_add(tx.writes.len() as u64, Ordering::Relaxed);
        crate::metrics::writes(tx.writes.len() as u64);
        self.commits.fetch_add(1, Ordering::Relaxed);
        Ok(lsn)
    }

    fn wait_durable(&self, lsn: u64) -> Result<()> {
        let mut s = self.sync.lock().unwrap();
        loop {
            if s.durable >= lsn {
                return Ok(());
            }
            if !s.syncing {
                s.syncing = true;
                drop(s);
                let (buf, upto) = {
                    let mut w = self.wal.lock().unwrap();
                    let b = std::mem::take(&mut w.buf);
                    w.size += b.len() as u64;
                    (b, w.next)
                };
                let r = (|| -> Result<()> {
                    let mut f = self.file.lock().unwrap();
                    f.write_all(&buf)?;
                    f.sync_data()?;
                    Ok(())
                })();
                if let Err(e) = r {
                    // A failed WAL write/fsync leaves the log in an unknown state
                    // (the record may or may not be durable). Continuing could let
                    // memory diverge from disk, so crash and let recovery decide.
                    eprintln!("pando: fatal WAL write/fsync error: {:#}; aborting", e);
                    std::process::abort();
                }
                s = self.sync.lock().unwrap();
                s.syncing = false;
                s.durable = s.durable.max(upto);
                self.sync_cv.notify_all();
            } else {
                s = self.sync_cv.wait(s).unwrap();
            }
        }
    }

    fn finish_lsn(&self, lsn: u64) {
        let mut a = self.applied.lock().unwrap();
        a.done.insert(lsn);
        loop {
            let next = a.stable + 1;
            if !a.done.remove(&next) {
                break;
            }
            a.stable = next;
        }
    }

    /// Largest LSN below which every commit is visible (committed publication order).
    pub fn stable_lsn(&self) -> u64 {
        match &self.remote {
            Some(r) => r.changes_since("\x02", u64::MAX).0, // empty prefix range: just the stable LSN
            None => self.applied.lock().unwrap().stable,
        }
    }

    /// Tracked keys under `prefix` written at LSNs above h (all applied ones), with their current values; and the
    /// stable LSN, the only safe new high-water mark. Work: the change-log entries above h, not the size of the store.
    pub fn changes_since(&self, prefix: &str, h: u64) -> (u64, Vec<(u64, String, Arc<Vec<u8>>)>) {
        let r = match &self.remote {
            Some(r) => r.changes_since(prefix, h),
            None => self.changes_local(prefix, h),
        };
        self.reads.fetch_add(1, Ordering::Relaxed);
        self.keys_scanned.fetch_add(r.1.len() as u64, Ordering::Relaxed);
        crate::metrics::store_scan(r.1.len() as u64);
        r
    }

    fn changes_local(&self, prefix: &str, h: u64) -> (u64, Vec<(u64, String, Arc<Vec<u8>>)>) {
        // Returned: every applied entry above h, including any above `stable` (applied ahead of an earlier LSN still
        // in flight), so an acknowledged pack is never invisible; the caller advances its mark only to `stable`, so
        // the earlier LSN is still returned once applied. Entries above stable are returned again later (idempotent).
        let s = self.applied.lock().unwrap().stable;
        if h == u64::MAX {
            return (s, Vec::new());
        }
        // lock order everywhere: data, then changes (commit takes them in the same order)
        let d = self.data.read().unwrap();
        let ch = self.changes.read().unwrap();
        let mut out = Vec::new();
        for (lsn, keys) in ch.range(h + 1..) {
            for k in keys {
                if k.starts_with(prefix) {
                    if let Some(v) = d.get(k) {
                        out.push((*lsn, k.clone(), v.data.clone()));
                    }
                }
            }
        }
        (s, out)
    }

    pub fn wal_size(&self) -> u64 {
        if self.remote.is_some() {
            return 0; // the service checkpoints its own log
        }
        self.wal.lock().unwrap().size
    }

    /// Write a snapshot and start a new WAL generation. Blocks commits briefly.
    pub fn checkpoint(&self) -> Result<()> {
        if self.remote.is_some() {
            return Ok(());
        }
        let _g = self.gate.write().unwrap();
        let mut cur_gen = self.generation.lock().unwrap();
        let ng = *cur_gen + 1;
        let snap = self.dir.join(format!("snapshot.{}", ng));
        let tmp = self.dir.join(format!("snapshot.{}.tmp", ng));
        {
            let d = self.data.read().unwrap();
            let mut buf = Vec::new();
            for (k, v) in d.iter() {
                encode_rec(&mut buf, v.ver, k, Some(&v.data));
            }
            // The highest LSN allocated so far (no commit is in flight under the gate), as a tombstone under a reserved
            // key: `open` takes the next LSN from the largest version it reads, and live keys alone can be older when
            // the newest commits were deletes; LSNs would then repeat (audit F7: stale reader marks, OCC versions).
            let last = self.wal.lock().unwrap().next; // `next` holds the last allocated LSN (pre-incremented in commit)
            encode_rec(&mut buf, last, CHECKPOINT_LSN_KEY, None);
            let mut f = File::create(&tmp)?;
            let crc = crc32fast::hash(&buf);
            f.write_all(&(buf.len() as u64).to_be_bytes())?;
            f.write_all(&crc.to_be_bytes())?;
            f.write_all(&buf)?;
            f.sync_all()?;
        }
        let nwal = self.dir.join(format!("wal.{}", ng));
        let nf = OpenOptions::new().create(true).write(true).read(true).truncate(true).open(&nwal)?;
        nf.sync_all()?;
        fs::rename(&tmp, &snap)?;
        fsync_dir(&self.dir)?;
        *self.file.lock().unwrap() = nf;
        self.wal.lock().unwrap().size = 0;
        let _ = fs::remove_file(self.dir.join(format!("wal.{}", *cur_gen)));
        let _ = fs::remove_file(self.dir.join(format!("snapshot.{}", *cur_gen)));
        *cur_gen = ng;
        Ok(())
    }
}

impl<'a> Txn<'a> {
    /// Read a key and record its version for commit-time validation.
    pub fn get(&mut self, k: &str) -> Option<Arc<Vec<u8>>> {
        if let Some(w) = self.writes.get(k) {
            return w.clone().map(Arc::new);
        }
        self.store.reads.fetch_add(1, Ordering::Relaxed);
        crate::metrics::store_point_read();
        let (ver, val) = match &self.store.remote {
            Some(r) => match r.get(k) {
                Some((ver, v)) => (ver, Some(v)),
                None => (0, None),
            },
            None => match self.store.data.read().unwrap().get(k) {
                Some(v) => (v.ver, Some(v.data.clone())),
                None => (0, None),
            },
        };
        self.reads.push((k.to_string(), ver));
        val
    }
    pub fn put(&mut self, k: &str, v: Vec<u8>) {
        self.writes.insert(k.to_string(), Some(v));
    }
    pub fn delete(&mut self, k: &str) {
        self.writes.insert(k.to_string(), None);
    }
    pub fn commit(self) -> Result<u64> {
        let s = self.store;
        s.commit(self)
    }
}

fn encode_rec(buf: &mut Vec<u8>, ver: u64, k: &str, v: Option<&[u8]>) {
    buf.extend_from_slice(&ver.to_be_bytes());
    buf.extend_from_slice(&(k.len() as u32).to_be_bytes());
    buf.extend_from_slice(k.as_bytes());
    match v {
        Some(v) => {
            buf.push(1);
            buf.extend_from_slice(&(v.len() as u32).to_be_bytes());
            buf.extend_from_slice(v);
        }
        None => buf.push(0),
    }
}

type Recs = Vec<(u64, String, Option<Vec<u8>>)>;

fn decode_payload(p: &[u8], out: &mut Recs) -> Result<()> {
    let mut i = 0;
    while i < p.len() {
        if i + 12 > p.len() {
            bail!("bad record");
        }
        let ver = u64::from_be_bytes(p[i..i + 8].try_into().unwrap());
        let kl = u32::from_be_bytes(p[i + 8..i + 12].try_into().unwrap()) as usize;
        i += 12;
        if i + kl + 1 > p.len() {
            bail!("bad record");
        }
        let k = String::from_utf8(p[i..i + kl].to_vec())?;
        i += kl;
        let tag = p[i];
        i += 1;
        if tag == 1 {
            if i + 4 > p.len() {
                bail!("bad record");
            }
            let vl = u32::from_be_bytes(p[i..i + 4].try_into().unwrap()) as usize;
            i += 4;
            if i + vl > p.len() {
                bail!("bad record");
            }
            out.push((ver, k, Some(p[i..i + vl].to_vec())));
            i += vl;
        } else {
            out.push((ver, k, None));
        }
    }
    Ok(())
}

/// Returns records and the byte length of the valid prefix.
fn parse_records(b: &[u8], snapshot: bool) -> Result<(Recs, usize)> {
    let mut out = Vec::new();
    if snapshot {
        if b.len() < 12 {
            bail!("short snapshot");
        }
        let n = u64::from_be_bytes(b[..8].try_into().unwrap()) as usize;
        let crc = u32::from_be_bytes(b[8..12].try_into().unwrap());
        if 12 + n > b.len() || crc32fast::hash(&b[12..12 + n]) != crc {
            bail!("snapshot checksum");
        }
        decode_payload(&b[12..12 + n], &mut out)?;
        return Ok((out, b.len()));
    }
    let mut i = 0;
    while i + 8 <= b.len() {
        let n = u32::from_be_bytes(b[i..i + 4].try_into().unwrap()) as usize;
        let crc = u32::from_be_bytes(b[i + 4..i + 8].try_into().unwrap());
        if i + 8 + n > b.len() {
            break;
        }
        let p = &b[i + 8..i + 8 + n];
        if crc32fast::hash(p) != crc {
            break;
        }
        let mut tmp = Vec::new();
        if decode_payload(p, &mut tmp).is_err() {
            break;
        }
        out.extend(tmp);
        i += 8 + n;
    }
    Ok((out, i))
}

/// Make the entries of directory `p` durable. On Apple platforms Rust's `sync_all` is `F_FULLFSYNC` (a full
/// device-cache flush, verified on APFS for directory descriptors). Errors are returned, never ignored
/// (CG7 audit B D-5): a directory whose entries cannot be made durable must not back an acknowledgement.
pub fn fsync_dir(p: &Path) -> std::io::Result<()> {
    crate::sys::fsync_dir(p)
}

// ---------------------------------------------------------------------------------------------------------------
// Shared metadata service (docs/DISTRIBUTED.md). Frames: u32 length + payload, over a Unix domain socket.
//   'P'                              -> 'K'                               (ping)
//   'G' key                          -> 0 | 1 ver:u64 val                 (point read with version)
//   'S' prefix                       -> n:u32 (key val)*                  (prefix scan)
//   'C' nr:u32 (key ver:u64)* nw:u32 (key tag[0|1] [val])*  -> 0 lsn:u64 | 1 (conflict) | 2 msg
//   'Q' prefix h:u64                 -> stable:u64 n:u32 (lsn:u64 key val)*  (change log, docs/INCREMENTAL-INDEX.md)
// Strings and values are u32 length + bytes. A commit reply is sent only after the WAL record is durable.

fn put_bytes(b: &mut Vec<u8>, s: &[u8]) {
    b.extend_from_slice(&(s.len() as u32).to_be_bytes());
    b.extend_from_slice(s);
}

struct Rd<'a>(&'a [u8], usize);
impl<'a> Rd<'a> {
    fn u8(&mut self) -> Result<u8> {
        let v = *self.0.get(self.1).ok_or_else(|| anyhow::anyhow!("short frame"))?;
        self.1 += 1;
        Ok(v)
    }
    fn u32(&mut self) -> Result<u32> {
        let s = self.0.get(self.1..self.1 + 4).ok_or_else(|| anyhow::anyhow!("short frame"))?;
        self.1 += 4;
        Ok(u32::from_be_bytes(s.try_into().unwrap()))
    }
    fn u64(&mut self) -> Result<u64> {
        let s = self.0.get(self.1..self.1 + 8).ok_or_else(|| anyhow::anyhow!("short frame"))?;
        self.1 += 8;
        Ok(u64::from_be_bytes(s.try_into().unwrap()))
    }
    fn bytes(&mut self) -> Result<Vec<u8>> {
        let n = self.u32()? as usize;
        let s = self.0.get(self.1..self.1 + n).ok_or_else(|| anyhow::anyhow!("short frame"))?;
        self.1 += n;
        Ok(s.to_vec())
    }
    fn string(&mut self) -> Result<String> {
        Ok(String::from_utf8(self.bytes()?)?)
    }
}

fn write_frame(w: &mut impl Write, p: &[u8]) -> std::io::Result<()> {
    let mut b = Vec::with_capacity(p.len() + 4);
    b.extend_from_slice(&(p.len() as u32).to_be_bytes());
    b.extend_from_slice(p);
    w.write_all(&b)
}

fn read_frame(r: &mut impl Read) -> std::io::Result<Vec<u8>> {
    let mut n = [0u8; 4];
    r.read_exact(&mut n)?;
    let mut b = vec![0u8; u32::from_be_bytes(n) as usize];
    r.read_exact(&mut b)?;
    Ok(b)
}

/// Where the metadata service listens: a Unix socket path, or `tcp:HOST:PORT` (docs/NETWORK-STORAGE.md; no
/// authentication or TLS: trusted local network only).
#[derive(Clone, Debug)]
pub enum Addr {
    Unix(PathBuf),
    Tcp(String),
}

impl Addr {
    pub fn parse(s: &str) -> Addr {
        match s.strip_prefix("tcp:") {
            Some(a) => Addr::Tcp(a.to_string()),
            None => Addr::Unix(PathBuf::from(s.strip_prefix("unix:").unwrap_or(s))),
        }
    }
}

enum Conn {
    #[cfg(unix)]
    Unix(std::os::unix::net::UnixStream),
    Tcp(std::net::TcpStream),
}
impl Read for Conn {
    fn read(&mut self, b: &mut [u8]) -> std::io::Result<usize> {
        match self {
            #[cfg(unix)]
            Conn::Unix(c) => c.read(b),
            Conn::Tcp(c) => c.read(b),
        }
    }
}
impl Write for Conn {
    fn write(&mut self, b: &[u8]) -> std::io::Result<usize> {
        match self {
            #[cfg(unix)]
            Conn::Unix(c) => c.write(b),
            Conn::Tcp(c) => c.write(b),
        }
    }
    fn flush(&mut self) -> std::io::Result<()> {
        match self {
            #[cfg(unix)]
            Conn::Unix(c) => c.flush(),
            Conn::Tcp(c) => c.flush(),
        }
    }
}

struct Remote {
    addr: Addr,
    pool: Mutex<Vec<Conn>>,
}

/// Raised when the service could not be reached or the connection broke mid-request. For a commit the outcome is
/// then unknown: the caller reports failure and never acknowledges (as for a crash between commit and reply).
#[derive(Debug)]
pub struct Unavailable(pub String);
impl std::fmt::Display for Unavailable {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "metadata service unavailable: {}", self.0)
    }
}
impl std::error::Error for Unavailable {}

pub fn env_secs(k: &str, default: u64) -> u64 {
    std::env::var(k).ok().and_then(|v| v.parse().ok()).filter(|&v| v > 0).unwrap_or(default)
}

/// TCP connect with a timeout, trying each resolved address in turn.
pub fn connect_timeout(addr: &str, t: std::time::Duration) -> std::io::Result<std::net::TcpStream> {
    use std::net::ToSocketAddrs;
    let mut last = std::io::Error::new(std::io::ErrorKind::NotFound, format!("{}: no address", addr));
    for a in addr.to_socket_addrs()? {
        match std::net::TcpStream::connect_timeout(&a, t) {
            Ok(c) => return Ok(c),
            Err(e) => last = e,
        }
    }
    Err(last)
}

impl Remote {
    fn conn(&self) -> std::io::Result<Conn> {
        if let Some(c) = self.pool.lock().unwrap().pop() {
            return Ok(c);
        }
        // Bounded waits (audit F6): a stalled service (alive, not answering) used to hang requests indefinitely.
        // Reads are then retried within the 10 s window; a commit whose reply times out reports "outcome unknown".
        let io = Some(std::time::Duration::from_secs(env_secs("PANDO_META_TIMEOUT", 20)));
        let mut c = match &self.addr {
            #[cfg(windows)]
            Addr::Unix(p) => {
                return Err(std::io::Error::new(std::io::ErrorKind::Unsupported,
                    format!("metadata service {}: Unix sockets are not supported on Windows; use tcp:HOST:PORT", p.display())));
            }
            #[cfg(unix)]
            Addr::Unix(p) => {
                let c = std::os::unix::net::UnixStream::connect(p)?;
                c.set_read_timeout(io)?;
                c.set_write_timeout(io)?;
                Conn::Unix(c)
            }
            Addr::Tcp(a) => {
                let c = connect_timeout(a, std::time::Duration::from_secs(3))?;
                c.set_nodelay(true)?;
                c.set_read_timeout(io)?;
                c.set_write_timeout(io)?;
                Conn::Tcp(c)
            }
        };
        if let Some(secret) = meta_secret() {
            // challenge-response (docs/FLY-DEPLOYMENT-PLAN.md G3): answer the service's nonce with HMAC(secret, nonce)
            let nonce = read_frame(&mut c)?;
            write_frame(&mut c, &crate::s3::hmac_sha256(&secret, &nonce))?;
            if read_frame(&mut c)? != b"K" {
                return Err(std::io::Error::new(std::io::ErrorKind::PermissionDenied, "metadata service rejected the handshake"));
            }
        }
        Ok(c)
    }

    fn call_once(&self, req: &[u8]) -> std::io::Result<Vec<u8>> {
        let mut c = self.conn()?;
        write_frame(&mut c, req)?;
        let r = read_frame(&mut c)?;
        self.pool.lock().unwrap().push(c);
        Ok(r)
    }

    /// Reads are idempotent: retried on a fresh connection for up to 10 s (service restart, D8).
    fn call(&self, req: &[u8]) -> Result<Vec<u8>> {
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
        loop {
            match self.call_once(req) {
                Ok(r) => return Ok(r),
                Err(e) => {
                    self.pool.lock().unwrap().clear();
                    if std::time::Instant::now() > deadline {
                        return Err(Unavailable(e.to_string()).into());
                    }
                    std::thread::sleep(std::time::Duration::from_millis(50));
                }
            }
        }
    }

    /// Point reads are infallible in the Store API; an unreachable service after the retry window aborts the request
    /// thread (panic -> request error), never a silent "absent".
    fn get(&self, k: &str) -> Option<(u64, Arc<Vec<u8>>)> {
        let mut q = vec![b'G'];
        put_bytes(&mut q, k.as_bytes());
        let r = self.call(&q).unwrap_or_else(|e| panic!("{:#}", e));
        let mut rd = Rd(&r, 0);
        match rd.u8().unwrap() {
            0 => None,
            _ => {
                let ver = rd.u64().unwrap();
                Some((ver, Arc::new(rd.bytes().unwrap())))
            }
        }
    }

    fn scan(&self, prefix: &str) -> Vec<(String, Arc<Vec<u8>>)> {
        let mut q = vec![b'S'];
        put_bytes(&mut q, prefix.as_bytes());
        let r = self.call(&q).unwrap_or_else(|e| panic!("{:#}", e));
        let mut rd = Rd(&r, 0);
        let n = rd.u32().unwrap();
        (0..n).map(|_| (rd.string().unwrap(), Arc::new(rd.bytes().unwrap()))).collect()
    }

    fn changes_since(&self, prefix: &str, h: u64) -> (u64, Vec<(u64, String, Arc<Vec<u8>>)>) {
        let mut q = vec![b'Q'];
        put_bytes(&mut q, prefix.as_bytes());
        q.extend_from_slice(&h.to_be_bytes());
        let r = self.call(&q).unwrap_or_else(|e| panic!("{:#}", e));
        let mut rd = Rd(&r, 0);
        let s = rd.u64().unwrap();
        let n = rd.u32().unwrap();
        (s, (0..n).map(|_| (rd.u64().unwrap(), rd.string().unwrap(), Arc::new(rd.bytes().unwrap()))).collect())
    }

    /// Not retried after the request may have been sent: the outcome would be unknown.
    fn commit(&self, reads: &[(String, u64)], writes: &BTreeMap<String, Option<Vec<u8>>>) -> Result<u64> {
        let mut q = vec![b'C'];
        q.extend_from_slice(&(reads.len() as u32).to_be_bytes());
        for (k, v) in reads {
            put_bytes(&mut q, k.as_bytes());
            q.extend_from_slice(&v.to_be_bytes());
        }
        q.extend_from_slice(&(writes.len() as u32).to_be_bytes());
        for (k, v) in writes {
            put_bytes(&mut q, k.as_bytes());
            match v {
                Some(v) => {
                    q.push(1);
                    put_bytes(&mut q, v);
                }
                None => q.push(0),
            }
        }
        let mut c = match self.conn() {
            Ok(c) => c,
            Err(e) => return Err(Unavailable(e.to_string()).into()),
        };
        let r = (|| -> std::io::Result<Vec<u8>> {
            write_frame(&mut c, &q)?;
            read_frame(&mut c)
        })();
        let r = match r {
            Ok(r) => {
                self.pool.lock().unwrap().push(c);
                r
            }
            Err(e) => {
                self.pool.lock().unwrap().clear();
                return Err(Unavailable(format!("commit outcome unknown: {}", e)).into());
            }
        };
        let mut rd = Rd(&r, 0);
        match rd.u8()? {
            0 => rd.u64(),
            1 => Err(Conflict.into()),
            _ => bail!("metadata service: {}", rd.string()?),
        }
    }
}

/// `PANDO_META_SECRET`, shared by the metadata service and its frontends. When set, every connection must complete a
/// challenge-response handshake before any request is served. Without it there is no authentication (trusted local
/// network only).
fn meta_secret() -> Option<Vec<u8>> {
    std::env::var("PANDO_META_SECRET").ok().filter(|v| !v.is_empty()).map(|v| v.into_bytes())
}

fn serve_conn(store: Arc<Store>, mut c: Conn) -> Result<()> {
    if let Some(secret) = meta_secret() {
        let mut nonce = [0u8; 32];
        crate::sys::random_bytes(&mut nonce)?;
        write_frame(&mut c, &nonce)?;
        let resp = read_frame(&mut c)?;
        if !crate::auth::ct_eq(&resp, &crate::s3::hmac_sha256(&secret, &nonce)) {
            let _ = write_frame(&mut c, b"N");
            bail!("handshake failed");
        }
        write_frame(&mut c, b"K")?;
    }
    loop {
        let req = match read_frame(&mut c) {
            Ok(r) => r,
            Err(_) => return Ok(()), // client closed
        };
        let mut rd = Rd(&req, 0);
        let mut out = Vec::new();
        match rd.u8()? {
            b'P' => out.push(b'K'),
            b'G' => {
                let k = rd.string()?;
                store.reads.fetch_add(1, Ordering::Relaxed);
                match store.get_ver(&k) {
                    None => out.push(0),
                    Some((ver, v)) => {
                        out.push(1);
                        out.extend_from_slice(&ver.to_be_bytes());
                        put_bytes(&mut out, &v);
                    }
                }
            }
            b'S' => {
                let p = rd.string()?;
                let v = store.scan_local(&p);
                store.reads.fetch_add(1, Ordering::Relaxed);
                store.keys_scanned.fetch_add(v.len() as u64, Ordering::Relaxed);
                out.extend_from_slice(&(v.len() as u32).to_be_bytes());
                for (k, val) in v {
                    put_bytes(&mut out, k.as_bytes());
                    put_bytes(&mut out, &val);
                }
            }
            b'Q' => {
                let p = rd.string()?;
                let h = rd.u64()?;
                let (st, v) = store.changes_local(&p, h);
                store.reads.fetch_add(1, Ordering::Relaxed);
                store.keys_scanned.fetch_add(v.len() as u64, Ordering::Relaxed);
                out.extend_from_slice(&st.to_be_bytes());
                out.extend_from_slice(&(v.len() as u32).to_be_bytes());
                for (lsn, k, val) in v {
                    out.extend_from_slice(&lsn.to_be_bytes());
                    put_bytes(&mut out, k.as_bytes());
                    put_bytes(&mut out, &val);
                }
            }
            b'C' => {
                let nr = rd.u32()?;
                let mut reads = Vec::with_capacity(nr as usize);
                for _ in 0..nr {
                    let k = rd.string()?;
                    reads.push((k, rd.u64()?));
                }
                let nw = rd.u32()?;
                let mut writes = BTreeMap::new();
                for _ in 0..nw {
                    let k = rd.string()?;
                    let v = if rd.u8()? == 1 { Some(rd.bytes()?) } else { None };
                    writes.insert(k, v);
                }
                match store.commit_external(reads, writes) {
                    Ok(lsn) => {
                        out.push(0);
                        out.extend_from_slice(&lsn.to_be_bytes());
                    }
                    Err(e) if e.is::<Conflict>() => out.push(1),
                    Err(e) => {
                        out.push(2);
                        put_bytes(&mut out, format!("{:#}", e).as_bytes());
                    }
                }
            }
            x => bail!("unknown request {}", x),
        }
        write_frame(&mut c, &out)?;
    }
}

/// `pando meta-serve --dir D (--sock S | --listen tcp:HOST:PORT)`: the shared metadata service. Holds an exclusive
/// lock on D, checkpoints its own WAL, serves each connection on its own thread.
pub fn serve(dir: &Path, addr: &Addr) -> Result<()> {
    fs::create_dir_all(dir)?;
    let lf = OpenOptions::new().create(true).write(true).truncate(false).open(dir.join("LOCK"))?;
    if !crate::sys::try_lock_exclusive(&lf) {
        bail!("metadata directory is locked by another service");
    }
    let store = Store::open(dir)?;
    let ck = store.clone();
    std::thread::spawn(move || loop {
        std::thread::sleep(std::time::Duration::from_secs(5));
        if ck.wal_size() > 64 << 20 {
            if let Err(e) = ck.checkpoint() {
                eprintln!("pando meta-serve: checkpoint error {:#}", e);
            }
        }
    });
    let spawn = |st: Arc<Store>, c: Conn| {
        std::thread::spawn(move || {
            if let Err(e) = serve_conn(st, c) {
                eprintln!("pando meta-serve: connection error {:#}", e);
            }
        });
    };
    match addr {
        #[cfg(windows)]
        Addr::Unix(sock) => bail!("{}: Unix sockets are not supported on Windows; use --listen tcp:HOST:PORT", sock.display()),
        #[cfg(unix)]
        Addr::Unix(sock) => {
            let _ = fs::remove_file(sock);
            let l = std::os::unix::net::UnixListener::bind(sock)?;
            eprintln!("pando meta-serve: {} on {}", dir.display(), sock.display());
            for c in l.incoming() {
                spawn(store.clone(), Conn::Unix(c?));
            }
        }
        Addr::Tcp(a) => {
            let l = std::net::TcpListener::bind(a)?;
            eprintln!("pando meta-serve: {} on tcp:{}", dir.display(), l.local_addr()?);
            for c in l.incoming() {
                let c = c?;
                c.set_nodelay(true)?;
                spawn(store.clone(), Conn::Tcp(c));
            }
        }
    }
    drop(lf);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeSet;
    use std::sync::atomic::AtomicBool;

    fn tmpdir(n: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("pando-meta-{}-{}-{}", n, std::process::id(), crate::repo::now_ms()));
        let _ = fs::remove_dir_all(&d);
        d
    }
    fn put(s: &Store, k: &str) -> u64 {
        let mut t = s.txn();
        t.put(k, k.as_bytes().to_vec());
        t.commit().unwrap()
    }
    const P: &str = "pack\x01r\x01";

    /// I1: a commit held between durability and application. A later LSN applies first. The stable LSN must not pass
    /// the held one; the later record is still returned (never invisible); after release the held record is returned.
    #[test]
    fn held_commit_is_not_skipped_by_the_high_water_mark() {
        let d = tmpdir("held");
        let s = Store::open(&d).unwrap();
        let at_hook = Arc::new(AtomicBool::new(false));
        let release = Arc::new(AtomicBool::new(false));
        {
            let (a, r) = (at_hook.clone(), release.clone());
            *s.fail_hook.lock().unwrap() = Some(Arc::new(move |p: &str| {
                if p == "before_apply" && std::thread::current().name() == Some("held") {
                    a.store(true, Ordering::SeqCst);
                    while !r.load(Ordering::SeqCst) {
                        std::thread::sleep(std::time::Duration::from_millis(1));
                    }
                }
                false
            }));
        }
        let s2 = s.clone();
        let th = std::thread::Builder::new().name("held".into()).spawn(move || put(&s2, &format!("{P}a"))).unwrap();
        while !at_hook.load(Ordering::SeqCst) {
            std::thread::sleep(std::time::Duration::from_millis(1));
        }
        let lb = put(&s, &format!("{P}b"));
        let (st, recs) = s.changes_since(P, 0);
        let keys: BTreeSet<String> = recs.iter().map(|r| r.1.clone()).collect();
        assert!(st < lb, "stable {} advanced past the held commit (b = {})", st, lb);
        assert!(keys.contains(&format!("{P}b")), "an applied record was not returned");
        assert!(!keys.contains(&format!("{P}a")));
        release.store(true, Ordering::SeqCst);
        let la = th.join().unwrap();
        assert!(la < lb);
        let (st2, recs2) = s.changes_since(P, st);
        let keys2: BTreeSet<String> = recs2.iter().map(|r| r.1.clone()).collect();
        assert!(keys2.contains(&format!("{P}a")), "held record missed after release");
        assert_eq!(st2, lb);
        let _ = fs::remove_dir_all(&d);
    }

    /// I1 randomised: 8 committers with random apply delays; a reader refreshing from its mark sees every key. A naive
    /// reader that advances to the largest returned LSN is run alongside for comparison (it may miss keys).
    #[test]
    fn randomised_out_of_order_application_misses_nothing() {
        let d = tmpdir("rand");
        let s = Store::open(&d).unwrap();
        *s.fail_hook.lock().unwrap() = Some(Arc::new(|p: &str| {
            if p == "before_apply" {
                let n = (crate::repo::now_ms() ^ (std::thread::current().id().as_u64_compat())) % 3;
                std::thread::sleep(std::time::Duration::from_micros(n * 700));
            }
            false
        }));
        let done = Arc::new(AtomicBool::new(false));
        let ths: Vec<_> = (0..8)
            .map(|t| {
                let s = s.clone();
                std::thread::spawn(move || {
                    for i in 0..60 {
                        put(&s, &format!("{P}{t}-{i}"));
                        if i % 7 == 0 {
                            put(&s, &format!("other\x01{t}-{i}")); // untracked writes interleaved
                        }
                    }
                })
            })
            .collect();
        let (s2, d2) = (s.clone(), done.clone());
        let reader = std::thread::spawn(move || {
            let (mut h, mut seen) = (0u64, BTreeSet::new());
            let (mut nh, mut nseen) = (0u64, BTreeSet::new());
            let mut ahead = 0u64; // records seen applied above the stable mark: out-of-order application happened
            loop {
                let fin = d2.load(Ordering::SeqCst);
                let (st, recs) = s2.changes_since(P, h);
                ahead += recs.iter().filter(|r| r.0 > st).count() as u64;
                seen.extend(recs.into_iter().map(|r| r.1));
                h = st;
                let (_, nrecs) = s2.changes_since(P, nh);
                for r in nrecs {
                    nh = nh.max(r.0);
                    nseen.insert(r.1);
                }
                if fin {
                    return (seen, nseen, ahead);
                }
            }
        });
        for t in ths {
            t.join().unwrap();
        }
        done.store(true, Ordering::SeqCst);
        let (seen, nseen, ahead) = reader.join().unwrap();
        // the property is only exercised if application really ran out of LSN order while the reader watched (audit)
        assert!(ahead > 0, "no out-of-order application observed: the test did not exercise its property");
        let all: BTreeSet<String> = (0..8).flat_map(|t| (0..60).map(move |i| format!("{P}{t}-{i}"))).collect();
        assert_eq!(seen, all, "reader with stable mark missed {} keys", all.difference(&seen).count());
        // and a reader that advances to the highest LSN it has seen (the defect this design prevents) misses keys here
        assert!(nseen.len() < all.len(), "the naive reader missed nothing: the test cannot distinguish the designs");
        eprintln!("randomised: stable-mark reader saw {}/{}; naive max-LSN reader saw {}/{}; {} records observed above the stable mark", seen.len(), all.len(), nseen.len(), all.len(), ahead);
        let _ = fs::remove_dir_all(&d);
    }

    /// The former 4096-way commit-lock striping (FNV-1a of the key), kept only to construct a collision below.
    fn old_stripe(k: &str) -> usize {
        let mut h: u64 = 0xcbf29ce484222325;
        for b in k.as_bytes() {
            h ^= *b as u64;
            h = h.wrapping_mul(0x100000001b3);
        }
        (h % 4096) as usize
    }

    /// Audit F12: a commit held while holding its locks delays only commits that touch one of its keys. Under the
    /// former striped locks an unrelated key that shared a stripe waited for the whole hold.
    #[test]
    fn unrelated_key_is_not_delayed_by_a_held_commit_even_on_an_old_stripe_collision() {
        let d = tmpdir("keylock");
        let s = Store::open(&d).unwrap();
        let a = format!("{P}held");
        let b = (0..).map(|i| format!("{P}other{}", i)).find(|k| old_stripe(k) == old_stripe(&a)).unwrap();
        *s.fail_hook.lock().unwrap() = Some(Arc::new(|p: &str| {
            if p == "before_wal" && std::thread::current().name() == Some("held") {
                std::thread::sleep(std::time::Duration::from_millis(1500));
            }
            false
        }));
        let (s2, a2) = (s.clone(), a.clone());
        let th = std::thread::Builder::new().name("held".into()).spawn(move || put(&s2, &a2)).unwrap();
        std::thread::sleep(std::time::Duration::from_millis(200));
        let t = std::time::Instant::now();
        put(&s, &b);
        let unrelated = t.elapsed();
        let t = std::time::Instant::now();
        put(&s, &a);
        let same = t.elapsed();
        th.join().unwrap();
        assert!(unrelated < std::time::Duration::from_millis(500), "unrelated key waited {:?} for the held commit", unrelated);
        assert!(same > std::time::Duration::from_millis(800), "a commit on the held key did not wait ({:?})", same);
        let _ = fs::remove_dir_all(&d);
    }

    /// Review V4 (the reviewer's test, adopted): a writer of a key that a stream of transactions keeps reading
    /// (shared) must not starve. Before writer preference it did not commit within 10 s (~9,500 reader commits).
    #[test]
    fn writer_of_a_read_shared_key_does_not_starve() {
        let d = tmpdir("starve");
        let s = Store::open(&d).unwrap();
        put(&s, "hot");
        let stop = Arc::new(AtomicBool::new(false));
        let readers: Vec<_> = (0..8)
            .map(|n| {
                let (s, stop) = (s.clone(), stop.clone());
                std::thread::spawn(move || {
                    let mut i = 0u64;
                    while !stop.load(Ordering::SeqCst) {
                        let mut t = s.txn();
                        let _ = t.get("hot");
                        t.put(&format!("r{}-{}", n, i), b"x".to_vec());
                        let _ = t.commit();
                        i += 1;
                    }
                    i
                })
            })
            .collect();
        std::thread::sleep(std::time::Duration::from_millis(300));
        let t0 = std::time::Instant::now();
        let (tx, rx) = std::sync::mpsc::channel();
        let s2 = s.clone();
        std::thread::spawn(move || {
            let mut t = s2.txn();
            let _ = t.get("hot");
            t.put("hot", b"v1".to_vec());
            let _ = tx.send(t.commit().is_ok());
        });
        let got = rx.recv_timeout(std::time::Duration::from_secs(10));
        let waited = t0.elapsed();
        stop.store(true, Ordering::SeqCst);
        let n: u64 = readers.into_iter().map(|h| h.join().unwrap()).sum();
        let _ = fs::remove_dir_all(&d);
        assert!(got == Ok(true) && waited < std::time::Duration::from_secs(2), "writer starved for {:?} ({} reader commits)", waited, n);
    }

    /// Writer preference must not deadlock: two transactions that each write one key and only read the other's,
    /// both waiting behind readers, must both commit.
    #[test]
    fn crossing_writers_with_shared_reads_do_not_deadlock() {
        let d = tmpdir("cross");
        let s = Store::open(&d).unwrap();
        put(&s, "a");
        put(&s, "b");
        // readers keep both keys held shared, so writers queue (and register as waiting) behind them
        let stop = Arc::new(AtomicBool::new(false));
        let readers: Vec<_> = (0..4)
            .map(|n| {
                let (s, stop) = (s.clone(), stop.clone());
                std::thread::spawn(move || {
                    let mut i = 0u64;
                    while !stop.load(Ordering::SeqCst) {
                        let mut t = s.txn();
                        let _ = t.get("a");
                        let _ = t.get("b");
                        t.put(&format!("rd{}-{}", n, i), b"x".to_vec());
                        let _ = t.commit();
                        i += 1;
                    }
                })
            })
            .collect();
        for round in 0..200 {
            let mk = |w: &'static str, r: &'static str, s: Arc<Store>| {
                std::thread::spawn(move || {
                    for _ in 0..1000 {
                        let mut t = s.txn();
                        let _ = t.get(r);
                        let _ = t.get(w);
                        t.put(w, b"x".to_vec());
                        match t.commit() {
                            Ok(_) => return true,
                            Err(e) if e.is::<Conflict>() => continue,
                            Err(_) => return false,
                        }
                    }
                    false
                })
            };
            let (x, y) = (mk("a", "b", s.clone()), mk("b", "a", s.clone()));
            let (tx, rx) = std::sync::mpsc::channel();
            std::thread::spawn(move || {
                let _ = tx.send((x.join().unwrap(), y.join().unwrap()));
            });
            let r = rx.recv_timeout(std::time::Duration::from_secs(10));
            if r != Ok((true, true)) {
                stop.store(true, Ordering::SeqCst);
                panic!("round {}: crossing writers did not both commit within 10 s (deadlock?)", round);
            }
        }
        stop.store(true, Ordering::SeqCst);
        for r in readers {
            r.join().unwrap();
        }
        let _ = fs::remove_dir_all(&d);
    }

    /// Audit F7: LSNs never go backwards across checkpoint + reopen, even when the newest commits were deletes (a
    /// snapshot keeps only live keys). A repeated LSN would hide new changes from a reader holding the old mark and
    /// let an OCC version repeat.
    #[test]
    fn lsn_never_regresses_across_checkpoint_after_deletes() {
        let d = tmpdir("ckpt");
        let last = {
            let s = Store::open(&d).unwrap();
            for i in 0..5 {
                put(&s, &format!("{P}k{}", i));
            }
            let mut last = 0;
            for i in 2..5 {
                let mut t = s.txn();
                t.delete(&format!("{P}k{}", i));
                last = t.commit().unwrap();
            }
            s.checkpoint().unwrap();
            last
        };
        let s = Store::open(&d).unwrap();
        let next = put(&s, &format!("{P}new"));
        assert!(next > last, "LSN regressed after checkpoint + reopen: {} after {}", next, last);
        let (_, recs) = s.changes_since(P, last);
        assert!(recs.iter().any(|r| r.1 == format!("{P}new")), "a reader holding mark {} misses the new key", last);
        assert!(s.get(CHECKPOINT_LSN_KEY).is_none(), "the reserved record must not become a live key");
        let _ = fs::remove_dir_all(&d);
    }

    /// I1': after reopen, changes above an old mark are found; those at or below it are not returned again.
    #[test]
    fn mark_survives_reopen() {
        let d = tmpdir("reopen");
        let h = {
            let s = Store::open(&d).unwrap();
            put(&s, &format!("{P}one"));
            s.stable_lsn()
        };
        let s = Store::open(&d).unwrap();
        assert_eq!(s.stable_lsn(), h);
        put(&s, &format!("{P}two"));
        let (_, recs) = s.changes_since(P, h);
        let keys: Vec<String> = recs.into_iter().map(|r| r.1).collect();
        assert_eq!(keys, vec![format!("{P}two")]);
        let (_, all) = s.changes_since(P, 0);
        assert_eq!(all.len(), 2);
        let _ = fs::remove_dir_all(&d);
    }

    trait IdCompat {
        fn as_u64_compat(&self) -> u64;
    }
    impl IdCompat for std::thread::ThreadId {
        fn as_u64_compat(&self) -> u64 {
            use std::hash::{Hash, Hasher};
            let mut h = std::collections::hash_map::DefaultHasher::new();
            self.hash(&mut h);
            h.finish()
        }
    }
}
