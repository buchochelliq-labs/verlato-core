//! JSON-lines stdio service used by the VS Code extension (`verlato service`). Protocol and
//! payloads match the retired Python service (`verlato/service_stdio.py`). [`call`] answers one
//! request in-process for the Python bindings.

use anyhow::Result;
use serde_json::{json, Map, Value};
use std::io::{BufRead, Write};
use std::path::Path;

use crate::pipeline::Context;

struct ProtocolError(String);

pub fn run(cwd: &Path) -> Result<i32> {
    let stdin = std::io::stdin();
    let mut stdout = std::io::stdout();
    for line in stdin.lock().lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let (response, shutdown) = handle_line(cwd, &line);
        writeln!(stdout, "{response}")?;
        stdout.flush()?;
        if shutdown {
            break;
        }
    }
    Ok(0)
}

/// Answer one service request in-process: `params_json` is the request's params object (empty
/// for none). Returns the response envelope (`{"id": null, "ok": ..., "result"|"error": ...}`).
pub fn call(cwd: &Path, method: &str, params_json: &str) -> String {
    let params: Value = if params_json.trim().is_empty() {
        json!({})
    } else {
        match serde_json::from_str(params_json) {
            Ok(value) => value,
            Err(error) => {
                return error_response(
                    &Value::Null,
                    "invalid_json",
                    &format!("Invalid JSON params: {error}"),
                )
            }
        }
    };
    let request = json!({"id": Value::Null, "method": method, "params": params});
    handle_line(cwd, &request.to_string()).0
}

fn handle_line(cwd: &Path, line: &str) -> (String, bool) {
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
    match dispatch(cwd, method, &params) {
        Ok(result) => (ok_response(&id, result), false),
        Err(Dispatch::Protocol(ProtocolError(message))) => {
            (error_response(&id, "invalid_request", &message), false)
        }
        Err(Dispatch::Handler(error)) => {
            match error.downcast_ref::<crate::pipeline::ServiceError>() {
                // Python answers unexpected exceptions with their class name and message.
                Some(typed) => (error_response(&id, typed.code, &typed.message), false),
                None => (
                    error_response(&id, "handler_error", &format!("{error:#}")),
                    false,
                ),
            }
        }
    }
}

/// `_optional_string(params.get(a) or params.get(b))`.
fn optional_either(params: &Map<String, Value>, keys: &[&str]) -> Option<String> {
    keys.iter()
        .find_map(|k| params.get(*k).filter(|v| value_truthy(v)))
        .map(|v| {
            v.as_str()
                .map(str::to_owned)
                .unwrap_or_else(|| v.to_string())
        })
}

fn value_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

