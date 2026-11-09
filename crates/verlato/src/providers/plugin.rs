//! Audited external-command provider (ports `verlato/providers/plugin.py`).
//!
//! Verlato owns prompts, evidence, response schema and validation: the plugin receives one
//! JSON request on stdin (`verlato.model_plugin.request.v1`) and answers with JSON on stdout,
//! echoing `request_hash`.

use serde_json::{json, Map, Value};
use std::time::Duration;

use super::process::{run_command, Env, RunError};
use super::{
    messages_for_request, request_audit, summarize_task, Provider, ProviderRequest,
    ProviderResponse,
};
use crate::pyjson;

pub const PLUGIN_REQUEST_SCHEMA: &str = "verlato.model_plugin.request.v1";
const DEFAULT_PLUGIN_TIMEOUT_SECONDS: f64 = 30.0;
const MAX_STDERR_CHARS: usize = 4000;

pub struct PluginProvider {
    id: String,
    display_name: String,
    capabilities: Map<String, Value>,
    command: Vec<String>,
    plugin_config: Map<String, Value>,
    env_names: Vec<String>,
    model: String,
    suggest_model: Option<String>,
    timeout_seconds: f64,
}

fn text_field(manifest: &Map<String, Value>, key: &str) -> Option<String> {
    manifest
        .get(key)
        .filter(|v| !v.is_null() && v.as_str() != Some(""))
        .map(|v| match v {
            Value::String(s) => s.trim().to_owned(),
            other => other.to_string(),
        })
}

/// Python's `f"{value:g}"` for the timeout values Verlato prints.
pub fn format_g(value: f64) -> String {
    if value.fract() == 0.0 && value.abs() < 1e16 {
        format!("{}", value as i64)
    } else {
        pyjson::float_repr(value)
    }
}

impl PluginProvider {
    pub fn new(
        manifest: &Map<String, Value>,
        command: Vec<String>,
        plugin_config: Map<String, Value>,
        env_names: Vec<String>,
        model: Option<String>,
        suggest_model: Option<String>,
        timeout_seconds: Option<f64>,
    ) -> PluginProvider {
        let id = text_field(manifest, "id").unwrap_or_else(|| "external".into());
        let display_name = text_field(manifest, "display_name")
            .or_else(|| text_field(manifest, "name"))
            .unwrap_or_else(|| id.clone());
        let command = if command.is_empty() {
            super::command_list(manifest.get("command"))
        } else {
            command
        };
        let model = model
            .filter(|m| !m.is_empty())
            .or_else(|| {
                std::env::var("VERLATO_PLUGIN_MODEL")
                    .ok()
                    .filter(|m| !m.is_empty())
            })
            .unwrap_or_else(|| id.clone());
        PluginProvider {
            capabilities: manifest
                .get("capabilities")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default(),
            id,
            display_name,
            command,
            plugin_config,
            env_names,
            model,
            suggest_model: suggest_model.filter(|m| !m.trim().is_empty()),
            timeout_seconds: timeout_seconds
                .filter(|t| *t > 0.0)
                .unwrap_or(DEFAULT_PLUGIN_TIMEOUT_SECONDS),
        }
    }

    fn task_model(&self, task: &str) -> &str {
        match (&self.suggest_model, task) {
            (Some(model), "suggest") => model,
            _ => &self.model,
        }
    }

    fn label(&self, model: &str) -> String {
        format!("plugin:{}:{model}", self.id)
    }

