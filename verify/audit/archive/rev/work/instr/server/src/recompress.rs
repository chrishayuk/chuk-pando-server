//! Recompressed covers (docs/RECOMPRESSION-ACCEPTANCE.md; decision in docs/COMPRESSION-WORKLOADS.md).
//!
//! A cover is first built by splicing its member L0 packs (unchanged). This module then re-deltas the cover's
//! objects with Git plumbing in a throw-away scratch repository under `<blobs>/tmp/`. The scratch repository is
//! never authoritative storage and never served; it is deleted afterwards, and at startup any leftover is removed.
//! The push path is not involved: pushed packs are still stored as received and never unpacked.
//!
//! The recompressed pack is accepted only if, re-ingested by our own single-pass decoder, it contains exactly the
//! same object set, has no REF_DELTA entries (self-contained; servable with `no-ref-delta`), and is smaller.
//! Otherwise the spliced cover is kept and the reason is reported.
use crate::pack::*;
use anyhow::{anyhow, bail, Context, Result};
use std::collections::{HashMap, HashSet};
use std::io::{Read, Write};
use std::path::Path;
use std::process::{Command, Stdio};

/// Delta search parameters. Window 250 as Git's `gc --aggressive`; depth 250 bounds chain length (reader cost)
/// while keeping the long same-path chains that splicing preserves (workload R). Measured in
/// verify/results/recompress-after-*.json.
pub const WINDOW: u32 = 250;
pub const DEPTH: u32 = 250;

pub fn enabled() -> bool {
    std::env::var("PANDO_RECOMPRESS").map(|v| v != "0").unwrap_or(true)
}

fn git_bin() -> String {
    std::env::var("PANDO_COMPACT_GIT").unwrap_or_else(|_| "git".to_string())
}

fn git(dir: &Path) -> Command {
    let mut c = Command::new(git_bin());
    c.current_dir(dir)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_COUNT", "2")
        .env("GIT_CONFIG_KEY_0", "gc.auto")
        .env("GIT_CONFIG_VALUE_0", "0")
        .env("GIT_CONFIG_KEY_1", "maintenance.auto")
        .env("GIT_CONFIG_VALUE_1", "false")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    c
}

fn run(mut c: Command, input: Option<&[u8]>) -> Result<Vec<u8>> {
    if input.is_some() {
        c.stdin(Stdio::piped());
    }
    let mut ch = c.spawn().context("spawn git (set PANDO_COMPACT_GIT or PANDO_RECOMPRESS=0)")?;
    let writer = input.map(|i| {
        let mut sin = ch.stdin.take().unwrap();
        let data = i.to_vec();
        std::thread::spawn(move || {
            let _ = sin.write_all(&data);
        })
    });
    let mut out = Vec::new();
    ch.stdout.take().unwrap().read_to_end(&mut out)?;
    let mut err = Vec::new();
    ch.stderr.take().unwrap().read_to_end(&mut err)?;
    if let Some(w) = writer {
        let _ = w.join();
    }
    let st = ch.wait()?;
    if !st.success() {
        bail!("{:?}: {}", c.get_args().collect::<Vec<_>>(), String::from_utf8_lossy(&err).trim());
    }
    Ok(out)
}

/// Parse the entries of a raw tree object: (name, oid).
fn tree_entries(data: &[u8]) -> Vec<(Vec<u8>, Oid)> {
    let mut v = Vec::new();
    let mut i = 0;
    while i < data.len() {
        let sp = match data[i..].iter().position(|&b| b == b' ') {
            Some(p) => i + p,
            None => break,
        };
        let nul = match data[sp..].iter().position(|&b| b == 0) {
            Some(p) => sp + p,
            None => break,
        };
        if nul + 21 > data.len() {
            break;
        }
        let mut o = [0u8; 20];
        o.copy_from_slice(&data[nul + 1..nul + 21]);
        v.push((data[sp + 1..nul].to_vec(), Oid(o)));
        i = nul + 21;
    }
    v
}

pub struct Outcome {
    pub bytes: Option<Vec<u8>>,
    pub reason: String,
    pub millis: u128,
}