fn conventional_request(params: &Map<String, Value>) -> crate::pipeline::ConventionalDraftRequest {
    crate::pipeline::ConventionalDraftRequest {
        model_provider: optional_either(params, &["model_provider", "modelProvider"]),
        privacy: optional_either(params, &["model_privacy", "modelPrivacy"]),
        allow_remote_patches: truthy(params, &["allow_remote_patches", "allowRemotePatches"]),
        evidence_mode: optional_either(params, &["evidence_mode", "evidenceMode"])
            .unwrap_or_else(|| "diff-only".into()),
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
        "commit.conventional" | "commit.conventional_many" => {
            let base = required(params, "base")?;
            let head = params
                .get("head")
                .filter(|h| value_truthy(h))
                .map(|h| {
                    h.as_str()
                        .map(str::to_owned)
                        .unwrap_or_else(|| h.to_string())
                })
                .unwrap_or_else(|| "HEAD".into());
            let request = conventional_request(params);
            if method == "commit.conventional" {
                let sha = required(params, "sha")?;
                return Ok(ctx.draft_conventional_commit(base, &head, sha, &request)?);
            }
            let shas: Option<Vec<String>> = match params.get("shas") {
                None | Some(Value::Null) => None,
                Some(Value::Array(items)) => Some(
                    items
                        .iter()
                        .map(|v| {
                            v.as_str()
                                .map(str::to_owned)
                                .unwrap_or_else(|| v.to_string())
                        })
                        .filter(|s| !s.trim().is_empty())
                        .collect(),
                ),
                Some(_) => {
                    return Err(Dispatch::Protocol(ProtocolError(
                        "Expected list param: shas".into(),
                    )))
                }
            };
            Ok(ctx.draft_conventional_commits(base, &head, shas.as_deref(), &request)?)
        }
        "state.show" => Ok(ctx.state_show()?),
        "state.clear_working" => Ok(ctx.clear_working_state()?),
        "state.clear_suggestion_cache" => Ok(crate::maintenance::clear_suggestion_cache(&ctx)?),
        "suggestion.items"
        | "suggestion.item"
        | "suggestion.patch"
        | "suggestion.semantic_diff"
        | "suggestion.semantic_search"
        | "suggestion.search" => {
            let base = required(params, "base")?;
            let head = params
                .get("head")
                .filter(|h| value_truthy(h))
                .map(|h| {
                    h.as_str()
                        .map(str::to_owned)
                        .unwrap_or_else(|| h.to_string())
                })
                .unwrap_or_else(|| "HEAD".into());
            // Python validates the remaining required params before building the provider.
            let item_id = if method == "suggestion.items" {
                ""
            } else {
                required(params, "item_id")?
            };
            let path = match method {
                "suggestion.patch" | "suggestion.semantic_diff" => required(params, "path")?,
                _ => "",
            };
            let query = match method {
                "suggestion.semantic_search" | "suggestion.search" => required(params, "query")?,
                _ => "",
            };
            let tools = crate::mcp::suggestion_tools(&ctx, base, &head)?;
            Ok(match method {
                "suggestion.items" => {
                    tools.list_suggestion_items(truthy(params, &["include_files", "includeFiles"]))
                }
                "suggestion.item" => tools.get_suggestion_item(item_id)?,
                "suggestion.patch" => tools.get_file_patch(
                    item_id,
                    path,
                    optional_int(params.get("offset"), 0),
                    optional_int(params.get("limit"), 12_000),
                )?,
                "suggestion.semantic_diff" => tools.get_file_semantic_diff(item_id, path)?,
                "suggestion.semantic_search" => tools.search_semantic_diff(
                    item_id,
                    query,
                    optional_int(params.get("limit"), 5),
                )?,
                _ => {
                    tools.search_item_diff(item_id, query, optional_int(params.get("limit"), 5))?
                }
            })
        }
        other => match crate::extensions::current().service_for(other) {
            Some(extension) => extension
                .call(cwd, other, params)
                .map_err(Dispatch::Handler),
            None => Err(Dispatch::Protocol(ProtocolError(format!(
                "Unknown service method: {other}"
            )))),
        },
    }
}

/// `_optional_int`: Python `int(value)`, or `default` when that fails.
fn optional_int(value: Option<&Value>, default: i64) -> i64 {
    match value {
        None | Some(Value::Null) => default,
        Some(Value::Bool(b)) => i64::from(*b),
        Some(Value::Number(n)) => n
            .as_i64()
            .or_else(|| {
                n.as_f64()
                    .filter(|f| f.is_finite())
                    .map(|f| f.trunc() as i64)
            })
            .unwrap_or(default),
        Some(Value::String(s)) => s.trim().replace('_', "").parse().unwrap_or(default),
        Some(_) => default,
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
    fn calls_answer_with_the_service_envelope() {
        let dir = std::env::temp_dir();
        let health: Value = serde_json::from_str(&call(&dir, "health", "{}")).unwrap();
        assert_eq!(health["ok"], json!(true));
        assert_eq!(health["result"]["runtime"], json!("rust"));
        let unknown: Value = serde_json::from_str(&call(&dir, "nope", "")).unwrap();
        assert_eq!(unknown["error"]["code"], json!("invalid_request"));
        let invalid: Value = serde_json::from_str(&call(&dir, "health", "[1]")).unwrap();
        assert_eq!(invalid["error"]["code"], json!("invalid_request"));
    }
}
