//! Structured event log (JSON lines) for trace checking. Every line has
//! `seq` (process-local, strictly increasing), `ts_ms`, `node`, `ev` and fields.
//! Vocabulary: see MODEL.md §6.
use serde_json::{json, Value};
use std::fs::{self, File, OpenOptions};
use std::io::{BufWriter, Write};
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

/// Deterministic mode (simulation): logical timestamps, counter-based ids.
pub static DETERMINISTIC: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
pub fn deterministic() -> bool {
    DETERMINISTIC.load(Ordering::Relaxed)
}

pub struct Events {
    out: Option<Mutex<BufWriter<File>>>,
    seq: AtomicU64,
    node: String,
    pub mem: Option<Mutex<Vec<Value>>>,
}

impl Events {
    pub fn open(dir: &Path, node: &str) -> Events {
        let _ = fs::create_dir_all(dir);
        let f = OpenOptions::new().create(true).append(true).open(dir.join("events.jsonl")).ok();
        Events { out: f.map(|f| Mutex::new(BufWriter::new(f))), seq: AtomicU64::new(0), node: node.to_string(), mem: None }
    }
    pub fn memory(node: &str) -> Events {
        Events { out: None, seq: AtomicU64::new(0), node: node.to_string(), mem: Some(Mutex::new(Vec::new())) }
    }
    pub fn emit(&self, ev: &str, mut fields: Value) {
        let seq = self.seq.fetch_add(1, Ordering::SeqCst) + 1;
        if let Value::Object(m) = &mut fields {
            m.insert("seq".into(), json!(seq));
            m.insert("ts_ms".into(), json!(if deterministic() { seq } else { crate::repo::now_ms() }));
            m.insert("node".into(), json!(self.node));
            m.insert("ev".into(), json!(ev));
        }
        if let Some(o) = &self.out {
            let mut w = o.lock().unwrap();
            let _ = serde_json::to_writer(&mut *w, &fields);
            let _ = w.write_all(b"\n");
            let _ = w.flush();
        }
        if let Some(m) = &self.mem {
            m.lock().unwrap().push(fields);
        }
    }
}
