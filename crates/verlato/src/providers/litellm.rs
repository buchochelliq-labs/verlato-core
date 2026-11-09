//! LiteLLM / OpenAI-compatible chat completions provider (ports `verlato/providers/litellm.py`).

use serde_json::{json, Map, Value};
use std::time::Duration;

use super::http::{post_json, HttpError};
use super::{
    messages_for_request, request_audit, summarize_task, Provider, ProviderRequest,
    ProviderResponse,
};

pub const DEFAULT_LITELLM_ENDPOINT: &str = "http://127.0.0.1:4000/v1/chat/completions";
pub const DEFAULT_LITELLM_MODEL: &str = "ollama/gemma4:latest";
const DEFAULT_LITELLM_TIMEOUT_SECONDS: f64 = 30.0;
pub const DEFAULT_LITELLM_MAX_TOKENS: i64 = 1024;
const MAX_LITELLM_SUGGEST_MAX_TOKENS: i64 = 4096;

pub struct LiteLlmProvider {
    endpoint: String,
    model: String,
    suggest_model: Option<String>,
    api_key_env: String,
    timeout_seconds: f64,
    max_tokens: i64,
}

fn non_empty(value: Option<String>) -> Option<String> {
    value.map(|v| v.trim().to_owned()).filter(|v| !v.is_empty())
}

fn env(name: &str) -> Option<String> {
    non_empty(std::env::var(name).ok())
}

impl LiteLlmProvider {
    pub fn new(
        endpoint: Option<String>,
        model: Option<String>,
        suggest_model: Option<String>,
        api_key_env: Option<String>,
        timeout_seconds: Option<f64>,
        max_tokens: Option<i64>,
    ) -> LiteLlmProvider {
        let max_tokens = match std::env::var("VERLATO_LITELLM_MAX_TOKENS")
            .ok()
            .filter(|v| !v.trim().is_empty())
            .and_then(|v| v.trim().parse::<i64>().ok())
        {
            Some(value) => value.max(0),
            None => max_tokens.map_or(DEFAULT_LITELLM_MAX_TOKENS, |v| v.max(0)),
        };
        LiteLlmProvider {
            endpoint: non_empty(endpoint)
                .or_else(|| env("VERLATO_LITELLM_ENDPOINT"))
                .unwrap_or_else(|| DEFAULT_LITELLM_ENDPOINT.into()),
            model: non_empty(model)
                .or_else(|| env("VERLATO_LITELLM_MODEL"))
                .unwrap_or_else(|| DEFAULT_LITELLM_MODEL.into()),
            suggest_model: non_empty(suggest_model)
                .or_else(|| env("VERLATO_LITELLM_SUGGEST_MODEL")),
            api_key_env: non_empty(api_key_env)
                .or_else(|| env("VERLATO_LITELLM_API_KEY_ENV"))
                .unwrap_or_else(|| "LITELLM_API_KEY".into()),
            timeout_seconds: timeout_seconds
                .filter(|t| *t > 0.0)
                .unwrap_or(DEFAULT_LITELLM_TIMEOUT_SECONDS),
            max_tokens,
        }
    }

    fn model_for_task(&self, task: &str) -> &str {
        match (&self.suggest_model, task) {
            (Some(model), "suggest") => model,
            _ => &self.model,
        }
    }

    /// The output cap for `task`; several suggestion variants get proportionally more.
    pub fn max_tokens_for_task(&self, task: &str, request: &ProviderRequest) -> i64 {
        if self.max_tokens <= 0 || task != "suggest" {
            return self.max_tokens;
        }
        let count = match request.evidence.get("suggestion_count") {
            Some(Value::Number(n)) => n.as_f64().map(|v| v as i64).unwrap_or(1),
            Some(Value::String(s)) => s.trim().parse().unwrap_or(1),
            _ => 1,
        }
        .max(1);
        (self.max_tokens * count).min(MAX_LITELLM_SUGGEST_MAX_TOKENS)
    }

