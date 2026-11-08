//! Git smart-HTTP protocol handlers (run on blocking threads): receive-pack
//! (protocol v0/v1, the only push protocol) and upload-pack protocol v2
//! (ls-refs, fetch with packfile-uris).
use crate::ingest;
use crate::pack::*;
use crate::packout::{self, OutOpts};
use crate::repo::*;
use anyhow::{anyhow, bail, Context, Result};
use serde_json::json;
use std::collections::HashSet;
use std::io::{BufRead, Read};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

pub const AGENT: &str = "agent=pando/0.1";

pub enum Pkt {
    Flush,
    Delim,
    End,
    Data(Vec<u8>),
}

pub fn read_pkt<R: Read + ?Sized>(r: &mut R) -> Result<Option<Pkt>> {
    let mut h = [0u8; 4];
    let mut got = 0;
    while got < 4 {
        let n = r.read(&mut h[got..])?;
        if n == 0 {
            if got == 0 {
                return Ok(None);
            }
            bail!("truncated pkt-line header");
        }
        got += n;
    }
    let s = std::str::from_utf8(&h)?;
    let n = usize::from_str_radix(s, 16).map_err(|_| anyhow!("bad pkt-line length {:?}", s))?;
    match n {
        0 => Ok(Some(Pkt::Flush)),
        1 => Ok(Some(Pkt::Delim)),
        2 => Ok(Some(Pkt::End)),
        3 => bail!("bad pkt-line length 3"),
        _ => {
            let mut v = vec![0u8; n - 4];
            r.read_exact(&mut v).context("truncated pkt-line")?;
            Ok(Some(Pkt::Data(v)))
        }
    }
}

pub fn pkt(out: &mut Vec<u8>, data: &[u8]) {
    out.extend_from_slice(format!("{:04x}", data.len() + 4).as_bytes());
    out.extend_from_slice(data);
}
pub fn pkt_str(out: &mut Vec<u8>, s: &str) {
    pkt(out, s.as_bytes());
}
pub fn flush(out: &mut Vec<u8>) {
    out.extend_from_slice(b"0000");
}
pub fn delim(out: &mut Vec<u8>) {
    out.extend_from_slice(b"0001");
}

fn trim_lf(v: &[u8]) -> &[u8] {
    if v.last() == Some(&b'\n') {
        &v[..v.len() - 1]
    } else {
        v
    }
}

static PUSH_CTR: AtomicU64 = AtomicU64::new(0);

thread_local! {
    /// Whether this request's response has switched to side-band multiplexing. An error after that point must be
    /// sent on band 3; a bare `ERR` pkt-line is then read as band 'E' (69) and the client reports "bad band #69"
    /// instead of the reason (audit F8). Requests run on one blocking thread each (http.rs), which resets it.
    static IN_SIDEBAND: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}
pub fn set_sideband(on: bool) {
    IN_SIDEBAND.with(|c| c.set(on));
}
pub fn in_sideband() -> bool {
    IN_SIDEBAND.with(|c| c.get())
}

pub const RECEIVE_CAPS: &str = "report-status report-status-v2 delete-refs side-band-64k quiet atomic ofs-delta push-options object-format=sha1";

/// Body of GET info/refs?service=git-receive-pack (after the service header).
pub fn receive_advertisement(core: &Core, repo: &str, explicit_haves: bool) -> Vec<u8> {
    let rs = core.repo(repo).unwrap();
    let mut refs = core.list_refs(repo);
    refs.sort_by(|a, b| a.0.cmp(&b.0));
    let mut caps = RECEIVE_CAPS.to_string();
    if explicit_haves {
        // trunk-only prerequisites, and self-contained deltas so every L0 pack
        // pushed by such a client is directly servable as a packfile URI
        caps.push_str(" explicit-haves no-ref-delta");
    }
    caps.push(' ');
    caps.push_str(AGENT);
    let mut lines: Vec<String> = Vec::new();
    for (name, p) in &refs {
        lines.push(format!("{} {}", p.tip, name));
    }
    if explicit_haves {
        // the pack prerequisite base: trunk only
        if let Some((_, p)) = refs.iter().find(|(n, _)| *n == rs.trunk) {
            lines.push(format!("{} .have", p.tip));
        }
    }
    let mut out = Vec::new();
    if lines.is_empty() {
        pkt_str(&mut out, &format!("{} capabilities^{{}}\0{}\n", ZERO_HEX, caps));
    } else {
        for (i, l) in lines.iter().enumerate() {
            if i == 0 {
                pkt_str(&mut out, &format!("{}\0{}\n", l, caps));
            } else {
                pkt_str(&mut out, &format!("{}\n", l));
            }
        }
    }
    flush(&mut out);
    core.events.emit("receive_advertised", json!({"repo": repo, "refs": refs.len(), "explicit_haves": explicit_haves}));
    out
}

