//! Git object / pack primitives: object ids, hashing, delta application,
//! entry headers, object reference parsing and the pack sidecar index (".pidx").
use anyhow::{anyhow, bail, Result};
use sha1::{Digest, Sha1};
use std::collections::HashMap;
use std::fmt;

#[derive(Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Default)]
pub struct Oid(pub [u8; 20]);

impl Oid {
    pub const ZERO: Oid = Oid([0u8; 20]);
    pub fn is_zero(&self) -> bool {
        self.0 == [0u8; 20]
    }
    pub fn hex(&self) -> String {
        let mut s = String::with_capacity(40);
        for b in self.0.iter() {
            s.push(HEX[(b >> 4) as usize] as char);
            s.push(HEX[(b & 15) as usize] as char);
        }
        s
    }
    pub fn from_hex(s: &str) -> Result<Oid> {
        let b = s.as_bytes();
        if b.len() != 40 {
            bail!("bad object id length: {:?}", s);
        }
        let mut o = [0u8; 20];
        for i in 0..20 {
            o[i] = (unhex(b[2 * i])? << 4) | unhex(b[2 * i + 1])?;
        }
        Ok(Oid(o))
    }
    pub fn from_slice(b: &[u8]) -> Oid {
        let mut o = [0u8; 20];
        o.copy_from_slice(&b[..20]);
        Oid(o)
    }
}
const HEX: &[u8; 16] = b"0123456789abcdef";
fn unhex(c: u8) -> Result<u8> {
    Ok(match c {
        b'0'..=b'9' => c - b'0',
        b'a'..=b'f' => c - b'a' + 10,
        b'A'..=b'F' => c - b'A' + 10,
        _ => bail!("bad hex digit"),
    })
}
impl fmt::Debug for Oid {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.hex())
    }
}
impl fmt::Display for Oid {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.hex())
    }
}

pub const T_COMMIT: u8 = 1;
pub const T_TREE: u8 = 2;
pub const T_BLOB: u8 = 3;
pub const T_TAG: u8 = 4;
pub const T_OFS: u8 = 6;
pub const T_REF: u8 = 7;

pub fn type_name(t: u8) -> &'static str {
    match t {
        T_COMMIT => "commit",
        T_TREE => "tree",
        T_BLOB => "blob",
        T_TAG => "tag",
        _ => "unknown",
    }
}
pub fn type_from_name(s: &[u8]) -> Option<u8> {
    match s {
        b"commit" => Some(T_COMMIT),
        b"tree" => Some(T_TREE),
        b"blob" => Some(T_BLOB),
        b"tag" => Some(T_TAG),
        _ => None,
    }
}

pub fn hash_object(t: u8, data: &[u8]) -> Oid {
    let mut h = Sha1::new();
    h.update(type_name(t).as_bytes());
    h.update(b" ");
    h.update(data.len().to_string().as_bytes());
    h.update([0u8]);
    h.update(data);
    Oid::from_slice(&h.finalize())
}

/// Encode a pack entry header (type + inflated size).
pub fn encode_entry_header(t: u8, mut size: u64, out: &mut Vec<u8>) {
    let mut c = ((t & 7) << 4) | (size & 15) as u8;
    size >>= 4;
    while size > 0 {
        out.push(c | 0x80);
        c = (size & 0x7f) as u8;
        size >>= 7;
    }
    out.push(c);
}

/// Encode an OFS_DELTA negative offset.
pub fn encode_ofs(mut off: u64, out: &mut Vec<u8>) {
    let mut buf = [0u8; 16];
    let mut pos = buf.len() - 1;
    buf[pos] = (off & 0x7f) as u8;
    off >>= 7;
    while off > 0 {
        off -= 1;
        pos -= 1;
        buf[pos] = 0x80 | (off & 0x7f) as u8;
        off >>= 7;
    }
    out.extend_from_slice(&buf[pos..]);
}

fn delta_varint(d: &[u8], pos: &mut usize) -> Result<u64> {
    let mut v = 0u64;
    let mut shift = 0;
    loop {
        let c = *d.get(*pos).ok_or_else(|| anyhow!("truncated delta header"))?;
        *pos += 1;
        v |= ((c & 0x7f) as u64) << shift;
        shift += 7;
        if c & 0x80 == 0 {
            return Ok(v);
        }
        if shift > 63 {
            bail!("delta varint overflow");
        }
    }
}

