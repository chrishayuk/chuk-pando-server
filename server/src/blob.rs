//! Filesystem blob store for immutable packs and their sidecar indexes.
//! Layout: <root>/repos/<enc(repo)>/packs/<packid>.pack + <packid>.pidx
use crate::ingest::inflate_bytes;
use crate::meta::fsync_dir;
use crate::pack::*;
use anyhow::{anyhow, bail, Context, Result};
use std::collections::{HashMap, HashSet};
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

pub struct Blobs {
    pub root: PathBuf,
    idx: Mutex<HashMap<String, Arc<PackIndex>>>,
    files: Mutex<HashMap<String, Arc<File>>>,
    objs: Mutex<ObjCache>,
    tmpctr: AtomicU64,
    /// Directories whose creation has been made durable in their parents (see `ensure_durable_dir`).
    durable_dirs: Mutex<HashSet<PathBuf>>,
    alts: Mutex<HashMap<String, Option<Arc<AltMap>>>>,
    /// Where this process stages files before install: `<root>/tmp` (single process) or its own locked area
    /// `<root>/tmp/i-<id>/` when the pack store is shared by several frontends (docs/DISTRIBUTED.md).
    tmp: PathBuf,
    _tmp_lock: Option<File>,
    /// Authoritative S3-compatible store (docs/NETWORK-STORAGE.md). When set, `root` is this process's private
    /// cache; every published object is in the store, uploaded create-only before any metadata references it.
    s3: Option<crate::s3::S3>,
}

fn try_flock(f: &File) -> bool {
    crate::sys::try_lock_exclusive(f)
}

pub fn enc(repo: &str) -> String {
    let mut s = String::new();
    for c in repo.chars() {
        if c.is_ascii_alphanumeric() || c == '.' || c == '-' || c == '_' {
            s.push(c);
        } else {
            for b in c.to_string().as_bytes() {
                s.push_str(&format!("%{:02X}", b));
            }
        }
    }
    s
}

impl Blobs {
    pub fn new(root: &Path) -> Result<Blobs> {
        fs::create_dir_all(root.join("tmp"))?;
        fs::create_dir_all(root.join("repos"))?;
        // The store's own directories must survive power loss before any pack is acknowledged.
        fsync_dir(&root.join("tmp"))?;
        fsync_dir(&root.join("repos"))?;
        fsync_dir(root)?;
        if let Some(parent) = root.parent() {
            fsync_dir(parent)?;
        }
        Self::finish(root, root.join("tmp"), None)
    }

    /// Open a pack store shared with other live frontends. Each process stages in its own area, held under an
    /// exclusive flock for its lifetime; at startup only areas whose lock is free (their owner died) are removed.
    pub fn new_shared(root: &Path) -> Result<Blobs> {
        // Windows: an instance area is published by renaming its directory while its LOCK file is open, which Windows
        // refuses; without that, the area could be seen unlocked and swept (audit F3). Refuse rather than weaken.
        if cfg!(windows) {
            bail!("a shared pack store (PANDO_BLOBS) is not supported on Windows; use a local store or S3 (docs/WINDOWS.md)");
        }
        for d in ["tmp", "repos"] {
            fs::create_dir_all(root.join(d))?;
            fsync_dir(&root.join(d))?;
        }
        fsync_dir(root)?;
        if let Some(parent) = root.parent() {
            fsync_dir(parent)?;
        }
        // Audit F3: an area must never be visible under its `i-` name without its owner's lock held, or a concurrent
        // sweep below would take it for dead. It is created, locked and made durable under a staging name (`n-`)
        // that the lock-based sweep never touches, then renamed into place; the flock stays with the open file.
        if let Ok(rd) = fs::read_dir(root.join("tmp")) {
            for e in rd.flatten() {
                let name = e.file_name().to_string_lossy().to_string();
                if name.starts_with("n-") && e.path().is_dir() {
                    // staging area of a process that died between mkdir and rename; creation takes milliseconds,
                    // and pids are not meaningful across hosts sharing the directory, so age decides
                    let old = e.metadata().and_then(|m| m.modified()).ok().and_then(|t| t.elapsed().ok())
                        .map(|a| a.as_secs() > STAGING_MAX_AGE_SECS).unwrap_or(false);
                    if old {
                        let _ = fs::remove_dir_all(e.path());
                    }
                    continue;
                }
                if !name.starts_with("i-") || !e.path().is_dir() {
                    continue; // not an instance area: never touched in shared mode
                }
                let dead = match File::open(e.path().join("LOCK")) {
                    Ok(f) => try_flock(&f),
                    Err(_) => true, // published areas always have a LOCK; one without it is debris
                };
                if dead {
                    let _ = fs::remove_dir_all(e.path());
                }
            }
        }
        static SEQ: AtomicU64 = AtomicU64::new(0);
        let tag = format!("{}-{}-{}", std::process::id(), now_nanos(), SEQ.fetch_add(1, Ordering::Relaxed));
        let staging = root.join("tmp").join(format!("n-{}", tag));
        let area = root.join("tmp").join(format!("i-{}", tag));
        fs::create_dir(&staging)?;
        let lf = OpenOptions::new().create(true).write(true).truncate(false).open(staging.join("LOCK"))?;
        if !try_flock(&lf) {
            bail!("could not lock own tmp area {}", staging.display());
        }
        fsync_dir(&staging)?;
        fs::rename(&staging, &area).with_context(|| format!("publish tmp area {}", area.display()))?;
        fsync_dir(&root.join("tmp"))?;
        let b = Self::finish(root, area, Some(lf))?;
        Ok(b)
    }