pub struct ReceiveOutcome {
    pub bytes: u64,
    pub packs_new: u64,
}

pub fn handle_receive<R: BufRead>(core: &Arc<Core>, repo: &str, mut body: R, out: &mut dyn FnMut(Vec<u8>) -> Result<()>) -> Result<()> {
    let push_id = if crate::events::deterministic() {
        format!("p{}", PUSH_CTR.fetch_add(1, Ordering::Relaxed))
    } else {
        format!("p{:x}-{}", crate::blob::now_nanos() & 0xffff_ffff_ffff, PUSH_CTR.fetch_add(1, Ordering::Relaxed))
    };
    let mut cmds: Vec<Command> = Vec::new();
    let mut caps: HashSet<String> = HashSet::new();
    let mut first = true;
    loop {
        match read_pkt(&mut body)? {
            None => break,
            Some(Pkt::Flush) => break,
            Some(Pkt::Data(d)) => {
                let d = trim_lf(&d);
                let (line, cap) = match d.iter().position(|&c| c == 0) {
                    Some(i) => (&d[..i], Some(&d[i + 1..])),
                    None => (d, None),
                };
                if first {
                    if let Some(c) = cap {
                        for x in String::from_utf8_lossy(c).split(' ') {
                            if !x.is_empty() {
                                caps.insert(x.to_string());
                            }
                        }
                    }
                    first = false;
                }
                let line = std::str::from_utf8(line)?;
                if line.starts_with("shallow ") {
                    bail!("shallow pushes are not supported");
                }
                if line.starts_with("push-cert") {
                    bail!("signed pushes are not supported");
                }
                let parts: Vec<&str> = line.splitn(3, ' ').collect();
                if parts.len() != 3 {
                    bail!("bad command line {:?}", line);
                }
                cmds.push(Command { old: Oid::from_hex(parts[0])?, new: Oid::from_hex(parts[1])?, name: parts[2].to_string() });
            }
            Some(_) => bail!("unexpected pkt in command list"),
        }
    }
    let mut push_options = Vec::new();
    if caps.contains("push-options") {
        loop {
            match read_pkt(&mut body)? {
                None | Some(Pkt::Flush) => break,
                Some(Pkt::Data(d)) => push_options.push(String::from_utf8_lossy(trim_lf(&d)).to_string()),
                _ => {}
            }
        }
    }
    let sideband = caps.contains("side-band-64k");
    let atomic = caps.contains("atomic");
    let explicit = caps.contains("explicit-haves");
    let t_push = std::time::Instant::now();
    let _ = crate::s3::prof_take(); // attribute object-store work from here to this push
    core.events.emit(
        "push_received",
        json!({"repo": repo, "push": push_id, "atomic": atomic, "explicit_haves": explicit,
               "cmds": cmds.iter().map(|c| json!({"ref": c.name, "old": c.old.hex(), "new": c.new.hex()})).collect::<Vec<_>>()}),
    );
    let need_pack = cmds.iter().any(|c| !c.new.is_zero());
    let rs = core.repo(repo).ok_or_else(|| anyhow!("no such repository"))?;

    let mut unpack_err: Option<String> = None;
    let mut pack: Option<(Arc<PackIndex>, u32)> = None;
    let mut pack_summary: Option<(String, u64, u64, usize)> = None; // for the operation record (diag.rs)
    if need_pack {
        let tmp = core.blobs.tmp_path();
        let core2 = core.clone();
        let rs2 = rs.clone();
        let mut ext = move |o: &Oid| -> Result<Option<(u8, Arc<Vec<u8>>, String)>> { core2.read_obj(&rs2, o) };
        let t_ingest = std::time::Instant::now();
        match ingest::ingest(&mut body, &tmp, &mut ext) {
            Ok(res) => {
                core.events.emit(
                    "pack_ingested",
                    json!({"repo": repo, "push": push_id, "pack": res.idx.id, "objects": res.count, "bytes": res.idx.pack_size,
                           "prereqs": res.idx.prereqs.len(), "thin_bases": res.idx.ext_bases.len(),
                           "ingest_ms": t_ingest.elapsed().as_secs_f64() * 1e3}),
                );
                pack_summary = Some((res.idx.id.clone(), res.count as u64, res.idx.pack_size, res.idx.ext_bases.len()));
                if res.count > 0 {
                    let t_install = std::time::Instant::now();
                    match core.blobs.install(repo, &res.tmp_path, &res.idx) {
                        Ok(()) => {
                            core.events.emit("pack_stored", json!({"repo": repo, "push": push_id, "pack": res.idx.id,
                                                                   "install_ms": t_install.elapsed().as_secs_f64() * 1e3}));
                            let idx = core.blobs.index(repo, &res.idx.id).unwrap_or_else(|_| Arc::new(res.idx.clone()));
                            pack = Some((idx, res.count));
                        }
                        Err(e) => unpack_err = Some(format!("storage error: {:#}", e)),
                    }
                } else {
                    let _ = std::fs::remove_file(&res.tmp_path);
                    pack = Some((Arc::new(res.idx), 0));
                }
            }
            Err(e) => {
                let _ = std::fs::remove_file(&tmp);
                core.events.emit("pack_rejected", json!({"repo": repo, "push": push_id, "error": e.to_string()}));
                unpack_err = Some(format!("{:#}", e).replace('\n', " "));
            }
        }
    }
    let mut results: Vec<CmdResult> = Vec::new();
    if unpack_err.is_none() {
        // validate ref names first
        let mut good = Vec::new();
        let mut bad = Vec::new();
        for c in &cmds {
            if !valid_refname(&c.name) {
                bad.push(CmdResult { name: c.name.clone(), ok: false, msg: "funny refname".into() });
            } else if c.old.is_zero() && c.new.is_zero() {
                bad.push(CmdResult { name: c.name.clone(), ok: false, msg: "deleting a non-existent ref".into() });
            } else {
                good.push(c.clone());
            }
        }
        if atomic && !bad.is_empty() {
            results = cmds
                .iter()
                .map(|c| bad.iter().find(|b| b.name == c.name).cloned().unwrap_or(CmdResult { name: c.name.clone(), ok: false, msg: "atomic push failed".into() }))
                .collect();
        } else {
            let r = core.push(PushInput { repo, cmds: good, pack, atomic, push_id: push_id.clone() });
            match r {
                Ok(v) => results = v,
                Err(e) => {
                    core.events.emit("push_failed", json!({"repo": repo, "push": push_id, "error": e.to_string()}));
                    // keep the results of commands committed before the failure (audit F4)
                    let done = e.downcast_ref::<crate::repo::PartialPush>().map(|p| p.done.clone()).unwrap_or_default();
                    results = cmds
                        .iter()
                        .map(|c| done.iter().find(|d| d.name == c.name).cloned()
                            .unwrap_or(CmdResult { name: c.name.clone(), ok: false, msg: format!("server error: {}", e) }))
                        .collect();
                }
            }
            results.extend(bad);
        }
    }
    // report-status
    let mut rep = Vec::new();
    match &unpack_err {
        None => pkt_str(&mut rep, "unpack ok\n"),
        Some(e) => pkt_str(&mut rep, &format!("unpack {}\n", e)),
    }
    for c in &cmds {
        if unpack_err.is_some() {
            pkt_str(&mut rep, &format!("ng {} unpacker error\n", c.name));
            continue;
        }
        match results.iter().find(|r| r.name == c.name) {
            Some(r) if r.ok => pkt_str(&mut rep, &format!("ok {}\n", c.name)),
            Some(r) => pkt_str(&mut rep, &format!("ng {} {}\n", c.name, r.msg)),
            None => pkt_str(&mut rep, &format!("ng {} internal error\n", c.name)),
        }
    }
    flush(&mut rep);
    core.events.emit(
        "push_reported",
        json!({"repo": repo, "push": push_id, "unpack": unpack_err, "results": results.iter().map(|r| json!({"ref": r.name, "ok": r.ok, "msg": r.msg})).collect::<Vec<_>>(),
               "server_ms": t_push.elapsed().as_secs_f64() * 1e3, "object_store": crate::s3::prof_take()}),
    );
    // Operation token (docs/CLI-DESIGN.md): unguessable, shown to this client only (a `remote:` line on band 2), the
    // key to `pando explain --op <token>`. Recorded after the report is built, off the push path.
    let token = crate::diag::new_token();
    if unpack_err.is_none() && !cmds.is_empty() {
        crate::diag::record_op(core, crate::diag::PushRecord {
            repo: repo.to_string(), token: token.clone(), principal: crate::diag::principal(),
            cmds: cmds.iter().map(|c| (c.name.clone(), c.old.hex(), c.new.hex())).collect(),
            results: results.clone(), pack: pack_summary.clone(), server_ms: t_push.elapsed().as_secs_f64() * 1e3,
        });
    }
    if sideband {
        let mut o = Vec::new();
        if unpack_err.is_none() && !cmds.is_empty() {
            let mut d = vec![2u8];
            d.extend_from_slice(format!("pando: operation {} (explain it: pando explain --op {})\n", token, token).as_bytes());
            pkt(&mut o, &d);
        }
        for chunk in rep.chunks(65000) {
            let mut d = vec![1u8];
            d.extend_from_slice(chunk);
            pkt(&mut o, &d);
        }
        flush(&mut o);
        out(o)?;
    } else {
        out(rep)?;
    }
    Ok(())
}