pub fn apply_delta(base: &[u8], delta: &[u8]) -> Result<Vec<u8>> {
    let mut p = 0usize;
    let src = delta_varint(delta, &mut p)?;
    let dst = delta_varint(delta, &mut p)?;
    if src as usize != base.len() {
        bail!("delta base size mismatch ({} != {})", src, base.len());
    }
    let mut out = Vec::with_capacity(dst as usize);
    while p < delta.len() {
        let c = delta[p];
        p += 1;
        if c & 0x80 != 0 {
            let mut off: u64 = 0;
            let mut sz: u64 = 0;
            for i in 0..4 {
                if c & (1 << i) != 0 {
                    off |= (*delta.get(p).ok_or_else(|| anyhow!("truncated delta"))? as u64) << (8 * i);
                    p += 1;
                }
            }
            for i in 0..3 {
                if c & (0x10 << i) != 0 {
                    sz |= (*delta.get(p).ok_or_else(|| anyhow!("truncated delta"))? as u64) << (8 * i);
                    p += 1;
                }
            }
            if sz == 0 {
                sz = 0x10000;
            }
            let (o, s) = (off as usize, sz as usize);
            if o.checked_add(s).map(|e| e > base.len()).unwrap_or(true) {
                bail!("delta copy out of range");
            }
            out.extend_from_slice(&base[o..o + s]);
        } else if c != 0 {
            let n = c as usize;
            if p + n > delta.len() {
                bail!("truncated delta insert");
            }
            out.extend_from_slice(&delta[p..p + n]);
            p += n;
        } else {
            bail!("delta opcode 0 is reserved");
        }
        if out.len() as u64 > dst {
            bail!("delta result too large");
        }
    }
    if out.len() as u64 != dst {
        bail!("delta result size mismatch");
    }
    Ok(out)
}

/// Parse the outgoing object references of an object (graph edges) and do
/// light-weight structural validation. Gitlinks (mode 160000) are skipped.
/// For tags the target type is returned via `tag_target`.
pub fn parse_refs(t: u8, data: &[u8], out: &mut Vec<Oid>, tag_target: &mut Option<(Oid, u8)>) -> Result<()> {
    match t {
        T_BLOB => Ok(()),
        T_TREE => {
            let mut p = 0;
            while p < data.len() {
                let sp = data[p..].iter().position(|&c| c == b' ').ok_or_else(|| anyhow!("bad tree entry"))? + p;
                let mode = &data[p..sp];
                let nul = data[sp..].iter().position(|&c| c == 0).ok_or_else(|| anyhow!("bad tree entry"))? + sp;
                if nul + 21 > data.len() {
                    bail!("truncated tree entry");
                }
                if nul == sp + 1 {
                    bail!("empty tree entry name");
                }
                let oid = Oid::from_slice(&data[nul + 1..nul + 21]);
                if mode != b"160000" {
                    out.push(oid);
                }
                p = nul + 21;
            }
            Ok(())
        }
        T_COMMIT => {
            let mut lines = data.split(|&c| c == b'\n');
            let first = lines.next().ok_or_else(|| anyhow!("empty commit"))?;
            if !first.starts_with(b"tree ") || first.len() != 45 {
                bail!("commit without tree");
            }
            out.push(Oid::from_hex(std::str::from_utf8(&first[5..])?)?);
            for l in lines {
                if l.starts_with(b"parent ") && l.len() == 47 {
                    out.push(Oid::from_hex(std::str::from_utf8(&l[7..])?)?);
                } else {
                    break;
                }
            }
            Ok(())
        }
        T_TAG => {
            let mut lines = data.split(|&c| c == b'\n');
            let first = lines.next().ok_or_else(|| anyhow!("empty tag"))?;
            if !first.starts_with(b"object ") || first.len() != 47 {
                bail!("tag without object");
            }
            let target = Oid::from_hex(std::str::from_utf8(&first[7..])?)?;
            let second = lines.next().ok_or_else(|| anyhow!("tag without type"))?;
            if !second.starts_with(b"type ") {
                bail!("tag without type");
            }
            let tt = type_from_name(&second[5..]).ok_or_else(|| anyhow!("bad tag type"))?;
            out.push(target);
            *tag_target = Some((target, tt));
            Ok(())
        }
        _ => bail!("bad object type {}", t),
    }
}

