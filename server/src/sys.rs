//! Operating-system specifics in one place (docs/WINDOWS.md). On Unix every function here is exactly the call
//! the code made before the Windows port (flock, pread, kill, setsid, setrlimit, getrusage), so Unix behaviour is
//! unchanged. On Windows each one has the equivalent that keeps the same guarantee, or refuses.

use std::fs::File;
use std::path::Path;

/// Try to take an exclusive, non-blocking lock on `f`, held until the file is closed or the process dies.
/// Unix: `flock(LOCK_EX | LOCK_NB)`. Windows: `LockFileEx` (exclusive, fail immediately) through std.
pub fn try_lock_exclusive(f: &File) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::io::AsRawFd;
        unsafe { libc::flock(f.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) == 0 }
    }
    #[cfg(windows)]
    {
        f.try_lock().is_ok()
    }
}

/// Positioned read of exactly `buf.len()` bytes at `off`, safe for concurrent readers sharing one handle.
/// Unix: `pread`. Windows: `ReadFile` with an explicit offset (`seek_read`), looped; nothing here uses the cursor.
pub fn read_exact_at(f: &File, mut buf: &mut [u8], mut off: u64) -> std::io::Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::FileExt;
        let _ = &mut off;
        let _ = &mut buf;
        f.read_exact_at(buf, off)
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::FileExt;
        while !buf.is_empty() {
            match f.seek_read(buf, off) {
                Ok(0) => return Err(std::io::Error::new(std::io::ErrorKind::UnexpectedEof, "failed to fill whole buffer")),
                Ok(n) => {
                    buf = &mut buf[n..];
                    off += n as u64;
                }
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => {}
                Err(e) => return Err(e),
            }
        }
        Ok(())
    }
}

/// Make the entries of directory `p` durable (a new name linked or renamed into it). Errors are returned, never
/// ignored (CG7 audit B D-5): a directory whose entries cannot be made durable must not back an acknowledgement.
/// Unix: `fsync` of the directory (on Apple platforms std's `sync_all` is `F_FULLFSYNC`).
/// Windows: the directory is opened with `FILE_FLAG_BACKUP_SEMANTICS` and flushed with `FlushFileBuffers`, which
/// on NTFS commits the volume's metadata log for it. If the file system refuses that (FAT, exFAT, some network
/// shares), the error propagates and the push fails: the guarantee is not silently weakened.
pub fn fsync_dir(p: &Path) -> std::io::Result<()> {
    #[cfg(unix)]
    {
        File::open(p)?.sync_all()
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        const FILE_FLAG_BACKUP_SEMANTICS: u32 = 0x0200_0000;
        std::fs::OpenOptions::new().read(true).write(true).custom_flags(FILE_FLAG_BACKUP_SEMANTICS).open(p)?.sync_all()
    }
}

/// Is process `pid` alive? Unix: `kill(pid, 0)`. Windows: the process exists and has not exited.
pub fn pid_alive(pid: i32) -> bool {
    if pid <= 0 {
        return false;
    }
    #[cfg(unix)]
    {
        unsafe { libc::kill(pid, 0) == 0 }
    }
    #[cfg(windows)]
    {
        use windows_sys::Win32::Foundation::{CloseHandle, STILL_ACTIVE};
        use windows_sys::Win32::System::Threading::{GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION};
        unsafe {
            let h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid as u32);
            if h.is_null() {
                return false;
            }
            let mut code = 0u32;
            let ok = GetExitCodeProcess(h, &mut code) != 0;
            CloseHandle(h);
            ok && code == STILL_ACTIVE as u32
        }
    }
}

/// Ask process `pid` (a server started by `pando start`) to stop. Unix: SIGTERM (it exits cleanly).
/// Windows: there is no signal a detached process can receive, so this terminates it (`TerminateProcess`), the
/// equivalent of SIGKILL. That is safe by design: every acknowledged push is already durable, and the crash
/// tests (CG2, P0) kill the server this way on every platform. The data-directory lock dies with the process.
pub fn request_stop(pid: i32) {
    #[cfg(unix)]
    unsafe {
        libc::kill(pid, libc::SIGTERM);
    }
    #[cfg(windows)]
    kill(pid);
}