/// Recompress the spliced cover `spliced` whose index is `idx`. `scratch` must be a fresh path under the blob
/// store's tmp directory; it is removed before returning.
pub fn recompress(spliced: &[u8], idx: &PackIndex, scratch: &Path) -> Outcome {
    let t0 = std::time::Instant::now();
    let r = recompress_inner(spliced, idx, scratch);
    let _ = std::fs::remove_dir_all(scratch);
    match r {
        Ok(b) => Outcome { bytes: Some(b), reason: "ok".into(), millis: t0.elapsed().as_millis() },
        Err(e) => Outcome { bytes: None, reason: format!("{:#}", e), millis: t0.elapsed().as_millis() },
    }
}

fn recompress_inner(spliced: &[u8], idx: &PackIndex, scratch: &Path) -> Result<Vec<u8>> {
    std::fs::create_dir_all(scratch)?;
    run({
        let mut c = git(scratch);
        c.args(["init", "-q", "--bare", "."]);
        c
    }, None)?;
    let pack = scratch.join("objects").join("pack").join("in.pack");
    std::fs::write(&pack, spliced)?;
    run({
        let mut c = git(scratch);
        c.arg("index-pack").arg(&pack);
        c
    }, None)?;
    // Path names steer Git's delta search (same/similar names are tried together); recover them from the
    // cover's own trees. Objects whose tree is outside the cover get no name.
    let trees: Vec<String> = idx.entries.iter().filter(|e| e.otype == T_TREE).map(|e| e.oid.hex()).collect();
    let mut names: HashMap<Oid, Vec<u8>> = HashMap::new();
    if !trees.is_empty() {
        let out = run({
            let mut c = git(scratch);
            c.args(["cat-file", "--batch"]);
            c
        }, Some((trees.join("\n") + "\n").as_bytes()))?;
        let mut i = 0;
        while i < out.len() {
            let nl = out[i..].iter().position(|&b| b == b'\n').ok_or_else(|| anyhow!("cat-file header"))? + i;
            let hdr = String::from_utf8_lossy(&out[i..nl]).to_string();
            let size: usize = hdr.split_whitespace().nth(2).ok_or_else(|| anyhow!("cat-file: {}", hdr))?.parse()?;
            let body = &out[nl + 1..nl + 1 + size];
            for (name, oid) in tree_entries(body) {
                names.entry(oid).or_insert(name);
            }
            i = nl + 1 + size + 1;
        }
    }
    let mut list = Vec::new();
    for e in &idx.entries {
        list.extend_from_slice(e.oid.hex().as_bytes());
        if let Some(n) = names.get(&e.oid) {
            list.push(b' ');
            list.extend_from_slice(n);
        }
        list.push(b'\n');
    }
    let out = run({
        let mut c = git(scratch);
        c.args(["pack-objects", "--stdout", "-q", "--delta-base-offset", "--no-reuse-delta", "--threads=1"])
            .arg(format!("--window={}", WINDOW))
            .arg(format!("--depth={}", DEPTH));
        c
    }, Some(&list))?;
    // Verify with our own decoder: same object set, self-contained (no REF_DELTA).
    let tmp = scratch.join("verify.pack");
    let mut nolookup = |_: &Oid| -> Result<Option<(u8, std::sync::Arc<Vec<u8>>, String)>> { Ok(None) };
    let res = crate::ingest::ingest(std::io::Cursor::new(&out), &tmp, &mut nolookup).context("re-ingest of recompressed pack")?;
    let want: HashSet<Oid> = idx.entries.iter().map(|e| e.oid).collect();
    let got: HashSet<Oid> = res.idx.entries.iter().map(|e| e.oid).collect();
    if want != got {
        bail!("object set differs: {} wanted, {} produced", want.len(), got.len());
    }
    if res.idx.entries.iter().any(|e| e.kind == T_REF) {
        bail!("recompressed pack contains REF_DELTA");
    }
    if out.len() >= spliced.len() {
        bail!("not smaller ({} >= {})", out.len(), spliced.len());
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn parses_tree_entries() {
        let mut t = Vec::new();
        t.extend_from_slice(b"100644 a.txt\0");
        t.extend_from_slice(&[1u8; 20]);
        t.extend_from_slice(b"40000 dir\0");
        t.extend_from_slice(&[2u8; 20]);
        let e = tree_entries(&t);
        assert_eq!(e.len(), 2);
        assert_eq!(e[0].0, b"a.txt");
        assert_eq!(e[1].0, b"dir");
        assert_eq!(e[1].1 .0, [2u8; 20]);
    }
}
