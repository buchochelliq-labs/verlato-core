//! GitHub Models chat completions provider (ports `verlato/providers/github_copilot.py`).

use serde_json::{json, Map, Value};
use std::time::Duration;

use super::http::{post_json, HttpError};
use super::model_log;
use super::process::{run_command, which, Env};
use super::{default_system_prompt, Provider, ProviderRequest, ProviderResponse};
use crate::pyjson::py_len;

pub const DEFAULT_GITHUB_MODELS_ENDPOINT: &str =
    "https://models.github.ai/inference/chat/completions";
pub const DEFAULT_GITHUB_MODELS_MODEL: &str = "openai/gpt-4o-mini";
const GITHUB_MODELS_API_VERSION: &str = "2022-11-28";
const TIMEOUT_SECONDS: f64 = 20.0;

pub struct GitHubModelsProvider {
    endpoint: String,
    token: Option<String>,
    model: String,
}

fn non_empty(value: Option<String>) -> Option<String> {
    value.filter(|v| !v.is_empty())
}

/// Bare OpenAI model names get the `openai/` publisher prefix GitHub Models expects.
pub fn normalize_model_id(model: &str) -> String {
    let normalized = model.trim();
    if normalized.is_empty() {
        return DEFAULT_GITHUB_MODELS_MODEL.into();
    }
    if normalized.contains('/') {
        return normalized.into();
    }
    if ["gpt-", "o1", "o3", "o4"]
        .iter()
        .any(|p| normalized.starts_with(p))
    {
        return format!("openai/{normalized}");
    }
    normalized.into()
}

fn gh_cli_token() -> Option<String> {
    let gh = which("gh")?;
    let output = run_command(
        &[
            gh.to_string_lossy().into_owned(),
            "auth".into(),
            "token".into(),
        ],
        None,
        &std::env::current_dir().ok()?,
        Env::Inherit,
        Duration::from_secs(5),
    )
    .ok()?;
    (output.code == 0)
        .then(|| output.stdout.trim().to_owned())
        .filter(|t| !t.is_empty())
}

/// Python's `http.HTTPStatus(code).phrase` for the statuses GitHub Models returns.
fn status_phrase(code: u16) -> &'static str {
    match code {
        400 => "Bad Request",
        401 => "Unauthorized",
        403 => "Forbidden",
        404 => "Not Found",
        408 => "Request Timeout",
        409 => "Conflict",
        413 => "Request Entity Too Large",
        422 => "Unprocessable Entity",
        429 => "Too Many Requests",
        500 => "Internal Server Error",
        502 => "Bad Gateway",
        503 => "Service Unavailable",
        504 => "Gateway Timeout",
        _ => "HTTP error",
    }
}

fn format_http_error(code: u16, body: &str) -> String {
    let body = body.trim();
    let details = if body.is_empty() {
        String::new()
    } else {
        match serde_json::from_str::<Value>(body) {
            Ok(parsed) => parsed
                .as_object()
                .and_then(|o| o.get("message"))
                .filter(|m| !m.is_null() && m.as_str() != Some(""))
                .map(|m| {
                    m.as_str()
                        .map(str::to_owned)
                        .unwrap_or_else(|| m.to_string())
                })
                .unwrap_or_else(|| body.to_owned()),
            Err(_) => body.to_owned(),
        }
    };
    let hint = match code {
        401 | 403 => "check that the token has access to GitHub Models",
        429 => "GitHub Models rate limit exceeded",
        _ => "GitHub Models request failed",
    };
    let suffix = if details.is_empty() {
        String::new()
    } else {
        format!(": {details}")
    };
    format!("{hint} ({code} {}){suffix}", status_phrase(code))
}