/// Kill process `pid` (and, on Unix, its process group) immediately.
pub fn kill(pid: i32) {
    #[cfg(unix)]
    unsafe {
        libc::kill(-pid, libc::SIGKILL);
        libc::kill(pid, libc::SIGKILL);
    }
    #[cfg(windows)]
    {
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{OpenProcess, TerminateProcess, PROCESS_TERMINATE};
        unsafe {
            let h = OpenProcess(PROCESS_TERMINATE, 0, pid as u32);
            if !h.is_null() {
                TerminateProcess(h, 1);
                CloseHandle(h);
            }
        }
    }
}

/// Detach a child server from the caller's session/console (`pando start`). Unix: `setsid` in the child.
/// Windows: a new process group with no console, so the caller's Ctrl-C does not reach it.
pub fn detach(cmd: &mut std::process::Command) {
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        unsafe {
            cmd.pre_exec(|| {
                libc::setsid();
                Ok(())
            });
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
        const DETACHED_PROCESS: u32 = 0x0000_0008;
        cmd.creation_flags(CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS);
    }
}

/// `pando serve`: leave the caller's session (Unix `setsid`, as before; a no-op when already a group leader).
/// Windows: nothing (Ctrl-C in the console must stop a foreground server).
pub fn new_session() {
    #[cfg(unix)]
    unsafe {
        libc::setsid();
    }
}

/// Many concurrent pushes need many sockets + pack files: lift the soft fd limit (shells often start with 256)
/// as far as the hard limit allows. Windows has no such per-process limit for handles: nothing to do.
pub fn raise_nofile() {
    #[cfg(unix)]
    unsafe {
        let mut rl: libc::rlimit = std::mem::zeroed();
        if libc::getrlimit(libc::RLIMIT_NOFILE, &mut rl) != 0 {
            return;
        }
        for want in [rl.rlim_max, 1 << 20, 65536, 10240] {
            if want <= rl.rlim_cur || want > rl.rlim_max {
                continue;
            }
            let n = libc::rlimit { rlim_cur: want, rlim_max: rl.rlim_max };
            if libc::setrlimit(libc::RLIMIT_NOFILE, &n) == 0 {
                return;
            }
        }
    }
}

/// User + system CPU seconds of this process. Unix: `getrusage`. Windows: `GetProcessTimes`.
pub fn cpu_seconds() -> f64 {
    #[cfg(unix)]
    unsafe {
        let mut ru: libc::rusage = std::mem::zeroed();
        libc::getrusage(libc::RUSAGE_SELF, &mut ru);
        ru.ru_utime.tv_sec as f64 + ru.ru_utime.tv_usec as f64 / 1e6 + ru.ru_stime.tv_sec as f64 + ru.ru_stime.tv_usec as f64 / 1e6
    }
    #[cfg(windows)]
    {
        use windows_sys::Win32::Foundation::FILETIME;
        use windows_sys::Win32::System::Threading::{GetCurrentProcess, GetProcessTimes};
        let z = || FILETIME { dwLowDateTime: 0, dwHighDateTime: 0 };
        let (mut c, mut e, mut k, mut u) = (z(), z(), z(), z());
        let ok = unsafe { GetProcessTimes(GetCurrentProcess(), &mut c, &mut e, &mut k, &mut u) } != 0;
        if !ok {
            return 0.0;
        }
        let t = |f: &FILETIME| (((f.dwHighDateTime as u64) << 32) | f.dwLowDateTime as u64) as f64 / 1e7;
        t(&k) + t(&u)
    }
}

