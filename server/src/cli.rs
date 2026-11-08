//! Client-side commands (docs/CLI-DESIGN.md): `pando status`, `pando explore fetch`, `pando explain last-push`.
//! They talk only to a Git frontend's read-only diagnostics (`<repo>/pando/v1/...`), with the credentials Git itself
//! would use; never to the metadata service or the object store. Every view says whether it shows what happened
//! (RECORDED), what is (STATE), what would happen (HYPOTHETICAL), or what was computed from the local checkout (LOCAL).
use anyhow::{anyhow, bail, Context, Result};
use serde_json::Value;
use std::io::{Read, Write};
use std::process::Command;

struct Opts {
    remote: Option<String>,
    json: bool,
    from: Option<String>,
    to: Option<String>,
    op: Option<String>,
    rest: Vec<String>,
}

fn parse(args: &[String]) -> Result<Opts> {
    let mut o = Opts { remote: None, json: false, from: None, to: None, op: None, rest: vec![] };
    let mut i = 0;
    let val = |i: usize| args.get(i + 1).cloned().ok_or_else(|| anyhow!("{} needs a value", args[i]));
    while i < args.len() {
        match args[i].as_str() {
            "--remote" => { o.remote = Some(val(i)?); i += 1; }
            "--from" => { o.from = Some(val(i)?); i += 1; }
            "--to" => { o.to = Some(val(i)?); i += 1; }
            "--op" => { o.op = Some(val(i)?); i += 1; }
            "--json" => o.json = true,
            "-h" | "--help" => o.rest.push("--help".into()),
            a if a.starts_with("--") => bail!("unknown option {}", a),
            a => o.rest.push(a.to_string()),
        }
        i += 1;
    }
    Ok(o)
}