    fn finish(root: &Path, tmp: PathBuf, lock: Option<File>) -> Result<Blobs> {
        // leftover temp files from a crash are garbage (single process: everything under tmp is ours)
        if lock.is_none() && let Ok(rd) = fs::read_dir(root.join("tmp")) {
            for e in rd.flatten() {
                // files: interrupted pushes/installs; directories: interrupted recompression workspaces
                if e.path().is_dir() {
                    let _ = fs::remove_dir_all(e.path());
                } else {
                    let _ = fs::remove_file(e.path());
                }
            }
        }
        Ok(Blobs {
            root: root.to_path_buf(),
            idx: Mutex::new(HashMap::new()),
            files: Mutex::new(HashMap::new()),
            objs: Mutex::new(ObjCache::new(256 << 20)),
            tmpctr: AtomicU64::new(0),
            durable_dirs: Mutex::new(HashSet::new()),
            alts: Mutex::new(HashMap::new()),
            tmp,
            _tmp_lock: lock,
            s3: None,
        })
    }

    /// A private local cache in front of an S3-compatible store.
    pub fn new_s3(cache_root: &Path, s3: crate::s3::S3) -> Result<Blobs> {
        // The cache is disposable (docs/S3-PROFILE.md): filled objects are written without fsync, so after a power
        // loss a cached file could be torn. Start every process with an empty cache; the store is authoritative.
        let _ = fs::remove_dir_all(cache_root.join("repos"));
        let mut b = Self::new(cache_root)?;
        b.s3 = Some(s3);
        Ok(b)
    }

    pub fn is_s3(&self) -> bool {
        self.s3.is_some()
    }

    fn obj_key(erepo: &str, id: &str, ext: &str) -> String {
        format!("repos/{}/packs/{}.{}", erepo, id, ext)
    }

    /// Write a downloaded, verified object into the local cache at `dst` write-once (tmp, link without replace).
    /// No fsync: the cache is disposable and emptied at startup (`new_s3`). A process crash cannot tear the file (it
    /// is complete before the link); a power loss is handled by the startup wipe. Measured: the fsync here was 62 %
    /// of server-side push time at 16 writers (docs/S3-PROFILE.md).
    fn put_local(&self, dst: &Path, bytes: &[u8]) -> Result<()> {
        if let Some(d) = dst.parent() {
            fs::create_dir_all(d)?;
        }
        let tmp = self.tmp_path();
        {
            let mut f = File::create(&tmp)?;
            f.write_all(bytes)?;
        }
        let _ = link_no_replace(&tmp, dst)?;
        let _ = fs::remove_file(&tmp);
        Ok(())
    }

