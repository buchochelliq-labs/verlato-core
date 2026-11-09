//! JSON-lines stdio service used by the VS Code extension (`verlato service`). Protocol and
//! payloads match `verlato/service_stdio.py`. Methods not yet ported are proxied to a lazily
//! started Python service process.

use anyhow::{anyhow, Context as _, Result};
use serde_json::{json, Map, Value};
use std::io::{BufRead, BufReader, Write};
use std::path::Path;
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

use crate::pipeline::Context;

const NATIVE_METHODS: &[&str] = &[
    "health",
    "info",
    "draft.get",
    "draft.preview",
    "draft.apply",
    "draft.review",
    "feedback.record",
    "state.show",
    "state.clear_working",
];

struct ProtocolError(String);

struct PythonService {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
}

impl PythonService {
    fn start(cwd: &Path) -> Result<PythonService> {
        let command =
            crate::delegate::python_cli().ok_or_else(|| anyhow!(crate::delegate::PYTHON_HINT))?;
        let mut child = Command::new(&command[0])
            .args(&command[1..])
            .arg("service")
            .current_dir(cwd)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .context("failed to start the Python service")?;
        let stdin = child.stdin.take().expect("piped");
        let stdout = BufReader::new(child.stdout.take().expect("piped"));
        Ok(PythonService {
            child,
            stdin,
            stdout,
        })
    }

    fn forward(&mut self, line: &str) -> Result<String> {
        self.stdin.write_all(line.trim_end().as_bytes())?;
        self.stdin.write_all(b"\n")?;
        self.stdin.flush()?;
        let mut response = String::new();
        if self.stdout.read_line(&mut response)? == 0 {
            return Err(anyhow!("the Python service exited"));
        }
        Ok(response.trim_end().to_owned())
    }
}

impl Drop for PythonService {
    fn drop(&mut self) {
        let _ = self
            .stdin
            .write_all(b"{\"id\":null,\"method\":\"shutdown\"}\n");
        let _ = self.stdin.flush();
        let _ = self.child.wait();
    }
}

pub fn run(cwd: &Path) -> Result<i32> {
    let stdin = std::io::stdin();
    let mut stdout = std::io::stdout();
    let mut python: Option<PythonService> = None;
    for line in stdin.lock().lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let (response, shutdown) = handle_line(cwd, &line, &mut python);
        writeln!(stdout, "{response}")?;
        stdout.flush()?;
        if shutdown {
            break;
        }
    }
    Ok(0)
}

fn handle_line(cwd: &Path, line: &str, python: &mut Option<PythonService>) -> (String, bool) {
    let request: Value = match serde_json::from_str(line) {
        Ok(value) => value,
        Err(error) => {
            return (
                error_response(
                    &Value::Null,
                    "invalid_json",
                    &format!("Invalid JSON request: {error}"),
                ),
                false,
            )
        }
    };
    let id = request.get("id").cloned().unwrap_or(Value::Null);
    let Some(object) = request.as_object() else {
        return (
            error_response(&id, "invalid_request", "Request must be a JSON object."),
            false,
        );
    };
    let Some(method) = object
        .get("method")
        .and_then(Value::as_str)
        .filter(|m| !m.trim().is_empty())
    else {
        return (
            error_response(&id, "invalid_request", "Request method is required."),
            false,
        );
    };
    let params = match object.get("params") {
        None => Map::new(),
        Some(Value::Object(map)) => map.clone(),
        Some(_) => {
            return (
                error_response(&id, "invalid_request", "Request params must be an object."),
                false,
            )
        }
    };
    if method == "shutdown" {
        return (ok_response(&id, json!({"shutdown": true})), true);
    }
    if !NATIVE_METHODS.contains(&method) || needs_python(cwd, method) {
        if python.is_none() {
            match PythonService::start(cwd) {
                Ok(service) => *python = Some(service),
                Err(error) => {
                    return (
                        error_response(
                            &id,
                            "unsupported",
                            &format!("Unknown service method: {method}. {error}"),
                        ),
                        false,
                    )
                }
            }
        }
        return match python.as_mut().unwrap().forward(line) {
            Ok(response) => (response, false),
            Err(error) => {
                *python = None;
                (
                    error_response(&id, "RuntimeError", &error.to_string()),
                    false,
                )
            }
        };
    }
    match dispatch(cwd, method, &params) {
        Ok(result) => (ok_response(&id, result), false),
        Err(Dispatch::Protocol(ProtocolError(message))) => {
            (error_response(&id, "invalid_request", &message), false)
        }
        Err(Dispatch::Handler(error)) => (
            error_response(&id, "handler_error", &format!("{error:#}")),
            false,
        ),
    }
}

/// Whether a native method must still run in Python. Draft methods detect changes, so they
/// keep Python semantics for the same prerequisites as the CLI (`python_reason` in main.rs):
/// commit intent inference or the GitHub PR adapter. Model providers are all native.
fn needs_python(cwd: &Path, method: &str) -> bool {
    if !method.starts_with("draft.") {
        return false;
    }
    match Context::load(cwd) {
        Ok(ctx) => ctx.pr_adapter().is_err() || ctx.config.commit_intent_inference(None) != "off",
        // Let the native handler report the configuration error.
        Err(_) => false,
    }
}

enum Dispatch {
    Protocol(ProtocolError),
    Handler(anyhow::Error),
}

impl From<anyhow::Error> for Dispatch {
    fn from(error: anyhow::Error) -> Self {
        Dispatch::Handler(error)
    }
}