    fn invoke(&self, task: &str, request: &ProviderRequest) -> ProviderResponse {
        let model = self.task_model(task).to_owned();
        if self.command.is_empty() {
            return ProviderResponse::error(
                "[plugin-unavailable] no command configured".into(),
                self.label(&model),
                Map::new(),
            );
        }
        let messages = messages_for_request(request, task);
        let audit = request_audit(request, &format!("plugin:{}", self.id), &model, &messages);
        let payload = json!({
            "schema": PLUGIN_REQUEST_SCHEMA,
            "plugin": {
                "id": self.id,
                "display_name": self.display_name,
                "capabilities": self.capabilities,
            },
            "request_hash": audit["request_hash"],
            "prompt_hash": audit["prompt_hash"],
            "evidence_hash": audit["evidence_hash"],
            "schema_hash": audit["schema_hash"],
            "task": request.task,
            "model": model,
            "messages": messages,
            "prompt": request.prompt,
            "evidence": request.evidence,
            "response_format": request.response_format,
            "config": self.plugin_config,
            "metadata": request.metadata,
        });
        let with_audit = |extra: &[(&str, Value)]| {
            let mut metadata = Map::new();
            metadata.insert("audit".into(), Value::Object(audit.clone()));
            for (key, value) in extra {
                metadata.insert((*key).into(), value.clone());
            }
            metadata
        };
        let input = pyjson::dumps(&payload, pyjson::DEFAULT_SORTED);
        let run = run_command(
            &self.command,
            Some(&input),
            &std::env::temp_dir(),
            Env::Minimal(&self.env_names),
            Duration::from_secs_f64(self.timeout_seconds),
        );
        let output = match run {
            Ok(output) => output,
            Err(RunError::Timeout(stderr)) => {
                return ProviderResponse::error(
                    format!(
                        "[plugin-timeout] command timed out after {}s",
                        format_g(self.timeout_seconds)
                    ),
                    self.label(&model),
                    with_audit(&[("stderr", json!(trim_text(&stderr)))]),
                )
            }
            Err(RunError::Start(error)) => {
                return ProviderResponse::error(
                    format!("[plugin-error] command failed to start: {error}"),
                    self.label(&model),
                    with_audit(&[]),
                )
            }
        };
        let stderr = trim_text(&output.stderr);
        if output.code != 0 {
            return ProviderResponse::error(
                format!("[plugin-error] command exited {}", output.code),
                self.label(&model),
                with_audit(&[
                    ("stderr", json!(stderr)),
                    ("returncode", json!(output.code)),
                ]),
            );
        }
        let stdout = if output.stdout.is_empty() {
            "{}"
        } else {
            &output.stdout
        };
        let raw: Value = match serde_json::from_str(stdout) {
            Ok(value) => value,
            Err(error) => {
                return ProviderResponse::error(
                    format!("[plugin-invalid-json] stdout was not valid JSON: {error}"),
                    self.label(&model),
                    with_audit(&[("stderr", json!(stderr))]),
                )
            }
        };
        let Some(raw) = raw.as_object() else {
            return ProviderResponse::error(
                "[plugin-invalid-json] stdout JSON must be an object".into(),
                self.label(&model),
                with_audit(&[("stderr", json!(stderr))]),
            );
        };
        let returned_hash = raw.get("request_hash").cloned().unwrap_or(Value::Null);
        if returned_hash != audit["request_hash"] {
            return ProviderResponse::error(
                "[plugin-tamper] request_hash missing or mismatched".into(),
                self.label(&model),
                with_audit(&[
                    ("stderr", json!(stderr)),
                    ("returned_request_hash", returned_hash),
                ]),
            );
        }
        let content = match raw.get("content") {
            Some(Value::String(s)) => s.trim().to_owned(),
            None | Some(Value::Null) | Some(Value::Bool(false)) => String::new(),
            Some(other) => other.to_string().trim().to_owned(),
        };
        if content.is_empty() {
            return ProviderResponse::error(
                "[plugin-empty] no content".into(),
                self.label(&model),
                with_audit(&[("stderr", json!(stderr))]),
            );
        }
        let mut metadata = raw
            .get("metadata")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        metadata.insert("audit".into(), Value::Object(audit.clone()));
        if !stderr.is_empty() {
            metadata.insert("stderr".into(), json!(stderr));
        }
        if let Some(schema) = raw.get("schema").filter(|s| truthy(s)) {
            metadata.insert("schema".into(), schema.clone());
        }
        let text = |key: &str| match raw.get(key) {
            Some(Value::String(s)) if !s.is_empty() => Some(s.clone()),
            Some(Value::String(_)) | None | Some(Value::Null) => None,
            Some(other) if truthy(other) => Some(other.to_string()),
            _ => None,
        };
        ProviderResponse {
            content,
            confidence: confidence(raw.get("confidence")),
            model: text("model").unwrap_or_else(|| self.label(&model)),
            metadata,
            reasoning: text("reasoning").unwrap_or_default(),
        }
    }
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

fn confidence(value: Option<&Value>) -> f64 {
    let parsed = match value {
        Some(Value::Number(n)) => n.as_f64(),
        Some(Value::Bool(b)) => Some(if *b { 1.0 } else { 0.0 }),
        Some(Value::String(s)) => s.trim().parse::<f64>().ok(),
        _ => None,
    };
    match parsed {
        Some(v) if !v.is_nan() => v.clamp(0.0, 1.0),
        Some(_) => 1.0,
        None => 0.75,
    }
}

fn trim_text(text: &str) -> String {
    text.trim().chars().take(MAX_STDERR_CHARS).collect()
}

impl Provider for PluginProvider {
    fn class_name(&self) -> &'static str {
        "ExternalModelPluginProvider"
    }

    fn model(&self) -> Option<String> {
        Some(self.model.clone())
    }

    fn suggest_model(&self) -> Option<String> {
        self.suggest_model.clone()
    }

    fn model_for_task(&self, task: &str) -> Option<String> {
        Some(self.task_model(task).to_owned())
    }

    fn summarize(&self, request: &ProviderRequest) -> ProviderResponse {
        self.invoke(summarize_task(&request.task), request)
    }

    fn classify(&self, request: &ProviderRequest) -> ProviderResponse {
        self.invoke("classify", request)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn formats_timeouts_like_python() {
        assert_eq!(format_g(30.0), "30");
        assert_eq!(format_g(2.5), "2.5");
    }

    #[test]
    fn confidence_defaults_and_clamps() {
        assert_eq!(confidence(None), 0.75);
        assert_eq!(confidence(Some(&json!("0.9"))), 0.9);
        assert_eq!(confidence(Some(&json!(4))), 1.0);
        assert_eq!(confidence(Some(&json!({}))), 0.75);
    }
}