/// Fill `b` from the operating system's CSPRNG. Unix: `/dev/urandom`, as before. Windows: the system RNG through
/// ring (already a dependency; the same source as operation tokens).
pub fn random_bytes(b: &mut [u8]) -> std::io::Result<()> {
    #[cfg(unix)]
    {
        use std::io::Read;
        File::open("/dev/urandom")?.read_exact(b)
    }
    #[cfg(windows)]
    {
        rustls::crypto::ring::default_provider().secure_random.fill(b).map_err(|_| std::io::Error::other("system random"))
    }
}

/// Whether `name` (a repository name, as stored) can be a directory name on this platform. The pack directory is
/// one component (`blob::enc` escapes `/`). Windows refuses device names (`CON`, `NUL`, `COM1`, ..., with or
/// without an extension) and silently strips a trailing dot, which would make `a.` and `a` the same directory.
/// Unix: every name `valid_repo_name` accepts.
pub fn repo_name_portable(name: &str) -> bool {
    if cfg!(windows) {
        if name.ends_with('.') {
            return false;
        }
        let e = crate::blob::enc(name);
        let stem = e.split('.').next().unwrap_or("").to_ascii_uppercase();
        let reserved = ["CON", "PRN", "AUX", "NUL"].contains(&stem.as_str())
            || ((stem.starts_with("COM") || stem.starts_with("LPT")) && stem.len() == 4 && stem.as_bytes()[3].is_ascii_digit());
        if reserved {
            return false;
        }
    }
    true
}

/// The empty configuration file to give git (`GIT_CONFIG_GLOBAL`). Git for Windows maps `/dev/null` too, but `NUL`
/// is the native name.
pub const NULL_DEVICE: &str = if cfg!(windows) { "NUL" } else { "/dev/null" };

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lock_is_exclusive_between_handles_and_released_on_close() {
        let d = std::env::temp_dir().join(format!("pando-sys-lock-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        let p = d.join("LOCK");
        let a = std::fs::OpenOptions::new().create(true).write(true).truncate(false).open(&p).unwrap();
        assert!(try_lock_exclusive(&a));
        let b = std::fs::OpenOptions::new().write(true).open(&p).unwrap();
        assert!(!try_lock_exclusive(&b), "a second handle must not get the lock");
        drop(a);
        assert!(try_lock_exclusive(&b), "the lock is released when its handle closes");
        drop(b);
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn positioned_reads_and_eof() {
        let d = std::env::temp_dir().join(format!("pando-sys-pread-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        let p = d.join("f");
        std::fs::write(&p, b"0123456789").unwrap();
        let f = File::open(&p).unwrap();
        let mut b = [0u8; 4];
        read_exact_at(&f, &mut b, 3).unwrap();
        assert_eq!(&b, b"3456");
        read_exact_at(&f, &mut b, 0).unwrap();
        assert_eq!(&b, b"0123");
        assert!(read_exact_at(&f, &mut b, 8).is_err());
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn directory_fsync_succeeds_on_the_test_file_system() {
        let d = std::env::temp_dir().join(format!("pando-sys-fsync-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        std::fs::write(d.join("x"), b"x").unwrap();
        fsync_dir(&d).unwrap();
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn this_process_is_alive_and_cpu_time_is_counted() {
        assert!(pid_alive(std::process::id() as i32));
        assert!(!pid_alive(0));
        assert!(cpu_seconds() >= 0.0);
        let mut b = [0u8; 32];
        random_bytes(&mut b).unwrap();
        assert_ne!(b, [0u8; 32]);
    }

    #[test]
    fn repository_names_that_windows_cannot_store() {
        for ok in ["demo", "a/b", "my.repo", "console", "com10", "lpt"] {
            assert!(repo_name_portable(ok), "{}", ok);
        }
        for bad in ["con", "NUL", "aux.git2", "com1", "LPT9.x", "a."] {
            assert_eq!(repo_name_portable(bad), !cfg!(windows), "{}", bad);
        }
    }
}
