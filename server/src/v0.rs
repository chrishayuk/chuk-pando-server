//! Protocol v0/v1 upload-pack over stateless HTTP (multi_ack_detailed, no-done,
//! side-band-64k, thin-pack, ofs-delta). Uses the same planner and pack writer as v2.
use crate::pack::*;
use crate::packout::{self, OutOpts};
use crate::proto::{flush, pkt, pkt_str, read_pkt, Pkt, AGENT};
use crate::repo::*;
use anyhow::{bail, Result};
use serde_json::json;
use std::collections::HashSet;
use std::io::BufRead;
use std::sync::Arc;

const CAPS: &str = "multi_ack_detailed no-done side-band-64k thin-pack ofs-delta no-progress object-format=sha1 allow-tip-sha1-in-want";

pub fn advertise(core: &Core, repo: &str) -> Vec<u8> {
    let rs = match core.repo(repo) {
        Some(r) => r,
        None => return Vec::new(),
    };
    let mut refs = core.list_refs(repo);
    refs.sort_by(|a, b| a.0.cmp(&b.0));
    let caps = CAPS;
    let mut lines: Vec<String> = Vec::new();
    let head = refs.iter().find(|(n, _)| *n == rs.trunk).map(|(_, p)| p.tip.clone());
    if let Some(h) = &head {
        lines.push(format!("{} HEAD", h));
    }
    for (n, p) in &refs {
        lines.push(format!("{} {}", p.tip, n));
        if let Some(pe) = &p.peeled {
            lines.push(format!("{} {}^{{}}", pe, n));
        }
    }
    let mut out = Vec::new();
    let symref = if head.is_some() { format!(" symref=HEAD:{}", rs.trunk) } else { String::new() };
    if lines.is_empty() {
        pkt_str(&mut out, &format!("{} capabilities^{{}}\0{} {}\n", ZERO_HEX, caps, AGENT));
    }
    for (i, l) in lines.iter().enumerate() {
        if i == 0 {
            pkt_str(&mut out, &format!("{}\0{}{} {}\n", l, caps, symref, AGENT));
        } else {
            pkt_str(&mut out, &format!("{}\n", l));
        }
    }
    flush(&mut out);
    out
}

pub fn handle_upload_v0<R: BufRead>(core: &Arc<Core>, repo: &str, mut body: R, out: &mut dyn FnMut(Vec<u8>) -> Result<()>) -> Result<()> {
    let mut wants = Vec::new();
    let mut haves = Vec::new();
    let mut caps: HashSet<String> = HashSet::new();
    let mut done = false;
    loop {
        match read_pkt(&mut body)? {
            None => break,
            Some(Pkt::Flush) => continue,
            Some(Pkt::Data(d)) => {
                let s = String::from_utf8_lossy(&d).trim_end().to_string();
                if let Some(w) = s.strip_prefix("want ") {
                    let mut it = w.split(' ');
                    wants.push(Oid::from_hex(it.next().unwrap_or(""))?);
                    for c in it {
                        caps.insert(c.to_string());
                    }
                } else if let Some(h) = s.strip_prefix("have ") {
                    haves.push(Oid::from_hex(h.trim())?);
                } else if s == "done" {
                    done = true;
                    break;
                } else if s.starts_with("shallow ") || s.starts_with("deepen") {
                    bail!("shallow fetch is not supported by this server");
                } else if s.starts_with("filter ") {
                    bail!("partial clone (filter) is not supported by this server");
                }
            }
            Some(_) => {}
        }
    }
    if wants.is_empty() {
        return Ok(());
    }
    let plan = core.plan_fetch(repo, &wants, &haves)?;
    let multi_ack = caps.contains("multi_ack_detailed") || caps.contains("multi_ack");
    let no_done = caps.contains("no-done");
    let mut o = Vec::new();
    let last = plan.acks.last().cloned();
    if !done {
        for a in &plan.acks {
            if caps.contains("multi_ack_detailed") {
                pkt_str(&mut o, &format!("ACK {} common\n", a));
            } else if multi_ack {
                pkt_str(&mut o, &format!("ACK {} continue\n", a));
            }
        }
        let ready = last.is_some() && caps.contains("multi_ack_detailed");
        if ready {
            pkt_str(&mut o, &format!("ACK {} ready\n", last.unwrap()));
        }
        pkt_str(&mut o, "NAK\n");
        if !(ready && no_done) {
            core.events.emit("fetch_negotiate", json!({"repo": repo, "protocol": 0, "wants": wants.len(), "haves": haves.len(), "acks": plan.acks.len()}));
            return out(o);
        }
        pkt_str(&mut o, &format!("ACK {}\n", last.unwrap()));
    } else {
        match last {
            Some(l) if multi_ack => pkt_str(&mut o, &format!("ACK {}\n", l)),
            _ => pkt_str(&mut o, "NAK\n"),
        }
    }
    out(o)?;
    let sideband = caps.contains("side-band-64k") || caps.contains("side-band");
    crate::proto::set_sideband(sideband);
    let max = if caps.contains("side-band-64k") { 65515 } else { 995 };
    let opts = OutOpts { ofs: caps.contains("ofs-delta"), thin: caps.contains("thin-pack"), no_ref_delta: false };
    let holds = |oid: &Oid| core.client_holds(&plan, oid);
    let mut chunk: Vec<u8> = Vec::new();
    let st;
    {
        let mut write = |b: &[u8]| -> Result<()> {
            chunk.extend_from_slice(b);
            while chunk.len() >= max {
                emit(&mut chunk, max, sideband, out)?;
            }
            Ok(())
        };
        let alt = |oid: &Oid, pack: &str| {
            // Covers carry a precomputed substitution map (built during compaction); others use the object index.
            if let Some(m) = core.blobs.alt_map(repo, pack) {
                crate::metrics::alt_map_lookup();
                return m.get(oid).cloned();
            }
            core.locate(&plan.rs, oid).ok().flatten().map(|(p, ei, _)| (p, ei))
        };
        let (s, _) = packout::write_spliced_alt(&core.blobs, repo, &plan.packs, &HashSet::new(), &opts, &holds, &alt, &mut write)?;
        st = s;
    }
    while !chunk.is_empty() {
        emit(&mut chunk, max, sideband, out)?;
    }
    if sideband {
        let mut f = Vec::new();
        flush(&mut f);
        out(f)?;
    }
    core.events.emit(
        "fetch_served",
        json!({"repo": repo, "protocol": 0, "wants": wants.iter().map(|w| w.hex()).collect::<Vec<_>>(), "haves": haves.len(), "acks": plan.acks.len(),
               "packs": plan.packs, "uri_packs": [], "inline_objects": st.objects, "inline_bytes": st.bytes, "expanded": st.expanded,
               "plan_steps": plan.steps, "covers_used": plan.covers_used, "plan_metadata_reads": plan.metadata_reads}),
    );
    Ok(())
}

fn emit(chunk: &mut Vec<u8>, max: usize, sideband: bool, out: &mut dyn FnMut(Vec<u8>) -> Result<()>) -> Result<()> {
    let n = chunk.len().min(max);
    let mut o = Vec::with_capacity(n + 8);
    if sideband {
        let mut d = Vec::with_capacity(n + 1);
        d.push(1u8);
        d.extend_from_slice(&chunk[..n]);
        pkt(&mut o, &d);
    } else {
        o.extend_from_slice(&chunk[..n]);
    }
    chunk.drain(..n);
    out(o)
}