/// How one planned pack is delivered: by packfile URI (`uri`, serving `served`: the pack itself or its prepared
/// self-contained copy) or inline (`uri` None). Shared by the fetch path and the read-only plan explanation
/// (diag.rs), so both describe the same decision.
pub struct Delivery {
    pub pack: String,
    pub served: String,
    pub uri: Option<String>,
}

pub fn delivery(core: &Core, repo: &str, plan: &FetchPlan, want_uris: bool, ofs: bool, no_ref_delta: bool,
                uri_for: &dyn Fn(&str, &str) -> Option<String>) -> Result<Vec<Delivery>> {
    let servable = |idx: &PackIndex| !idx.is_thin() && !(no_ref_delta && idx.has_ref_delta()) && (ofs || !idx.entries.iter().any(|e| e.kind == T_OFS));
    let mut out = Vec::new();
    for p in &plan.packs {
        let mut d = Delivery { pack: p.clone(), served: p.clone(), uri: None };
        if want_uris {
            let mut idx = core.blobs.index(repo, p)?;
            if !servable(&idx) {
                // its self-contained copy, prepared in the background after the push (D-13). Never built here:
                // until it is published the pack is sent inline (docs/BACKGROUND-COPIES.md, fallback policy).
                if let Some(t) = core.twin(&plan.rs, p) {
                    idx = core.blobs.index(repo, &t)?;
                    d.served = t;
                }
            }
            if servable(&idx) {
                d.uri = uri_for(repo, &d.served);
            }
            if d.uri.is_none() {
                d.served = p.clone();
            }
        }
        out.push(d);
    }
    Ok(out)
}

