//! Build the inline pack for a fetch by splicing stored packs: compressed entry
//! data is copied verbatim; only entry headers / delta base references are
//! re-encoded. Duplicate objects across packs are dropped (their dependants are
//! re-pointed), and delta bases the client may not hold are expanded.
use crate::blob::Blobs;
use crate::pack::*;
use anyhow::{anyhow, Result};
use flate2::write::ZlibEncoder;
use flate2::Compression;
use sha1::{Digest, Sha1};
use std::collections::{HashMap, HashSet};
use std::io::Write;
use std::sync::Arc;

pub struct OutOpts {
    pub ofs: bool,
    pub thin: bool,
    pub no_ref_delta: bool,
}

#[derive(Default, Debug)]
pub struct OutStats {
    pub objects: u64,
    pub bytes: u64,
    pub expanded: u64,
    /// whole objects from covers replaced by their L0 delta
    pub substituted: u64,
}

pub struct Sink<'a> {
    f: &'a mut dyn FnMut(&[u8]) -> Result<()>,
    sha: Sha1,
    n: u64,
}
impl<'a> Sink<'a> {
    pub fn new(f: &'a mut dyn FnMut(&[u8]) -> Result<()>) -> Self {
        Sink { f, sha: Sha1::new(), n: 0 }
    }
    pub fn write(&mut self, b: &[u8]) -> Result<()> {
        self.sha.update(b);
        self.n += b.len() as u64;
        (self.f)(b)
    }
    pub fn finish(self) -> Result<(u64, Oid)> {
        let d = Oid::from_slice(&self.sha.clone().finalize());
        (self.f)(&d.0)?;
        Ok((self.n + 20, d))
    }
}

pub fn compress(data: &[u8]) -> Vec<u8> {
    let mut e = ZlibEncoder::new(Vec::with_capacity(data.len() / 2 + 64), Compression::new(4));
    e.write_all(data).unwrap();
    e.finish().unwrap()
}

/// `exclude`: objects that must not be emitted (they are delivered by
/// packfile URIs and will be indexed after this pack). `holds(oid)` tells
/// whether the client certainly already holds an object (for thin deltas).
pub fn write_spliced(
    blobs: &Blobs,
    repo: &str,
    packs: &[String],
    exclude: &HashSet<Oid>,
    opts: &OutOpts,
    holds: &dyn Fn(&Oid) -> bool,
    out: &mut dyn FnMut(&[u8]) -> Result<()>,
) -> Result<(OutStats, Oid)> {
    write_spliced_alt(blobs, repo, packs, exclude, opts, holds, &|_, _| None, out)
}

