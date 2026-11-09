//! Subprocess helpers for command-backed providers (plugin, Codex CLI, Claude Code):
//! stdin input, captured output, a hard timeout, and Python `shutil.which` lookup.

use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc;
use std::time::{Duration, Instant};

/// How long to wait for output readers once the child has been killed or has exited; a
/// descendant that inherited the pipes must not keep the call open.
const READER_GRACE: Duration = Duration::from_millis(500);

fn spawn_reader(mut source: impl Read + Send + 'static) -> mpsc::Receiver<String> {
    let (sender, receiver) = mpsc::channel();
    std::thread::spawn(move || {
        let mut text = String::new();
        let _ = source.read_to_string(&mut text);
        let _ = sender.send(text);
    });
    receiver
}

fn kill_tree(child: &mut Child) {
    #[cfg(unix)]
    {
        // The child leads its own process group: signal the whole group so descendants that
        // inherited stdout/stderr die with it.
        unsafe {
            libc::killpg(child.id() as libc::pid_t, libc::SIGKILL);
        }
    }
    let _ = child.kill();
    let _ = child.wait();
}

pub struct Output {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
}

pub enum RunError {
    Start(std::io::Error),
    /// The command exceeded the timeout and was killed (stderr so far).
    Timeout(String),
}

/// How the child's environment is built.
pub enum Env<'a> {
    /// Inherit the parent environment.
    Inherit,
    /// Only the platform basics (`PATH`, temp dirs, ...) plus these variable names.
    Minimal(&'a [String]),
}

pub fn run_command(
    command: &[String],
    input: Option<&str>,
    cwd: &Path,
    env: Env<'_>,
    timeout: Duration,
) -> Result<Output, RunError> {
    let mut process = Command::new(&command[0]);
    process
        .args(&command[1..])
        .current_dir(cwd)
        .stdin(if input.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        process.process_group(0);
    }
    if let Env::Minimal(names) = env {
        process.env_clear();
        for name in [
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "TEMP",
            "TMP",
        ] {
            if let Ok(value) = std::env::var(name) {
                if !value.is_empty() {
                    process.env(name, value);
                }
            }
        }
        for name in names {
            if let Ok(value) = std::env::var(name) {
                process.env(name, value);
            }
        }
    }
    let mut child = process.spawn().map_err(RunError::Start)?;
    let writer = child.stdin.take().map(|mut stdin| {
        let input = input.unwrap_or_default().to_owned();
        std::thread::spawn(move || {
            let _ = stdin.write_all(input.as_bytes());
        })
    });
    let out_reader = spawn_reader(child.stdout.take().expect("piped stdout"));
    let err_reader = spawn_reader(child.stderr.take().expect("piped stderr"));
    let deadline = Instant::now() + timeout;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Some(status),
            Ok(None) if Instant::now() >= deadline => break None,
            Ok(None) => std::thread::sleep(Duration::from_millis(10)),
            Err(_) => break None,
        }
    };
    // The stdin writer finishes once the child exits or is killed (the pipe closes); it is
    // not joined so a descendant holding stdin can't block either.
    drop(writer);
    let Some(status) = status else {
        kill_tree(&mut child);
        let stderr = err_reader.recv_timeout(READER_GRACE).unwrap_or_default();
        return Err(RunError::Timeout(stderr));
    };
    // Output is complete once the pipes close; give the readers until the deadline (or a short
    // grace) so a background descendant still holding a pipe can't hang the caller.
    let wait = deadline
        .saturating_duration_since(Instant::now())
        .max(READER_GRACE);
    let stdout = out_reader.recv_timeout(wait).unwrap_or_default();
    let stderr = err_reader.recv_timeout(READER_GRACE).unwrap_or_default();
    Ok(Output {
        code: status.code().unwrap_or(-1),
        stdout,
        stderr,
    })
}

fn is_executable(path: &Path) -> bool {
    if !path.is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        path.metadata()
            .map(|m| m.permissions().mode() & 0o111 != 0)
            .unwrap_or(false)
    }
    #[cfg(not(unix))]
    {
        true
    }
}

/// Python `shutil.which`: a path with a separator is checked as given, otherwise `PATH` is
/// searched (with `PATHEXT` extensions on Windows).
pub fn which(command: &str) -> Option<PathBuf> {
    let candidate = Path::new(command);
    if candidate.components().count() > 1 {
        return is_executable(candidate).then(|| candidate.to_path_buf());
    }
    let extensions: Vec<String> = if cfg!(windows) {
        std::env::var("PATHEXT")
            .unwrap_or_else(|_| ".COM;.EXE;.BAT;.CMD".into())
            .split(';')
            .map(str::to_owned)
            .collect()
    } else {
        vec![String::new()]
    };
    let path = std::env::var_os("PATH")?;
    std::env::split_paths(&path).find_map(|dir| {
        extensions.iter().find_map(|ext| {
            let full = dir.join(format!("{command}{ext}"));
            is_executable(&full).then_some(full)
        })
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(unix)]
    #[test]
    fn timeout_kills_descendants_holding_the_pipes() {
        let start = Instant::now();
        let result = run_command(
            &["sh".into(), "-c".into(), "sleep 3".into()],
            None,
            &std::env::temp_dir(),
            Env::Inherit,
            Duration::from_millis(100),
        );
        assert!(matches!(result, Err(RunError::Timeout(_))));
        assert!(
            start.elapsed() < Duration::from_secs(2),
            "{:?}",
            start.elapsed()
        );
    }
}
