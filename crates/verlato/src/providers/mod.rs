//! Model providers (ports `verlato/providers/`): stub, mock, Ollama, LiteLLM/OpenAI-compatible,
//! GitHub Models, the external-command plugin, the Codex CLI and Claude Code agents, and
//! fallback chains of those.

pub mod cli_agents;
pub mod github;
pub mod http;
pub mod litellm;
pub mod mock;
pub mod model_log;
pub mod ollama;
pub mod plugin;
mod process;

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use crate::pyjson;

pub use litellm::LiteLlmProvider;
pub use ollama::{OllamaProvider, OllamaSettings};
pub use plugin::PluginProvider;

#[derive(Debug, Clone, Default)]
pub struct ProviderRequest {
    pub task: String,
    pub prompt: String,
    pub evidence: Value,
    pub response_format: Value,
    pub metadata: Map<String, Value>,
}

#[derive(Debug, Clone)]
pub struct ProviderResponse {
    pub content: String,
    pub confidence: f64,
    pub model: String,
    pub metadata: Map<String, Value>,
    pub reasoning: String,
}

impl ProviderResponse {
    pub fn error(content: String, model: String, metadata: Map<String, Value>) -> Self {
        ProviderResponse {
            content,
            confidence: 0.0,
            model,
            metadata,
            reasoning: String::new(),
        }
    }
}

pub trait Provider: Send + Sync {
    fn summarize(&self, request: &ProviderRequest) -> ProviderResponse;
    fn classify(&self, request: &ProviderRequest) -> ProviderResponse;

    /// The provider's `model` attribute, when it has one (Python `getattr(provider, "model")`).
    fn model(&self) -> Option<String> {
        None
    }

    /// The provider's `suggest_model` attribute, when it has one.
    fn suggest_model(&self) -> Option<String> {
        None
    }

    /// `provider.model_for_task(task)`, for providers that route tasks to models.
    fn model_for_task(&self, _task: &str) -> Option<String> {
        None
    }

    /// The Python class name, used where Python falls back to `provider.__class__.__name__`.
    fn class_name(&self) -> &'static str;
}

/// `_provider_effective_model`: the model a task would use.
pub fn effective_model(provider: &dyn Provider, task: &str) -> String {
    provider
        .model_for_task(task)
        .filter(|m| !m.is_empty())
        .or_else(|| provider.model().map(|m| m.trim().to_owned()))
        .unwrap_or_default()
}

/// Error markers Verlato's providers put at the start of `content` when a call failed.
pub const PROVIDER_ERROR_MARKERS: &[&str] = &[
    "[github-copilot-",
    "[ollama-",
    "[codex-cli-",
    "[claude-code-",
    "[litellm-",
    "[plugin-",
    "[fallback-",
];

pub fn is_provider_error_marker(content: &str, include_stub: bool) -> bool {
    let lowered = content.trim().to_lowercase();
    PROVIDER_ERROR_MARKERS
        .iter()
        .any(|marker| lowered.starts_with(marker))
        || (include_stub && lowered.starts_with("[stub-"))
}

fn sha256_hex(text: &str) -> String {
    hex::encode(Sha256::digest(text.as_bytes()))
}

/// `sha256_hex(canonical_json(value))` from `verlato/providers/base.py`.
pub fn canonical_hash(value: &Value) -> String {
    sha256_hex(&pyjson::dumps(value, pyjson::CANONICAL))
}

pub fn default_system_prompt(task: &str) -> &'static str {
    match task {
        "summarize" | "suggest" => {
            "You are Verlato. Write a useful release note with enough context for downstream consumers. \
             Use paragraphs or bullet points when they make the change clearer, but stay focused and avoid padding."
        }
        "infer-conventional" | "derive-conventional" | "draft-conventional" => {
            "You are Verlato. Infer or draft a conventional commit from the supplied evidence. \
             Return only strict JSON matching the requested schema."
        }
        _ => "You are Verlato. Return one line: kind/area/audience/semver.",
    }
}

pub fn messages_for_request(request: &ProviderRequest, task: &str) -> Value {
    json!([
        {"role": "system", "content": default_system_prompt(task)},
        {"role": "user", "content": request.prompt},
    ])
}

