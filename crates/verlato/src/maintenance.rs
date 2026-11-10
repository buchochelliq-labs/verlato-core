//! Maintenance commands: `model-logs export`, `feedback export|recommend`,
//! `state clear-suggestion-cache`, `doctor ollama` and `doctor model`. Ports the matching
//! `_run_*` functions of `verlato/cli/main.py` with byte-identical JSON output.

use anyhow::{bail, Result};
use indexmap::IndexMap;
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, HashSet};
use std::path::{Component, Path, PathBuf};
use std::time::{Duration, Instant};

use crate::assist::Style;
use crate::pipeline::Context;
use crate::providers::http::{get_text, HttpError};
use crate::providers::model_log;
use crate::providers::process::{run_command, which, Env, RunError};
use crate::providers::{
    build_provider_with, effective_model, is_provider_error_marker, Provider, ProviderConfig,
    ProviderRequest,
};
use crate::pyjson;

const MAX_SUGGESTION_PARALLELISM: i64 = 8;
const FULL_DIFF_SUGGESTION_PROMPT_SHAPE: &str = "full-diff-json-v1";

// -- Python value helpers -------------------------------------------------------------------

/// Python truthiness of a JSON value.
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

/// Python `repr()` of a JSON value.
fn py_repr(value: &Value) -> String {
    match value {
        Value::String(s) => crate::mcp::py_repr_str(s),
        Value::Array(items) => format!(
            "[{}]",
            items.iter().map(py_repr).collect::<Vec<_>>().join(", ")
        ),
        Value::Object(map) => format!(
            "{{{}}}",
            map.iter()
                .map(|(k, v)| format!("{}: {}", crate::mcp::py_repr_str(k), py_repr(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
        other => py_str(other),
    }
}

/// Python `str()` of a JSON value.
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
        other => py_repr(other),
    }
}

/// `str(value or default)`.
fn str_or(value: Option<&Value>, default: &str) -> String {
    match value.filter(|v| truthy(v)) {
        Some(v) => py_str(v),
        None => default.to_owned(),
    }
}

/// `_as_dict`: the value when it is an object, else an empty map.
fn as_dict(value: Option<&Value>) -> Map<String, Value> {
    value
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default()
}

/// `_optional_float`: numbers (bools count, as in Python) and numeric strings.
fn optional_float(value: Option<&Value>) -> Option<f64> {
    match value? {
        Value::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
        Value::Number(n) => n.as_f64(),
        Value::String(s) if !s.trim().is_empty() => s.trim().parse().ok(),
        _ => None,
    }
}

/// `_optional_int`.
fn optional_int(value: Option<&Value>) -> Option<i64> {
    optional_float(value).map(|f| f as i64)
}

/// `str(pathlib.Path(text))`: drops `.` components and repeated or trailing separators.
fn py_path(text: &str) -> String {
    if text.is_empty() {
        return ".".into();
    }
    let path = Path::new(text);
    let mut normalized = PathBuf::new();
    for component in path.components() {
        if component != Component::CurDir {
            normalized.push(component.as_os_str());
        }
    }
    let rendered = normalized.to_string_lossy().into_owned();
    if rendered.is_empty() {
        ".".into()
    } else {
        rendered
    }
}

fn elapsed_ms(start: Instant) -> i64 {
    (start.elapsed().as_secs_f64() * 1000.0).round().max(0.0) as i64
}

/// Python's `round(value, digits)` for the values reported here.
fn round_to(value: f64, digits: usize) -> f64 {
    format!("{value:.digits$}").parse().unwrap_or(value)
}

/// Write `text` to `output` or stdout (`print(text, end="")`).
fn emit(text: &str, output: Option<&str>) -> Result<()> {
    match output {
        Some(path) => std::fs::write(path, text)?,
        None => {
            use std::io::Write;
            let mut stdout = std::io::stdout();
            stdout.write_all(text.as_bytes())?;
            stdout.flush()?;
        }
    }
    Ok(())
}

/// The `--format json|jsonl` export shared by `model-logs export` and `feedback export`.
fn export_text(records: &[Value], format: &str, header: Map<String, Value>) -> String {
    if format == "jsonl" {
        let mut text = records
            .iter()
            .map(|r| pyjson::dumps(r, pyjson::DEFAULT_SORTED))
            .collect::<Vec<_>>()
            .join("\n");
        if !text.is_empty() {
            text.push('\n');
        }
        return text;
    }
    let mut document = header;
    document.insert("record_count".into(), json!(records.len()));
    document.insert("records".into(), Value::Array(records.to_vec()));
    let mut text = pyjson::dumps_indented(&Value::Object(document), 2);
    text.push('\n');
    text
}

// -- model-logs export ------------------------------------------------------------------------

fn redact_string(value: &str) -> (String, bool) {
    if model_log::redaction_enabled() {
        model_log::redact(value)
    } else {
        (value.to_owned(), false)
    }
}

fn redact_value(value: &Value) -> (Value, bool) {
    match value {
        Value::String(s) => {
            let (safe, changed) = redact_string(s);
            (Value::String(safe), changed)
        }
        Value::Array(items) => {
            let mut changed = false;
            let values = items
                .iter()
                .map(|item| {
                    let (safe, item_changed) = redact_value(item);
                    changed |= item_changed;
                    safe
                })
                .collect();
            (Value::Array(values), changed)
        }
        Value::Object(map) => {
            let mut changed = false;
            let mapping = map
                .iter()
                .map(|(key, item)| {
                    let (safe, item_changed) = redact_value(item);
                    changed |= item_changed;
                    (key.clone(), safe)
                })
                .collect();
            (Value::Object(mapping), changed)
        }
        other => (other.clone(), false),
    }
}

/// `_attributes_with_redaction`.
fn with_redaction(attributes: Value, redacted: bool) -> Value {
    let mut safe = attributes.as_object().cloned().unwrap_or_default();
    let previous = safe.get("redacted").is_some_and(truthy);
    safe.insert("redacted".into(), json!(previous || redacted));
    Value::Object(safe)
}

/// `_normalize_model_log_record`.
fn normalize_model_log_record(raw: &Map<String, Value>, line_number: usize) -> Value {
    if raw.contains_key("EventName") || raw.contains_key("Body") {
        let attributes = Value::Object(as_dict(raw.get("Attributes")));
        let (body, body_redacted) = redact_string(&str_or(raw.get("Body"), ""));
        let (safe_attributes, attributes_redacted) = redact_value(&attributes);
        let get = |key: &str| raw.get(key).cloned().unwrap_or(Value::Null);
        return json!({
            "line_number": line_number,
            "raw_format": "otel",
            "timestamp": get("Timestamp"),
            "observed_timestamp": get("ObservedTimestamp"),
            "severity_text": get("SeverityText"),
            "severity_number": get("SeverityNumber"),
            "event": str_or(raw.get("EventName"), "verlato.model.log"),
            "body": body,
            "attributes": with_redaction(safe_attributes, body_redacted || attributes_redacted),
            "resource": as_dict(raw.get("Resource")),
            "instrumentation_scope": as_dict(raw.get("InstrumentationScope")),
        });
    }
    let attributes = Value::Object(as_dict(raw.get("attributes")));
    let (message, message_redacted) = redact_string(&str_or(raw.get("message"), ""));
    let (safe_attributes, attributes_redacted) = redact_value(&attributes);
    let mut normalized = Map::from_iter([
        ("line_number".to_owned(), json!(line_number)),
        ("raw_format".to_owned(), json!("cli")),
        (
            "event".to_owned(),
            json!(str_or(raw.get("event"), "verlato.model.event")),
        ),
        ("message".to_owned(), json!(message)),
        ("attributes".to_owned(), safe_attributes.clone()),
    ]);
    let mut payload_redacted = false;
    if let Some(Value::String(payload)) = raw.get("payload") {
        let (safe, changed) = redact_string(payload);
        payload_redacted = changed;
        normalized.insert("payload".into(), json!(safe));
    }
    normalized.insert(
        "attributes".into(),
        with_redaction(
            safe_attributes,
            message_redacted || attributes_redacted || payload_redacted,
        ),
    );
    Value::Object(normalized)
}

/// `verlato model-logs export`.
pub fn model_logs_export(
    file: &str,
    format: &str,
    output: Option<&str>,
    events: &[String],
) -> Result<()> {
    let source = py_path(file);
    if !Path::new(file).exists() {
        bail!("Model log file not found: {source}");
    }
    let text = std::fs::read_to_string(file)?;
    let filter: HashSet<&str> = events.iter().map(String::as_str).collect();
    let mut records = Vec::new();
    for (index, line) in pyjson::splitlines(&text).into_iter().enumerate() {
        let line_number = index + 1;
        if line.trim().is_empty() {
            continue;
        }
        let raw: Value = serde_json::from_str(line)
            .map_err(|e| anyhow::anyhow!("Invalid JSONL model log at line {line_number}: {e}"))?;
        let Some(raw) = raw.as_object() else {
            bail!("Invalid JSONL model log at line {line_number}: expected an object");
        };
        let normalized = normalize_model_log_record(raw, line_number);
        let event = normalized["event"].as_str().unwrap_or_default();
        if !filter.is_empty() && !filter.contains(event) {
            continue;
        }
        records.push(normalized);
    }
    let header = Map::from_iter([
        ("schema_version".to_owned(), json!("verlato.model_logs.v1")),
        ("source".to_owned(), json!(source)),
    ]);
    emit(&export_text(&records, format, header), output)
}

// -- feedback ---------------------------------------------------------------------------------

/// `verlato feedback export`.
pub fn feedback_export(
    ctx: &Context,
    format: &str,
    output: Option<&str>,
    events: &[String],
) -> Result<()> {
    let state = ctx.store().load()?;
    let filter: HashSet<&str> = events.iter().map(String::as_str).collect();
    let records: Vec<Value> = state
        .feedback_events
        .into_iter()
        .filter(|event| {
            filter.is_empty()
                || filter.contains(py_str(event.get("event").unwrap_or(&Value::Null)).as_str())
        })
        .collect();
    let header = Map::from_iter([("schema_version".to_owned(), json!("verlato.feedback.v1"))]);
    emit(&export_text(&records, format, header), output)
}

/// `verlato feedback recommend`.
pub fn feedback_recommend(ctx: &Context) -> Result<()> {
    let events = ctx.store().load()?.feedback_events;
    let payload = feedback_recommendations(&events);
    println!("{}", pyjson::dumps_indented(&payload, 2));
    Ok(())
}

#[derive(Default)]
struct Counter(BTreeMap<String, i64>);

impl Counter {
    fn add(&mut self, key: &str) {
        *self.0.entry(key.to_owned()).or_insert(0) += 1;
    }
    fn get(&self, key: &str) -> i64 {
        self.0.get(key).copied().unwrap_or(0)
    }
    fn total(&self) -> i64 {
        self.0.values().sum()
    }
    fn to_value(&self) -> Value {
        Value::Object(self.0.iter().map(|(k, v)| (k.clone(), json!(v))).collect())
    }
}

/// `_feedback_float`.
fn feedback_float(value: Option<&Value>) -> Option<f64> {
    match value? {
        Value::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.trim().parse().ok(),
        _ => None,
    }
}

/// `_feedback_best_tone`: the tone with the best applied/edited rate (at least 60%).
fn best_tone(tones: &IndexMap<String, Counter>) -> Option<(String, i64, i64)> {
    let mut best: Option<(String, i64, i64, f64)> = None;
    for (tone, counts) in tones {
        let accepted = counts.get("suggestion_applied") + counts.get("summary_edited");
        let total = counts.total();
        if total < 2 || accepted == 0 {
            continue;
        }
        let rate = accepted as f64 / total as f64;
        if rate < 0.6 {
            continue;
        }
        let better = match &best {
            None => true,
            Some((_, b_accepted, b_total, b_rate)) => (rate, accepted, total)
                .partial_cmp(&(*b_rate, *b_accepted, *b_total))
                .is_some_and(|o| o.is_gt()),
        };
        if better {
            best = Some((tone.clone(), accepted, total, rate));
        }
    }
    best.map(|(tone, accepted, total, _)| (tone, accepted, total))
}

/// `_build_feedback_recommendations`.
pub fn feedback_recommendations(events: &[Value]) -> Value {
    let safe: Vec<&Map<String, Value>> = events.iter().filter_map(Value::as_object).collect();
    let mut event_counts = Counter::default();
    let mut tone_counts: IndexMap<String, Counter> = IndexMap::new();
    let mut specificity_scores: Vec<f64> = Vec::new();
    let negative_events = [
        "suggestion_skipped",
        "suggestion_rejected",
        "suggestion_not_useful",
        "suggestion_regenerated",
    ];
    for event in &safe {
        let name = str_or(event.get("event"), "unknown");
        event_counts.add(&name);
        let metadata = as_dict(event.get("metadata"));
        if let Some(Value::String(tone)) = metadata.get("tone") {
            if !tone.is_empty() {
                tone_counts.entry(tone.clone()).or_default().add(&name);
            }
        }
        if let Some(score) = feedback_float(metadata.get("specificity_score")) {
            if name == "suggestion_applied" || name == "summary_edited" {
                specificity_scores.push(score);
            }
        }
    }
    let positive = event_counts.get("suggestion_applied") + event_counts.get("summary_edited");
    let negative: i64 = negative_events.iter().map(|n| event_counts.get(n)).sum();
    let regenerated = event_counts.get("suggestion_regenerated");
    let total = safe.len() as i64;
    let mut recommendations: Vec<Value> = Vec::new();
    if total == 0 {
        recommendations.push(json!({
            "kind": "collect-more-feedback",
            "title": "Collect more feedback before tuning prompts",
            "detail": "No local feedback events are available yet, so Verlato cannot infer style preferences safely.",
            "confidence": "high",
        }));
    }
    if total > 0 && negative >= positive.max(3) {
        recommendations.push(json!({
            "kind": "tighten-style-guidance",
            "title": "Add project-specific style guidance",
            "detail": format!(
                "{negative} skip/reject/regenerate event(s) versus {positive} applied/edit event(s) \
                 suggest that generated summaries are missing the project's preferred voice."
            ),
            "confidence": "medium",
            "suggested_config": {
                "suggestion_style": {
                    "guidance": "Describe the user-visible outcome first, then include the concrete changed area.",
                }
            },
        }));
    }
    if regenerated >= 2 {
        recommendations.push(json!({
            "kind": "generate-more-options",
            "title": "Consider a larger suggestion count",
            "detail": format!(
                "{regenerated} regenerate event(s) suggest reviewers often need another option before \
                 applying wording."
            ),
            "confidence": "medium",
            "suggested_config": {"suggestion_count": 2},
        }));
    }
    let average = (!specificity_scores.is_empty())
        .then(|| specificity_scores.iter().sum::<f64>() / specificity_scores.len() as f64);
    if let Some(average) = average.filter(|a| *a < 0.45) {
        recommendations.push(json!({
            "kind": "raise-specificity-floor",
            "title": "Raise the specificity floor",
            "detail": format!(
                "Accepted or edited suggestions average {average:.2} specificity, so vague candidates \
                 may still be getting through."
            ),
            "confidence": "medium",
            "suggested_config": {"suggestion_style": {"specificity_floor": 0.45}},
        }));
    }
    if let Some((tone, accepted, tone_total)) = best_tone(&tone_counts) {
        recommendations.push(json!({
            "kind": "prefer-tone",
            "title": format!("Prefer the {tone} tone"),
            "detail": format!("{accepted} of {tone_total} feedback event(s) for this tone were applied or edited."),
            "confidence": if tone_total < 5 { "low" } else { "medium" },
            "suggested_config": {"suggestion_tone": tone},
        }));
    }
    if total > 0 && recommendations.is_empty() {
        recommendations.push(json!({
            "kind": "keep-current-style",
            "title": "Keep the current prompt style",
            "detail": "Current aggregate feedback does not show a strong reason to adjust prompt or style settings.",
            "confidence": "medium",
        }));
    }
    let mut tones: Vec<(&String, &Counter)> = tone_counts.iter().collect();
    tones.sort_by(|a, b| a.0.cmp(b.0));
    json!({
        "schema_version": "verlato.feedback_recommendations.v1",
        "event_count": total,
        "event_counts": event_counts.to_value(),
        "signals": {
            "positive_count": positive,
            "negative_count": negative,
            "regenerated_count": regenerated,
            "average_specificity": average.map(|a| round_to(a, 3)),
            "tones": Value::Object(
                tones.into_iter().map(|(t, c)| (t.clone(), c.to_value())).collect()
            ),
        },
        "recommendations": recommendations,
    })
}

// -- state clear-suggestion-cache -------------------------------------------------------------

/// The suggestion cache settings (`_resolve_suggestion_cache_backend`, the
/// `VERLATO_SUGGESTION_CACHE_PATH` override and `_resolve_suggestion_cache_ttl_days`).
fn cache_settings(ctx: &Context) -> (String, String, i64) {
    let raw = &ctx.config.raw;
    let text = |key: &str| {
        raw.get(key)
            .filter(|v| truthy(v))
            .map(py_str)
            .map(|v| v.trim().to_owned())
            .filter(|v| !v.is_empty())
    };
    let backend = std::env::var("VERLATO_SUGGESTION_CACHE_BACKEND")
        .ok()
        .filter(|v| !v.is_empty())
        .or_else(|| text("suggestion_cache_backend"))
        .unwrap_or_else(|| "json".into())
        .trim()
        .to_lowercase();
    let backend = if backend.is_empty() {
        "json".into()
    } else {
        backend
    };
    let path = std::env::var("VERLATO_SUGGESTION_CACHE_PATH").unwrap_or_else(|_| {
        raw.get("suggestion_cache_path")
            .filter(|v| truthy(v))
            .map(py_str)
            .unwrap_or_else(|| ".verlato_suggestion_cache.json".into())
    });
    let configured = optional_int(raw.get("suggestion_cache_ttl_days")).unwrap_or(30);
    let ttl = match std::env::var("VERLATO_SUGGESTION_CACHE_TTL_DAYS") {
        Ok(value) => value.trim().parse().unwrap_or(configured),
        Err(_) => configured,
    };
    (backend, path, ttl.max(0))
}

/// Clear the suggestion cache of `backend` and return `(removed entries, cache path)`.
///
/// Backend dispatch point: only the built-in `json` backend exists natively (as in Python,
/// where other backends must be registered by code). Add new backends here.
fn clear_suggestion_cache_backend(
    backend: &str,
    path: &str,
    ttl_days: i64,
) -> Result<(usize, String)> {
    match backend {
        "json" => {
            let mut cache = crate::suggestion_cache::JsonSuggestionCache::load(path, ttl_days)?;
            let removed = cache.clear()?;
            Ok((removed, py_path(&cache.path.to_string_lossy())))
        }
        other => bail!(
            "Unknown suggestion cache backend {}; available: json",
            crate::mcp::py_repr_str(other)
        ),
    }
}

/// `verlato state clear-suggestion-cache` and the `state.clear_suggestion_cache` service method.
pub fn clear_suggestion_cache(ctx: &Context) -> Result<Value> {
    let (backend, path, ttl) = cache_settings(ctx);
    let (removed, path) = clear_suggestion_cache_backend(&backend, &path, ttl)?;
    Ok(json!({"removed_entries": removed, "path": path}))
}

// -- doctor ollama ----------------------------------------------------------------------------

fn configured_parallelism(ctx: &Context) -> i64 {
    let configured = optional_int(ctx.config.raw.get("suggestion_parallelism")).unwrap_or(1);
    let value = match std::env::var("VERLATO_SUGGESTION_PARALLELISM") {
        Ok(raw) => raw.trim().parse().unwrap_or(configured),
        Err(_) => configured,
    };
    value.clamp(1, MAX_SUGGESTION_PARALLELISM)
}

/// `_resolve_suggestion_adaptive_recommendations`.
pub fn adaptive_recommendations_enabled(ctx: &Context, requested: bool) -> bool {
    let configured = ctx
        .config
        .raw
        .get("suggestion_adaptive_recommendations")
        .is_some_and(truthy);
    if requested {
        return true;
    }
    match std::env::var("VERLATO_SUGGESTION_ADAPTIVE_RECOMMENDATIONS") {
        Err(_) => configured,
        Ok(raw) => match raw.trim().to_lowercase().as_str() {
            "1" | "true" | "yes" | "on" => true,
            "0" | "false" | "no" | "off" => false,
            _ => configured,
        },
    }
}

fn doctor_prompt() -> &'static str {
    "Return one concise release-note style sentence for this diagnostic change: \
     Verlato adds a model health check for suggestion performance."
}

/// `_ollama_doctor_config`.
fn doctor_config(provider: &dyn Provider, request: &ProviderRequest, parallelism: i64) -> Value {
    let attributes = provider.ollama_attributes(request).unwrap_or_default();
    let attr = |key: &str| attributes.get(key).cloned().unwrap_or(Value::Null);
    let model = provider.model();
    let suggest_model = provider
        .suggest_model()
        .map(|m| m.trim().to_owned())
        .filter(|m| !m.is_empty());
    let effective = Some(effective_model(provider, "suggest"))
        .filter(|m| !m.is_empty())
        .unwrap_or_else(|| model.clone().unwrap_or_default());
    json!({
        "endpoint": attr("endpoint"),
        "model": model,
        "suggest_model": suggest_model,
        "effective_suggest_model": effective,
        "timeout_seconds": attr("timeout_seconds"),
        "stream": attr("stream"),
        "keep_alive": attr("keep_alive"),
        "num_ctx": attr("num_ctx"),
        "num_predict": attr("num_predict"),
        "suggest_num_predict": attr("suggest_num_predict"),
        "suggest_num_predict_effective": attr("suggest_num_predict_effective"),
        "suggest_think": attr("suggest_think"),
        "suggestion_parallelism": parallelism,
    })
}

/// `_ollama_probe_status`.
fn probe_status(content: &str) -> (bool, Value) {
    let content = content.trim();
    let lowered = content.to_lowercase();
    if lowered.starts_with("[ollama-unavailable]") || lowered.starts_with("[ollama-error]") {
        return (false, json!(content));
    }
    if lowered.starts_with("[ollama-empty]") || content.is_empty() {
        let message = if content.is_empty() {
            "[ollama-empty] no content"
        } else {
            content
        };
        return (false, json!(message));
    }
    (true, Value::Null)
}

/// `_run_ollama_doctor_probe`.
fn run_probe(
    provider: &dyn Provider,
    request: &ProviderRequest,
    name: &str,
    purpose: &str,
) -> Value {
    let started = Instant::now();
    let response = provider.summarize(request);
    let elapsed = elapsed_ms(started);
    let metadata = response.metadata.clone();
    let mut timings = Map::from_iter([("model_ms".to_owned(), json!(elapsed))]);
    timings.extend(crate::suggest::provider_timings(&metadata, elapsed));
    let (ok, error) = probe_status(&response.content);
    json!({
        "name": name,
        "purpose": purpose,
        "ok": ok,
        "error": error,
        "elapsed_ms": elapsed,
        "prompt_chars": pyjson::py_len(&request.prompt),
        "model": response.model,
        "content_chars": pyjson::py_len(&response.content),
        "timings": timings,
        "metadata": metadata,
    })
}

/// `_ollama_base_url`.
fn ollama_base_url(endpoint: &Value) -> Option<String> {
    let endpoint = endpoint.as_str().map(str::trim).filter(|e| !e.is_empty())?;
    let normalized = endpoint.trim_end_matches('/');
    for suffix in ["/api/chat", "/api/generate", "/api"] {
        if let Some(base) = normalized.strip_suffix(suffix) {
            return Some(base.trim_end_matches('/').to_owned());
        }
    }
    Some(normalized.to_owned())
}

/// Python's `str(exc)` for the urllib failures `_get_json` can raise.
fn urllib_error_text(error: &HttpError) -> String {
    match error {
        HttpError::Status(..) => error.to_string(),
        HttpError::Transport(message) | HttpError::Disconnected(message) => {
            let lowered = message.to_lowercase();
            if lowered.contains("timed out") || lowered.contains("timeout") {
                "timed out".into()
            } else if lowered.contains("connection refused") {
                "<urlopen error [Errno 111] Connection refused>".into()
            } else {
                format!("<urlopen error {message}>")
            }
        }
    }
}

/// `_collect_ollama_api_ps`.
fn collect_api_ps(config: &Value) -> Value {
    let Some(base) = ollama_base_url(&config["endpoint"]) else {
        return json!({"ok": false, "url": null, "models": [], "error": "No Ollama endpoint configured."});
    };
    let url = format!("{base}/api/ps");
    let timeout = optional_float(config.get("timeout_seconds"))
        .filter(|t| *t != 0.0)
        .unwrap_or(15.0);
    let fetched = get_text(
        &url,
        &[("Accept", "application/json".to_owned())],
        Duration::from_secs_f64(timeout.max(0.001)),
    );
    let text = match fetched {
        Ok(text) => text,
        Err(error) => {
            return json!({"ok": false, "url": url, "models": [], "error": urllib_error_text(&error)})
        }
    };
    let data = match serde_json::from_str::<Value>(&text) {
        Ok(Value::Object(data)) => data,
        Ok(_) => {
            return json!({"ok": false, "url": url, "models": [], "error": "expected JSON object: line 1 column 1 (char 0)"})
        }
        Err(error) => {
            return json!({"ok": false, "url": url, "models": [], "error": error.to_string()})
        }
    };
    let models = data.get("models").cloned().unwrap_or(Value::Null);
    let is_list = models.is_array();
    json!({
        "ok": is_list,
        "url": url,
        "models": if is_list { models } else { json!([]) },
        "response": data,
    })
}

/// `_normalize_ollama_ps_header`.
fn normalize_ps_header(value: &str) -> String {
    let re = regex::Regex::new(r"[^a-z0-9]+").expect("valid regex");
    re.replace_all(&value.trim().to_lowercase(), "_")
        .trim_matches('_')
        .to_owned()
}

/// `_parse_ollama_ps_rows`: `ollama ps` columns are separated by two or more spaces.
fn parse_ps_rows(raw: &str) -> Vec<Value> {
    let split = regex::Regex::new(r"\s{2,}").expect("valid regex");
    let lines: Vec<&str> = pyjson::splitlines(raw)
        .into_iter()
        .map(str::trim)
        .filter(|l| !l.is_empty())
        .collect();
    if lines.len() < 2 {
        return Vec::new();
    }
    let headers: Vec<String> = split
        .split(lines[0])
        .filter(|p| !p.trim().is_empty())
        .map(normalize_ps_header)
        .collect();
    let mut rows = Vec::new();
    for line in &lines[1..] {
        let mut cells: Vec<String> = split
            .split(line)
            .map(str::trim)
            .filter(|p| !p.is_empty())
            .map(str::to_owned)
            .collect();
        if cells.is_empty() || headers.is_empty() {
            continue;
        }
        if cells.len() > headers.len() {
            let tail = cells[headers.len() - 1..].join(" ");
            cells.truncate(headers.len() - 1);
            cells.push(tail);
        }
        let row: Map<String, Value> = headers
            .iter()
            .zip(&cells)
            .map(|(h, c)| (h.clone(), json!(c)))
            .collect();
        rows.push(Value::Object(row));
    }
    rows
}

/// `_collect_ollama_cli_ps`.
fn collect_cli_ps() -> Value {
    let Some(executable) = which("ollama") else {
        return json!({"ok": false, "available": false, "rows": []});
    };
    let executable = executable.to_string_lossy().into_owned();
    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    let result = run_command(
        &[executable.clone(), "ps".into()],
        None,
        &cwd,
        Env::Inherit,
        Duration::from_secs(3),
    );
    let output = match result {
        Ok(output) => output,
        Err(RunError::Start(error)) => {
            return json!({"ok": false, "available": true, "rows": [], "error": error.to_string()})
        }
        Err(RunError::Timeout(_)) => {
            return json!({
                "ok": false,
                "available": true,
                "rows": [],
                "error": format!(
                    "Command '[{}, 'ps']' timed out after 3 seconds",
                    crate::mcp::py_repr_str(&executable)
                ),
            })
        }
    };
    let stdout = output.stdout.trim().to_owned();
    let stderr = output.stderr.trim().to_owned();
    let mut payload = Map::from_iter([
        ("ok".to_owned(), json!(output.code == 0)),
        ("available".to_owned(), json!(true)),
        ("returncode".to_owned(), json!(output.code)),
        (
            "rows".to_owned(),
            Value::Array(if output.code == 0 {
                parse_ps_rows(&stdout)
            } else {
                Vec::new()
            }),
        ),
        ("raw".to_owned(), json!(stdout)),
    ]);
    if !stderr.is_empty() {
        payload.insert("stderr".into(), json!(stderr));
        if output.code != 0 {
            payload.insert("error".into(), json!(stderr));
        }
    }
    Value::Object(payload)
}

fn model_aliases(model: &str) -> HashSet<String> {
    let normalized = model.trim().to_lowercase();
    let mut aliases = HashSet::new();
    if !normalized.is_empty() {
        aliases.insert(normalized.clone());
        if !normalized.contains(':') {
            aliases.insert(format!("{normalized}:latest"));
        }
    }
    if let Some(stripped) = normalized.strip_suffix(":latest") {
        aliases.insert(stripped.to_owned());
    }
    aliases
}

fn names_of(entry: &Map<String, Value>) -> Vec<String> {
    ["name", "model"]
        .iter()
        .map(|k| str_or(entry.get(*k), ""))
        .filter(|n| !n.is_empty())
        .map(|n| n.trim().to_lowercase())
        .collect()
}

/// `_select_ollama_runtime_model`.
fn select_runtime_model(configured: &str, api_ps: &Value, cli_ps: &Value) -> Value {
    let api_ok = api_ps.get("ok") == Some(&Value::Bool(true));
    let aliases = model_aliases(configured);
    if api_ok {
        for model in api_ps["models"].as_array().into_iter().flatten() {
            let Some(model) = model.as_object() else {
                continue;
            };
            if !names_of(model).iter().any(|n| aliases.contains(n)) {
                continue;
            }
            let mut selected = model.clone();
            selected.insert("loaded".into(), json!(true));
            let processor = cli_ps["rows"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_object)
                .find(|row| names_of(row).iter().any(|n| aliases.contains(n)))
                .map(|row| row.get("processor").filter(|p| truthy(p)).map(py_str));
            if let Some(Some(processor)) = processor {
                selected.insert("processor".into(), json!(processor));
            }
            return Value::Object(selected);
        }
    }
    json!({
        "name": configured,
        "model": configured,
        "loaded": if api_ok { json!(false) } else { Value::Null },
    })
}

/// `_collect_ollama_runtime`.
fn collect_runtime(config: &Value) -> Value {
    let api_ps = collect_api_ps(config);
    let cli_ps = collect_cli_ps();
    let configured = [&config["effective_suggest_model"], &config["model"]]
        .into_iter()
        .find(|v| truthy(v))
        .map(py_str)
        .unwrap_or_default();
    let selected = select_runtime_model(&configured, &api_ps, &cli_ps);
    json!({"api_ps": api_ps, "cli_ps": cli_ps, "selected_model": selected})
}

/// `_seconds_until`: seconds from now until an ISO 8601 timestamp.
fn seconds_until(value: Option<&Value>) -> Option<i64> {
    let raw = value?.as_str()?.trim();
    if raw.is_empty() {
        return None;
    }
    let normalized = raw.replace('Z', "+00:00");
    let expires = chrono::DateTime::parse_from_rfc3339(&normalized)
        .map(|d| d.with_timezone(&chrono::Utc))
        .ok()
        .or_else(|| {
            ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%d %H:%M:%S%.f", "%Y-%m-%d"]
                .iter()
                .find_map(|f| chrono::NaiveDateTime::parse_from_str(&normalized, f).ok())
                .map(|n| n.and_utc())
        })?;
    let delta = (expires - chrono::Utc::now()).num_microseconds()? as f64 / 1e6;
    Some(delta.round_ties_even() as i64)
}

fn int_of(value: &Value) -> Option<i64> {
    value.as_i64()
}

/// `_ollama_runtime_diagnostics`.
fn runtime_diagnostics(runtime: &Value, config: &Value) -> Vec<String> {
    let api_ps = as_dict(runtime.get("api_ps"));
    let selected = as_dict(runtime.get("selected_model"));
    if api_ps.get("ok") != Some(&Value::Bool(true)) {
        return vec![format!(
            "Ollama runtime state unavailable from /api/ps: {}.",
            str_or(api_ps.get("error"), "unknown error")
        )];
    }
    let mut diagnostics = Vec::new();
    let model_name = [
        config.get("model"),
        selected.get("model"),
        selected.get("name"),
    ]
    .into_iter()
    .flatten()
    .find(|v| truthy(v))
    .map(py_str)
    .unwrap_or_else(|| "configured model".into());
    if selected.get("loaded") == Some(&Value::Bool(false)) {
        diagnostics.push(format!(
            "Configured model {model_name} was not reported by Ollama /api/ps."
        ));
        return diagnostics;
    }
    if let Some(expires) = seconds_until(selected.get("expires_at")).filter(|s| *s <= 120) {
        diagnostics.push(format!(
            "Loaded model expires in {}s; short keep-alive can cause reloads.",
            expires.max(0)
        ));
    }
    let context_length = optional_int(selected.get("context_length"));
    let parallelism = int_of(&config["suggestion_parallelism"]);
    if let (Some(context), Some(parallelism)) = (context_length, parallelism) {
        if context >= 8192 && parallelism > 1 {
            diagnostics.push(format!(
                "Loaded model context length is {context}; with suggestion parallelism {parallelism}, memory pressure can multiply."
            ));
        }
    }
    let size_vram = optional_int(selected.get("size_vram"));
    let processor = str_or(selected.get("processor"), "").trim().to_owned();
    if size_vram.is_none_or(|v| v <= 0) {
        diagnostics.push(
            "Loaded model did not report GPU VRAM residency; check ollama ps processor placement."
                .into(),
        );
    }
    let lowered = processor.to_lowercase();
    if !processor.is_empty() && lowered.contains("cpu") && !lowered.contains("gpu") {
        diagnostics.push(format!(
            "Ollama processor placement is {processor}; CPU-only inference can slow suggestions."
        ));
    }
    diagnostics
}

/// `_ollama_runtime_recommendations`.
fn runtime_recommendations(runtime: &Value, config: &Value) -> Vec<String> {
    let api_ps = as_dict(runtime.get("api_ps"));
    let selected = as_dict(runtime.get("selected_model"));
    if api_ps.get("ok") != Some(&Value::Bool(true)) {
        return vec![
            "Check Ollama /api/ps or ollama ps when timing telemetry is inconclusive.".into(),
        ];
    }
    let mut recommendations = Vec::new();
    if selected.get("loaded") == Some(&Value::Bool(false)) {
        recommendations.push(
            "Use Warm model or raise keep_alive so the configured model is loaded before all-visible suggestions."
                .into(),
        );
    }
    if seconds_until(selected.get("expires_at")).is_some_and(|s| s <= 120) {
        recommendations.push(
            "Raise VERLATO_OLLAMA_KEEP_ALIVE or provider.keep_alive if suggestions often reload the model."
                .into(),
        );
    }
    let context_length = optional_int(selected.get("context_length"));
    let parallelism = int_of(&config["suggestion_parallelism"]);
    if let (Some(context), Some(parallelism)) = (context_length, parallelism) {
        if context >= 8192 && parallelism > 1 {
            recommendations.push(
                "Lower suggestionParallelism to 1 or reduce OLLAMA_CONTEXT_LENGTH/VERLATO_OLLAMA_NUM_CTX \
                 before enabling server-side parallelism."
                    .into(),
            );
        }
    }
    let size_vram = optional_int(selected.get("size_vram"));
    let processor = str_or(selected.get("processor"), "").trim().to_lowercase();
    if size_vram.is_none_or(|v| v <= 0) || (processor.contains("cpu") && !processor.contains("gpu"))
    {
        recommendations.push(
            "Check ollama ps processor placement and consider a smaller/faster model if generation remains slow."
                .into(),
        );
    }
    recommendations
}

fn unique_strings(values: Vec<String>) -> Vec<String> {
    let mut seen = HashSet::new();
    values
        .into_iter()
        .filter(|v| !v.is_empty() && seen.insert(v.clone()))
        .collect()
}

/// `_max_probe_timing`.
fn max_probe_timing(probes: &[&Value], key: &str) -> i64 {
    probes
        .iter()
        .filter_map(|p| p["timings"].get(key))
        .filter_map(|v| match v {
            Value::Number(n) => n.as_f64().map(|f| f as i64),
            Value::Bool(b) => Some(i64::from(*b)),
            _ => None,
        })
        .max()
        .unwrap_or(0)
}

fn timing_ratio(value: i64, total: i64) -> f64 {
    if total <= 0 {
        0.0
    } else {
        value as f64 / total as f64
    }
}

/// `_ollama_doctor_bottleneck`.
fn bottleneck(probes: &[&Value]) -> (&'static str, String) {
    let queue = max_probe_timing(probes, "queue_ms");
    let load = max_probe_timing(probes, "load_ms");
    let prompt_eval = max_probe_timing(probes, "prompt_eval_ms");
    let generation = max_probe_timing(probes, "eval_ms");
    let model =
        max_probe_timing(probes, "model_ms").max(max_probe_timing(probes, "ollama_total_ms"));
    if queue >= 250 && (model <= 0 || queue as f64 / model as f64 >= 0.2) {
        return (
            "queue",
            format!("Ollama queue time reached {queue}ms during the probe."),
        );
    }
    if load >= 500 && timing_ratio(load, model) >= 0.35 {
        return ("load", format!("Ollama model load time reached {load}ms."));
    }
    if prompt_eval >= 500 && timing_ratio(prompt_eval, model) >= 0.35 {
        return (
            "prompt_eval",
            format!("Ollama prompt evaluation reached {prompt_eval}ms."),
        );
    }
    if generation >= 500 && timing_ratio(generation, model) >= 0.35 {
        return (
            "generation",
            format!("Ollama generation reached {generation}ms."),
        );
    }
    if model >= 2500 {
        return (
            "generation",
            format!("Ollama probe took {model}ms without a more specific timing breakdown."),
        );
    }
    (
        "healthy",
        format!("Ollama probe completed in {model}ms with no obvious bottleneck."),
    )
}

fn context_recommendation(config: &Value) -> String {
    match int_of(&config["num_ctx"]) {
        Some(num_ctx) if num_ctx >= 8192 => format!(
            "Configured num_ctx is {num_ctx}; lower it if suggestion prompts do not need that much context."
        ),
        _ => "Avoid oversized OLLAMA_CONTEXT_LENGTH or VERLATO_OLLAMA_NUM_CTX values until prompt eval is healthy."
            .into(),
    }
}

/// `_ollama_doctor_recommendations`.
fn doctor_recommendations(kind: &str, config: &Value, warmup_requested: bool) -> Vec<String> {
    let parallelism = int_of(&config["suggestion_parallelism"]);
    match kind {
        "queue" => vec![
            match parallelism {
                Some(p) if p > 1 => format!(
                    "Lower Verlato suggestion parallelism from {p} to 1 if Ollama is serializing requests."
                ),
                _ => "Keep Verlato suggestion parallelism at 1 unless Ollama is configured for server-side parallelism.".into(),
            },
            "Set OLLAMA_NUM_PARALLEL before starting Ollama if your hardware has memory for parallel requests.".into(),
            "Keep OLLAMA_CONTEXT_LENGTH or VERLATO_OLLAMA_NUM_CTX conservative because each parallel slot uses memory.".into(),
        ],
        "load" => {
            let mut recommendations = vec![
                "Keep the model warm with VERLATO_OLLAMA_KEEP_ALIVE or provider.keep_alive.".to_owned(),
                "Use verlato doctor ollama --warmup or the VS Code Warm model action before large all-visible suggestion runs.".into(),
            ];
            if warmup_requested {
                recommendations.push(
                    "Warmup was requested; if load time remains high, raise keep_alive or check memory pressure.".into(),
                );
            }
            recommendations
        }
        "prompt_eval" => vec![
            "Reduce prompt budget further or inspect prompt budget telemetry for files/hunks that dominate.".into(),
            context_recommendation(config),
            "Consider a later multi-item batching pass if prompt eval remains the main cost.".into(),
        ],
        "generation" => vec![
            "Lower VERLATO_OLLAMA_SUGGEST_NUM_PREDICT or provider.suggest_num_predict.".into(),
            if truthy(&config["suggest_model"]) {
                "Try a smaller/faster local suggestion model if generation remains slow.".into()
            } else {
                "Try a smaller/faster local model with provider.suggest_model or VERLATO_OLLAMA_SUGGEST_MODEL.".into()
            },
        ],
        _ => vec!["No obvious Ollama bottleneck was detected by the probe.".into()],
    }
}

/// `_ollama_doctor_recommended_settings`.
fn recommended_settings(kind: &str, config: &Value) -> Value {
    let mut settings = Map::new();
    if kind == "queue" && int_of(&config["suggestion_parallelism"]).is_some_and(|p| p > 1) {
        settings.insert("suggestion_parallelism".into(), json!(1));
    }
    if kind == "generation"
        && int_of(&config["suggest_num_predict_effective"]).is_some_and(|c| c > 96)
    {
        settings.insert("suggest_num_predict".into(), json!(96));
    }
    if kind == "generation" && !truthy(&config["suggest_model"]) {
        settings.insert("suggest_model".into(), json!("smaller-local-model"));
    }
    Value::Object(settings)
}

/// `_ollama_warmup_diagnostic`.
fn warmup_diagnostic(probe: Option<&Value>) -> String {
    let Some(probe) = probe else {
        return "Warmup was requested but no warmup probe was recorded.".into();
    };
    if probe.get("ok") != Some(&Value::Bool(true)) {
        return format!(
            "Warmup failed: {}.",
            str_or(probe.get("error"), "Ollama returned no usable content")
        );
    }
    let elapsed = py_str(&probe["elapsed_ms"]);
    match probe["timings"].get("load_ms").and_then(Value::as_i64) {
        Some(load) if load > 0 => {
            format!("Warmup completed in {elapsed}ms and absorbed {load}ms of model load time.")
        }
        _ => format!("Warmup completed in {elapsed}ms."),
    }
}

/// `_summarize_ollama_doctor`.
fn summarize_doctor(probes: &[Value], config: &Value, runtime: &Value, warmup: bool) -> Value {
    let diagnostics_rt = runtime_diagnostics(runtime, config);
    let recommendations_rt = runtime_recommendations(runtime, config);
    let ok_probes: Vec<&Value> = probes
        .iter()
        .filter(|p| p.get("ok") == Some(&Value::Bool(true)))
        .collect();
    if ok_probes.is_empty() {
        let mut diagnostics: Vec<String> = probes
            .iter()
            .map(|p| str_or(p.get("error"), "Ollama probe failed"))
            .collect();
        diagnostics.extend(diagnostics_rt);
        let mut recommendations = vec![
            "Check that Ollama is running and reachable at the configured endpoint.".to_owned(),
            "Confirm the configured model is pulled and the endpoint matches Ollama's /api/chat route.".into(),
        ];
        recommendations.extend(recommendations_rt);
        return json!({
            "status": "error",
            "bottleneck": "unavailable",
            "warmed_up": false,
            "diagnostics": diagnostics,
            "recommendations": unique_strings(recommendations),
            "recommended_settings": {},
        });
    }
    let warmup_probe = probes
        .iter()
        .find(|p| p.get("purpose").and_then(Value::as_str) == Some("warmup"));
    let mut measured: Vec<&Value> = ok_probes
        .iter()
        .copied()
        .filter(|p| p.get("purpose").and_then(Value::as_str) != Some("warmup"))
        .collect();
    if measured.is_empty() {
        measured = ok_probes.clone();
    }
    let (kind, diagnostic) = bottleneck(&measured);
    let mut diagnostics = vec![diagnostic];
    let warmup_ok = warmup_probe.is_some_and(|p| p.get("ok") == Some(&Value::Bool(true)));
    if warmup {
        diagnostics.push(warmup_diagnostic(warmup_probe));
    }
    if let Some(num_ctx) = int_of(&config["num_ctx"]).filter(|n| *n >= 8192) {
        diagnostics.push(format!(
            "Configured num_ctx is {num_ctx}; large contexts increase memory use and can limit parallel requests."
        ));
    }
    diagnostics.extend(diagnostics_rt);
    let mut recommendations = doctor_recommendations(kind, config, warmup);
    recommendations.extend(recommendations_rt);
    json!({
        "status": if kind == "healthy" { "ok" } else { "warning" },
        "bottleneck": kind,
        "warmed_up": warmup_ok,
        "diagnostics": diagnostics,
        "recommendations": unique_strings(recommendations),
        "recommended_settings": recommended_settings(kind, config),
    })
}

/// `verlato doctor ollama` (without adaptive recommendations).
pub fn doctor_ollama(
    ctx: &Context,
    parallel_probe: bool,
    warmup: bool,
    suggest_model: Option<&str>,
) -> Result<Value> {
    let parallelism = configured_parallelism(ctx);
    let provider_config = ProviderConfig::from_raw(&ctx.config.raw);
    let provider = build_provider_with(&provider_config, Some("ollama"), suggest_model);
    let provider: &dyn Provider = provider.as_ref();
    let request = ProviderRequest {
        task: "suggest".into(),
        prompt: doctor_prompt().into(),
        evidence: json!({
            "title": "Ollama doctor probe",
            "item_id": "doctor_ollama",
            "suggestion_count": 1,
            "doctor": true,
        }),
        ..Default::default()
    };
    let config = doctor_config(provider, &request, parallelism);
    let mut probes = Vec::new();
    if warmup {
        probes.push(run_probe(provider, &request, "warmup", "warmup"));
    }
    probes.push(run_probe(provider, &request, "single", "measure"));
    if parallel_probe {
        let (a, b) = std::thread::scope(|scope| {
            let a = scope.spawn(|| run_probe(provider, &request, "parallel-a", "measure"));
            let b = scope.spawn(|| run_probe(provider, &request, "parallel-b", "measure"));
            (a.join(), b.join())
        });
        probes.push(a.map_err(|_| anyhow::anyhow!("Ollama doctor probe panicked"))?);
        probes.push(b.map_err(|_| anyhow::anyhow!("Ollama doctor probe panicked"))?);
    }
    let runtime = collect_runtime(&config);
    let summary = summarize_doctor(&probes, &config, &runtime, warmup);
    Ok(json!({
        "schema": "ollama_doctor.v1",
        "provider": "ollama",
        "config": config,
        "runtime": runtime,
        "probes": probes,
        "summary": summary,
    }))
}

// -- doctor model -----------------------------------------------------------------------------

/// The fixed full-diff item `_doctor_suggestion_work` builds.
fn doctor_full_diff_item(item_id: &str) -> Value {
    json!({
        "item_id": item_id,
        "title": "Doctor model diff probe",
        "current_summary": "Checks whether the selected model can produce structured suggestions from diff evidence.",
        "classification": {
            "kind": "changed",
            "area": "general",
            "audience": "external",
            "semver": "patch",
        },
        "labels": [],
        "pr": null,
        "commits": [
            {"sha": "doctor", "subject": "docs: tune release note copy", "message": "docs: tune release note copy", "author": "Verlato"}
        ],
        "files": [
            {
                "path": "docs/release-notes.md",
                "status": "M",
                "patch": "@@ -1,2 +1,2 @@\n-Old release note text\n+Clearer release note text for external readers\n",
                "patch_chars": 86,
                "patch_available": true,
                "patch_unavailable_reason": "",
            }
        ],
        "file_count": 1,
        "patch_chars": 86,
        "missing_patch_count": 0,
        "diff_summary": {
            "summary": "M docs/release-notes.md",
            "files_by_area": {"docs": ["docs/release-notes.md"]},
            "file_statuses": {"docs/release-notes.md": "M"},
            "api_changes": [],
            "migration_changes": [],
            "config_changes": [],
        },
    })
}

/// `_build_full_diff_suggestion_batch_prompt` for the doctor's single git-patch item (patch
/// evidence present, no semantic diff).
// TODO(dedupe): share with the full-diff-json strategy once suggest.rs exports its prompt builder.
fn doctor_full_diff_prompt(item: Value, count: i64, style: &Style) -> String {
    let payload = json!({
        "suggestion_count": count,
        "tone": style.tone,
        "style_profile": style.profile(),
        "prompt_shape": FULL_DIFF_SUGGESTION_PROMPT_SHAPE,
        "items": [item],
    });
    format!(
        "Generate release-note suggestion variants from the full diff evidence.\n\
         Use the file patches as the primary source of truth. Do not infer behavior from titles alone.\n\
         Return only JSON that matches the requested schema. Do not include Markdown fences or prose.\n\
         For each item, return exactly {count} suggestion string(s) in the suggestions array.\n\
         Do not combine items, rename item ids, omit an item, or mention implementation details unless they matter to \
         release-note readers.\n\
         If a file has no semantic_diff (and patch_available=false), treat that file as changed but do not invent \
         unavailable details.\n\
         Input JSON:\n{}",
        pyjson::dumps_indented(&payload, 2)
    )
}

/// `_suggestion_batch_response_format`.
fn batch_response_format(count: i64) -> Value {
    json!({
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "string"},
                        "suggestions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "maxItems": count,
                        },
                    },
                    "required": ["item_id", "suggestions"],
                },
            }
        },
        "required": ["items"],
    })
}

