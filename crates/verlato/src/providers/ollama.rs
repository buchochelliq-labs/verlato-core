//! Local Ollama provider (ports `verlato/providers/ollama.py`, without tool calling, which
//! only the Python suggestion strategies use).

use serde_json::{json, Map, Value};
use std::time::Duration;

use super::http::{post_json, post_lines, HttpError};
use super::model_log;
use super::plugin::format_g;
use super::{default_system_prompt, summarize_task, Provider, ProviderRequest, ProviderResponse};
use crate::pyjson::{self, py_len};

pub const DEFAULT_OLLAMA_ENDPOINT: &str = "http://127.0.0.1:11434/api/chat";
pub const DEFAULT_OLLAMA_MODEL: &str = "gemma4:latest";
const DEFAULT_OLLAMA_TIMEOUT_SECONDS: f64 = 15.0;
const DEFAULT_SUMMARY_NUM_PREDICT: i64 = 180;
const DEFAULT_CLASSIFY_NUM_PREDICT: i64 = 24;
const MIN_SUGGEST_NUM_PREDICT: i64 = 96;
const MAX_SUGGEST_NUM_PREDICT: i64 = 180;
const STRUCTURED_NO_THINK_TASKS: &[&str] = &[
    "suggest",
    "infer-conventional",
    "derive-conventional",
    "draft-conventional",
];
const METADATA_KEYS: &[&str] = &[
    "total_duration",
    "load_duration",
    "prompt_eval_count",
    "prompt_eval_duration",
    "eval_count",
    "eval_duration",
    "done_reason",
];

#[derive(Debug, Clone, Default)]
pub struct OllamaSettings {
    pub endpoint: Option<String>,
    pub model: Option<String>,
    pub suggest_model: Option<String>,
    pub timeout_seconds: Option<f64>,
    pub stream: bool,
    pub keep_alive: Option<String>,
    pub num_ctx: Option<i64>,
    pub num_predict: Option<i64>,
    pub summary_num_predict: Option<i64>,
    pub classify_num_predict: Option<i64>,
    pub suggest_num_predict: Option<i64>,
    pub suggest_think: Option<bool>,
}

pub struct OllamaProvider {
    endpoint: String,
    model: String,
    suggest_model: Option<String>,
    timeout_seconds: f64,
    stream: bool,
    keep_alive: Option<String>,
    num_ctx: Option<i64>,
    num_predict: Option<i64>,
    summary_num_predict: Option<i64>,
    classify_num_predict: Option<i64>,
    suggest_num_predict: Option<i64>,
    suggest_think: Option<bool>,
}

fn env(name: &str) -> Option<String> {
    std::env::var(name).ok()
}

fn env_int(name: &str, configured: Option<i64>) -> Option<i64> {
    match env(name) {
        Some(raw) if !raw.trim().is_empty() => raw.trim().parse().ok().or(configured),
        _ => configured,
    }
}

fn env_bool(name: &str, configured: Option<bool>) -> Option<bool> {
    match env(name) {
        Some(raw) if !raw.trim().is_empty() => Some(matches!(
            raw.trim().to_lowercase().as_str(),
            "1" | "true" | "yes" | "on"
        )),
        _ => configured,
    }
}

fn non_empty(value: Option<String>) -> Option<String> {
    value.map(|v| v.trim().to_owned()).filter(|v| !v.is_empty())
}

/// Python `str(value)` for the response metadata the provider traces.
fn py_str(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::String(s) => s.clone(),
        Value::Number(n) => match n.as_f64() {
            Some(f) if !n.is_i64() && !n.is_u64() => pyjson::float_repr(f),
            _ => n.to_string(),
        },
        other => other.to_string(),
    }
}

fn py_bool(value: bool) -> &'static str {
    if value {
        "True"
    } else {
        "False"
    }
}

