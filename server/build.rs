// Build identity for `pando version` (docs/RELEASE-PLAN.md section 2): the target triple, and the source commit.
// The commit is the last one that changed this package (server/), so a documentation-only commit does not change
// the binary (the regression gate ties its results to the binary's hash). It comes from PANDO_BUILD_COMMIT if set,
// else from git, else "unknown".
use std::process::Command;

fn git(args: &[&str]) -> Option<String> {
    let o = Command::new("git").args(args).output().ok()?;
    if !o.status.success() {
        return None;
    }
    Some(String::from_utf8(o.stdout).ok()?.trim().to_string()).filter(|s| !s.is_empty())
}

fn main() {
    println!("cargo:rustc-env=PANDO_TARGET={}", std::env::var("TARGET").unwrap_or_default());
    println!("cargo:rerun-if-env-changed=PANDO_BUILD_COMMIT");
    println!("cargo:rerun-if-changed=build.rs");
    let commit = match std::env::var("PANDO_BUILD_COMMIT").ok().filter(|s| !s.is_empty()) {
        Some(c) => c,
        None => {
            // rebuild when HEAD moves (the HEAD file, the branch it points to, and packed refs)
            for p in ["HEAD", "packed-refs"] {
                if let Some(f) = git(&["rev-parse", "--git-path", p]) {
                    println!("cargo:rerun-if-changed={}", f);
                }
            }
            if let Some(r) = git(&["symbolic-ref", "-q", "HEAD"]) {
                if let Some(f) = git(&["rev-parse", "--git-path", &r]) {
                    println!("cargo:rerun-if-changed={}", f);
                }
            }
            git(&["log", "-1", "--format=%h", "--abbrev=12", "--", "."]).unwrap_or_else(|| "unknown".into())
        }
    };
    println!("cargo:rustc-env=PANDO_COMMIT={}", commit);
}