fn required<'a>(params: &'a Map<String, Value>, key: &str) -> Result<&'a str, Dispatch> {
    params
        .get(key)
        .and_then(Value::as_str)
        .filter(|v| !v.trim().is_empty())
        .ok_or_else(|| {
            Dispatch::Protocol(ProtocolError(format!(
                "Missing required string param: {key}"
            )))
        })
}

fn optional(params: &Map<String, Value>, key: &str) -> Option<String> {
    match params.get(key) {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) => Some(s.clone()),
        Some(other) => Some(other.to_string()),
    }
}

fn truthy(params: &Map<String, Value>, keys: &[&str]) -> bool {
    keys.iter().any(|k| match params.get(*k) {
        Some(Value::Bool(b)) => *b,
        Some(Value::Null) | None => false,
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(_) => true,
    })
}

struct ReleaseRequest {
    base: String,
    head: String,
    version: String,
    include_confidence: bool,
    include_provenance: bool,
    model_provider: Option<String>,
}

impl ReleaseRequest {
    /// The service forwards only the requested provider to draft detection, as Python does.
    fn model(&self) -> crate::pipeline::ModelRequest {
        crate::pipeline::ModelRequest {
            provider: self.model_provider.clone(),
            ..Default::default()
        }
    }
}

fn release_request(params: &Map<String, Value>) -> Result<ReleaseRequest, Dispatch> {
    let request = params
        .get("request")
        .and_then(Value::as_object)
        .unwrap_or(params);
    let text_or = |key: &str, default: &str| {
        request
            .get(key)
            .and_then(Value::as_str)
            .filter(|v| !v.is_empty())
            .unwrap_or(default)
            .to_owned()
    };
    Ok(ReleaseRequest {
        base: required(request, "base")?.to_owned(),
        head: text_or("head", "HEAD"),
        version: text_or("version", "Unreleased"),
        include_confidence: truthy(request, &["include_confidence", "includeConfidence"]),
        include_provenance: truthy(request, &["include_provenance", "includeProvenance"]),
        model_provider: ["model_provider", "modelProvider"]
            .iter()
            .find_map(|k| request.get(*k).and_then(Value::as_str))
            .map(|p| p.trim().to_owned())
            .filter(|p| !p.is_empty()),
    })
}

fn dispatch(cwd: &Path, method: &str, params: &Map<String, Value>) -> Result<Value, Dispatch> {
    let ctx = Context::load(cwd)?;
    let to_value = |v: serde_json::Result<Value>| v.map_err(|e| Dispatch::Handler(e.into()));
    match method {
        "health" => {
            Ok(json!({"status": "ok", "version": env!("CARGO_PKG_VERSION"), "runtime": "rust"}))
        }
        "info" => {
            let state = ctx.store().load()?;
            Ok(json!({
                "status": "ok",
                "version": env!("CARGO_PKG_VERSION"),
                "runtime": "rust",
                "diff_engine": crate::engine::engine_info(ctx.diff_engine(), &ctx.config.semantic_cache_path),
                "state_path": ctx.config.state_path(),
                "schema_version": state.schema_version,
                "tracked_items": state.items.len(),
                "targets": ctx.config.targets.keys().collect::<Vec<_>>(),
                "python_delegate": crate::delegate::python_cli().map(|c| c.join(" ")),
            }))
        }
        "draft.get" | "draft.preview" | "draft.apply" => {
            let request = release_request(params)?;
            let document = ctx.build_draft(
                &request.base,
                &request.head,
                &request.version,
                request.include_confidence,
                request.include_provenance,
                Some(&request.model()),
            )?;
            match method {
                "draft.get" => to_value(serde_json::to_value(&document)),
                "draft.preview" => Ok(json!(document.rendered["github-release"])),
                _ => {
                    let target = params
                        .get("target")
                        .and_then(Value::as_str)
                        .filter(|t| !t.is_empty())
                        .unwrap_or("changelog");
                    let result =
                        ctx.apply_draft(&document, target, optional(params, "file").as_deref())?;
                    to_value(serde_json::to_value(result))
                }
            }
        }
        "draft.review" => {
            let request = release_request(params)?;
            let provenance = params
                .get("summary_provenance")
                .and_then(Value::as_object)
                .cloned();
            let updated = ctx.update_review(
                &request.base,
                &request.head,
                required(params, "item_id")?,
                optional(params, "status").as_deref(),
                optional(params, "summary").as_deref(),
                optional(params, "reviewer").as_deref(),
                provenance,
                Some(&request.model()),
            )?;
            to_value(serde_json::to_value(updated))
        }
        "feedback.record" => {
            let metadata = params
                .get("metadata")
                .filter(|m| m.is_object())
                .cloned()
                .unwrap_or_else(|| json!({}));
            Ok(ctx.record_feedback(
                required(params, "event")?,
                required(params, "item_id")?,
                metadata,
            )?)
        }
        "state.show" => Ok(ctx.state_show()?),
        "state.clear_working" => Ok(ctx.clear_working_state()?),
        other => Err(Dispatch::Protocol(ProtocolError(format!(
            "Unknown service method: {other}"
        )))),
    }
}

fn ok_response(id: &Value, result: Value) -> String {
    json!({"id": id, "ok": true, "result": result}).to_string()
}

fn error_response(id: &Value, code: &str, message: &str) -> String {
    json!({"id": id, "ok": false, "error": {"code": code, "message": message}}).to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn draft_methods_route_to_python_only_for_the_github_adapter() {
        let dir = std::env::temp_dir();
        assert!(!needs_python(&dir, "draft.get"));
        std::env::set_var("VERLATO_PR_ADAPTER", "github");
        let github = needs_python(&dir, "draft.get");
        let state = needs_python(&dir, "state.show");
        std::env::remove_var("VERLATO_PR_ADAPTER");
        assert!(github);
        assert!(!state);
    }
}