/// Ports `provider_request_audit`: request, prompt, evidence and schema hashes.
pub fn request_audit(
    request: &ProviderRequest,
    provider: &str,
    model: &str,
    messages: &Value,
) -> Map<String, Value> {
    let prompt_hash = canonical_hash(&json!({"prompt": request.prompt, "messages": messages}));
    let evidence_hash = canonical_hash(&request.evidence);
    let schema_hash = canonical_hash(&request.response_format);
    let request_hash = canonical_hash(&json!({
        "schema": "verlato.model_request.v1",
        "provider": provider,
        "model": model,
        "task": request.task,
        "prompt_hash": prompt_hash,
        "evidence_hash": evidence_hash,
        "schema_hash": schema_hash,
    }));
    let mut audit = Map::new();
    audit.insert("request_hash".into(), json!(request_hash));
    audit.insert("prompt_hash".into(), json!(prompt_hash));
    audit.insert("evidence_hash".into(), json!(evidence_hash));
    audit.insert("schema_hash".into(), json!(schema_hash));
    audit
}

/// Tasks a provider's `summarize` forwards as themselves; everything else is "summarize".
pub fn summarize_task(task: &str) -> &str {
    match task {
        "suggest" | "infer-conventional" | "derive-conventional" | "draft-conventional" => task,
        _ => "summarize",
    }
}

pub struct StubProvider;

impl Provider for StubProvider {
    fn class_name(&self) -> &'static str {
        "StubProvider"
    }

    fn summarize(&self, _request: &ProviderRequest) -> ProviderResponse {
        ProviderResponse {
            content: String::new(),
            confidence: 0.4,
            model: "stub".into(),
            metadata: Map::new(),
            reasoning: String::new(),
        }
    }

    fn classify(&self, _request: &ProviderRequest) -> ProviderResponse {
        ProviderResponse {
            content: "[stub-classification] changed/general/external/patch".into(),
            confidence: 0.4,
            model: "stub".into(),
            metadata: Map::new(),
            reasoning: String::new(),
        }
    }
}

/// Explicit provider chain (ports `FallbackProvider`).
pub struct FallbackProvider {
    providers: Vec<Box<dyn Provider>>,
}

impl FallbackProvider {
    fn invoke(&self, call: impl Fn(&dyn Provider) -> ProviderResponse) -> ProviderResponse {
        let mut attempts = Vec::new();
        let mut last: Option<ProviderResponse> = None;
        for (index, provider) in self.providers.iter().enumerate() {
            let response = call(provider.as_ref());
            let failed = is_fallback_failure(&response.content);
            attempts.push(json!({"index": index + 1, "model": response.model, "failed": failed}));
            if !response.content.trim().is_empty() && !failed {
                let mut response = response;
                response
                    .metadata
                    .insert("fallback_attempts".into(), json!(attempts));
                response
                    .metadata
                    .insert("fallback_selected_index".into(), json!(index + 1));
                return response;
            }
            last = Some(response);
        }
        match last {
            Some(mut response) => {
                response
                    .metadata
                    .insert("fallback_attempts".into(), json!(attempts));
                response
            }
            None => ProviderResponse::error(
                "[fallback-unavailable] no providers configured".into(),
                "fallback-chain".into(),
                Map::from_iter([("fallback_attempts".to_owned(), json!(attempts))]),
            ),
        }
    }
}

fn is_fallback_failure(content: &str) -> bool {
    is_provider_error_marker(content, true)
}

impl Provider for FallbackProvider {
    fn class_name(&self) -> &'static str {
        "FallbackProvider"
    }

    fn model(&self) -> Option<String> {
        Some("fallback-chain".into())
    }

    fn summarize(&self, request: &ProviderRequest) -> ProviderResponse {
        self.invoke(|p| p.summarize(request))
    }

    fn classify(&self, request: &ProviderRequest) -> ProviderResponse {
        self.invoke(|p| p.classify(request))
    }
}

// ---------------------------------------------------------------------------
// configuration (ports `ProviderConfig` parsing and `providers/factory.py`)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Default)]
pub struct ProviderConfig {
    pub name: String,
    pub model: Option<String>,
    pub suggest_model: Option<String>,
    pub endpoint: Option<String>,
    pub api_key_env: Option<String>,
    pub timeout_seconds: Option<f64>,
    pub keep_alive: Option<String>,
    pub num_ctx: Option<i64>,
    pub num_predict: Option<i64>,
    pub summary_num_predict: Option<i64>,
    pub classify_num_predict: Option<i64>,
    pub suggest_num_predict: Option<i64>,
    pub suggest_think: Option<bool>,
    pub plugin_manifest: Map<String, Value>,
    pub plugin_command: Vec<String>,
    pub plugin_config: Map<String, Value>,
    pub plugin_env: Vec<String>,
    pub fallbacks: Vec<Map<String, Value>>,
}

fn optional_text(value: Option<&Value>) -> Option<String> {
    match value {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) => Some(s.trim().to_owned()).filter(|s| !s.is_empty()),
        Some(Value::Bool(false)) => None,
        Some(other) => Some(other.to_string().trim().to_owned()).filter(|s| !s.is_empty()),
    }
}