    fn invoke(&self, task: &str, request: &ProviderRequest) -> ProviderResponse {
        let model = self.model_for_task(task).to_owned();
        let label = format!("litellm:{model}");
        let messages = messages_for_request(request, task);
        let audit = request_audit(request, "litellm", &model, &messages);
        let mut body = json!({
            "model": model,
            "temperature": 0.1,
            "messages": messages,
            "metadata": {
                "verlato_request_hash": audit["request_hash"],
                "verlato_prompt_hash": audit["prompt_hash"],
                "verlato_evidence_hash": audit["evidence_hash"],
                "verlato_schema_hash": audit["schema_hash"],
            },
        });
        let max_tokens = self.max_tokens_for_task(task, request);
        if max_tokens > 0 {
            body["max_tokens"] = json!(max_tokens);
        }
        match &request.response_format {
            Value::Null => {}
            Value::String(format) => body["response_format"] = json!(format),
            schema => {
                body["response_format"] = json!({
                    "type": "json_schema",
                    "json_schema": {"name": format!("verlato_{task}_response"), "schema": schema, "strict": true},
                })
            }
        }
        let mut headers = vec![
            ("Accept", "application/json".to_owned()),
            ("Content-Type", "application/json".to_owned()),
        ];
        if let Some(key) = env("VERLATO_LITELLM_API_KEY").or_else(|| env(&self.api_key_env)) {
            headers.push(("Authorization", format!("Bearer {key}")));
        }
        let audit_only = || Map::from_iter([("audit".to_owned(), Value::Object(audit.clone()))]);
        let text = match post_json(
            &self.endpoint,
            &headers,
            &body.to_string(),
            Duration::from_secs_f64(self.timeout_seconds),
        ) {
            Ok(text) => text,
            Err(HttpError::Status(code, reason)) => {
                let mut metadata = audit_only();
                metadata.insert("http_status".into(), json!(code));
                return ProviderResponse::error(
                    format!("[litellm-error] request failed ({code}): {reason}"),
                    label,
                    metadata,
                );
            }
            Err(HttpError::Transport(error)) => {
                return ProviderResponse::error(
                    format!("[litellm-error] request failed: {error}"),
                    label,
                    audit_only(),
                )
            }
        };
        let data: Value = match serde_json::from_str(&text) {
            Ok(data) => data,
            Err(error) => {
                return ProviderResponse::error(
                    format!("[litellm-error] request failed: {error}"),
                    label,
                    audit_only(),
                )
            }
        };
        let message = data
            .get("choices")
            .and_then(Value::as_array)
            .and_then(|c| c.first())
            .and_then(|c| c.get("message"))
            .and_then(Value::as_object);
        let field = |key: &str| {
            message
                .and_then(|m| m.get(key))
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim()
                .to_owned()
        };
        let content = field("content");
        let reasoning = Some(field("reasoning"))
            .filter(|r| !r.is_empty())
            .unwrap_or_else(|| field("reasoning_content"));
        let mut metadata = Map::new();
        if let Some(usage) = data.get("usage").and_then(Value::as_object) {
            for (from, to) in [
                ("prompt_tokens", "prompt_tokens"),
                ("completion_tokens", "eval_tokens"),
                ("total_tokens", "total_tokens"),
            ] {
                if let Some(value) = usage.get(from).filter(|v| v.is_i64() || v.is_u64()) {
                    metadata.insert(to.into(), value.clone());
                }
            }
        }
        metadata.insert("audit".into(), Value::Object(audit.clone()));
        if model != self.model {
            metadata.insert("base_model".into(), json!(self.model));
            metadata.insert("suggest_model".into(), json!(self.suggest_model));
            metadata.insert("effective_model".into(), json!(model));
        }
        if content.is_empty() {
            return ProviderResponse {
                content: "[litellm-empty] no content".into(),
                confidence: 0.0,
                model: label,
                metadata,
                reasoning,
            };
        }
        ProviderResponse {
            content,
            confidence: 0.75,
            model: label,
            metadata,
            reasoning,
        }
    }
}

impl Provider for LiteLlmProvider {
    fn summarize(&self, request: &ProviderRequest) -> ProviderResponse {
        self.invoke(summarize_task(&request.task), request)
    }

    fn classify(&self, request: &ProviderRequest) -> ProviderResponse {
        self.invoke("classify", request)
    }
}
