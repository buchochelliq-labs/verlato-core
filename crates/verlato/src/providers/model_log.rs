//! OpenTelemetry-style model activity logs (ports `verlato/providers/model_logging.py`):
//! `[verlato:model] <body>` on stderr with `VERLATO_TRACE_MODEL_CALLS=1`, and JSONL records at
//! `VERLATO_MODEL_LOG_PATH`. The editor parses the stderr lines into its activity view.

use regex::{Captures, Regex};
use serde_json::{json, Map, Value};
use std::io::Write as _;
use std::sync::{Mutex, OnceLock};

use crate::pyjson;

const REDACTION_TEXT: &str = "[REDACTED]";

fn lock() -> &'static Mutex<()> {
    static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
    LOCK.get_or_init(|| Mutex::new(()))
}

fn timestamp() -> String {
    chrono::Utc::now()
        .format("%Y-%m-%dT%H:%M:%S%.6f+00:00")
        .to_string()
}

/// Emit one model log record to the configured sinks.
pub fn emit(body: &str, event_name: &str, attributes: Map<String, Value>, error: bool) {
    let to_stderr = std::env::var("VERLATO_TRACE_MODEL_CALLS").as_deref() == Ok("1");
    let path = std::env::var("VERLATO_MODEL_LOG_PATH")
        .ok()
        .filter(|p| !p.is_empty());
    if !to_stderr && path.is_none() {
        return;
    }
    let _guard = lock().lock().unwrap_or_else(|e| e.into_inner());
    if to_stderr {
        eprintln!("[verlato:model] {body}");
    }
    if let Some(path) = path {
        let now = timestamp();
        let record = json!({
            "Timestamp": now,
            "ObservedTimestamp": now,
            "SeverityText": if error { "ERROR" } else { "INFO" },
            "SeverityNumber": if error { 17 } else { 9 },
            "Body": body,
            "Resource": {"service.name": "verlato"},
            "InstrumentationScope": {"name": "verlato.providers", "version": "0.1.0"},
            "Attributes": attributes,
            "EventName": event_name,
        });
        let path = std::path::PathBuf::from(path);
        if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
            let _ = std::fs::create_dir_all(parent);
        }
        if let Ok(mut file) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&path)
        {
            let _ = writeln!(file, "{}", pyjson::dumps(&record, pyjson::DEFAULT_SORTED));
        }
    }
}

fn patterns() -> &'static Vec<Regex> {
    static PATTERNS: OnceLock<Vec<Regex>> = OnceLock::new();
    PATTERNS.get_or_init(|| {
        [
            r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b",
            r"\bgithub_pat_[A-Za-z0-9_]{20,}\b",
            r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{16,}",
            r#"(?i)\b(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*['"]?[^'"\s]+"#,
            r"(?m)^([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY|AUTH)[A-Z0-9_]*\s*=\s*).+$",
            r"\b[A-Fa-f0-9]{40,}\b",
            r"\b[A-Za-z0-9+/]{48,}={0,2}\b",
        ]
        .iter()
        .map(|p| Regex::new(p).expect("redaction pattern"))
        .collect()
    })
}

fn replacement(found: &Captures) -> String {
    if let Some(prefix) = found.get(1).filter(|m| !m.as_str().is_empty()) {
        return format!("{}{REDACTION_TEXT}", prefix.as_str());
    }
    let text = &found[0];
    if let Some((key, _)) = text.split_once(':') {
        return format!("{key}: {REDACTION_TEXT}");
    }
    if let Some((key, _)) = text.split_once('=') {
        return format!("{key}={REDACTION_TEXT}");
    }
    REDACTION_TEXT.into()
}

/// Redact secrets from a model payload (ports `redact_model_payload`).
pub fn redact(value: &str) -> (String, bool) {
    let mut redacted = value.to_owned();
    for pattern in patterns() {
        redacted = pattern.replace_all(&redacted, replacement).into_owned();
    }
    let changed = redacted != value;
    (redacted, changed)
}

pub fn redaction_enabled() -> bool {
    match std::env::var("VERLATO_REDACT_MODEL_PAYLOADS") {
        Err(_) => true,
        Ok(raw) => !matches!(
            raw.trim().to_lowercase().as_str(),
            "0" | "false" | "no" | "off"
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn redacts_like_python() {
        // Expected values are the Python implementation's output.
        assert_eq!(redact("token=abc123").0, "token[REDACTED]");
        assert_eq!(
            redact("Authorization: Bearer abcdefghijklmnopqrstuvwxyz").0,
            "Authorization[REDACTED] [REDACTED]"
        );
        assert_eq!(
            redact(&format!("GH_TOKEN=ghp_{}", "a".repeat(30))).0,
            "GH_TOKEN=[REDACTED]"
        );
        assert!(!redact("plain words").1);
    }
}