/// One entry of a stored pack, in pack (offset) order.
#[derive(Clone, Debug)]
pub struct Entry {
    pub oid: Oid,
    pub offset: u64,
    /// offset of the zlib stream (after header and base reference)
    pub data_off: u64,
    /// end offset (start of next entry or trailer)
    pub end: u64,
    /// final object type (1..4)
    pub otype: u8,
    /// stored kind: 1..4 (whole), 6 (ofs delta), 7 (ref delta)
    pub kind: u8,
    /// size field from the entry header (inflated size of data as stored)
    pub size: u64,
    /// OFS: absolute base offset. REF: base offset if the base is inside
    /// this pack, otherwise u64::MAX (then `base_oid` is external).
    pub base_off: u64,
    pub base_oid: Oid,
}

/// Sidecar index of a pack (never modifies the pack itself).
#[derive(Clone, Debug, Default)]
pub struct PackIndex {
    pub id: String,
    pub entries: Vec<Entry>,
    /// indices into `entries` sorted by oid
    pub sorted: Vec<u32>,
    /// Graph prerequisites: referenced but not contained object ids.
    pub prereqs: Vec<Oid>,
    /// External delta bases (thin pack): base oid -> pack id where it was found.
    pub ext_bases: Vec<(Oid, String)>,
    /// annotated tags contained: (tag oid, target oid, target type)
    pub tags: Vec<(Oid, Oid, u8)>,
    pub pack_size: u64,
}

impl PackIndex {
    pub fn finish(&mut self) {
        let mut s: Vec<u32> = (0..self.entries.len() as u32).collect();
        s.sort_by(|a, b| self.entries[*a as usize].oid.cmp(&self.entries[*b as usize].oid));
        self.sorted = s;
    }
    pub fn find(&self, oid: &Oid) -> Option<usize> {
        self.sorted
            .binary_search_by(|i| self.entries[*i as usize].oid.cmp(oid))
            .ok()
            .map(|i| self.sorted[i] as usize)
    }
    pub fn by_offset(&self, off: u64) -> Option<usize> {
        self.entries.binary_search_by(|e| e.offset.cmp(&off)).ok()
    }
    pub fn is_thin(&self) -> bool {
        !self.ext_bases.is_empty()
    }
    pub fn has_ref_delta(&self) -> bool {
        self.entries.iter().any(|e| e.kind == T_REF)
    }
    pub fn ext_base_pack(&self, oid: &Oid) -> Option<&str> {
        self.ext_bases.iter().find(|(o, _)| o == oid).map(|(_, p)| p.as_str())
    }

    /// Is `other` (e.g. an index already stored under this pack's key by another writer) an equivalent index of the
    /// same pack? Everything derived from the pack bytes must be identical: id, size, every entry (offset, type,
    /// stored kind, sizes, delta bases), prerequisites and tags. The one legitimate difference between two correct
    /// indexes of one thin pack is which pack each external delta base was located in (each frontend records the
    /// pack it found the base in). Returns those (base oid, `other`'s pack) pairs that differ, for the caller to
    /// check that `other`'s choice really holds the base; Err describes the first real difference.
    pub fn equivalent_to(&self, other: &PackIndex) -> std::result::Result<Vec<(Oid, String)>, String> {
        if self.id != other.id {
            return Err(format!("index is for pack {}, not {}", other.id, self.id));
        }
        if self.pack_size != other.pack_size {
            return Err(format!("index records pack size {}, expected {}", other.pack_size, self.pack_size));
        }
        if self.entries.len() != other.entries.len() {
            return Err(format!("index has {} entries, expected {}", other.entries.len(), self.entries.len()));
        }
        for (a, b) in self.entries.iter().zip(&other.entries) {
            if (a.oid, a.offset, a.data_off, a.end, a.otype, a.kind, a.size, a.base_off, a.base_oid)
                != (b.oid, b.offset, b.data_off, b.end, b.otype, b.kind, b.size, b.base_off, b.base_oid)
            {
                return Err(format!("index entry at offset {} differs (object {} vs {})", a.offset, a.oid.hex(), b.oid.hex()));
            }
        }
        let sorted = |v: &Vec<Oid>| {
            let mut v = v.clone();
            v.sort();
            v
        };
        if sorted(&self.prereqs) != sorted(&other.prereqs) {
            return Err("index prerequisites differ".into());
        }
        let mut ta = self.tags.clone();
        let mut tb = other.tags.clone();
        ta.sort();
        tb.sort();
        if ta != tb {
            return Err("index tag records differ".into());
        }
        let mut ba: Vec<Oid> = self.ext_bases.iter().map(|x| x.0).collect();
        let mut bb: Vec<Oid> = other.ext_bases.iter().map(|x| x.0).collect();
        ba.sort();
        bb.sort();
        if ba != bb {
            return Err("index external delta bases differ".into());
        }
        Ok(other.ext_bases.iter().filter(|(o, p)| self.ext_base_pack(o) != Some(p.as_str())).cloned().collect())
    }