    /// Create-only publication of a local file to the store. If the key already exists (412), the stored object is
    /// downloaded and must be content-equivalent to ours before this returns Ok (and so before any metadata commit
    /// that would acknowledge a push referencing it); it is never replaced:
    /// - pack: byte-identical (its name is its SHA-1, so any other bytes are wrong);
    /// - index: semantically equivalent (`PackIndex::equivalent_to`); where it names a different pack for an external
    ///   delta base, that pack's index must contain the base;
    /// - substitution map: identical, or every entry valid (`check_alt`), since two frontends may pick different
    ///   L0 copies of a duplicated object (audit F10);
    /// - anything else: byte-identical.
    /// Audit F2: verification was size (and pack trailer) only, so a push could be acknowledged against other bytes.
    fn publish(&self, repo: &str, id: &str, ext: &str, local: &Path) -> Result<()> {
        let s3 = self.s3.as_ref().unwrap();
        let key = Self::obj_key(&enc(repo), id, ext);
        let bytes = fs::read(local).with_context(|| format!("read {:?}", local))?;
        if s3.put_if_absent(&key, &bytes)? {
            crate::metrics::s3_op("put_created");
            return Ok(());
        }
        crate::metrics::s3_op("put_exists");
        let stored = s3.get(&key)?.ok_or_else(|| anyhow!("{}: 412 but object absent", key))?;
        if stored == bytes {
            return Ok(());
        }
        let refuse = |why: String| anyhow!("stored object {} differs from ours ({}): refusing to proceed", key, why);
        match ext {
            "pidx" => {
                let mine = PackIndex::deserialize(&bytes)?;
                let theirs = PackIndex::deserialize(&stored).map_err(|e| refuse(format!("not a valid index: {}", e)))?;
                for (oid, p) in mine.equivalent_to(&theirs).map_err(refuse)? {
                    // The named pack must be usable by every frontend: a well-formed pack id (never a path), its pack
                    // object present in the store (a download is then verified against the id), and its index
                    // holding the base. Review V1: any index named that way was accepted, even "../../<other repo>"
                    // or an index-only object, and a cold frontend then could not serve the push.
                    if !valid_pack_id(&p) {
                        return Err(refuse(format!("names {:?} for delta base {}, which is not a pack id", p, oid.hex())));
                    }
                    let pack_stored = s3.head(&Self::obj_key(&enc(repo), &p, "pack"))?.is_some();
                    let holds = pack_stored && self.index(repo, &p).map(|i| i.find(&oid).is_some()).unwrap_or(false);
                    if !holds {
                        return Err(refuse(format!("names pack {} for delta base {}, which is not stored or does not hold it", p, oid.hex())));
                    }
                }
                Ok(())
            }
            "alt" => {
                let m = parse_alt(&stored).ok_or_else(|| refuse("not a valid substitution map".into()))?;
                self.check_alt(repo, id, &m).map_err(refuse)
            }
            _ => Err(refuse(format!("{} vs {} bytes, content differs", stored.len(), bytes.len()))),
        }
    }

    /// Every entry of a substitution map for cover `id` must name an object of the cover and an L0 pack entry that
    /// stores that same object as a delta (what the fetch path substitutes). Entries may be absent (the object is
    /// then sent whole), so validity, not equality with our own map, is what serving needs.
    fn check_alt(&self, repo: &str, id: &str, m: &AltMap) -> std::result::Result<(), String> {
        let cover = self.index(repo, id).map_err(|e| format!("cover index: {:#}", e))?;
        for (oid, (p, ei)) in m {
            if !valid_pack_id(p) {
                return Err(format!("map entry {} names {:?}, which is not a pack id", oid.hex(), p));
            }
            if cover.find(oid).is_none() {
                return Err(format!("map entry {} is not an object of the cover", oid.hex()));
            }
            let li = self.index(repo, p).map_err(|e| format!("index of {}: {:#}", p, e))?;
            match li.entries.get(*ei) {
                Some(e) if e.oid == *oid && matches!(e.kind, crate::pack::T_OFS | crate::pack::T_REF) => {}
                _ => return Err(format!("map entry {} -> {}#{} does not hold that object as a delta", oid.hex(), p, ei)),
            }
        }
        Ok(())
    }

    /// Fetch `ext` of pack `id` from the store into the local cache, verified. Ok(false) if the store lacks it.
    fn fetch(&self, erepo: &str, id: &str, ext: &str) -> Result<bool> {
        check_pack_id(id)?;
        let s3 = match &self.s3 {
            Some(s) => s,
            None => return Ok(false),
        };
        let t_fill = std::time::Instant::now();
        let key = Self::obj_key(erepo, id, ext);
        let b = match s3.get(&key)? {
            Some(b) => b,
            None => return Ok(false),
        };
        crate::metrics::s3_op("get");
        if std::env::var("PANDO_DEBUG_FILLS").is_ok() {
            // diagnostic only (docs/S3-PROFILE.md): which code path fills the cache
            eprintln!("cache fill {} ({} B)\n{}", key, b.len(), std::backtrace::Backtrace::force_capture());
        }
        let t_verify = std::time::Instant::now();
        match ext {
            "pack" => {
                use sha1::{Digest, Sha1};
                if b.len() < 32 {
                    bail!("object {} too short", key);
                }
                let d = Sha1::digest(&b[..b.len() - 20]);
                if d.as_slice() != &b[b.len() - 20..] || crate::s3::hex(&b[b.len() - 20..]) != id {
                    bail!("object {} failed verification (content hash differs from its id): not served", key);
                }
            }
            "pidx" => {
                let i = PackIndex::deserialize(&b).with_context(|| format!("object {} is not a valid index", key))?;
                if i.id != id {
                    bail!("object {} is the index of {}, not {}", key, i.id, id);
                }
            }
            _ => {}
        }
        let verify_us = t_verify.elapsed().as_micros() as u64;
        let t_write = std::time::Instant::now();
        let dst = self.root.join("repos").join(erepo).join("packs").join(format!("{}.{}", id, ext));
        self.put_local(&dst, &b)?;
        crate::s3::prof_fill(t_fill.elapsed().as_micros() as u64, verify_us, t_write.elapsed().as_micros() as u64);
        Ok(true)
    }

