//! Shallow fetch (`deepen <n>`): the one case where the server must walk the
//! object graph, because a depth-limited pack is not a union of stored packs.
//! Objects are read from the immutable packs (raw entry data reused for
//! non-delta entries) and written into a fresh pack.
use crate::pack::*;
use crate::packout::{compress, Sink};
use crate::repo::*;
use anyhow::{anyhow, bail, Result};
use std::collections::{HashMap, HashSet, VecDeque};

pub struct ShallowPlan {
    pub shallow: Vec<Oid>,
    pub objects: Vec<Oid>,
}

pub fn plan(core: &Core, repo: &str, wants: &[Oid], depth: u64, haves: &[Oid]) -> Result<ShallowPlan> {
    let rs = core.repo(repo).ok_or_else(|| anyhow!("no such repository"))?;
    let have: HashSet<Oid> = haves.iter().cloned().collect();
    let mut objects: Vec<Oid> = Vec::new();
    let mut seen: HashSet<Oid> = HashSet::new();
    let mut commit_depth: HashMap<Oid, u64> = HashMap::new();
    let mut q: VecDeque<(Oid, u64)> = VecDeque::new();
    for w in wants {
        // peel tags (tag objects are included at depth 0)
        let mut cur = *w;
        for _ in 0..16 {
            let (t, d, _) = core.read_obj(&rs, &cur)?.ok_or_else(|| anyhow!("want {} not found", cur))?;
            if t == T_TAG {
                if seen.insert(cur) {
                    objects.push(cur);
                }
                let mut refs = Vec::new();
                let mut tt = None;
                parse_refs(T_TAG, &d, &mut refs, &mut tt)?;
                cur = tt.ok_or_else(|| anyhow!("bad tag"))?.0;
            } else if t == T_COMMIT {
                q.push_back((cur, 1));
                break;
            } else {
                if seen.insert(cur) {
                    objects.push(cur);
                }
                break;
            }
        }
    }
    let mut shallow = Vec::new();
    let mut commits: Vec<Oid> = Vec::new();
    while let Some((c, d)) = q.pop_front() {
        if have.contains(&c) {
            continue;
        }
        if let Some(&od) = commit_depth.get(&c) {
            if od <= d {
                continue;
            }
        }
        commit_depth.insert(c, d);
        let (t, data, _) = core.read_obj(&rs, &c)?.ok_or_else(|| anyhow!("commit {} not found", c))?;
        if t != T_COMMIT {
            bail!("{} is not a commit", c);
        }
        let mut refs = Vec::new();
        let mut tt = None;
        parse_refs(T_COMMIT, &data, &mut refs, &mut tt)?;
        let parents = &refs[1..];
        if !commits.contains(&c) {
            commits.push(c);
        }
        if d >= depth {
            if !parents.is_empty() && !parents.iter().all(|p| have.contains(p)) {
                shallow.push(c);
            }
        } else {
            for p in parents {
                q.push_back((*p, d + 1));
            }
        }
    }
    // commits whose depth was later lowered below the limit are not shallow
    shallow.retain(|c| commit_depth.get(c).map(|d| *d >= depth).unwrap_or(true));
    shallow.sort();
    shallow.dedup();
    for c in &commits {
        if seen.insert(*c) {
            objects.push(*c);
        }
    }
    // trees and blobs
    let mut stack: Vec<Oid> = Vec::new();
    for c in &commits {
        let (_, data, _) = core.read_obj(&rs, c)?.unwrap();
        let mut refs = Vec::new();
        let mut tt = None;
        parse_refs(T_COMMIT, &data, &mut refs, &mut tt)?;
        stack.push(refs[0]);
    }
    while let Some(o) = stack.pop() {
        if !seen.insert(o) {
            continue;
        }
        objects.push(o);
        let (t, data, _) = core.read_obj(&rs, &o)?.ok_or_else(|| anyhow!("object {} not found", o))?;
        if t == T_TREE {
            let mut refs = Vec::new();
            let mut tt = None;
            parse_refs(T_TREE, &data, &mut refs, &mut tt)?;
            for r in refs {
                if !seen.contains(&r) {
                    stack.push(r);
                }
            }
        }
    }
    Ok(ShallowPlan { shallow, objects })
}

/// Write the objects as a fresh pack (whole objects only).
pub fn write_pack(core: &Core, repo: &str, objects: &[Oid], out: &mut dyn FnMut(&[u8]) -> Result<()>) -> Result<u64> {
    let rs = core.repo(repo).ok_or_else(|| anyhow!("no such repository"))?;
    let mut sink = Sink::new(out);
    let mut hdr = Vec::new();
    hdr.extend_from_slice(b"PACK");
    hdr.extend_from_slice(&2u32.to_be_bytes());
    hdr.extend_from_slice(&(objects.len() as u32).to_be_bytes());
    sink.write(&hdr)?;
    let mut buf = Vec::new();
    for o in objects {
        buf.clear();
        let (pack, ei, _) = core.locate(&rs, o)?.ok_or_else(|| anyhow!("object {} vanished", o))?;
        let idx = core.blobs.index(repo, &pack)?;
        let e = &idx.entries[ei];
        if e.kind == e.otype {
            buf.extend_from_slice(&core.blobs.read_at(repo, &pack, e.offset, (e.end - e.offset) as usize)?);
        } else {
            let (t, d) = core.blobs.read_object(repo, &pack, ei)?;
            encode_entry_header(t, d.len() as u64, &mut buf);
            buf.extend_from_slice(&compress(&d));
        }
        sink.write(&buf)?;
    }
    let (n, _) = sink.finish()?;
    Ok(n)
}