pub fn v2_advertisement() -> Vec<u8> {
    let mut out = Vec::new();
    pkt_str(&mut out, "version 2\n");
    pkt_str(&mut out, &format!("{}\n", AGENT));
    pkt_str(&mut out, "ls-refs=unborn\n");
    pkt_str(&mut out, "fetch=shallow packfile-uris no-ref-delta\n");
    pkt_str(&mut out, "server-option\n");
    pkt_str(&mut out, "object-format=sha1\n");
    flush(&mut out);
    out
}

pub struct UriProvider<'a> {
    /// returns a URI for a pack id, or None if pack URIs are disabled
    pub uri_for: &'a (dyn Fn(&str, &str) -> Option<String> + Sync),
}

pub fn handle_upload_v2<R: BufRead>(core: &Arc<Core>, repo: &str, mut body: R, uris: &UriProvider, out: &mut dyn FnMut(Vec<u8>) -> Result<()>) -> Result<()> {
    let mut command = String::new();
    let mut args: Vec<String> = Vec::new();
    let mut in_args = false;
    loop {
        match read_pkt(&mut body)? {
            None | Some(Pkt::Flush) => break,
            Some(Pkt::Delim) => in_args = true,
            Some(Pkt::End) => break,
            Some(Pkt::Data(d)) => {
                let s = String::from_utf8_lossy(trim_lf(&d)).to_string();
                if in_args {
                    args.push(s);
                } else if let Some(c) = s.strip_prefix("command=") {
                    command = c.to_string();
                }
            }
        }
    }
    match command.as_str() {
        "ls-refs" => ls_refs(core, repo, &args, out),
        "fetch" => fetch(core, repo, &args, uris, out),
        "" => Ok(()),
        c => {
            let mut o = Vec::new();
            pkt_str(&mut o, &format!("ERR unknown command {}\n", c));
            out(o)
        }
    }
}