/// `_parse_suggestion_batch_json`.
fn parse_batch_json(content: &str) -> Option<BTreeMap<String, Vec<String>>> {
    let parsed: Value = serde_json::from_str(content).ok()?;
    let items = parsed.as_object()?.get("items")?.as_array()?;
    let mut by_item = BTreeMap::new();
    for item in items {
        let Some(item) = item.as_object() else {
            continue;
        };
        let (Some(Value::String(id)), Some(Value::Array(raw))) =
            (item.get("item_id"), item.get("suggestions"))
        else {
            continue;
        };
        let suggestions: Vec<String> = raw
            .iter()
            .filter_map(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .collect();
        if !suggestions.is_empty() {
            by_item.insert(id.clone(), suggestions);
        }
    }
    Some(by_item)
}

/// `verlato doctor model`.
pub fn doctor_model(
    ctx: &Context,
    model_provider: Option<&str>,
    suggest_model: Option<&str>,
    timeout_seconds: Option<f64>,
) -> Result<Value> {
    let provider_config = ProviderConfig::from_raw(&ctx.config.raw);
    let mode = model_provider
        .filter(|m| !m.is_empty())
        .map(str::to_owned)
        .unwrap_or_else(|| crate::suggest::resolve_suggest_model_provider(ctx, None));
    let provider = build_provider_with(&provider_config, Some(&mode), suggest_model);
    let provider: &dyn Provider = provider.as_ref();
    let item_id = "doctor_model";
    let style = Style::resolve(&ctx.config.raw, None);
    let prompt = doctor_full_diff_prompt(doctor_full_diff_item(item_id), 1, &style);
    let request = ProviderRequest {
        task: "suggest".into(),
        prompt: prompt.clone(),
        evidence: json!({
            "doctor": true,
            "strategy": "full-diff-json",
            "prompt_shape": FULL_DIFF_SUGGESTION_PROMPT_SHAPE,
            "item_ids": [item_id],
            "item_count": 1,
            "suggestion_count": 1,
            "full_diff_file_count": 1,
            "full_diff_patch_chars": 86,
            "missing_patch_count": 0,
        }),
        response_format: batch_response_format(1),
        ..Default::default()
    };
    let started = Instant::now();
    let response = provider.summarize(&request);
    let elapsed = elapsed_ms(started);
    let parsed = parse_batch_json(&response.content);
    let provider_error = is_provider_error_marker(&response.content, false);
    let has_item = parsed.as_ref().is_some_and(|p| p.contains_key(item_id));
    let valid = has_item && !provider_error;
    let mut warnings: Vec<String> = Vec::new();
    let budget_seconds = timeout_seconds
        .filter(|t| *t != 0.0)
        .or(provider_config.timeout_seconds.filter(|t| *t != 0.0))
        .unwrap_or(10.0);
    let budget_ms = (budget_seconds * 1000.0) as i64;
    if elapsed >= budget_ms {
        warnings.push(format!(
            "probe exceeded the advisory timeout budget ({budget_ms}ms)"
        ));
    } else if elapsed >= 5000 {
        warnings
            .push("cold probe was slow; the selected suggest model may not feel responsive".into());
    } else if elapsed >= 2000 {
        warnings.push("warm response may feel sluggish for editor suggestions".into());
    }
    if provider_error {
        warnings.push("provider returned an error marker".into());
    }
    match &parsed {
        // An empty mapping is falsy in Python.
        None => warnings.push("provider did not return valid structured JSON".into()),
        Some(p) if p.is_empty() => {
            warnings.push("provider did not return valid structured JSON".into())
        }
        Some(_) if !has_item => {
            warnings.push("provider structured JSON omitted the requested item".into())
        }
        Some(_) => {}
    }
    let mut result = Map::from_iter([
        ("schema".to_owned(), json!("model_doctor.v1")),
        ("provider".to_owned(), json!(mode)),
    ]);
    result.extend(crate::suggest::provider_model_context(provider, &mode));
    let status = if valid && warnings.is_empty() {
        "ok"
    } else if valid {
        "warning"
    } else {
        "error"
    };
    result.extend([
        ("ok".to_owned(), json!(valid)),
        ("status".to_owned(), json!(status)),
        ("warnings".to_owned(), json!(warnings)),
        ("elapsed_ms".to_owned(), json!(elapsed)),
        ("prompt_chars".to_owned(), json!(pyjson::py_len(&prompt))),
        ("model".to_owned(), json!(response.model)),
        (
            "content_chars".to_owned(),
            json!(pyjson::py_len(&response.content)),
        ),
        ("metadata".to_owned(), Value::Object(response.metadata)),
    ]);
    Ok(Value::Object(result))
}

/// Print a document the way `print(json.dumps(value, indent=2))` does.
pub fn print_unsorted(value: &Value) {
    println!("{}", pyjson::dumps_indented_unsorted(value, 2));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pathlib_rendering() {
        assert_eq!(py_path("./a//b/"), "a/b");
        assert_eq!(py_path("."), ".");
        assert_eq!(py_path(""), ".");
        assert_eq!(py_path("/tmp/./x.json"), "/tmp/x.json");
    }

    #[test]
    fn ollama_ps_rows() {
        let raw = "NAME            ID              SIZE      PROCESSOR    UNTIL\n\
                   gemma4:latest   a2af6cc3eb7f    6.6 GB    100% GPU     4 minutes from now";
        let rows = parse_ps_rows(raw);
        assert_eq!(
            rows,
            vec![
                json!({"name": "gemma4:latest", "id": "a2af6cc3eb7f", "size": "6.6 GB", "processor": "100% GPU", "until": "4 minutes from now"})
            ]
        );
        assert_eq!(normalize_ps_header(" Size (GB) "), "size_gb");
    }

    #[test]
    fn base_urls() {
        assert_eq!(
            ollama_base_url(&json!("http://h:1/api/chat/")).as_deref(),
            Some("http://h:1")
        );
        assert_eq!(
            ollama_base_url(&json!("http://h:1/api")).as_deref(),
            Some("http://h:1")
        );
        assert_eq!(ollama_base_url(&json!(" ")), None);
    }

    #[test]
    fn recommendations_without_feedback() {
        let payload = feedback_recommendations(&[]);
        assert_eq!(
            payload["recommendations"][0]["kind"],
            "collect-more-feedback"
        );
        assert_eq!(payload["signals"]["average_specificity"], Value::Null);
    }

    #[test]
    fn best_tone_prefers_rate_then_counts() {
        let events: Vec<Value> = [
            ("suggestion_applied", "concise"),
            ("suggestion_applied", "concise"),
            ("suggestion_skipped", "technical"),
            ("suggestion_applied", "technical"),
            ("suggestion_applied", "technical"),
        ]
        .iter()
        .map(|(e, t)| json!({"event": e, "item_id": "x", "metadata": {"tone": t}}))
        .collect();
        let payload = feedback_recommendations(&events);
        let prefer = payload["recommendations"]
            .as_array()
            .unwrap()
            .iter()
            .find(|r| r["kind"] == "prefer-tone")
            .unwrap()
            .clone();
        assert_eq!(prefer["suggested_config"]["suggestion_tone"], "concise");
    }

    #[test]
    fn batch_json_parsing() {
        let parsed =
            parse_batch_json(r#"{"items":[{"item_id":"a","suggestions":[" x ","",3]}]}"#).unwrap();
        assert_eq!(parsed["a"], vec!["x".to_owned()]);
        assert!(parse_batch_json("[]").is_none());
    }
}