/// Python `str(value)` for traced metadata values.
fn py_str(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

impl GitHubModelsProvider {
    pub fn new(endpoint: Option<String>, model: Option<String>) -> GitHubModelsProvider {
        let env = |name: &str| non_empty(std::env::var(name).ok());
        GitHubModelsProvider {
            endpoint: non_empty(endpoint)
                .or_else(|| env("VERLATO_GITHUB_MODELS_ENDPOINT"))
                .unwrap_or_else(|| DEFAULT_GITHUB_MODELS_ENDPOINT.into()),
            token: env("GITHUB_TOKEN")
                .or_else(|| env("GH_TOKEN"))
                .or_else(gh_cli_token),
            model: normalize_model_id(
                &non_empty(model)
                    .or_else(|| env("VERLATO_GITHUB_MODEL"))
                    .unwrap_or_else(|| DEFAULT_GITHUB_MODELS_MODEL.into()),
            ),
        }
    }

    fn trace(&self, message: &str, error: bool) {
        model_log::emit(
            message,
            "verlato.model.github_copilot",
            Map::from_iter([
                ("provider".to_owned(), json!("github-copilot")),
                ("model".to_owned(), json!(self.model)),
            ]),
            error,
        );
    }

    fn invoke(&self, task: &str, request: &ProviderRequest) -> ProviderResponse {
        let label = format!("github-copilot:{}", self.model);
        let Some(token) = &self.token else {
            self.trace(
                &format!("unavailable provider=github-copilot task={task} reason=missing-token"),
                true,
            );
            return ProviderResponse::error(
                "[github-copilot-unavailable] missing GITHUB_TOKEN/GH_TOKEN and no logged-in gh user"
                    .into(),
                label,
                Map::new(),
            );
        };
        let body = json!({
            "model": self.model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": default_system_prompt(task)},
                {"role": "user", "content": request.prompt},
            ],
        });
        self.trace(
            &format!(
                "provider=github-copilot task={task} model={} endpoint={} prompt_chars={} evidence_keys={}",
                self.model,
                self.endpoint,
                py_len(&request.prompt),
                request.evidence.as_object().map_or(0, Map::len),
            ),
            false,
        );
        let headers = [
            ("Accept", "application/vnd.github+json".to_owned()),
            ("Authorization", format!("Bearer {token}")),
            ("Content-Type", "application/json".to_owned()),
            ("X-GitHub-Api-Version", GITHUB_MODELS_API_VERSION.to_owned()),
        ];
        self.trace(
            &format!(
                "post provider=github-copilot task={task} endpoint={}",
                self.endpoint
            ),
            false,
        );
        let data: Value = match post_json(
            &self.endpoint,
            &headers,
            &body.to_string(),
            Duration::from_secs_f64(TIMEOUT_SECONDS),
        )
        .and_then(|text| {
            serde_json::from_str(&text).map_err(|e| HttpError::Transport(e.to_string()))
        }) {
            Ok(data) => data,
            Err(HttpError::Status(code, _, body)) => {
                self.trace(
                    &format!("error provider=github-copilot task={task} http_status={code}"),
                    true,
                );
                return ProviderResponse::error(
                    format!("[github-copilot-error] {}", format_http_error(code, &body)),
                    label,
                    Map::new(),
                );
            }
            Err(error) => {
                let reason = if error.to_string().to_lowercase().contains("timed out") {
                    "TimeoutError"
                } else {
                    "URLError"
                };
                self.trace(
                    &format!("error provider=github-copilot task={task} reason={reason}"),
                    true,
                );
                return ProviderResponse::error(
                    format!("[github-copilot-error] request failed: {error}"),
                    label,
                    Map::new(),
                );
            }
        };
        let choices = data
            .get("choices")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let message = choices
            .first()
            .and_then(|c| c.get("message"))
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        let mut content = message
            .get("content")
            .filter(|v| !v.is_null() && v.as_str() != Some(""))
            .map(py_str)
            .unwrap_or_default()
            .trim()
            .to_owned();
        let reasoning = ["reasoning", "reasoning_content", "thinking"]
            .iter()
            .filter_map(|k| {
                message
                    .get(*k)
                    .filter(|v| !v.is_null() && v.as_str() != Some(""))
            })
            .map(|v| py_str(v).trim().to_owned())
            .filter(|v| !v.is_empty())
            .collect::<Vec<_>>()
            .join("\n");
        let mut confidence = 0.75;
        if content.is_empty() {
            content = "[github-copilot-empty] no content".into();
            confidence = 0.0;
        }
        let mut metadata = Map::new();
        if let Some(usage) = data.get("usage").and_then(Value::as_object) {
            for key in ["prompt_tokens", "completion_tokens", "total_tokens"] {
                if let Some(value) = usage.get(key) {
                    metadata.insert(key.into(), value.clone());
                }
            }
        }
        if let Some(id) = data.get("id") {
            metadata.insert("response_id".into(), id.clone());
        }
        let mut keys: Vec<&String> = metadata.keys().collect();
        keys.sort();
        let metadata_text = keys
            .iter()
            .map(|k| format!("{k}={}", py_str(&metadata[*k])))
            .collect::<Vec<_>>()
            .join(" ");
        let suffix = if metadata_text.is_empty() {
            String::new()
        } else {
            format!(" {metadata_text}")
        };
        self.trace(
            &format!(
                "response provider=github-copilot task={task} choices={} content_chars={} empty={} reasoning_chars={}{suffix}",
                choices.len(),
                py_len(&content),
                if content.starts_with("[github-copilot-empty]") { "True" } else { "False" },
                py_len(&reasoning),
            ),
            false,
        );
        ProviderResponse {
            content,
            confidence,
            model: label,
            metadata,
            reasoning,
        }
    }
}

/// GitHub Models and the CLI agents forward only the conventional-commit tasks as themselves.
pub fn agent_summarize_task(task: &str) -> &str {
    match task {
        "infer-conventional" | "derive-conventional" | "draft-conventional" => task,
        _ => "summarize",
    }
}

impl Provider for GitHubModelsProvider {
    fn class_name(&self) -> &'static str {
        "GitHubCopilotProvider"
    }

    fn model(&self) -> Option<String> {
        Some(self.model.clone())
    }

    fn summarize(&self, request: &ProviderRequest) -> ProviderResponse {
        self.invoke(agent_summarize_task(&request.task), request)
    }

    fn classify(&self, request: &ProviderRequest) -> ProviderResponse {
        self.invoke("classify", request)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn model_ids_and_errors_match_python() {
        assert_eq!(normalize_model_id("gpt-4.1"), "openai/gpt-4.1");
        assert_eq!(normalize_model_id("meta/llama"), "meta/llama");
        assert_eq!(normalize_model_id(" "), DEFAULT_GITHUB_MODELS_MODEL);
        assert_eq!(
            format_http_error(429, r#"{"message": "slow down"}"#),
            "GitHub Models rate limit exceeded (429 Too Many Requests): slow down"
        );
        assert_eq!(
            format_http_error(401, ""),
            "check that the token has access to GitHub Models (401 Unauthorized)"
        );
    }
}