fn ls_refs(core: &Arc<Core>, repo: &str, args: &[String], out: &mut dyn FnMut(Vec<u8>) -> Result<()>) -> Result<()> {
    let rs = core.repo(repo).ok_or_else(|| anyhow!("no such repository"))?;
    let peel = args.iter().any(|a| a == "peel");
    let symrefs = args.iter().any(|a| a == "symrefs");
    let unborn = args.iter().any(|a| a == "unborn");
    let prefixes: Vec<&str> = args.iter().filter_map(|a| a.strip_prefix("ref-prefix ")).collect();
    let matches = |n: &str| prefixes.is_empty() || prefixes.iter().any(|p| n.starts_with(p));
    let mut refs = core.list_refs(repo);
    refs.sort_by(|a, b| a.0.cmp(&b.0));
    let mut o = Vec::new();
    if matches("HEAD") {
        match refs.iter().find(|(n, _)| *n == rs.trunk) {
            Some((n, p)) => {
                let mut l = format!("{} HEAD", p.tip);
                if symrefs {
                    l.push_str(&format!(" symref-target:{}", n));
                }
                pkt_str(&mut o, &format!("{}\n", l));
            }
            None => {
                if unborn && symrefs {
                    pkt_str(&mut o, &format!("unborn HEAD symref-target:{}\n", rs.trunk));
                }
            }
        }
    }
    for (n, p) in &refs {
        if !matches(n) {
            continue;
        }
        let mut l = format!("{} {}", p.tip, n);
        if peel {
            if let Some(pe) = &p.peeled {
                l.push_str(&format!(" peeled:{}", pe));
            }
        }
        pkt_str(&mut o, &format!("{}\n", l));
    }
    flush(&mut o);
    core.events.emit("ls_refs", json!({"repo": repo, "refs": refs.len(), "snapshot": refs.iter().map(|(n, p)| json!([n, p.seq, p.tip])).collect::<Vec<_>>()}));
    out(o)
}