    pub fn serialize(&self) -> Vec<u8> {
        let mut v = Vec::with_capacity(64 + self.entries.len() * 80);
        v.extend_from_slice(b"PIDX\0\0\0\x01");
        put_str(&mut v, &self.id);
        put_u64(&mut v, self.pack_size);
        put_u64(&mut v, self.entries.len() as u64);
        for e in &self.entries {
            v.extend_from_slice(&e.oid.0);
            put_u64(&mut v, e.offset);
            put_u64(&mut v, e.data_off);
            put_u64(&mut v, e.end);
            v.push(e.otype);
            v.push(e.kind);
            put_u64(&mut v, e.size);
            put_u64(&mut v, e.base_off);
            v.extend_from_slice(&e.base_oid.0);
        }
        put_u64(&mut v, self.prereqs.len() as u64);
        for p in &self.prereqs {
            v.extend_from_slice(&p.0);
        }
        put_u64(&mut v, self.ext_bases.len() as u64);
        for (o, p) in &self.ext_bases {
            v.extend_from_slice(&o.0);
            put_str(&mut v, p);
        }
        put_u64(&mut v, self.tags.len() as u64);
        for (a, b, t) in &self.tags {
            v.extend_from_slice(&a.0);
            v.extend_from_slice(&b.0);
            v.push(*t);
        }
        let mut h = Sha1::new();
        h.update(&v);
        v.extend_from_slice(&h.finalize());
        v
    }

    pub fn deserialize(b: &[u8]) -> Result<PackIndex> {
        if b.len() < 28 || &b[..8] != b"PIDX\0\0\0\x01" {
            bail!("bad pidx header");
        }
        let (body, sum) = b.split_at(b.len() - 20);
        let mut h = Sha1::new();
        h.update(body);
        if &h.finalize()[..] != sum {
            bail!("pidx checksum mismatch");
        }
        let mut r = Rd { b: body, p: 8 };
        let id = r.str()?;
        let pack_size = r.u64()?;
        let n = r.u64()? as usize;
        let mut entries = Vec::with_capacity(n);
        for _ in 0..n {
            let oid = r.oid()?;
            let offset = r.u64()?;
            let data_off = r.u64()?;
            let end = r.u64()?;
            let otype = r.u8()?;
            let kind = r.u8()?;
            let size = r.u64()?;
            let base_off = r.u64()?;
            let base_oid = r.oid()?;
            entries.push(Entry { oid, offset, data_off, end, otype, kind, size, base_off, base_oid });
        }
        let n = r.u64()? as usize;
        let mut prereqs = Vec::with_capacity(n);
        for _ in 0..n {
            prereqs.push(r.oid()?);
        }
        let n = r.u64()? as usize;
        let mut ext_bases = Vec::with_capacity(n);
        for _ in 0..n {
            let o = r.oid()?;
            ext_bases.push((o, r.str()?));
        }
        let n = r.u64()? as usize;
        let mut tags = Vec::with_capacity(n);
        for _ in 0..n {
            let a = r.oid()?;
            let bb = r.oid()?;
            tags.push((a, bb, r.u8()?));
        }
        let mut idx = PackIndex { id, entries, sorted: vec![], prereqs, ext_bases, tags, pack_size };
        idx.finish();
        Ok(idx)
    }
}

fn put_u64(v: &mut Vec<u8>, x: u64) {
    v.extend_from_slice(&x.to_be_bytes());
}
fn put_str(v: &mut Vec<u8>, s: &str) {
    put_u64(v, s.len() as u64);
    v.extend_from_slice(s.as_bytes());
}
struct Rd<'a> {
    b: &'a [u8],
    p: usize,
}
impl<'a> Rd<'a> {
    fn take(&mut self, n: usize) -> Result<&'a [u8]> {
        if self.p + n > self.b.len() {
            bail!("truncated pidx");
        }
        let s = &self.b[self.p..self.p + n];
        self.p += n;
        Ok(s)
    }
    fn u64(&mut self) -> Result<u64> {
        Ok(u64::from_be_bytes(self.take(8)?.try_into().unwrap()))
    }
    fn u8(&mut self) -> Result<u8> {
        Ok(self.take(1)?[0])
    }
    fn oid(&mut self) -> Result<Oid> {
        Ok(Oid::from_slice(self.take(20)?))
    }
    fn str(&mut self) -> Result<String> {
        let n = self.u64()? as usize;
        Ok(String::from_utf8(self.take(n)?.to_vec())?)
    }
}