/// Python `shlex.split` (POSIX mode) for configured command lines.
pub fn command_list(value: Option<&Value>) -> Vec<String> {
    match value {
        Some(Value::Array(items)) => items
            .iter()
            .map(|item| match item {
                Value::String(s) => s.clone(),
                other => other.to_string(),
            })
            .filter(|part| !part.trim().is_empty())
            .collect(),
        Some(Value::String(text)) => shlex::split(text).unwrap_or_default(),
        _ => Vec::new(),
    }
}

impl ProviderConfig {
    /// `verlato.json`'s `provider` block (defaults as in `verlato/config.py`).
    pub fn from_raw(raw: &Value) -> ProviderConfig {
        let empty = Map::new();
        let provider = raw
            .get("provider")
            .and_then(Value::as_object)
            .unwrap_or(&empty);
        let mut config = ProviderConfig {
            name: "ollama".into(),
            model: Some("gemma4:latest".into()),
            keep_alive: Some("10m".into()),
            suggest_think: Some(false),
            ..Default::default()
        };
        if provider.is_empty() {
            return config;
        }
        config.name = optional_text(provider.get("name")).unwrap_or_else(|| config.name.clone());
        config.model = optional_text(provider.get("model"));
        config.suggest_model = optional_text(provider.get("suggest_model"));
        config.endpoint = optional_text(provider.get("endpoint"));
        config.api_key_env = optional_text(provider.get("api_key_env"));
        config.timeout_seconds = provider.get("timeout_seconds").and_then(Value::as_f64);
        if let Some(keep_alive) = provider.get("keep_alive") {
            config.keep_alive = match keep_alive {
                Value::Null => None,
                Value::String(s) => Some(s.trim().to_owned()),
                other => Some(other.to_string()),
            };
        }
        let int = |key: &str| provider.get(key).and_then(Value::as_i64);
        config.num_ctx = int("num_ctx");
        config.num_predict = int("num_predict");
        config.summary_num_predict = int("summary_num_predict");
        config.classify_num_predict = int("classify_num_predict");
        config.suggest_num_predict = int("suggest_num_predict");
        if let Some(think) = provider.get("suggest_think") {
            config.suggest_think = match think {
                Value::Null => None,
                Value::Bool(b) => Some(*b),
                other => Some(truthy(other)),
            };
        }
        config.plugin_manifest = provider
            .get("plugin_manifest")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        config.plugin_command = command_list(
            provider
                .get("plugin_command")
                .filter(|v| !v.is_null())
                .or_else(|| provider.get("command")),
        );
        config.plugin_config = provider
            .get("plugin_config")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        config.plugin_env = provider
            .get("plugin_env")
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .filter_map(|v| optional_text(Some(v)))
                    .collect()
            })
            .unwrap_or_default();
        config.fallbacks = provider
            .get("fallbacks")
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .filter_map(|v| v.as_object().cloned())
                    .collect()
            })
            .unwrap_or_default();
        config
    }

    /// A fallback entry layered over the primary config (ports `_provider_config_from_mapping`).
    fn fallback(&self, raw: &Map<String, Value>) -> ProviderConfig {
        let text_or = |key: &str, default: &Option<String>| {
            if raw.contains_key(key) {
                optional_text(raw.get(key))
            } else {
                default.clone()
            }
        };
        ProviderConfig {
            name: optional_text(raw.get("name")).unwrap_or_else(|| self.name.clone()),
            model: text_or("model", &self.model),
            suggest_model: text_or("suggest_model", &self.suggest_model),
            endpoint: text_or("endpoint", &self.endpoint),
            api_key_env: text_or("api_key_env", &self.api_key_env),
            timeout_seconds: raw
                .get("timeout_seconds")
                .and_then(Value::as_f64)
                .or(self.timeout_seconds),
            keep_alive: self.keep_alive.clone(),
            num_ctx: self.num_ctx,
            num_predict: self.num_predict,
            summary_num_predict: self.summary_num_predict,
            classify_num_predict: self.classify_num_predict,
            suggest_num_predict: self.suggest_num_predict,
            suggest_think: self.suggest_think,
            plugin_manifest: raw
                .get("plugin_manifest")
                .and_then(Value::as_object)
                .filter(|m| !m.is_empty())
                .cloned()
                .unwrap_or_else(|| self.plugin_manifest.clone()),
            plugin_command: if raw.contains_key("plugin_command") || raw.contains_key("command") {
                command_list(raw.get("plugin_command").or_else(|| raw.get("command")))
            } else {
                self.plugin_command.clone()
            },
            plugin_config: raw
                .get("plugin_config")
                .and_then(Value::as_object)
                .filter(|m| !m.is_empty())
                .cloned()
                .unwrap_or_else(|| self.plugin_config.clone()),
            plugin_env: raw
                .get("plugin_env")
                .and_then(Value::as_array)
                .filter(|items| !items.is_empty())
                .map(|items| {
                    items
                        .iter()
                        .filter_map(|v| optional_text(Some(v)))
                        .collect()
                })
                .unwrap_or_else(|| self.plugin_env.clone()),
            fallbacks: Vec::new(),
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

fn env_text(name: &str) -> Option<String> {
    std::env::var(name).ok()
}

fn env_float(name: &str, fallback: Option<f64>) -> Option<f64> {
    match env_text(name) {
        Some(raw) if !raw.trim().is_empty() => raw.trim().parse().ok().or(fallback),
        _ => fallback,
    }
}

fn normalize_optional(value: Option<String>) -> Option<String> {
    value.map(|v| v.trim().to_owned()).filter(|v| !v.is_empty())
}

/// The provider mode a config entry selects (CLI/env override first, as in `_build_single_provider`).
fn selected_mode(config: &ProviderConfig, mode: Option<&str>) -> String {
    mode.map(str::to_owned)
        .filter(|m| !m.is_empty())
        .or_else(|| env_text("VERLATO_MODEL_PROVIDER").filter(|m| !m.is_empty()))
        .unwrap_or_else(|| config.name.clone())
        .to_lowercase()
}

pub fn build_provider(config: &ProviderConfig, mode: Option<&str>) -> Box<dyn Provider> {
    build_provider_with(config, mode, None)
}

/// `build_provider(config, mode=..., suggest_model=...)`: an explicit `suggest_model` (even an
/// empty one) overrides the configured/env suggest model of Ollama, LiteLLM and plugin providers.
pub fn build_provider_with(
    config: &ProviderConfig,
    mode: Option<&str>,
    suggest_model: Option<&str>,
) -> Box<dyn Provider> {
    let primary = build_single(config, mode, suggest_model);
    if config.fallbacks.is_empty() {
        return primary;
    }
    let mut providers = vec![primary];
    for raw in &config.fallbacks {
        providers.push(build_single(&config.fallback(raw), None, suggest_model));
    }
    Box::new(FallbackProvider { providers })
}

fn build_single(
    config: &ProviderConfig,
    mode: Option<&str>,
    suggest_override: Option<&str>,
) -> Box<dyn Provider> {
    let override_or = |env: Option<String>, configured: &Option<String>| match suggest_override {
        Some(explicit) => normalize_optional(Some(explicit.to_owned())),
        None => match env {
            Some(raw) => normalize_optional(Some(raw)),
            None => normalize_optional(configured.clone()),
        },
    };
    let mode = selected_mode(config, mode);
    let named = |names: &[&str]| names.contains(&config.name.to_lowercase().as_str());
    match mode.as_str() {
        "plugin" | "external-plugin" | "model-plugin" => {
            let command = match env_text("VERLATO_PLUGIN_COMMAND") {
                Some(raw) if !raw.trim().is_empty() => shlex::split(&raw).unwrap_or_default(),
                _ => config.plugin_command.clone(),
            };
            let env_names = match env_text("VERLATO_PLUGIN_ENV") {
                Some(raw) => raw
                    .split(',')
                    .map(|p| p.trim().to_owned())
                    .filter(|p| !p.is_empty())
                    .collect(),
                None => config.plugin_env.clone(),
            };
            let model = env_text("VERLATO_PLUGIN_MODEL")
                .filter(|m| !m.is_empty())
                .or_else(|| {
                    named(&["plugin", "external-plugin", "model-plugin"])
                        .then(|| config.model.clone())
                        .flatten()
                });
            let suggest_model = override_or(
                env_text("VERLATO_PLUGIN_SUGGEST_MODEL"),
                &config.suggest_model,
            );
            Box::new(PluginProvider::new(
                &config.plugin_manifest,
                command,
                config.plugin_config.clone(),
                env_names,
                model,
                suggest_model,
                env_float("VERLATO_PLUGIN_TIMEOUT_SECONDS", config.timeout_seconds),
            ))
        }
        "litellm" | "lite-llm" => {
            let model = env_text("VERLATO_LITELLM_MODEL")
                .filter(|m| !m.is_empty())
                .or_else(|| {
                    named(&["litellm", "lite-llm"])
                        .then(|| config.model.clone())
                        .flatten()
                });
            let suggest_model = override_or(
                env_text("VERLATO_LITELLM_SUGGEST_MODEL"),
                &config.suggest_model,
            );
            Box::new(LiteLlmProvider::new(
                env_text("VERLATO_LITELLM_ENDPOINT").or_else(|| config.endpoint.clone()),
                model,
                suggest_model,
                env_text("VERLATO_LITELLM_API_KEY_ENV").or_else(|| config.api_key_env.clone()),
                env_float("VERLATO_LITELLM_TIMEOUT_SECONDS", config.timeout_seconds),
                config.num_predict,
            ))
        }
        "mock" => Box::new(mock::MockProvider),
        "github-copilot" | "github" | "copilot" => Box::new(github::GitHubModelsProvider::new(
            env_text("VERLATO_GITHUB_MODELS_ENDPOINT").or_else(|| config.endpoint.clone()),
            env_text("VERLATO_GITHUB_MODEL").or_else(|| config.model.clone()),
        )),
        "codex-cli" | "codex" => Box::new(cli_agents::CliAgentProvider::new(
            cli_agents::Agent::Codex,
            config.model.clone(),
            env_float("VERLATO_CODEX_TIMEOUT_SECONDS", config.timeout_seconds),
        )),
        "claude-code" | "claude" => Box::new(cli_agents::CliAgentProvider::new(
            cli_agents::Agent::ClaudeCode,
            config.model.clone(),
            env_float("VERLATO_CLAUDE_TIMEOUT_SECONDS", config.timeout_seconds),
        )),
        "ollama" | "local" => {
            let env_flag = |name: &str| {
                matches!(
                    env_text(name)
                        .unwrap_or_default()
                        .trim()
                        .to_lowercase()
                        .as_str(),
                    "1" | "true" | "yes" | "on"
                )
            };
            let env_int = |name: &str, fallback: Option<i64>| match env_text(name) {
                Some(raw) if !raw.trim().is_empty() => raw.trim().parse().ok().or(fallback),
                _ => fallback,
            };
            let suggest_model = override_or(
                env_text("VERLATO_OLLAMA_SUGGEST_MODEL"),
                &config.suggest_model,
            );
            Box::new(OllamaProvider::new(OllamaSettings {
                endpoint: env_text("VERLATO_OLLAMA_ENDPOINT").or_else(|| config.endpoint.clone()),
                model: env_text("VERLATO_OLLAMA_MODEL").or_else(|| config.model.clone()),
                suggest_model,
                timeout_seconds: env_float(
                    "VERLATO_OLLAMA_TIMEOUT_SECONDS",
                    config.timeout_seconds,
                )
                .or(Some(15.0)),
                stream: env_flag("VERLATO_OLLAMA_STREAM"),
                keep_alive: env_text("VERLATO_OLLAMA_KEEP_ALIVE")
                    .or_else(|| config.keep_alive.clone()),
                num_ctx: env_int("VERLATO_OLLAMA_NUM_CTX", config.num_ctx),
                num_predict: env_int("VERLATO_OLLAMA_NUM_PREDICT", config.num_predict),
                summary_num_predict: env_int(
                    "VERLATO_OLLAMA_SUMMARY_NUM_PREDICT",
                    config.summary_num_predict,
                ),
                classify_num_predict: env_int(
                    "VERLATO_OLLAMA_CLASSIFY_NUM_PREDICT",
                    config.classify_num_predict,
                ),
                suggest_num_predict: env_int(
                    "VERLATO_OLLAMA_SUGGEST_NUM_PREDICT",
                    config.suggest_num_predict,
                ),
                suggest_think: match env_text("VERLATO_OLLAMA_SUGGEST_THINK") {
                    Some(raw) if !raw.trim().is_empty() => Some(matches!(
                        raw.trim().to_lowercase().as_str(),
                        "1" | "true" | "yes" | "on"
                    )),
                    _ => config.suggest_think,
                },
            }))
        }
        _ => Box::new(StubProvider),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn audit_hashes_use_python_canonical_json() {
        let request = ProviderRequest {
            task: "summarize".into(),
            prompt: "Prompt é".into(),
            evidence: json!({"b": 1, "a": [0.5]}),
            ..Default::default()
        };
        let messages = messages_for_request(&request, "summarize");
        let audit = request_audit(&request, "litellm", "m", &messages);
        // sha256('{"a":[0.5],"b":1}'), as computed by Python's canonical_json.
        assert_eq!(
            audit["evidence_hash"],
            json!(sha256_hex("{\"a\":[0.5],\"b\":1}"))
        );
        assert_eq!(audit["schema_hash"], json!(sha256_hex("null")));
    }
}