fn fetch(core: &Arc<Core>, repo: &str, args: &[String], uris: &UriProvider, out: &mut dyn FnMut(Vec<u8>) -> Result<()>) -> Result<()> {
    let mut wants = Vec::new();
    let mut haves = Vec::new();
    let mut done = false;
    let mut thin = false;
    let mut ofs = false;
    let mut no_progress = false;
    let mut no_ref_delta = false;
    let mut uri_protos: Vec<String> = Vec::new();
    let mut sideband_all = false;
    let mut wait_for_done = false;
    let mut client_shallow: Vec<Oid> = Vec::new();
    let mut depth: u64 = 0;
    for a in args {
        if let Some(w) = a.strip_prefix("want ") {
            wants.push(Oid::from_hex(w.trim())?);
        } else if let Some(h) = a.strip_prefix("have ") {
            haves.push(Oid::from_hex(h.trim())?);
        } else if a == "done" {
            done = true;
        } else if a == "thin-pack" {
            thin = true;
        } else if a == "ofs-delta" {
            ofs = true;
        } else if a == "no-progress" {
            no_progress = true;
        } else if a == "no-ref-delta" {
            no_ref_delta = true;
        } else if a == "sideband-all" {
            sideband_all = true;
        } else if a == "wait-for-done" {
            wait_for_done = true;
        } else if let Some(p) = a.strip_prefix("packfile-uris ") {
            uri_protos = p.split(',').map(|s| s.to_string()).collect();
        } else if let Some(s) = a.strip_prefix("shallow ") {
            client_shallow.push(Oid::from_hex(s.trim())?);
        } else if let Some(d) = a.strip_prefix("deepen ") {
            depth = d.trim().parse().unwrap_or(0);
        } else if a.starts_with("deepen-") {
            let mut o = Vec::new();
            pkt_str(&mut o, &format!("ERR {} is not supported by this server (use --depth)\n", a.split(' ').next().unwrap_or(a)));
            return out(o);
        } else if a.starts_with("filter ") {
            let mut o = Vec::new();
            pkt_str(&mut o, "ERR partial clone (filter) is not supported by this server\n");
            return out(o);
        } else if a.starts_with("want-ref ") {
            let mut o = Vec::new();
            pkt_str(&mut o, "ERR want-ref is not supported\n");
            return out(o);
        }
    }
    let _ = sideband_all;
    if depth > 0 {
        return shallow_fetch(core, repo, &wants, &haves, &client_shallow, depth, done, no_progress, out);
    }
    let plan = match core.plan_fetch_opts(repo, &wants, &haves, !client_shallow.is_empty()) {
        Ok(p) => p,
        Err(e) => {
            let mut o = Vec::new();
            pkt_str(&mut o, &format!("ERR {}\n", e));
            return out(o);
        }
    };
    let ready = !plan.acks.is_empty() && !wait_for_done;
    if !done && !ready {
        let mut o = Vec::new();
        pkt_str(&mut o, "acknowledgments\n");
        if plan.acks.is_empty() {
            pkt_str(&mut o, "NAK\n");
        } else {
            for a in &plan.acks {
                pkt_str(&mut o, &format!("ACK {}\n", a));
            }
        }
        flush(&mut o);
        core.events.emit("fetch_negotiate", json!({"repo": repo, "wants": wants.len(), "haves": haves.len(), "acks": plan.acks.len()}));
        return out(o);
    }
    let mut o = Vec::new();
    if !done {
        pkt_str(&mut o, "acknowledgments\n");
        for a in &plan.acks {
            pkt_str(&mut o, &format!("ACK {}\n", a));
        }
        pkt_str(&mut o, "ready\n");
        delim(&mut o);
    }
    // packfile URIs: only self-contained stored packs qualify
    let want_uris = uri_protos.iter().any(|p| p == "http" || p == "https");
    let mut uri_packs: Vec<(String, String)> = Vec::new();
    let mut exclude: HashSet<Oid> = HashSet::new();
    let mut inline_packs: Vec<String> = Vec::new();
    let mut inline_fallback = 0u64;
    for d in delivery(core, repo, &plan, want_uris, ofs, no_ref_delta, uris.uri_for)? {
        match d.uri {
            Some(u) => {
                for e in &core.blobs.index(repo, &d.served)?.entries {
                    exclude.insert(e.oid);
                }
                uri_packs.push((d.served, u));
            }
            None => {
                if want_uris {
                    inline_fallback += 1;
                }
                inline_packs.push(d.pack);
            }
        }
    }
    if !uri_packs.is_empty() {
        pkt_str(&mut o, "packfile-uris\n");
        for (id, u) in &uri_packs {
            pkt_str(&mut o, &format!("{} {}\n", id, u));
        }
        delim(&mut o);
    }
    pkt_str(&mut o, "packfile\n");
    set_sideband(true); // from here an error must go out on band 3 (audit F8)
    if !no_progress {
        let msg = format!("pando: sending {} stored pack(s) ({} via packfile-uris)\n", plan.packs.len(), uri_packs.len());
        let mut d = vec![2u8];
        d.extend_from_slice(msg.as_bytes());
        pkt(&mut o, &d);
    }
    out(o)?;
    let mut chunk: Vec<u8> = Vec::with_capacity(65536);
    let sb = |b: &[u8], chunk: &mut Vec<u8>, force: bool, out: &mut dyn FnMut(Vec<u8>) -> Result<()>| -> Result<()> {
        chunk.extend_from_slice(b);
        while chunk.len() >= 65515 || (force && !chunk.is_empty()) {
            let n = chunk.len().min(65515);
            let mut o = Vec::with_capacity(n + 8);
            let mut d = Vec::with_capacity(n + 1);
            d.push(1u8);
            d.extend_from_slice(&chunk[..n]);
            pkt(&mut o, &d);
            chunk.drain(..n);
            out(o)?;
        }
        Ok(())
    };
    let opts = OutOpts { ofs, thin, no_ref_delta };
    let holds = |oid: &Oid| core.client_holds(&plan, oid);
    let stats;
    {
        let mut write = |b: &[u8]| -> Result<()> { sb(b, &mut chunk, false, out) };
        let alt = |oid: &Oid, pack: &str| {
            // Covers carry a precomputed substitution map (built during compaction); others use the object index.
            if let Some(m) = core.blobs.alt_map(repo, pack) {
                crate::metrics::alt_map_lookup();
                return m.get(oid).cloned();
            }
            core.locate(&plan.rs, oid).ok().flatten().map(|(p, ei, _)| (p, ei))
        };
        let (st, _sum) = packout::write_spliced_alt(&core.blobs, repo, &inline_packs, &exclude, &opts, &holds, &alt, &mut write)?;
        stats = st;
    }
    sb(&[], &mut chunk, true, out)?;
    let mut o = Vec::new();
    flush(&mut o);
    out(o)?;
    core.events.emit(
        "fetch_served",
        json!({"repo": repo, "wants": wants.iter().map(|w| w.hex()).collect::<Vec<_>>(), "haves": haves.len(), "acks": plan.acks.len(),
               "thin": thin, "ofs": ofs, "no_ref_delta": no_ref_delta, "packfile_uris": want_uris, "inline_fallback_packs": inline_fallback,
               "packs": plan.packs, "uri_packs": uri_packs.iter().map(|x| x.0.clone()).collect::<Vec<_>>(),
               "inline_objects": stats.objects, "inline_bytes": stats.bytes, "expanded": stats.expanded, "l0_substituted": stats.substituted,
               "plan_steps": plan.steps, "covers_used": plan.covers_used, "plan_metadata_reads": plan.metadata_reads}),
    );
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn shallow_fetch(
    core: &Arc<Core>,
    repo: &str,
    wants: &[Oid],
    haves: &[Oid],
    client_shallow: &[Oid],
    depth: u64,
    done: bool,
    no_progress: bool,
    out: &mut dyn FnMut(Vec<u8>) -> Result<()>,
) -> Result<()> {
    // Like upload-pack's deepen, the depth walk starts at the wants regardless of
    // haves (a have may itself be a shallow tip that is being deepened). Cost:
    // objects the client already has within the depth are sent again.
    let sp = match crate::shallow::plan(core, repo, wants, depth, &[]) {
        Ok(p) => p,
        Err(e) => {
            let mut o = Vec::new();
            pkt_str(&mut o, &format!("ERR {}\n", e));
            return out(o);
        }
    };
    let mut o = Vec::new();
    if !done {
        pkt_str(&mut o, "acknowledgments\n");
        pkt_str(&mut o, "NAK\n");
        pkt_str(&mut o, "ready\n");
        delim(&mut o);
    }
    let included: HashSet<Oid> = sp.objects.iter().cloned().collect();
    let shallow_set: HashSet<Oid> = sp.shallow.iter().cloned().collect();
    pkt_str(&mut o, "shallow-info\n");
    for s in &sp.shallow {
        pkt_str(&mut o, &format!("shallow {}\n", s));
    }
    for c in client_shallow {
        if included.contains(c) && !shallow_set.contains(c) {
            pkt_str(&mut o, &format!("unshallow {}\n", c));
        }
    }
    delim(&mut o);
    pkt_str(&mut o, "packfile\n");
    set_sideband(true); // from here an error must go out on band 3 (audit F8)
    if !no_progress {
        let mut d = vec![2u8];
        d.extend_from_slice(format!("pando: shallow pack, depth {}, {} objects\n", depth, sp.objects.len()).as_bytes());
        pkt(&mut o, &d);
    }
    out(o)?;
    let mut chunk: Vec<u8> = Vec::with_capacity(65536);
    let bytes;
    {
        let mut write = |b: &[u8]| -> Result<()> {
            chunk.extend_from_slice(b);
            while chunk.len() >= 65515 {
                let mut o = Vec::new();
                let mut d = vec![1u8];
                d.extend_from_slice(&chunk[..65515]);
                pkt(&mut o, &d);
                chunk.drain(..65515);
                out(o)?;
            }
            Ok(())
        };
        bytes = crate::shallow::write_pack(core, repo, &sp.objects, &mut write)?;
    }
    let mut o = Vec::new();
    if !chunk.is_empty() {
        let mut d = vec![1u8];
        d.extend_from_slice(&chunk);
        pkt(&mut o, &d);
    }
    flush(&mut o);
    out(o)?;
    core.events.emit(
        "fetch_served",
        json!({"repo": repo, "shallow": true, "depth": depth, "wants": wants.iter().map(|w| w.hex()).collect::<Vec<_>>(),
               "haves": haves.len(), "packs": [], "uri_packs": [], "inline_objects": sp.objects.len(), "inline_bytes": bytes,
               "shallow_commits": sp.shallow.len()}),
    );
    Ok(())
}