/// Parse an entry header at the start of `b`: returns (type, size, header_len).
pub fn decode_entry_header(b: &[u8]) -> Result<(u8, u64, usize)> {
    let mut p = 0;
    let c = *b.first().ok_or_else(|| anyhow!("truncated entry header"))?;
    p += 1;
    let t = (c >> 4) & 7;
    let mut size = (c & 15) as u64;
    let mut shift = 4;
    let mut cc = c;
    while cc & 0x80 != 0 {
        cc = *b.get(p).ok_or_else(|| anyhow!("truncated entry header"))?;
        p += 1;
        size |= ((cc & 0x7f) as u64) << shift;
        shift += 7;
        if shift > 63 {
            bail!("entry size overflow");
        }
    }
    Ok((t, size, p))
}

/// Simple byte-budgeted cache used during ingest / object reads.
pub struct ObjCache {
    map: HashMap<u64, (u8, std::sync::Arc<Vec<u8>>)>,
    order: std::collections::VecDeque<u64>,
    bytes: usize,
    budget: usize,
}
impl ObjCache {
    pub fn new(budget: usize) -> Self {
        ObjCache { map: HashMap::new(), order: Default::default(), bytes: 0, budget }
    }
    pub fn get(&self, k: u64) -> Option<(u8, std::sync::Arc<Vec<u8>>)> {
        self.map.get(&k).cloned()
    }
    pub fn put(&mut self, k: u64, t: u8, d: std::sync::Arc<Vec<u8>>) {
        if d.len() > self.budget / 2 || self.map.contains_key(&k) {
            return;
        }
        self.bytes += d.len();
        self.map.insert(k, (t, d));
        self.order.push_back(k);
        while self.bytes > self.budget {
            if let Some(o) = self.order.pop_front() {
                if let Some((_, d)) = self.map.remove(&o) {
                    self.bytes -= d.len();
                }
            } else {
                break;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn oid(b: u8) -> Oid {
        Oid([b; 20])
    }

    fn idx() -> PackIndex {
        let e = |o: u8, off: u64, kind: u8| Entry {
            oid: oid(o), offset: off, data_off: off + 2, end: off + 10, otype: 3, kind, size: 7, base_off: u64::MAX, base_oid: oid(9),
        };
        let mut i = PackIndex {
            id: "ab".repeat(20),
            entries: vec![e(1, 12, 3), e(2, 22, 7)],
            prereqs: vec![oid(5), oid(6)],
            ext_bases: vec![(oid(9), "p1".into())],
            tags: vec![],
            pack_size: 52,
            sorted: vec![],
        };
        i.finish();
        i
    }

    /// Audit F2: what a 412 may accept. Only the pack recorded for an external base may differ (reported back for
    /// the caller to check); anything derived from the pack bytes must be identical.
    #[test]
    fn index_equivalence_accepts_only_a_different_base_pack() {
        let a = idx();
        assert_eq!(a.equivalent_to(&a.clone()), Ok(vec![]));
        let mut b = a.clone();
        b.prereqs.reverse();
        assert_eq!(a.equivalent_to(&b), Ok(vec![]), "order of prerequisites is not content");
        let mut b = a.clone();
        b.ext_bases[0].1 = "p2".into();
        assert_eq!(a.equivalent_to(&b), Ok(vec![(oid(9), "p2".to_string())]));
        let roundtrip = PackIndex::deserialize(&b.serialize()).unwrap();
        assert_eq!(a.equivalent_to(&roundtrip), Ok(vec![(oid(9), "p2".to_string())]));
        let differs: Vec<Box<dyn Fn(&mut PackIndex)>> = vec![
            Box::new(|i| i.id = "cd".repeat(20)),
            Box::new(|i| i.pack_size += 1),
            Box::new(|i| i.entries[1].size += 1),
            Box::new(|i| i.entries[0].oid = oid(3)),
            Box::new(|i| i.entries[1].base_oid = oid(8)),
            Box::new(|i| { i.entries.pop(); }),
            Box::new(|i| i.prereqs[0] = oid(7)),
            Box::new(|i| i.ext_bases[0].0 = oid(8)),
            Box::new(|i| i.tags.push((oid(1), oid(2), 1))),
        ];
        for (n, f) in differs.iter().enumerate() {
            let mut b = a.clone();
            f(&mut b);
            assert!(a.equivalent_to(&b).is_err(), "difference {} accepted", n);
        }
    }
}
