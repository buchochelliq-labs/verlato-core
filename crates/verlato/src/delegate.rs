//! Delegation to the Python implementation for features not yet ported to Rust (batched and
//! tool-retrieval suggestion strategies, adaptive recommendations and a few maintenance
//! commands).
//!
//! The Python CLI is located with `VERLATO_PYTHON_CLI` (a command line, e.g.
//! `python3 -m verlato.cli`), then `verlato-py` on PATH. Delegated commands keep the exact
//! Python behaviour and output.

use anyhow::{bail, Context, Result};
use std::process::{Command, Stdio};

pub const PYTHON_HINT: &str = "Some features (batched and tool-retrieval suggestion strategies and adaptive \
     recommendations) are still served by the Python implementation. \
     Install it with `pip install -e .` from the Verlato repository (it provides `verlato-py`), or set \
     VERLATO_PYTHON_CLI to the command that runs it.";

/// The command that runs the Python CLI, if one is available.
pub fn python_cli() -> Option<Vec<String>> {
    if let Ok(raw) = std::env::var("VERLATO_PYTHON_CLI") {
        let trimmed = raw.trim();
        if !trimmed.is_empty() && std::path::Path::new(trimmed).is_file() {
            // A path to the verlato-py executable (may contain spaces).
            return Some(vec![trimmed.to_owned()]);
        }
        let parts: Vec<String> = trimmed.split_whitespace().map(str::to_owned).collect();
        if !parts.is_empty() {
            return Some(parts);
        }
    }
    let probe = Command::new("verlato-py")
        .arg("--help")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
    if probe.is_ok_and(|s| s.success()) {
        return Some(vec!["verlato-py".into()]);
    }
    None
}

/// Run the Python CLI with `args`, inheriting stdio, and return its exit code.
pub fn run(args: &[String], reason: &str) -> Result<i32> {
    let Some(command) = python_cli() else {
        bail!(
            "`verlato {}` is not available in the Rust build yet ({reason}). {PYTHON_HINT}",
            args.join(" ")
        );
    };
    if std::env::var("VERLATO_TRACE_DELEGATION").as_deref() == Ok("1") {
        eprintln!(
            "[verlato:delegate] {reason}: {} {}",
            command.join(" "),
            args.join(" ")
        );
    }
    let status = Command::new(&command[0])
        .args(&command[1..])
        .args(args)
        .status()
        .with_context(|| {
            format!(
                "failed to run the Python implementation ({})",
                command.join(" ")
            )
        })?;
    Ok(status.code().unwrap_or(1))
}
