//! Honest metadata accounting (CG7 audits: B D-1, A D15).
//!
//! A *logical metadata read* is any read of a metadata record, however it is served:
//! - a point read from the store;
//! - each key returned by a range scan (an empty scan counts 1);
//! - a hit in an in-process cache of metadata records (ref versions, covers, twins);
//! - a lookup in the in-memory object -> pack index.
//!
//! In a distributed deployment each of these would be a read against the metadata service, so counting only
//! physical store operations would under-report the work.
//!
//! Work is attributed to **foreground** (serving a request) or **background** (compaction, checkpointing,
//! `maintain`, startup catch-up) via a thread-local flag, so per-request measurements are not polluted by
//! concurrent background work. A second thread-local counter gives exact per-operation counts (e.g. one fetch
//! plan) on the current thread.
use serde_json::{json, Value};
use std::cell::Cell;
use std::sync::atomic::{AtomicU64, Ordering::Relaxed};

thread_local! {
    static BG: Cell<bool> = const { Cell::new(false) };
    static LOCAL_READS: Cell<u64> = const { Cell::new(0) };
}

pub struct Split {
    fg: AtomicU64,
    bg: AtomicU64,
}

impl Split {
    const fn new() -> Split {
        Split { fg: AtomicU64::new(0), bg: AtomicU64::new(0) }
    }
    fn add(&self, n: u64) {
        if BG.with(|b| b.get()) {
            self.bg.fetch_add(n, Relaxed);
        } else {
            self.fg.fetch_add(n, Relaxed);
        }
    }
    fn get(&self) -> (u64, u64) {
        (self.fg.load(Relaxed), self.bg.load(Relaxed))
    }
}

/// All logical reads (sum of the categories below).
static READS: Split = Split::new();
static STORE_POINT: Split = Split::new();
static STORE_SCAN_KEYS: Split = Split::new();
static CACHE_HITS: Split = Split::new();
static INDEX_LOOKUPS: Split = Split::new();
static WRITES: Split = Split::new();
static PLAN_STEPS: Split = Split::new();
/// Consultations of a cover's precomputed substitution map (pack-local immutable sidecar, NOT a metadata read).
static ALT_MAP_LOOKUPS: Split = Split::new();

fn read(cat: &Split, n: u64) {
    cat.add(n);
    READS.add(n);
    LOCAL_READS.with(|c| c.set(c.get() + n));
}

pub fn store_point_read() {
    read(&STORE_POINT, 1);
}
pub fn store_scan(keys: u64) {
    read(&STORE_SCAN_KEYS, keys.max(1));
}
pub fn cache_hit() {
    read(&CACHE_HITS, 1);
}
pub fn index_lookup() {
    read(&INDEX_LOOKUPS, 1);
}
pub fn writes(n: u64) {
    WRITES.add(n);
}
pub fn alt_map_lookup() {
    ALT_MAP_LOOKUPS.add(1);
}
/// Object-store requests (docs/NETWORK-STORAGE.md): uploads that created an object, uploads refused because the key
/// existed (412, then verified), and downloads into the local cache. Not metadata reads.
static S3_PUT_CREATED: Split = Split::new();
static S3_PUT_EXISTS: Split = Split::new();
static S3_GET: Split = Split::new();
static S3_RETRY: Split = Split::new();
static S3_GAVE_UP: Split = Split::new();
pub fn s3_op(kind: &str) {
    match kind {
        "put_created" => S3_PUT_CREATED.add(1),
        "put_exists" => S3_PUT_EXISTS.add(1),
        "retry" => S3_RETRY.add(1),
        "gave_up" => S3_GAVE_UP.add(1),
        _ => S3_GET.add(1),
    }
}
pub fn plan_steps(n: u64) {
    PLAN_STEPS.add(n);
}

/// Logical reads performed so far on this thread (for exact per-operation deltas).
pub fn local_reads() -> u64 {
    LOCAL_READS.with(|c| c.get())
}

/// Mark the current thread as doing background work until the guard is dropped.
pub struct Background(bool);
pub fn background() -> Background {
    Background(BG.with(|b| b.replace(true)))
}
impl Drop for Background {
    fn drop(&mut self) {
        let prev = self.0;
        BG.with(|b| b.set(prev));
    }
}

/// Fields for `/_stats`. `metadata_reads` / `metadata_writes` are FOREGROUND logical counts.
pub fn stats() -> Value {
    let (r, rb) = READS.get();
    let (w, wb) = WRITES.get();
    let f = |s: &Split| {
        let (a, b) = s.get();
        json!({"foreground": a, "background": b})
    };
    json!({
        "metadata_reads": r,
        "metadata_writes": w,
        "metadata_reads_background": rb,
        "metadata_writes_background": wb,
        "metadata_read_breakdown": {
            "store_point_reads": f(&STORE_POINT),
            "store_scan_keys": f(&STORE_SCAN_KEYS),
            "cache_hits": f(&CACHE_HITS),
            "object_index_lookups": f(&INDEX_LOOKUPS),
        },
        "plan_steps": f(&PLAN_STEPS),
        "alt_map_lookups": f(&ALT_MAP_LOOKUPS),
        "object_store": {"put_created": f(&S3_PUT_CREATED), "put_exists": f(&S3_PUT_EXISTS), "get": f(&S3_GET), "retries": f(&S3_RETRY), "gave_up": f(&S3_GAVE_UP)},
        "metadata_counting": "logical: every metadata record read (store point read, each scanned key, cache hit, object-index lookup); foreground = request-serving threads, background = compaction/checkpoint/maintain/startup",
    })
}