    /// Make the pack file of `id` (encoded repository name) present locally; used by the packfile-URI cache nodes.
    pub fn ensure_local_pack(&self, erepo: &str, id: &str) -> Result<()> {
        check_pack_id(id)?;
        let p = self.root.join("repos").join(erepo).join("packs").join(format!("{}.pack", id));
        if p.exists() || self.s3.is_none() {
            return Ok(());
        }
        if !self.fetch(erepo, id, "pack")? {
            bail!("pack {} not in the object store", id);
        }
        Ok(())
    }
    pub fn pack_dir(&self, repo: &str) -> PathBuf {
        self.root.join("repos").join(enc(repo)).join("packs")
    }
    pub fn pack_path(&self, repo: &str, id: &str) -> PathBuf {
        self.pack_dir(repo).join(format!("{}.pack", id))
    }
    pub fn tmp_path(&self) -> PathBuf {
        let n = self.tmpctr.fetch_add(1, Ordering::Relaxed);
        self.tmp.join(format!("in-{}-{}-{}", std::process::id(), n, now_nanos()))
    }
    pub fn exists(&self, repo: &str, id: &str) -> bool {
        if self.pack_path(repo, id).exists() && self.pack_dir(repo).join(format!("{}.pidx", id)).exists() {
            return true;
        }
        match &self.s3 {
            Some(s3) => {
                let er = enc(repo);
                matches!(s3.head(&Self::obj_key(&er, id, "pidx")), Ok(Some(_))) && matches!(s3.head(&Self::obj_key(&er, id, "pack")), Ok(Some(_)))
            }
            None => false,
        }
    }

    /// Create `dir` (if needed) and make every directory entry from `dir` up to the store root durable in its
    /// parent, once per process. Without this, the first pack of a new repository could be acknowledged while
    /// the `repos/<repo>` entry itself is not yet on stable storage (CG7 audit B D-3).
    fn ensure_durable_dir(&self, dir: &Path) -> Result<()> {
        if self.s3.is_some() {
            // S3 mode: the local tree is a disposable cache (docs/S3-PROFILE.md); durability is the store's
            fs::create_dir_all(dir)?;
            return Ok(());
        }
        let mut done = self.durable_dirs.lock().unwrap();
        if done.contains(dir) {
            return Ok(());
        }
        fs::create_dir_all(dir)?;
        let mut p = dir.to_path_buf();
        loop {
            fsync_dir(&p)?;
            if p == self.root {
                break;
            }
            match p.parent() {
                Some(q) => p = q.to_path_buf(),
                None => break,
            }
        }
        done.insert(dir.to_path_buf());
        Ok(())
    }