fn event_suffix(message: &str) -> &'static str {
    for (prefix, suffix) in [
        ("stream-chunk", "stream_chunk"),
        ("stream-complete", "stream_complete"),
        ("response ", "response"),
        ("post ", "request"),
        ("error ", "error"),
        ("fallback ", "fallback"),
    ] {
        if message.starts_with(prefix) {
            return suffix;
        }
    }
    "log"
}

fn reasoning_of(data: &Value) -> String {
    let mut candidates: Vec<String> = Vec::new();
    let keys = ["thinking", "reasoning", "reasoning_content"];
    if let Some(message) = data.get("message").and_then(Value::as_object) {
        for key in keys {
            if let Some(value) = message.get(key).filter(|v| truthy(v)) {
                candidates.push(py_str(value));
            }
        }
    }
    for key in keys {
        if let Some(value) = data.get(key).filter(|v| truthy(v)) {
            candidates.push(py_str(value));
        }
    }
    candidates
        .iter()
        .map(|c| c.trim())
        .filter(|c| !c.is_empty())
        .collect::<Vec<_>>()
        .join("\n")
}

fn message_reasoning(message: &Map<String, Value>) -> String {
    ["thinking", "reasoning", "reasoning_content"]
        .iter()
        .filter_map(|key| message.get(*key).filter(|v| truthy(v)).map(py_str))
        .collect()
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

impl OllamaProvider {
    pub fn new(settings: OllamaSettings) -> OllamaProvider {
        let keep_alive = match env("VERLATO_OLLAMA_KEEP_ALIVE") {
            Some(raw) => non_empty(Some(raw)),
            None => non_empty(settings.keep_alive),
        };
        let timeout_seconds = match env("VERLATO_OLLAMA_TIMEOUT_SECONDS") {
            Some(raw) if !raw.trim().is_empty() => raw.trim().parse().ok(),
            _ => None,
        }
        .or(settings.timeout_seconds)
        .filter(|t: &f64| *t > 0.0)
        .unwrap_or(DEFAULT_OLLAMA_TIMEOUT_SECONDS);
        OllamaProvider {
            endpoint: non_empty(settings.endpoint)
                .or_else(|| non_empty(env("VERLATO_OLLAMA_ENDPOINT")))
                .unwrap_or_else(|| DEFAULT_OLLAMA_ENDPOINT.into()),
            model: non_empty(settings.model)
                .or_else(|| non_empty(env("VERLATO_OLLAMA_MODEL")))
                .unwrap_or_else(|| DEFAULT_OLLAMA_MODEL.into()),
            suggest_model: non_empty(settings.suggest_model),
            timeout_seconds,
            stream: settings.stream,
            keep_alive,
            num_ctx: env_int("VERLATO_OLLAMA_NUM_CTX", settings.num_ctx),
            num_predict: env_int("VERLATO_OLLAMA_NUM_PREDICT", settings.num_predict),
            summary_num_predict: env_int(
                "VERLATO_OLLAMA_SUMMARY_NUM_PREDICT",
                settings
                    .summary_num_predict
                    .or(Some(DEFAULT_SUMMARY_NUM_PREDICT)),
            ),
            classify_num_predict: env_int(
                "VERLATO_OLLAMA_CLASSIFY_NUM_PREDICT",
                settings
                    .classify_num_predict
                    .or(Some(DEFAULT_CLASSIFY_NUM_PREDICT)),
            ),
            suggest_num_predict: env_int(
                "VERLATO_OLLAMA_SUGGEST_NUM_PREDICT",
                settings.suggest_num_predict,
            ),
            suggest_think: env_bool("VERLATO_OLLAMA_SUGGEST_THINK", settings.suggest_think),
        }
    }

    fn task_model(&self, task: &str) -> &str {
        match (&self.suggest_model, task) {
            (Some(model), "suggest") => model,
            _ => &self.model,
        }
    }

    fn trace(&self, message: &str, error: bool) {
        model_log::emit(
            message,
            &format!("verlato.model.ollama.{}", event_suffix(message)),
            Map::from_iter([
                ("provider".to_owned(), json!("ollama")),
                ("model".to_owned(), json!(self.model)),
            ]),
            error,
        );
    }

    fn trace_chunk(&self, task: &str, kind: &str, chunk: &str) {
        let show_reasoning = matches!(
            env("VERLATO_TRACE_MODEL_REASONING")
                .unwrap_or_default()
                .trim()
                .to_lowercase()
                .as_str(),
            "1" | "true" | "yes" | "on"
        );
        let (payload, redacted) = if kind == "reasoning" && !show_reasoning {
            (String::new(), false)
        } else if model_log::redaction_enabled() {
            model_log::redact(chunk)
        } else {
            (chunk.to_owned(), false)
        };
        let record = json!({
            "event": "stream_chunk",
            "message": format!("stream {kind} chunk"),
            "attributes": {
                "provider": "ollama",
                "model": self.model,
                "task": task,
                "kind": kind,
                "chars": py_len(chunk),
                "chunks": 1,
                "redacted": redacted,
            },
            "payload": payload,
        });
        model_log::emit(
            &format!("json {}", pyjson::dumps(&record, pyjson::DEFAULT_SORTED)),
            "verlato.model.ollama.stream_chunk",
            Map::from_iter([
                ("provider".to_owned(), json!("ollama")),
                ("model".to_owned(), json!(self.model)),
            ]),
            false,
        );
    }

    fn options_for_task(&self, task: &str, request: &ProviderRequest) -> Value {
        let mut options = json!({"temperature": 0.1});
        if let Some(num_ctx) = self.num_ctx {
            options["num_ctx"] = json!(num_ctx);
        }
        let num_predict = self.num_predict.or(match task {
            "classify" => self.classify_num_predict,
            "suggest" => self.suggest_num_predict.or_else(|| {
                let count = match request.evidence.get("suggestion_count") {
                    Some(Value::Number(n)) => n.as_f64().map(|v| v as i64).unwrap_or(1),
                    Some(Value::String(s)) => s.trim().parse().unwrap_or(1),
                    _ => 1,
                };
                Some((60 * count).clamp(MIN_SUGGEST_NUM_PREDICT, MAX_SUGGEST_NUM_PREDICT))
            }),
            _ => self.summary_num_predict,
        });
        if let Some(num_predict) = num_predict {
            options["num_predict"] = json!(num_predict);
        }
        options
    }

    fn generate_endpoint(&self) -> String {
        let trimmed = self.endpoint.trim_end_matches('/');
        match trimmed.strip_suffix("/api/chat") {
            Some(base) => format!("{base}/api/generate"),
            None => self.endpoint.clone(),
        }
    }

    fn post(&self, endpoint: &str, body: &Value, task: &str) -> Result<Value, HttpError> {
        let headers = [("Content-Type", "application/json".to_owned())];
        let timeout = Duration::from_secs_f64(self.timeout_seconds);
        if !self.stream {
            let text = post_json(endpoint, &headers, &body.to_string(), timeout)?;
            return match serde_json::from_str::<Value>(&text) {
                Ok(data @ Value::Object(_)) => Ok(data),
                Ok(_) => Err(HttpError::Transport("expected JSON object".into())),
                Err(error) => Err(HttpError::Transport(error.to_string())),
            };
        }
        let mut content_parts: Vec<String> = Vec::new();
        let mut reasoning_parts: Vec<String> = Vec::new();
        let mut final_data = Map::new();
        let mut chunks = 0usize;
        post_lines(endpoint, &headers, &body.to_string(), timeout, |line| {
            let chunk: Value = serde_json::from_str(line).map_err(|e| e.to_string())?;
            let Value::Object(chunk) = chunk else {
                return Err("expected JSON object".into());
            };
            chunks += 1;
            let (content, reasoning) = match chunk.get("message").and_then(Value::as_object) {
                Some(message) => (
                    message
                        .get("content")
                        .map(py_str)
                        .filter(|_| message.get("content").is_some_and(truthy)),
                    message_reasoning(message),
                ),
                None => (
                    chunk.get("response").filter(|v| truthy(v)).map(py_str),
                    reasoning_of(&Value::Object(chunk.clone())),
                ),
            };
            final_data.extend(chunk);
            if let Some(content) = content.filter(|c| !c.is_empty()) {
                self.trace_chunk(task, "content", &content);
                content_parts.push(content);
            }
            if !reasoning.is_empty() {
                self.trace_chunk(task, "reasoning", &reasoning);
                reasoning_parts.push(reasoning);
            }
            Ok(())
        })?;
        let has_message = final_data
            .get("message")
            .is_some_and(|m| m.is_object() && truthy(m));
        if has_message {
            let mut message = final_data["message"]
                .as_object()
                .cloned()
                .unwrap_or_default();
            message.insert("content".into(), json!(content_parts.concat()));
            if !reasoning_parts.is_empty() {
                message.insert("reasoning".into(), json!(reasoning_parts.concat()));
            }
            final_data.insert("message".into(), Value::Object(message));
        } else {
            final_data.insert("response".into(), json!(content_parts.concat()));
            if !reasoning_parts.is_empty() {
                final_data.insert("reasoning".into(), json!(reasoning_parts.concat()));
            }
        }
        self.trace(
            &format!(
                "stream-complete provider=ollama task={task} chunks={chunks} content_chars={} reasoning_chars={}",
                content_parts.iter().map(|p| py_len(p)).sum::<usize>(),
                reasoning_parts.iter().map(|p| py_len(p)).sum::<usize>(),
            ),
            false,
        );
        Ok(Value::Object(final_data))
    }

    fn unavailable(&self, error: &HttpError, model: &str) -> ProviderResponse {
        ProviderResponse::error(
            format!(
                "[ollama-unavailable] unable to reach local Ollama at {}: {error}",
                self.endpoint
            ),
            format!("ollama:{model}"),
            Map::new(),
        )
    }

    fn error_reason(error: &HttpError) -> &'static str {
        match error {
            HttpError::Status(..) => "HTTPError",
            HttpError::Disconnected(_) => "RemoteDisconnected",
            HttpError::Transport(message) if message.to_lowercase().contains("timed out") => {
                "TimeoutError"
            }
            HttpError::Transport(_) => "URLError",
        }
    }

    fn invoke(&self, task: &str, request: &ProviderRequest) -> ProviderResponse {
        let model = self.task_model(task).to_owned();
        let system_prompt = default_system_prompt(task);
        let model_detail = if model != self.model {
            format!("model={model} base_model={}", self.model)
        } else {
            format!("model={model}")
        };
        self.trace(
            &format!(
                "provider=ollama task={task} {model_detail} endpoint={} prompt_chars={} evidence_keys={} \
                 timeout_seconds={} stream={} keep_alive={}",
                self.endpoint,
                py_len(&request.prompt),
                request.evidence.as_object().map_or(0, Map::len),
                format_g(self.timeout_seconds),
                py_bool(self.stream),
                self.keep_alive.as_deref().unwrap_or("default"),
            ),
            false,
        );
        let options = self.options_for_task(task, request);
        let mut body = json!({
            "model": model,
            "stream": self.stream,
            "options": options,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": request.prompt},
            ],
        });
        let extend = |body: &mut Value| {
            if !request.response_format.is_null() {
                body["format"] = request.response_format.clone();
            }
            if let (true, Some(think)) = (
                STRUCTURED_NO_THINK_TASKS.contains(&task),
                self.suggest_think,
            ) {
                body["think"] = json!(think);
            }
            if let Some(keep_alive) = &self.keep_alive {
                body["keep_alive"] = json!(keep_alive);
            }
        };
        extend(&mut body);
        self.trace(
            &format!(
                "post provider=ollama task={task} endpoint={} mode=chat",
                self.endpoint
            ),
            false,
        );
        let data = match self.post(&self.endpoint, &body, task) {
            Ok(data) => data,
            Err(HttpError::Disconnected(_)) => {
                self.trace(
                    &format!("fallback provider=ollama task={task} from=chat to=generate reason=remote-disconnected"),
                    false,
                );
                let mut generate = json!({
                    "model": model,
                    "stream": self.stream,
                    "options": self.options_for_task(task, request),
                    "system": system_prompt,
                    "prompt": request.prompt,
                });
                extend(&mut generate);
                let endpoint = self.generate_endpoint();
                self.trace(
                    &format!(
                        "post provider=ollama task=generate endpoint={endpoint} mode=generate"
                    ),
                    false,
                );
                match self.post(&endpoint, &generate, "generate") {
                    Ok(data) => data,
                    Err(error) => {
                        self.trace(
                            &format!(
                                "error provider=ollama task={task} stage=generate reason={}",
                                Self::error_reason(&error)
                            ),
                            true,
                        );
                        return self.unavailable(&error, &model);
                    }
                }
            }
            Err(HttpError::Status(code, reason, _)) => {
                self.trace(
                    &format!("error provider=ollama task={task} stage=chat http_status={code}"),
                    true,
                );
                return ProviderResponse::error(
                    format!("[ollama-error] local model request failed ({code}): {reason}"),
                    format!("ollama:{model}"),
                    Map::new(),
                );
            }
            Err(error) => {
                self.trace(
                    &format!(
                        "error provider=ollama task={task} stage=chat reason={}",
                        Self::error_reason(&error)
                    ),
                    true,
                );
                return self.unavailable(&error, &model);
            }
        };
        let message_content = data
            .get("message")
            .and_then(Value::as_object)
            .and_then(|m| m.get("content"))
            .filter(|v| truthy(v));
        let content = message_content
            .or_else(|| data.get("response").filter(|v| truthy(v)))
            .map(py_str)
            .unwrap_or_default()
            .trim()
            .to_owned();
        let reasoning = reasoning_of(&data);
        let mut metadata = Map::new();
        for key in METADATA_KEYS {
            if let Some(value) = data.get(*key) {
                metadata.insert((*key).into(), value.clone());
            }
        }
        if model != self.model {
            metadata.insert("base_model".into(), json!(self.model));
            metadata.insert("suggest_model".into(), json!(self.suggest_model));
            metadata.insert("effective_model".into(), json!(model));
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
                "response provider=ollama task={task} content_chars={} empty={} reasoning_chars={}{suffix}",
                py_len(&content),
                py_bool(content.is_empty()),
                py_len(&reasoning),
            ),
            false,
        );
        let model_label = format!("ollama:{model}");
        if content.is_empty() {
            return ProviderResponse {
                content: "[ollama-empty] no content".into(),
                confidence: 0.0,
                model: model_label,
                metadata,
                reasoning,
            };
        }
        ProviderResponse {
            content,
            confidence: 0.75,
            model: model_label,
            metadata,
            reasoning,
        }
    }
}

impl Provider for OllamaProvider {
    fn class_name(&self) -> &'static str {
        "OllamaProvider"
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
    fn options_follow_python_defaults() {
        let provider = OllamaProvider::new(OllamaSettings::default());
        let request = ProviderRequest::default();
        assert_eq!(
            provider.options_for_task("summarize", &request),
            json!({"temperature": 0.1, "num_predict": 180})
        );
        assert_eq!(
            provider.options_for_task("classify", &request),
            json!({"temperature": 0.1, "num_predict": 24})
        );
        let three = ProviderRequest {
            evidence: json!({"suggestion_count": 3}),
            ..Default::default()
        };
        assert_eq!(
            provider.options_for_task("suggest", &three)["num_predict"],
            json!(180)
        );
        assert_eq!(
            provider.options_for_task("suggest", &request)["num_predict"],
            json!(96)
        );
    }

    #[test]
    fn generate_endpoint_replaces_chat() {
        let provider = OllamaProvider::new(OllamaSettings {
            endpoint: Some("http://host:11434/api/chat/".into()),
            ..Default::default()
        });
        assert_eq!(
            provider.generate_endpoint(),
            "http://host:11434/api/generate"
        );
    }
}
