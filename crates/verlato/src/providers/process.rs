//! Subprocess helpers for command-backed providers (plugin, Codex CLI, Claude Code):
//! stdin input, captured output, a hard timeout, and Python `shutil.which` lookup.

use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

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
    let mut stdout = child.stdout.take().expect("piped stdout");
    let mut stderr = child.stderr.take().expect("piped stderr");
    let out_reader = std::thread::spawn(move || {
        let mut text = String::new();
        let _ = stdout.read_to_string(&mut text);
        text
    });
    let err_reader = std::thread::spawn(move || {
        let mut text = String::new();
        let _ = stderr.read_to_string(&mut text);
        text
    });
    let deadline = Instant::now() + timeout;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Some(status),
            Ok(None) if Instant::now() >= deadline => break None,
            Ok(None) => std::thread::sleep(Duration::from_millis(10)),
            Err(_) => break None,
        }
    };
    let join_writer = |writer: Option<std::thread::JoinHandle<()>>| {
        if let Some(writer) = writer {
            let _ = writer.join();
        }
    };
    let Some(status) = status else {
        let _ = child.kill();
        let _ = child.wait();
        join_writer(writer);
        let _ = out_reader.join();
        return Err(RunError::Timeout(err_reader.join().unwrap_or_default()));
    };
    join_writer(writer);
    Ok(Output {
        code: status.code().unwrap_or(-1),
        stdout: out_reader.join().unwrap_or_default(),
        stderr: err_reader.join().unwrap_or_default(),
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