    /// Durably install a pack (already fsynced at tmp) and its index.
    ///
    /// Write-once: both files are published with `hard_link`, which fails if the name exists, so an installed
    /// pack or index is never replaced (CG7 audit B D-6). If another installer published the same pack id first,
    /// its files win and callers must re-read the index from disk (they do: `Blobs::index`). The pack directory
    /// is fsynced on every path, including the already-installed one, so a concurrent installer's not-yet-
    /// synced directory entry is durable before this push is acknowledged (CG7 audit B D-4).
    pub fn install(&self, repo: &str, tmp_pack: &Path, idx: &PackIndex) -> Result<()> {
        let dir = self.pack_dir(repo);
        self.ensure_durable_dir(&dir)?;
        let pidx = dir.join(format!("{}.pidx", idx.id));
        let pack = dir.join(format!("{}.pack", idx.id));
        if pack.exists() && pidx.exists() {
            let _ = fs::remove_file(tmp_pack);
            self.sync_dir_unless_cache(&dir)?;
            // S3: always (re)publish create-only: a crash after the local install may have preceded the upload
            return self.publish_pack(repo, &idx.id);
        }
        let tmpi = self.tmp_path();
        {
            let mut f = File::create(&tmpi)?;
            f.write_all(&idx.serialize())?;
            if self.s3.is_none() {
                f.sync_all()?;
            }
        }
        // Pack before index: `exists` requires both, so a crash in between leaves an unpublished pack that a
        // retried push completes.
        let new_pack = link_no_replace(tmp_pack, &pack)?;
        let new_pidx = link_no_replace(&tmpi, &pidx)?;
        let _ = fs::remove_file(tmp_pack);
        let _ = fs::remove_file(&tmpi);
        for (created, p) in [(new_pack, &pack), (new_pidx, &pidx)] {
            if created {
                let mut perm = fs::metadata(p)?.permissions();
                perm.set_readonly(true);
                fs::set_permissions(p, perm)?;
            }
        }
        self.sync_dir_unless_cache(&dir)?;
        self.publish_pack(repo, &idx.id)
    }

    /// fsync a directory, except in S3 mode where the local tree is a disposable cache and the object store,
    /// written before any metadata commit, provides durability (docs/S3-PROFILE.md).
    fn sync_dir_unless_cache(&self, dir: &Path) -> Result<()> {
        if self.s3.is_none() {
            fsync_dir(dir)?;
        }
        Ok(())
    }

    /// Upload pack then index, create-only (no-op without S3). The index is the completion marker: readers only
    /// look for a pack after metadata references it, and metadata is committed only after this returns.
    fn publish_pack(&self, repo: &str, id: &str) -> Result<()> {
        if self.s3.is_none() {
            return Ok(());
        }
        let dir = self.pack_dir(repo);
        self.publish(repo, id, "pack", &dir.join(format!("{}.pack", id)))?;
        if std::env::var("PANDO_TEST_ABORT_AFTER_PACK_PUT").map(|v| v == "1").unwrap_or(false) {
            // test-only (N3): die between the two uploads
            eprintln!("pando: PANDO_TEST_ABORT_AFTER_PACK_PUT: aborting after uploading pack {}", id);
            std::process::abort();
        }
        self.publish(repo, id, "pidx", &dir.join(format!("{}.pidx", id)))
    }

    /// Publish the substitution map of a derived pack (cover) write-once, next to it: for each whole object in the
    /// cover, the L0 pack + entry where the same object is stored as a delta. Built in the background at cover
    /// time; immutable because both packs are. Must be published before the cover's metadata transaction.
    pub fn install_alt(&self, repo: &str, id: &str, map: &[(Oid, String, u32)]) -> Result<()> {
        let dir = self.pack_dir(repo);
        self.ensure_durable_dir(&dir)?;
        let dst = dir.join(format!("{}.alt", id));
        if dst.exists() {
            self.sync_dir_unless_cache(&dir)?;
            if self.s3.is_some() {
                self.publish(repo, id, "alt", &dst)?;
            }
            return Ok(());
        }
        let mut b = Vec::with_capacity(8 + map.len() * 64);
        b.extend_from_slice(b"PALT");
        b.extend_from_slice(&(map.len() as u32).to_be_bytes());
        for (o, p, ei) in map {
            b.extend_from_slice(&o.0);
            let pb = p.as_bytes();
            b.push(pb.len() as u8);
            b.extend_from_slice(pb);
            b.extend_from_slice(&ei.to_be_bytes());
        }
        let tmp = self.tmp_path();
        {
            let mut f = File::create(&tmp)?;
            f.write_all(&b)?;
            if self.s3.is_none() {
                f.sync_all()?;
            }
        }
        let created = link_no_replace(&tmp, &dst)?;
        let _ = fs::remove_file(&tmp);
        if created {
            let mut perm = fs::metadata(&dst)?.permissions();
            perm.set_readonly(true);
            fs::set_permissions(&dst, perm)?;
        }
        self.sync_dir_unless_cache(&dir)?;
        if self.s3.is_some() {
            self.publish(repo, id, "alt", &dst)?;
        }
        Ok(())
    }

