//! Single-pass streaming pack decoder. The incoming bytes are teed verbatim to a
//! staging file (later renamed into the blob store) while every entry is
//! inflated, delta-resolved, hashed and its graph references recorded.
//! Nothing is ever written into a Git repository.
use crate::pack::*;
use anyhow::{anyhow, bail, Context, Result};
use flate2::{Decompress, FlushDecompress, Status};
use sha1::{Digest, Sha1};
use std::collections::{HashMap, HashSet};
use std::fs::File;
use std::io::{BufRead, BufWriter, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;

pub type ExtLookup<'a> = dyn FnMut(&Oid) -> Result<Option<(u8, Arc<Vec<u8>>, String)>> + 'a;

pub struct IngestResult {
    pub idx: PackIndex,
    pub tmp_path: PathBuf,
    pub count: u32,
}

struct In<R: BufRead> {
    r: R,
    pos: u64,
    sha: Sha1,
    out: BufWriter<File>,
}

impl<R: BufRead> In<R> {
    fn take(&mut self, n: usize) -> Result<Vec<u8>> {
        let mut v = vec![0u8; n];
        self.r.read_exact(&mut v).context("truncated pack")?;
        self.sha.update(&v);
        self.out.write_all(&v)?;
        self.pos += n as u64;
        Ok(v)
    }
    fn byte(&mut self) -> Result<u8> {
        Ok(self.take(1)?[0])
    }
    fn inflate(&mut self, size: usize) -> Result<Vec<u8>> {
        let mut d = Decompress::new(true);
        let mut out: Vec<u8> = Vec::with_capacity(size.min(1 << 26) + 16);
        loop {
            let buf = self.r.fill_buf()?;
            if buf.is_empty() {
                bail!("truncated pack (inside zlib stream)");
            }
            if out.capacity() - out.len() < 16 {
                out.reserve(64 * 1024);
            }
            let before = d.total_in();
            let st = d.decompress_vec(buf, &mut out, FlushDecompress::None).map_err(|e| anyhow!("zlib: {}", e))?;
            let used = (d.total_in() - before) as usize;
            self.sha.update(&buf[..used]);
            self.out.write_all(&buf[..used])?;
            self.pos += used as u64;
            self.r.consume(used);
            if out.len() > size {
                bail!("object larger than declared");
            }
            if st == Status::StreamEnd {
                break;
            }
        }
        if out.len() != size {
            bail!("object size mismatch: {} != {}", out.len(), size);
        }
        Ok(out)
    }
}

/// Inflate `size` bytes from a zlib stream in `raw`.
pub fn inflate_bytes(raw: &[u8], size: usize) -> Result<Vec<u8>> {
    let mut d = Decompress::new(true);
    let mut out: Vec<u8> = Vec::with_capacity(size + 16);
    loop {
        let before_in = d.total_in() as usize;
        if out.capacity() - out.len() < 16 {
            out.reserve(64 * 1024);
        }
        let st = d
            .decompress_vec(&raw[before_in..], &mut out, FlushDecompress::Finish)
            .map_err(|e| anyhow!("zlib: {}", e))?;
        if st == Status::StreamEnd {
            break;
        }
        if out.len() > size {
            bail!("object larger than declared");
        }
        if d.total_in() as usize >= raw.len() && st != Status::StreamEnd && out.capacity() > out.len() {
            bail!("truncated zlib stream");
        }
    }
    if out.len() != size {
        bail!("object size mismatch");
    }
    Ok(out)
}

pub fn ingest<R: BufRead>(r: R, tmp_path: &Path, ext: &mut ExtLookup) -> Result<IngestResult> {
    let file = File::create(tmp_path).with_context(|| format!("create {:?}", tmp_path))?;
    let mut inp = In { r, pos: 0, sha: Sha1::new(), out: BufWriter::with_capacity(1 << 20, file) };
    let hdr = inp.take(12)?;
    if &hdr[..4] != b"PACK" {
        bail!("not a pack stream");
    }
    let ver = u32::from_be_bytes(hdr[4..8].try_into().unwrap());
    if ver != 2 && ver != 3 {
        bail!("unsupported pack version {}", ver);
    }
    let count = u32::from_be_bytes(hdr[8..12].try_into().unwrap());

    // the header count is untrusted: do not pre-allocate from it
    let cap = (count as usize).min(1 << 16);
    let mut st = State {
        entries: Vec::with_capacity(cap),
        resolved: Vec::with_capacity(cap),
        by_off: HashMap::new(),
        by_oid: HashMap::new(),
        cache: ObjCache::new(128 << 20),
        ext_cache: HashMap::new(),
        ext_bases: Vec::new(),
        referenced: HashSet::new(),
        tags: Vec::new(),
        reader: None,
        tmp: tmp_path.to_path_buf(),
    };
    let mut pending: Vec<(usize, Vec<u8>)> = Vec::new();

    for i in 0..count as usize {
        let offset = inp.pos;
        let mut c = inp.byte()?;
        let t = (c >> 4) & 7;
        let mut size = (c & 15) as u64;
        let mut shift = 4;
        while c & 0x80 != 0 {
            c = inp.byte()?;
            size |= ((c & 0x7f) as u64) << shift;
            shift += 7;
            if shift > 63 {
                bail!("entry size overflow");
            }
        }
        let mut base_off = u64::MAX;
        let mut base_oid = Oid::ZERO;
        match t {
            T_COMMIT | T_TREE | T_BLOB | T_TAG => {}
            T_OFS => {
                let mut c = inp.byte()?;
                let mut off = (c & 0x7f) as u64;
                while c & 0x80 != 0 {
                    c = inp.byte()?;
                    off = ((off + 1) << 7) | (c & 0x7f) as u64;
                }
                if off == 0 || off > offset {
                    bail!("bad ofs-delta offset");
                }
                base_off = offset - off;
                if !st.by_off.contains_key(&base_off) {
                    bail!("ofs-delta base is not an entry start");
                }
            }
            T_REF => {
                base_oid = Oid::from_slice(&inp.take(20)?);
            }
            _ => bail!("invalid pack entry type {}", t),
        }
        let data_off = inp.pos;
        let data = inp.inflate(size as usize)?;
        let end = inp.pos;
        st.by_off.insert(offset, i);
        st.entries.push(Entry { oid: Oid::ZERO, offset, data_off, end, otype: 0, kind: t, size, base_off, base_oid });
        st.resolved.push(false);
        match t {
            T_COMMIT | T_TREE | T_BLOB | T_TAG => st.finish_obj(i, t, Arc::new(data))?,
            _ => {
                if !st.try_resolve(i, &data, &mut inp.out, ext, false)? {
                    pending.push((i, data));
                }
            }
        }
    }
    let digest = Oid::from_slice(&inp.sha.clone().finalize());
    let mut trailer = [0u8; 20];
    inp.r.read_exact(&mut trailer).context("truncated pack trailer")?;
    if trailer != digest.0 {
        bail!("pack checksum mismatch");
    }
    inp.out.write_all(&trailer)?;
    let pack_size = inp.pos + 20;
    // resolve deferred deltas (bases appearing later in the pack)
    while !pending.is_empty() {
        let before = pending.len();
        let mut still = Vec::new();
        for (i, data) in pending.drain(..) {
            if !st.try_resolve(i, &data, &mut inp.out, ext, false)? {
                still.push((i, data));
            }
        }
        if still.len() == before {
            // final attempt with external lookup errors reported
            let (i, _) = &still[0];
            let e = &st.entries[*i];
            bail!("unresolvable delta base {} (missing necessary objects)", if e.kind == T_REF { e.base_oid.hex() } else { format!("@{}", e.base_off) });
        }
        pending = still;
    }
    inp.out.flush()?;
    inp.out.get_ref().sync_all()?;

    let mut prereqs: Vec<Oid> = st.referenced.iter().filter(|o| !st.by_oid.contains_key(*o)).cloned().collect();
    prereqs.sort();
    // base_off of REF deltas whose base is inside this pack
    let offs: HashMap<Oid, u64> = st.entries.iter().map(|e| (e.oid, e.offset)).collect();
    for e in st.entries.iter_mut() {
        if e.kind == T_REF {
            e.base_off = offs.get(&e.base_oid).cloned().unwrap_or(u64::MAX);
        }
    }
    let mut idx = PackIndex {
        id: digest.hex(),
        entries: st.entries,
        sorted: vec![],
        prereqs,
        ext_bases: st.ext_bases,
        tags: st.tags,
        pack_size,
    };
    idx.finish();
    Ok(IngestResult { idx, tmp_path: tmp_path.to_path_buf(), count })
}

struct State {
    entries: Vec<Entry>,
    resolved: Vec<bool>,
    by_off: HashMap<u64, usize>,
    by_oid: HashMap<Oid, usize>,
    cache: ObjCache,
    ext_cache: HashMap<Oid, (u8, Arc<Vec<u8>>)>,
    ext_bases: Vec<(Oid, String)>,
    referenced: HashSet<Oid>,
    tags: Vec<(Oid, Oid, u8)>,
    reader: Option<File>,
    tmp: PathBuf,
}

impl State {
    fn by_off_rev(&self, j: usize) -> u64 {
        self.entries[j].offset
    }
    fn finish_obj(&mut self, i: usize, t: u8, data: Arc<Vec<u8>>) -> Result<()> {
        let oid = hash_object(t, &data);
        let mut refs = Vec::new();
        let mut tt = None;
        parse_refs(t, &data, &mut refs, &mut tt).with_context(|| format!("invalid {} {}", type_name(t), oid))?;
        for r in refs {
            self.referenced.insert(r);
        }
        if let Some((target, ttype)) = tt {
            self.tags.push((oid, target, ttype));
        }
        let e = &mut self.entries[i];
        e.oid = oid;
        e.otype = t;
        self.resolved[i] = true;
        self.by_oid.entry(oid).or_insert(i);
        self.cache.put(e.offset, t, data);
        Ok(())
    }

    /// content of an already-resolved entry
    fn content(&mut self, j: usize, out: &mut BufWriter<File>, ext: &mut ExtLookup) -> Result<(u8, Arc<Vec<u8>>)> {
        self.content_d(j, out, ext, 0)
    }

    fn content_d(&mut self, j: usize, out: &mut BufWriter<File>, ext: &mut ExtLookup, depth: usize) -> Result<(u8, Arc<Vec<u8>>)> {
        let off = self.entries[j].offset;
        if let Some(c) = self.cache.get(off) {
            return Ok(c);
        }
        if depth > 4000 {
            bail!("delta chain too deep (or cyclic) at offset {}", off);
        }
        out.flush()?;
        if self.reader.is_none() {
            self.reader = Some(File::open(&self.tmp)?);
        }
        let e = self.entries[j].clone();
        let mut raw = vec![0u8; (e.end - e.data_off) as usize];
        {
            let f = self.reader.as_mut().unwrap();
            f.seek(SeekFrom::Start(e.data_off))?;
            f.read_exact(&mut raw)?;
        }
        let data = inflate_bytes(&raw, e.size as usize)?;
        let res = match e.kind {
            T_COMMIT | T_TREE | T_BLOB | T_TAG => (e.kind, Arc::new(data)),
            T_OFS => {
                let b = self.by_off[&e.base_off];
                let (bt, bd) = self.content_d(b, out, ext, depth + 1)?;
                (bt, Arc::new(apply_delta(&bd, &data)?))
            }
            _ => {
                let (bt, bd) = if let Some(&b) = self.by_oid.get(&e.base_oid).filter(|&&b| b != j) {
                    self.content_d(b, out, ext, depth + 1)?
                } else {
                    self.ext_content(&e.base_oid, ext)?.ok_or_else(|| anyhow!("lost external base"))?
                };
                (bt, Arc::new(apply_delta(&bd, &data)?))
            }
        };
        self.cache.put(off, res.0, res.1.clone());
        Ok(res)
    }

    fn ext_content(&mut self, oid: &Oid, ext: &mut ExtLookup) -> Result<Option<(u8, Arc<Vec<u8>>)>> {
        if let Some(c) = self.ext_cache.get(oid) {
            return Ok(Some(c.clone()));
        }
        match ext(oid)? {
            Some((t, d, pack)) => {
                self.ext_bases.push((*oid, pack));
                self.ext_cache.insert(*oid, (t, d.clone()));
                Ok(Some((t, d)))
            }
            None => Ok(None),
        }
    }

    fn try_resolve(&mut self, i: usize, delta: &[u8], out: &mut BufWriter<File>, ext: &mut ExtLookup, _final: bool) -> Result<bool> {
        let e = self.entries[i].clone();
        let base = if e.kind == T_OFS {
            let b = self.by_off[&e.base_off];
            if !self.resolved[b] {
                return Ok(false);
            }
            self.content(b, out, ext)?
        } else if let Some(&b) = self.by_oid.get(&e.base_oid) {
            self.content(b, out, ext)?
        } else {
            match self.ext_content(&e.base_oid, ext)? {
                Some(c) => c,
                None => return Ok(false),
            }
        };
        let data = apply_delta(&base.1, delta).with_context(|| format!("bad delta at offset {}", e.offset))?;
        self.finish_obj(i, base.0, Arc::new(data))?;
        Ok(true)
    }
}