fn git(args: &[&str]) -> Option<String> {
    let out = Command::new("git").args(args).env("GIT_TERMINAL_PROMPT", "0").output().ok()?;
    if !out.status.success() {
        return None;
    }
    Some(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

/// The remote to explain: --remote, else the current branch's upstream remote, else `origin`. Returns (url, how found).
fn discover(o: &Opts) -> Result<(String, String)> {
    if let Some(r) = &o.remote {
        if r.contains("://") {
            return Ok((r.clone(), "--remote".into()));
        }
        if let Some(u) = git(&["remote", "get-url", r]) {
            return Ok((u, format!("git remote {}", r)));
        }
        bail!("--remote {}: not a URL or a remote of this repository", r);
    }
    if git(&["rev-parse", "--git-dir"]).is_none() {
        bail!("not inside a Git repository. Run it inside a checkout whose remote is a Pando server, or use --remote <url>.");
    }
    let name = git(&["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
        .and_then(|u| u.split('/').next().map(|s| s.to_string()))
        .unwrap_or_else(|| "origin".into());
    let url = git(&["remote", "get-url", &name]).ok_or_else(|| anyhow!("this repository has no remote '{}'; use --remote", name))?;
    Ok((url, format!("git remote {}", name)))
}

struct Url {
    https: bool,
    host: String,
    port: u16,
    path: String, // repository path, no trailing slash
    userinfo: Option<String>,
}

fn parse_url(u: &str) -> Result<Url> {
    let (https, rest) = if let Some(r) = u.strip_prefix("https://") { (true, r) } else if let Some(r) = u.strip_prefix("http://") { (false, r) } else {
        bail!("{}: only http(s) remotes can be explained (ssh and file remotes have no diagnostics)", u)
    };
    let (auth_host, path) = rest.split_once('/').map(|(a, p)| (a, format!("/{}", p))).unwrap_or((rest, "/".into()));
    let (userinfo, hostport) = match auth_host.rsplit_once('@') { Some((ui, h)) => (Some(ui.to_string()), h), None => (None, auth_host) };
    let (host, port) = match hostport.rsplit_once(':') {
        Some((h, p)) if !h.ends_with(']') || hostport.starts_with('[') => (h.to_string(), p.parse()?),
        _ => (hostport.to_string(), if https { 443 } else { 80 }),
    };
    Ok(Url { https, host, port, path: path.trim_end_matches('/').to_string(), userinfo })
}

/// The credentials Git would send to this URL (`git credential fill`, never prompting), or those in the URL.
fn credentials(u: &Url) -> Option<String> {
    if let Some(ui) = &u.userinfo {
        return Some(ui.clone());
    }
    let input = format!("protocol={}\nhost={}{}\npath={}\n\n", if u.https { "https" } else { "http" }, u.host,
                        if (u.https && u.port == 443) || (!u.https && u.port == 80) { String::new() } else { format!(":{}", u.port) },
                        u.path.trim_start_matches('/'));
    let mut child = Command::new("git").args(["credential", "fill"]).env("GIT_TERMINAL_PROMPT", "0")
        .stdin(std::process::Stdio::piped()).stdout(std::process::Stdio::piped()).stderr(std::process::Stdio::null()).spawn().ok()?;
    child.stdin.take()?.write_all(input.as_bytes()).ok()?;
    let out = child.wait_with_output().ok()?;
    if !out.status.success() {
        return None;
    }
    let t = String::from_utf8_lossy(&out.stdout);
    let get = |k: &str| t.lines().find_map(|l| l.strip_prefix(&format!("{}=", k)).map(|s| s.to_string()));
    Some(format!("{}:{}", get("username")?, get("password")?))
}

/// GET <repo>/pando/v1/<what> -> (status, body)
fn diag(u: &Url, what: &str) -> Result<(u16, String)> {
    let tcp = crate::meta::connect_timeout(&format!("{}:{}", u.host, u.port), std::time::Duration::from_secs(5))
        .with_context(|| format!("connect {}:{}", u.host, u.port))?;
    tcp.set_read_timeout(Some(std::time::Duration::from_secs(30)))?;
    let mut c: Box<dyn crate::s3::ReadWrite> = if u.https {
        let name = rustls::pki_types::ServerName::try_from(u.host.clone())?;
        Box::new(rustls::StreamOwned::new(rustls::ClientConnection::new(crate::s3::tls_config()?, name)?, tcp))
    } else {
        Box::new(tcp)
    };
    let auth = credentials(u).map(|c| format!("Authorization: Basic {}\r\n", crate::auth::b64encode(c.as_bytes()))).unwrap_or_default();
    write!(c, "GET {}/pando/v1/{} HTTP/1.1\r\nHost: {}\r\nUser-Agent: pando/{}\r\n{}Connection: close\r\n\r\n",
           u.path, what, u.host, env!("CARGO_PKG_VERSION"), auth)?;
    let mut raw = Vec::new();
    match c.read_to_end(&mut raw) {
        Ok(_) => {}
        Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof && !raw.is_empty() => {}
        Err(e) => return Err(e.into()),
    }
    let t = String::from_utf8_lossy(&raw).to_string();
    let (head, body) = t.split_once("\r\n\r\n").ok_or_else(|| anyhow!("bad HTTP response"))?;
    let code: u16 = head.split(' ').nth(1).and_then(|c| c.parse().ok()).ok_or_else(|| anyhow!("bad HTTP status"))?;
    let body = if head.to_ascii_lowercase().contains("transfer-encoding: chunked") { dechunk(body) } else { body.to_string() };
    Ok((code, body))
}

fn dechunk(b: &str) -> String {
    let (mut out, mut rest) = (String::new(), b);
    while let Some((len, after)) = rest.split_once("\r\n") {
        let n = usize::from_str_radix(len.trim(), 16).unwrap_or(0);
        if n == 0 || after.len() < n {
            break;
        }
        out.push_str(&after[..n]);
        rest = after[n..].trim_start_matches("\r\n");
    }
    out
}

/// Is this remote a Pando server? Some(capabilities) or None (with the reason printed in human mode).
fn capabilities(u: &Url) -> Result<Option<Value>> {
    let (code, body) = diag(u, "capabilities")?;
    if code == 401 {
        bail!("the server requires credentials, and Git has none stored for it (run a `git fetch` first, or put them in the URL)");
    }
    let v: Option<Value> = serde_json::from_str(&body).ok();
    Ok(v.filter(|v| code == 200 && v["data"]["server"] == "pando"))
}

fn get_json(u: &Url, what: &str) -> Result<Value> {
    let (code, body) = diag(u, what)?;
    let v: Value = serde_json::from_str(&body).map_err(|_| anyhow!("{} -> HTTP {}: {}", what, code, body.trim()))?;
    if code != 200 {
        bail!("{}", v["error"].as_str().unwrap_or(body.trim()));
    }
    Ok(v)
}

fn short(s: &str) -> &str {
    &s[..s.len().min(10)]
}

pub fn main(cmd: &str, args: &[String]) -> Result<()> {
    let o = parse(args)?;
    if o.rest.iter().any(|a| a == "--help") {
        println!("{}", HELP);
        return Ok(());
    }
    let (url_s, how) = discover(&o)?;
    let u = parse_url(&url_s)?;
    let caps = capabilities(&u)?;
    if caps.is_none() {
        if o.json {
            println!("{}", serde_json::json!({"schema": crate::diag::SCHEMA, "kind": "not-pando", "remote": url_s}));
        } else {
            println!("{} ({}) is not a Pando server, or has diagnostics disabled.", url_s, how);
            println!("Git works with it as usual; there is nothing Pando-specific to explain.");
        }
        return Ok(());
    }
    let repo_of = |v: &Value| v["data"]["repo"].as_str().unwrap_or("").to_string();
    match cmd {
        "status" => {
            let refs = get_json(&u, "refs")?;
            if o.json {
                println!("{}", serde_json::json!({"capabilities": caps, "refs": refs}));
                return Ok(());
            }
            let c = caps.as_ref().unwrap();
            println!("Remote   {}  ({})", url_s, how);
            println!("Server   pando {}   [STATE]", c["data"]["version"].as_str().unwrap_or("?"));
            println!("Repo     {}", repo_of(&refs));
            for r in refs["data"]["refs"].as_array().into_iter().flatten() {
                let name = r["ref"].as_str().unwrap_or("");
                let local = git(&["rev-parse", "--verify", "-q", &format!("{}^{{commit}}", name.replace("refs/heads/", "refs/remotes/origin/"))]);
                let note = match local { Some(l) if l == r["tip"].as_str().unwrap_or("") => "up to date".to_string(), Some(_) => "you are behind or ahead".into(), None => String::new() };
                println!("  {:<28} version {:<5} tip {}  {}", name, r["version"], short(r["tip"].as_str().unwrap_or("")), note);
            }
            println!("\nNext: `pando explore fetch` (what a fetch would send you), `pando explain last-push`.");
        }
        "explore-fetch" => {
            let to = o.to.clone().unwrap_or_else(|| git(&["symbolic-ref", "--short", "HEAD"]).unwrap_or_else(|| "main".into()));
            let have = match &o.from {
                Some(f) if f == "empty" => None,
                Some(f) => Some(git(&["rev-parse", "--verify", "-q", &format!("{}^{{commit}}", f)]).unwrap_or_else(|| f.clone())),
                None => git(&["rev-parse", "--verify", "-q", &format!("refs/remotes/origin/{}^{{commit}}", to.trim_start_matches("refs/heads/"))]),
            };
            let q = format!("plan?want={}{}", to, have.as_ref().map(|h| format!("&have={}", h)).unwrap_or_default());
            let p = get_json(&u, &q)?;
            if o.json {
                println!("{}", p);
                return Ok(());
            }
            let d = &p["data"];
            println!("HYPOTHETICAL: the plan the server would use now for this fetch. Nothing was sent or changed.\n");
            println!("  want  {} (version {})", d["want"]["ref"].as_str().unwrap_or(d["want"]["tip"].as_str().unwrap_or("")), d["want"]["version"]);
            match have {
                Some(h) => println!("  have  {} (version {})", short(&h), d["have"]["version"]),
                None => println!("  have  nothing (a fresh clone)"),
            }
            println!();
            for (i, k) in d["packs"].as_array().into_iter().flatten().enumerate() {
                let what = match (&k["cover"], k["level"].as_u64()) {
                    (c, _) if c.is_object() => format!("L{} cover of versions {}", c["level"], c["versions"].as_str().unwrap_or("")),
                    (_, Some(0)) => format!("push pack ({})", k["versions"].as_array().map(|v| v.iter().filter_map(|x| x.as_str()).collect::<Vec<_>>().join(", ")).unwrap_or_default()),
                    _ => "pack".into(),
                };
                println!("  {:>2}. {:<44} {:>7} objects {:>10} bytes   {}", i + 1, what, k["objects"], k["bytes"], k["delivery"].as_str().unwrap_or(""));
            }
            let s = &d["summary"];
            println!("\n  {} pack(s), {} by packfile URI; {} cover(s); {} planning step(s); {} metadata read(s).",
                     s["packs"], s["by_uri"], s["covers"], s["plan_steps"], s["metadata_reads"]);
            if let Some(n) = s["pushes_without_covers"].as_u64() {
                println!("  Without covers this fetch would need one pack per push it depends on: {} pack(s).", n);
            }
        }
        "explore-history" | "explore-covers" => {
            let r = o.to.clone().or_else(|| o.rest.first().cloned()).unwrap_or_else(|| git(&["symbolic-ref", "--short", "HEAD"]).unwrap_or_else(|| "main".into()));
            let what = if cmd == "explore-history" { "history" } else { "covers" };
            let v = get_json(&u, &format!("{}?ref={}", what, r))?;
            if o.json {
                println!("{}", v);
                return Ok(());
            }
            let d = &v["data"];
            println!("STATE: {} of {} ({} versions)\n", what, d["ref"].as_str().unwrap_or(""), d["versions"]);
            if cmd == "explore-history" {
                for h in d["history"].as_array().into_iter().flatten() {
                    let deps: Vec<&str> = h["depends_on"].as_array().map(|a| a.iter().filter_map(|x| x.as_str()).collect()).unwrap_or_default();
                    println!("  v{:<4} tip {}  pack {}  {}{}", h["version"], short(h["tip"].as_str().unwrap_or("")),
                             h["pack"].as_str().map(short).unwrap_or("(none: tip already stored)"),
                             if deps.is_empty() { "depends on nothing".to_string() } else { format!("depends on {}", deps.join(", ")) },
                             if h["linked_to_previous"].as_bool() == Some(true) { "  (extends the previous version)" } else { "" });
                }
            } else {
                let cs = d["covers"].as_array().cloned().unwrap_or_default();
                if cs.is_empty() {
                    println!("  no covers yet: every version is still one push pack (covers are built every 4 versions)");
                }
                let top = cs.iter().filter_map(|c| c["level"].as_u64()).max().unwrap_or(1);
                for c in &cs {
                    let lvl = c["level"].as_u64().unwrap_or(1);
                    println!("  {}L{} versions {:>4}..{:<4} one pack for {:>4} pushes  {:>6} objects {:>9} bytes",
                             "    ".repeat((top - lvl) as usize), lvl, c["first_version"], c["last_version"], c["pushes"],
                             c["objects"], c["bytes"]);
                }
                println!("\n  L0: {} push pack(s), all kept unchanged; covers are new files built in the background.", d["versions"]);
            }
        }
        "explain" => {
            let what = match (&o.op, o.rest.first().map(|s| s.as_str())) {
                (Some(t), _) => format!("ops/{}", t),
                (None, Some("last-push")) | (None, None) => "ops/last".into(),
                (None, Some(x)) => bail!("pando explain: unknown subject {:?} (try last-push or --op <token>)", x),
            };
            let v = get_json(&u, &what)?;
            if o.json {
                println!("{}", v);
                return Ok(());
            }
            let d = &v["data"];
            println!("RECORDED by the server for your push to {}:\n", repo_of(&v));
            for r in d["refs"].as_array().into_iter().flatten() {
                let name = r["ref"].as_str().unwrap_or("");
                if r["ok"].as_bool() == Some(true) {
                    let (old, new) = (r["old"].as_str().unwrap_or(""), r["new"].as_str().unwrap_or(""));
                    println!("  {}  {} -> {}   now version {}", name, short(old), short(new), r["version"]);
                    if let Some(dep) = r["depends_on"].as_array() {
                        println!("      depends on: {}", if dep.is_empty() { "nothing (self-contained history)".into() } else { dep.iter().filter_map(|x| x.as_str()).collect::<Vec<_>>().join(", ") });
                    }
                    // LOCAL: what Git had to send, computed from this checkout (if it has the objects)
                    let range = if old.chars().all(|c| c == '0') { new.to_string() } else { format!("{}..{}", old, new) };
                    if let Some(n) = git(&["rev-list", "--objects", "--count", &range]) {
                        println!("      LOCAL: {} object(s) between the old and new tip in your repository", n);
                    }
                } else {
                    println!("  {}  rejected: {}", name, r["msg"].as_str().unwrap_or(""));
                }
            }
            let p = &d["pack"];
            if p.is_object() {
                println!("\n  stored as one immutable pack {}: {} object(s), {} bytes{}", short(p["id"].as_str().unwrap_or("")), p["objects"], p["bytes"],
                         if p["external_bases"].as_u64().unwrap_or(0) > 0 { format!(", thin ({} delta base(s) from earlier packs)", p["external_bases"]) } else { String::new() });
            }
            println!("  server time {:.1} ms", d["server_ms"].as_f64().unwrap_or(0.0));
            if let Some(c) = d["now"]["self_contained_copy"].as_str() {
                println!("\n  STATE now: self-contained copy for packfile-URI clients: {}", c);
            }
        }
        _ => bail!("unknown command"),
    }
    Ok(())
}

pub const HELP: &str = "pando status | explore fetch|history|covers | explain last-push   (run inside a Git checkout)

  pando status [--remote R]                 the remote, whether it is a Pando server, and your branches' versions
  pando explore fetch [--from C] [--to REF] what the server would send you for a fetch (HYPOTHETICAL; nothing sent)
                      --from empty           ... for a fresh clone
  pando explore history [REF]               the ref's versions, their packs and dependencies (STATE)
  pando explore covers [REF]                the compaction hierarchy over the ref's push packs (STATE)
  pando explain last-push [--remote R]      what the server recorded for your last push (RECORDED)
  pando explain --op TOKEN                  ... for the push that printed `pando: operation TOKEN`
  --json                                    machine-readable output (schema pando.explain/1)

R is a remote name or URL (default: the current branch's upstream, else origin). Credentials are the ones Git uses.";