    /// The substitution map of pack `id`, if it has one (covers built by this version). Cached; immutable.
    pub fn alt_map(&self, repo: &str, id: &str) -> Option<Arc<AltMap>> {
        if !valid_pack_id(id) {
            return None;
        }
        // PANDO_ALT_MAP=0: ignore maps (fall back to the object index). For same-data A/B equivalence tests.
        if std::env::var("PANDO_ALT_MAP").map(|v| v == "0").unwrap_or(false) {
            return None;
        }
        let key = format!("{}\x01{}", repo, id);
        if let Some(v) = self.alts.lock().unwrap().get(&key) {
            return v.clone();
        }
        let ap = self.pack_dir(repo).join(format!("{}.alt", id));
        if !ap.exists() && self.s3.is_some() {
            if let Err(e) = self.fetch(&enc(repo), id, "alt") {
                // transient (store unreachable): no map for this fetch, but do not remember "no map" (audit F9)
                eprintln!("pando: substitution map of {} unavailable, using the object index: {:#}", id, e);
                return None;
            }
        }
        // Absent file: this cover has no map. Present: it must parse and every entry must be valid, whoever wrote it
        // (another frontend, or the store); an invalid map is never used (audit F9).
        let v = match fs::read(&ap) {
            Err(_) => None,
            Ok(b) => match parse_alt(&b).ok_or_else(|| "unparseable".to_string()).and_then(|m| self.check_alt(repo, id, &m).map(|_| m)) {
                Ok(m) => Some(Arc::new(m)),
                Err(why) => {
                    eprintln!("pando: substitution map of {} rejected ({}); using the object index", id, why);
                    None
                }
            },
        };
        self.alts.lock().unwrap().insert(key, v.clone());
        v
    }

    /// Write a pack produced by the server itself (compaction) from bytes.
    pub fn install_bytes(&self, repo: &str, bytes: &[u8], idx: &PackIndex) -> Result<()> {
        let tmp = self.tmp_path();
        {
            let mut f = File::create(&tmp)?;
            f.write_all(bytes)?;
            f.sync_all()?;
        }
        self.install(repo, &tmp, idx)
    }

    pub fn index(&self, repo: &str, id: &str) -> Result<Arc<PackIndex>> {
        check_pack_id(id)?;
        let key = format!("{}\x01{}", repo, id);
        if let Some(i) = self.idx.lock().unwrap().get(&key) {
            return Ok(i.clone());
        }
        let p = self.pack_dir(repo).join(format!("{}.pidx", id));
        if !p.exists() && self.s3.is_some() {
            self.fetch(&enc(repo), id, "pidx")?;
        }
        let b = fs::read(&p).with_context(|| format!("read {:?}", p))?;
        let i = Arc::new(PackIndex::deserialize(&b)?);
        self.idx.lock().unwrap().insert(key, i.clone());
        Ok(i)
    }

    pub fn file(&self, repo: &str, id: &str) -> Result<Arc<File>> {
        check_pack_id(id)?;
        let key = format!("{}\x01{}", repo, id);
        let mut f = self.files.lock().unwrap();
        if let Some(x) = f.get(&key) {
            return Ok(x.clone());
        }
        if f.len() > 512 {
            f.clear();
        }
        if self.s3.is_some() && !self.pack_path(repo, id).exists() {
            drop(f);
            if !self.fetch(&enc(repo), id, "pack")? {
                bail!("pack {} is missing from the object store", id);
            }
            // The pack is verified against its id by `fetch`; it must also be the size its index records (audit F9).
            let want = self.index(repo, id)?.pack_size;
            let got = fs::metadata(self.pack_path(repo, id))?.len();
            if got != want {
                bail!("pack {} is {} bytes but its index records {}: not served", id, got, want);
            }
            f = self.files.lock().unwrap();
        }
        let x = Arc::new(File::open(self.pack_path(repo, id)).with_context(|| format!("open pack {}", id))?);
        f.insert(key, x.clone());
        Ok(x)
    }

    pub fn read_at(&self, repo: &str, id: &str, off: u64, len: usize) -> Result<Vec<u8>> {
        let f = self.file(repo, id)?;
        let mut v = vec![0u8; len];
        crate::sys::read_exact_at(&f, &mut v, off)?;
        Ok(v)
    }

    /// Read and fully resolve the object stored at entry `ei` of pack `id`.
    pub fn read_object(&self, repo: &str, id: &str, ei: usize) -> Result<(u8, Arc<Vec<u8>>)> {
        let idx = self.index(repo, id)?;
        self.read_object_idx(repo, &idx, ei, 0)
    }