/// `alt(oid)`: the object's entry in the L0 pack that introduced it. A whole object taken
/// from a cover is replaced by that L0 entry when the L0 entry is a delta whose base is
/// already in the output (or, for thin-pack clients, provably held): covers keep planning
/// logarithmic while the inline pack stays as small as the L0 deltas.
#[allow(clippy::too_many_arguments)]
pub fn write_spliced_alt(
    blobs: &Blobs,
    repo: &str,
    packs: &[String],
    exclude: &HashSet<Oid>,
    opts: &OutOpts,
    holds: &dyn Fn(&Oid) -> bool,
    alt: &dyn Fn(&Oid, &str) -> Option<(String, usize)>,
    out: &mut dyn FnMut(&[u8]) -> Result<()>,
) -> Result<(OutStats, Oid)> {
    let idxs: Vec<Arc<PackIndex>> = packs.iter().map(|p| blobs.index(repo, p)).collect::<Result<_>>()?;
    // plan
    let mut planned: HashSet<Oid> = HashSet::new();
    let mut items: Vec<(usize, usize)> = Vec::new();
    for (pi, idx) in idxs.iter().enumerate() {
        for (ei, e) in idx.entries.iter().enumerate() {
            if exclude.contains(&e.oid) {
                continue;
            }
            if planned.insert(e.oid) {
                items.push((pi, ei));
            }
        }
    }
    let mut st = OutStats::default();
    let mut sink = Sink::new(out);
    let mut hdr = Vec::with_capacity(12);
    hdr.extend_from_slice(b"PACK");
    hdr.extend_from_slice(&2u32.to_be_bytes());
    hdr.extend_from_slice(&(items.len() as u32).to_be_bytes());
    sink.write(&hdr)?;
    let mut emitted: HashMap<Oid, u64> = HashMap::with_capacity(items.len());
    let mut pos: u64 = 12;
    let mut buf: Vec<u8> = Vec::new();
    for (pi, ei) in items {
        let idx = &idxs[pi];
        let e = &idx.entries[ei];
        buf.clear();
        let mut expand = false;
        match e.kind {
            T_COMMIT | T_TREE | T_BLOB | T_TAG => {
                let mut done = false;
                if let Some((lp, lei)) = alt(&e.oid, &idx.id).filter(|(lp, _)| *lp != idx.id) {
                    let lidx = blobs.index(repo, &lp)?;
                    // maps are validated when loaded (Blobs::alt_map); still never index out of range here
                    if let Some(le) = lidx.entries.get(lei).filter(|le| le.oid == e.oid && (le.kind == T_OFS || le.kind == T_REF)) {
                        let base = if le.kind == T_OFS { lidx.by_offset(le.base_off).map(|b| lidx.entries[b].oid) } else { Some(le.base_oid) };
                        if let Some(b) = base {
                            if let Some(&bpos) = emitted.get(&b) {
                                if opts.ofs {
                                    encode_entry_header(T_OFS, le.size, &mut buf);
                                    encode_ofs(pos - bpos, &mut buf);
                                    done = true;
                                } else if !opts.no_ref_delta {
                                    encode_entry_header(T_REF, le.size, &mut buf);
                                    buf.extend_from_slice(&b.0);
                                    done = true;
                                }
                            } else if !planned.contains(&b) && opts.thin && !opts.no_ref_delta && !exclude.contains(&b) && holds(&b) {
                                encode_entry_header(T_REF, le.size, &mut buf);
                                buf.extend_from_slice(&b.0);
                                done = true;
                            }
                            if done {
                                buf.extend_from_slice(&blobs.read_at(repo, &lp, le.data_off, (le.end - le.data_off) as usize)?);
                                st.substituted += 1;
                            }
                        }
                    }
                }
                if !done {
                    let raw = blobs.read_at(repo, &idx.id, e.offset, (e.end - e.offset) as usize)?;
                    buf.extend_from_slice(&raw);
                }
            }
            _ => {
                let base_oid = if e.kind == T_OFS {
                    let bi = idx.by_offset(e.base_off).ok_or_else(|| anyhow!("bad ofs base"))?;
                    idx.entries[bi].oid
                } else {
                    e.base_oid
                };
                if let Some(&bpos) = emitted.get(&base_oid) {
                    if opts.ofs {
                        encode_entry_header(T_OFS, e.size, &mut buf);
                        encode_ofs(pos - bpos, &mut buf);
                    } else if !opts.no_ref_delta {
                        encode_entry_header(T_REF, e.size, &mut buf);
                        buf.extend_from_slice(&base_oid.0);
                    } else {
                        expand = true;
                    }
                } else if planned.contains(&base_oid) && !opts.no_ref_delta {
                    // base appears later in this pack: allowed for REF_DELTA
                    encode_entry_header(T_REF, e.size, &mut buf);
                    buf.extend_from_slice(&base_oid.0);
                } else if !planned.contains(&base_oid) && opts.thin && !opts.no_ref_delta && !exclude.contains(&base_oid) && holds(&base_oid) {
                    encode_entry_header(T_REF, e.size, &mut buf);
                    buf.extend_from_slice(&base_oid.0);
                } else {
                    expand = true;
                }
                if !expand {
                    let raw = blobs.read_at(repo, &idx.id, e.data_off, (e.end - e.data_off) as usize)?;
                    buf.extend_from_slice(&raw);
                }
            }
        }
        if expand {
            let (t, data) = blobs.read_object(repo, &idx.id, ei)?;
            encode_entry_header(t, data.len() as u64, &mut buf);
            buf.extend_from_slice(&compress(&data));
            st.expanded += 1;
        }
        emitted.insert(e.oid, pos);
        sink.write(&buf)?;
        pos += buf.len() as u64;
        st.objects += 1;
    }
    let (n, sum) = sink.finish()?;
    st.bytes = n;
    Ok((st, sum))
}