    fn read_object_idx(&self, repo: &str, idx: &Arc<PackIndex>, ei: usize, depth: usize) -> Result<(u8, Arc<Vec<u8>>)> {
        if depth > 10000 {
            bail!("delta chain too deep");
        }
        let e = &idx.entries[ei];
        let ckey = cache_key(&idx.id, e.offset);
        if let Some(c) = self.objs.lock().unwrap().get(ckey) {
            return Ok(c);
        }
        let raw = self.read_at(repo, &idx.id, e.data_off, (e.end - e.data_off) as usize)?;
        let data = inflate_bytes(&raw, e.size as usize)?;
        let res = match e.kind {
            T_COMMIT | T_TREE | T_BLOB | T_TAG => (e.kind, Arc::new(data)),
            T_OFS | T_REF => {
                let (bt, bd) = if e.base_off != u64::MAX {
                    let bi = idx.by_offset(e.base_off).ok_or_else(|| anyhow!("bad base offset"))?;
                    self.read_object_idx(repo, idx, bi, depth + 1)?
                } else {
                    let bp = idx.ext_base_pack(&e.base_oid).ok_or_else(|| anyhow!("unknown external base {}", e.base_oid))?.to_string();
                    let bidx = self.index(repo, &bp)?;
                    let bi = bidx.find(&e.base_oid).ok_or_else(|| anyhow!("external base missing in {}", bp))?;
                    self.read_object_idx(repo, &bidx, bi, depth + 1)?
                };
                (bt, Arc::new(apply_delta(&bd, &data)?))
            }
            _ => bail!("bad kind"),
        };
        self.objs.lock().unwrap().put(ckey, res.0, res.1.clone());
        Ok(res)
    }

    pub fn list_packs(&self, repo: &str) -> Vec<String> {
        let mut v = Vec::new();
        if let Ok(rd) = fs::read_dir(self.pack_dir(repo)) {
            for e in rd.flatten() {
                let n = e.file_name().to_string_lossy().to_string();
                if let Some(id) = n.strip_suffix(".pack") {
                    v.push(id.to_string());
                }
            }
        }
        v
    }
}

fn cache_key(id: &str, off: u64) -> u64 {
    let mut h: u64 = 0xcbf29ce484222325;
    for b in id.as_bytes() {
        h ^= *b as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    h ^ off.wrapping_mul(0x9E3779B97F4A7C15)
}

/// A pack id: 40 lowercase hex digits (SHA-1 of the pack). Ids read from stored data (indexes, substitution maps) or
/// requests are checked before they become a path or an object key (review V1: "../../<repo>/packs/<id>").
pub fn valid_pack_id(id: &str) -> bool {
    id.len() == 40 && id.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

fn check_pack_id(id: &str) -> Result<()> {
    if !valid_pack_id(id) {
        bail!("{:?} is not a pack id", id);
    }
    Ok(())
}

/// Age after which an unpublished staging area (`tmp/n-*`, shared mode) is debris.
const STAGING_MAX_AGE_SECS: u64 = 3600;

pub fn now_nanos() -> u128 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()
}

pub type AltMap = HashMap<Oid, (String, usize)>;

fn parse_alt(b: &[u8]) -> Option<AltMap> {
    if b.len() < 8 || &b[..4] != b"PALT" {
        return None;
    }
    let n = u32::from_be_bytes(b[4..8].try_into().ok()?) as usize;
    let mut m = HashMap::with_capacity(n);
    let mut i = 8;
    for _ in 0..n {
        let mut o = [0u8; 20];
        o.copy_from_slice(b.get(i..i + 20)?);
        i += 20;
        let l = *b.get(i)? as usize;
        i += 1;
        let p = String::from_utf8(b.get(i..i + l)?.to_vec()).ok()?;
        i += l;
        let ei = u32::from_be_bytes(b.get(i..i + 4)?.try_into().ok()?) as usize;
        i += 4;
        m.insert(Oid(o), (p, ei));
    }
    Some(m)
}

/// Publish `src` at `dst` only if `dst` does not exist. Returns whether this call created `dst`.
fn link_no_replace(src: &Path, dst: &Path) -> Result<bool> {
    match fs::hard_link(src, dst) {
        Ok(()) => Ok(true),
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => Ok(false),
        Err(e) => Err(anyhow!("publish {:?}: {}", dst, e)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmpdir(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("pando-blob-test-{}-{}-{}", tag, std::process::id(), now_nanos()));
        fs::create_dir_all(&d).unwrap();
        d
    }

    /// Audit F3: concurrent starts on one shared directory must never sweep a live instance's area. Threads open
    /// the store concurrently (flock conflicts between separate opens, as between processes); every store that
    /// opened must still have its area, and a dead (dropped) instance's area is swept by the next start.
    /// Windows: the shared store is refused, because the rename that publishes an instance area while its LOCK file
    /// is open is refused by Windows (checked here directly, so docs/WINDOWS.md's reason stays true).
    #[cfg(windows)]
    #[test]
    fn shared_store_is_refused_on_windows_because_an_open_lock_blocks_the_area_rename() {
        let root = tmpdir("shared-win");
        let e = Blobs::new_shared(&root).err().expect("shared store must be refused on Windows");
        assert!(format!("{:#}", e).contains("not supported on Windows"), "{:#}", e);
        let staging = root.join("n-probe");
        fs::create_dir_all(&staging).unwrap();
        let lf = OpenOptions::new().create(true).write(true).truncate(false).open(staging.join("LOCK")).unwrap();
        assert!(try_flock(&lf));
        let r = fs::rename(&staging, root.join("i-probe"));
        assert!(r.is_err(), "Windows renamed a directory holding an open, locked file: the shared store could be supported");
        drop(lf);
        let _ = fs::remove_dir_all(&root);
    }

    #[cfg(unix)]
    #[test]
    fn concurrent_shared_starts_never_sweep_a_live_area() {
        let root = tmpdir("shared");
        for round in 0..50 {
            let bar = Arc::new(std::sync::Barrier::new(12));
            let hs: Vec<_> = (0..12)
                .map(|_| {
                    let (r, b) = (root.clone(), bar.clone());
                    std::thread::spawn(move || {
                        b.wait();
                        Blobs::new_shared(&r)
                    })
                })
                .collect();
            let stores: Vec<Blobs> = hs.into_iter().map(|h| h.join().unwrap().expect("start failed")).collect();
            for st in &stores {
                assert!(st.tmp.join("LOCK").exists(), "round {}: live area {:?} was swept", round, st.tmp);
                File::create(st.tmp_path()).expect("live instance cannot stage a file");
            }
            drop(stores);
        }
        let _last = Blobs::new_shared(&root).unwrap();
        let left: Vec<_> = fs::read_dir(root.join("tmp")).unwrap().flatten().collect();
        assert_eq!(left.len(), 1, "dead areas not swept: {:?}", left.iter().map(|e| e.file_name()).collect::<Vec<_>>());
    }

    /// CG7 audit B D-6. State left by a crash (or a concurrent installer) where `<id>.pidx` is published but
    /// `<id>.pack` is not: a later install must complete the pack and must NOT replace the existing index.
    #[test]
    fn install_never_replaces_existing_index() {
        let root = tmpdir("wo");
        let b = Blobs::new(&root).unwrap();
        let id = "ab".repeat(20);
        let dir = b.pack_dir("r");
        fs::create_dir_all(&dir).unwrap();
        let existing = b"existing index bytes".to_vec();
        fs::write(dir.join(format!("{}.pidx", id)), &existing).unwrap();
        let tmp = b.tmp_path();
        fs::write(&tmp, b"PACK bytes").unwrap();
        let idx = PackIndex { id: id.clone(), ..Default::default() };
        b.install("r", &tmp, &idx).unwrap();
        assert_eq!(fs::read(dir.join(format!("{}.pidx", id))).unwrap(), existing, "index was replaced");
        assert_eq!(fs::read(dir.join(format!("{}.pack", id))).unwrap(), b"PACK bytes");
        assert!(!tmp.exists());
        let _ = fs::remove_dir_all(&root);
    }

    /// Installing the same pack twice keeps the first copy (same inode) and both names stay read-only.
    #[test]
    fn install_twice_keeps_first_copy() {
        let root = tmpdir("twice");
        let b = Blobs::new(&root).unwrap();
        let id = "cd".repeat(20);
        let idx = PackIndex { id: id.clone(), ..Default::default() };
        let t1 = b.tmp_path();
        fs::write(&t1, b"one").unwrap();
        b.install("r", &t1, &idx).unwrap();
        let p = b.pack_path("r", &id);
        #[cfg(unix)]
        let ino = { use std::os::unix::fs::MetadataExt; fs::metadata(&p).unwrap().ino() };
        let t2 = b.tmp_path();
        fs::write(&t2, b"two").unwrap();
        b.install("r", &t2, &idx).unwrap();
        #[cfg(unix)]
        assert_eq!({ use std::os::unix::fs::MetadataExt; fs::metadata(&p).unwrap().ino() }, ino);
        assert_eq!(fs::read(&p).unwrap(), b"one");
        assert!(fs::metadata(&p).unwrap().permissions().readonly());
        let _ = fs::remove_dir_all(&root);
    }
}
