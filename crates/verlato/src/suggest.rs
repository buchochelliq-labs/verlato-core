//! Model suggestions for draft items (ports the `draft prompt` / `draft suggest` flows of
//! `verlato/cli/main.py`).

use anyhow::{bail, Result};
use serde_json::{json, Value};

use crate::assist::{
    build_summary_prompt, can_send_raw_patches_for_config, change_summary_payload,
    model_request_record, privacy_summary, prompt_profile, raw_patch_chars_available,
    resolve_model_privacy, PromptInput, Style, OMITTED_PATCH_EVIDENCE,
};
use crate::classifier::classify;
use crate::evidence;
use crate::models::{changed_files_for_group, ChangeGroup, ChangeItem};
use crate::pipeline::Context;
use crate::providers::ProviderConfig;

/// Options shared by `draft prompt` and `draft suggest`.
#[derive(Debug, Clone, Default)]
pub struct SuggestOptions {
    pub model_provider: Option<String>,
    pub tone: Option<String>,
    pub privacy: Option<String>,
    pub allow_remote_patches: bool,
}

/// `_resolve_suggest_model_provider`: suggestions always use a real model; `stub` (or nothing
/// configured) means the local Ollama default.
pub fn resolve_suggest_model_provider(ctx: &Context, requested: Option<&str>) -> String {
    if let Some(requested) = requested.filter(|p| !p.is_empty()) {
        return requested.to_owned();
    }
    let configured = std::env::var("VERLATO_MODEL_PROVIDER")
        .unwrap_or_else(|_| ProviderConfig::from_raw(&ctx.config.raw).name)
        .trim()
        .to_lowercase();
    if !configured.is_empty() && configured != "stub" {
        configured
    } else {
        "ollama".into()
    }
}

/// `_find_group_for_item_id`: the change group whose deterministic item has `item_id`.
pub fn find_group(
    ctx: &Context,
    base: &str,
    head: &str,
    item_id: &str,
) -> Result<(ChangeGroup, ChangeItem)> {
    for group in ctx.collect_groups(base, head)? {
        let item = classify(&group, base, head, None);
        if item.id == item_id {
            return Ok((group, item));
        }
    }
    bail!("Could not find draft item {item_id} for range {base}..{head}")
}

/// `draft prompt`: the exact prompt, evidence and audit record a suggestion would send.
pub fn draft_prompt(
    ctx: &Context,
    base: &str,
    head: &str,
    item_id: &str,
    options: &SuggestOptions,
) -> Result<Value> {
    let provider = resolve_suggest_model_provider(ctx, options.model_provider.as_deref());
    let raw = &ctx.config.raw;
    let privacy = resolve_model_privacy(
        raw.get("model_privacy").and_then(Value::as_str),
        options.privacy.as_deref(),
        options.allow_remote_patches,
    );
    let provider_config = ProviderConfig::from_raw(raw);
    let raw_patches_allowed =
        can_send_raw_patches_for_config(&provider_config, Some(&provider), &privacy);
    let style = Style::resolve(raw, options.tone.as_deref());
    let (group, item) = find_group(ctx, base, head, item_id)?;
    let files = changed_files_for_group(&group);
    let diff = evidence::build(&files, Some(&group.commits));
    let labels: Vec<String> = group
        .pr
        .as_ref()
        .map(|pr| pr.labels.iter().map(|l| l.to_lowercase()).collect())
        .unwrap_or_default();
    let prompt = build_summary_prompt(&PromptInput {
        title: &item.content.title,
        labels: &labels,
        pr_body: group.pr.as_ref().map(|p| p.body.as_str()).unwrap_or(""),
        diff: &diff,
        commits: &group.commits,
        suggestion_count: 1,
        kind: None,
        audience: None,
        style: &style,
        profile: prompt_profile("suggestion"),
        include_hunks: raw_patches_allowed,
    });
    let raw_chars = raw_patch_chars_available(&files);
    let raw_sent = prompt
        .budget
        .get("raw_patch_chars_sent")
        .and_then(Value::as_u64)
        .unwrap_or(0) as usize;
    let mut budget = prompt.budget.clone();
    budget.insert("privacy_mode".into(), json!(privacy));
    budget.insert("raw_patch_chars_available".into(), json!(raw_chars));
    let evidence = json!({
        "title": item.content.title,
        "item_id": item.id,
        "change_summary": change_summary_payload(&group, Some(&item), &diff, &files),
        "privacy_mode": privacy,
    });
    let request = model_request_record(
        "suggest-release-note",
        Some(&provider),
        &privacy,
        &prompt.prompt,
        &evidence,
        raw_chars,
        raw_sent,
        if raw_patches_allowed {
            &[]
        } else {
            OMITTED_PATCH_EVIDENCE
        },
        if raw_patches_allowed {
            &[]
        } else {
            &["metadata-only mode sends ChangeSummary instead of raw diff text"]
        },
    );
    Ok(json!({
        "item_id": item.id,
        "title": item.content.title,
        "provider": provider,
        "privacy": privacy_summary(&provider, &privacy, raw_chars, raw_sent),
        "tone": style.tone,
        "style_profile": style.profile(),
        "prompt": prompt.prompt,
        "evidence": evidence,
        "ai_request": request,
        "prompt_budget": budget,
    }))
}

// ---------------------------------------------------------------------------
// draft suggest --preview / --preflight (single strategy)
// ---------------------------------------------------------------------------

use regex::Regex;
use serde_json::Map;
use sha2::{Digest, Sha256};
use std::sync::OnceLock;
use std::time::Instant;

use crate::assist::summary_rejection;
use crate::models::ChangedFile;
use crate::pipeline::trace_progress;
use crate::providers::{
    build_provider_with, effective_model, is_provider_error_marker, model_log, Provider,
    ProviderRequest, ProviderResponse,
};
use crate::pyjson::{self, py_len, splitlines};
use crate::semantic::{render_semantic_diff, semantic_diff_for_prompt};
use crate::suggestion_cache::JsonSuggestionCache;

const SUGGESTION_STRATEGY_CHOICES: &[&str] = &[
    "auto",
    "single",
    "paired",
    "full-diff-json",
    "tool-retrieval",
];
const SUGGESTION_PROMPT_PROFILE_CHOICES: &[&str] = &["conservative", "lean", "minimal"];
const MAX_SUGGESTION_PARALLELISM: i64 = 8;
const SINGLE_SUGGESTION_PROMPT_SHAPE: &str = "single-prompt-v1";

/// Options of `draft suggest`.
#[derive(Debug, Clone, Default)]
pub struct SuggestArgs {
    pub item_id: Option<String>,
    pub all_visible: bool,
    pub count: Option<i64>,
    pub parallelism: Option<i64>,
    pub strategy: Option<String>,
    pub prompt_profile: Option<String>,
    pub suggest_model: Option<String>,
    pub no_cache: bool,
    pub refresh_cache: bool,
    pub adaptive_recommendations: bool,
    pub preflight: bool,
    pub options: SuggestOptions,
}

fn raw_text(raw: &Value, key: &str) -> Option<String> {
    match raw.get(key) {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) => Some(s.trim().to_owned()).filter(|s| !s.is_empty()),
        Some(other) => Some(other.to_string()),
    }
}

fn raw_int(raw: &Value, key: &str, default: i64) -> i64 {
    raw.get(key)
        .and_then(|v| v.as_i64().or_else(|| v.as_f64().map(|f| f as i64)))
        .unwrap_or(default)
}

fn env_or(requested: Option<i64>, env: &str, configured: i64) -> i64 {
    match requested {
        Some(value) => value,
        None => match std::env::var(env) {
            Ok(raw) => raw.trim().parse().unwrap_or(configured),
            Err(_) => configured,
        },
    }
}

/// `_resolve_suggestion_strategy`.
fn resolve_strategy(configured: Option<&str>, requested: Option<&str>) -> Result<String> {
    let env = std::env::var("VERLATO_SUGGESTION_STRATEGY").ok();
    let raw = [requested, env.as_deref(), configured]
        .into_iter()
        .flatten()
        .find(|v| !v.is_empty())
        .unwrap_or("auto");
    let strategy = raw.trim().to_lowercase();
    if strategy == "instant" {
        bail!(
            "The instant suggestion strategy has been removed. Use --strategy auto so suggestions are generated from Verlato-built diff evidence."
        );
    }
    Ok(
        if SUGGESTION_STRATEGY_CHOICES.contains(&strategy.as_str()) {
            strategy
        } else {
            "auto".into()
        },
    )
}

fn batching_allowed(provider: &str) -> bool {
    matches!(
        provider.trim().to_lowercase().as_str(),
        "ollama" | "local" | "litellm" | "plugin" | "external-plugin"
    )
}

/// `_effective_suggestion_strategy` plus the raw-patch downgrade in the preview planner.
fn effective_strategy(
    strategy: &str,
    provider: &str,
    privacy: &str,
    raw_patches_allowed: bool,
) -> String {
    let provider_norm = provider.trim().to_lowercase();
    let patches = crate::assist::can_send_raw_patches(provider, privacy);
    let local = matches!(provider_norm.as_str(), "ollama" | "local");
    let mut effective = match strategy {
        "auto" if !patches => "single",
        "auto" if local => "tool-retrieval",
        "auto" if batching_allowed(provider) => "full-diff-json",
        "auto" => "single",
        "tool-retrieval" | "full-diff-json" | "paired" if !patches => "single",
        "tool-retrieval" if !local => "single",
        "paired" | "full-diff-json" if !batching_allowed(provider) => "single",
        other => other,
    }
    .to_owned();
    if !raw_patches_allowed
        && matches!(
            effective.as_str(),
            "paired" | "full-diff-json" | "tool-retrieval"
        )
    {
        effective = "single".into();
    }
    effective
}

/// Resolved settings of a suggestion run.
struct Settings {
    count: i64,
    count_source: &'static str,
    parallelism: i64,
    prompt_profile: String,
    adaptive: bool,
    privacy: String,
    style: Style,
    provider: String,
    strategy: String,
    raw_patches_allowed: bool,
    cache_backend: String,
    cache_path: String,
    cache_ttl_days: i64,
}

fn resolve_settings(ctx: &Context, args: &SuggestArgs) -> Result<Settings> {
    let raw = &ctx.config.raw;
    let count = env_or(
        args.count,
        "VERLATO_SUGGESTION_COUNT",
        raw_int(raw, "suggestion_count", 1),
    )
    .clamp(1, 5);
    let count_source = if args.count.is_some() {
        "cli"
    } else if std::env::var("VERLATO_SUGGESTION_COUNT").is_ok_and(|v| !v.is_empty()) {
        "env"
    } else {
        "config"
    };
    let parallelism = env_or(
        args.parallelism,
        "VERLATO_SUGGESTION_PARALLELISM",
        raw_int(raw, "suggestion_parallelism", 1),
    )
    .clamp(1, MAX_SUGGESTION_PARALLELISM);
    let strategy = resolve_strategy(
        raw_text(raw, "suggestion_strategy").as_deref(),
        args.strategy.as_deref(),
    )?;
    let profile_env = std::env::var("VERLATO_SUGGESTION_PROMPT_PROFILE").ok();
    let configured_profile = raw_text(raw, "suggestion_prompt_profile");
    let prompt_profile = [
        args.prompt_profile.as_deref(),
        profile_env.as_deref(),
        configured_profile.as_deref(),
    ]
    .into_iter()
    .flatten()
    .find(|v| !v.is_empty())
    .unwrap_or("conservative")
    .trim()
    .to_lowercase();
    let prompt_profile = if SUGGESTION_PROMPT_PROFILE_CHOICES.contains(&prompt_profile.as_str()) {
        prompt_profile
    } else {
        "conservative".into()
    };
    let configured_adaptive = match raw.get("suggestion_adaptive_recommendations") {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(_) => true,
    };
    let adaptive = args.adaptive_recommendations
        || match std::env::var("VERLATO_SUGGESTION_ADAPTIVE_RECOMMENDATIONS") {
            Err(_) => configured_adaptive,
            Ok(raw) => match raw.trim().to_lowercase().as_str() {
                "1" | "true" | "yes" | "on" => true,
                "0" | "false" | "no" | "off" => false,
                _ => configured_adaptive,
            },
        };
    let privacy = resolve_model_privacy(
        raw.get("model_privacy").and_then(Value::as_str),
        args.options.privacy.as_deref(),
        args.options.allow_remote_patches,
    );
    let provider = resolve_suggest_model_provider(ctx, args.options.model_provider.as_deref());
    let provider_config = ProviderConfig::from_raw(raw);
    let raw_patches_allowed =
        can_send_raw_patches_for_config(&provider_config, Some(&provider), &privacy);
    let strategy = effective_strategy(&strategy, &provider, &privacy, raw_patches_allowed);
    let cache_backend = std::env::var("VERLATO_SUGGESTION_CACHE_BACKEND")
        .ok()
        .filter(|v| !v.is_empty())
        .or_else(|| raw_text(raw, "suggestion_cache_backend"))
        .unwrap_or_else(|| "json".into())
        .trim()
        .to_lowercase();
    let cache_backend = if cache_backend.is_empty() {
        "json".into()
    } else {
        cache_backend
    };
    let cache_path = std::env::var("VERLATO_SUGGESTION_CACHE_PATH").unwrap_or_else(|_| {
        raw.get("suggestion_cache_path")
            .filter(|v| v.as_str().is_some_and(|s| !s.is_empty()))
            .map(|v| v.as_str().unwrap_or_default().to_owned())
            .unwrap_or_else(|| ".verlato_suggestion_cache.json".into())
    });
    let cache_ttl_days = env_or(
        None,
        "VERLATO_SUGGESTION_CACHE_TTL_DAYS",
        raw_int(raw, "suggestion_cache_ttl_days", 30),
    )
    .max(0);
    Ok(Settings {
        count,
        count_source,
        parallelism,
        prompt_profile,
        adaptive,
        privacy,
        style: Style::resolve(raw, args.options.tone.as_deref()),
        provider,
        strategy,
        raw_patches_allowed,
        cache_backend,
        cache_path,
        cache_ttl_days,
    })
}

/// Why `draft suggest` must run in Python for these arguments, if it must.
pub fn python_reason(ctx: &Context, args: &SuggestArgs, preview: bool) -> Result<Option<String>> {
    if !preview && !args.preflight {
        return Ok(Some(
            "draft suggest without --preview persists review state".into(),
        ));
    }
    let settings = resolve_settings(ctx, args)?;
    if settings.adaptive {
        return Ok(Some("adaptive suggestion recommendations".into()));
    }
    if settings.strategy != "single" {
        return Ok(Some(format!(
            "the {} suggestion strategy",
            settings.strategy
        )));
    }
    if !args.no_cache && settings.cache_backend != "json" {
        return Ok(Some(format!(
            "the {} suggestion cache backend",
            settings.cache_backend
        )));
    }
    Ok(None)
}

// -- traces -------------------------------------------------------------------

fn trace_model_enabled() -> bool {
    std::env::var("VERLATO_TRACE_MODEL_CALLS").as_deref() == Ok("1")
}

fn env_is_one(name: &str) -> bool {
    std::env::var(name).as_deref() == Ok("1")
}

fn safe_payload(payload: &str) -> (String, bool) {
    if model_log::redaction_enabled() {
        model_log::redact(payload)
    } else {
        (payload.to_owned(), false)
    }
}

/// `_trace_model_line`: a `[verlato:model]` line (plus a JSON line with the payload) and the
/// matching `VERLATO_MODEL_LOG_PATH` debug record.
fn trace_model_line(
    message: &str,
    event: &str,
    attributes: Option<Map<String, Value>>,
    payload: Option<&str>,
) {
    let line = if message.starts_with("[verlato:model]") {
        message.to_owned()
    } else {
        format!("[verlato:model] {message}")
    };
    let mut lines = vec![line.clone()];
    if let Some(payload) = payload {
        let (safe, redacted) = safe_payload(payload);
        let mut safe_attributes = attributes.clone().unwrap_or_default();
        safe_attributes.insert("redacted".into(), json!(redacted));
        lines.push(format!(
            "[verlato:model] json {}",
            pyjson::dumps(
                &json!({"event": event, "message": message, "attributes": safe_attributes, "payload": safe}),
                pyjson::DEFAULT_SORTED
            )
        ));
    }
    model_log::stderr_lines(&lines);
    if std::env::var("VERLATO_MODEL_LOG_PATH").is_ok_and(|p| !p.is_empty()) {
        let attributes: Map<String, Value> = attributes
            .unwrap_or_default()
            .into_iter()
            .filter(|(_, v)| !v.is_null())
            .collect();
        let mut record = json!({"event": event, "message": line, "attributes": attributes});
        if let Some(payload) = payload {
            let (safe, redacted) = safe_payload(payload);
            record["payload"] = json!(safe);
            record["attributes"]["redacted"] = json!(redacted);
        }
        model_log::append_log_line(&pyjson::dumps(&record, pyjson::DEFAULT_SORTED));
    }
}

fn trace_stage(stage: &str, detail: &str) {
    if trace_model_enabled() {
        trace_model_line(
            &format!("[verlato:model] suggest stage={stage} {detail}"),
            &stage.replace('-', "_"),
            None,
            None,
        );
    }
}

fn trace_payload(stage: &str, payload: &str) {
    if !trace_model_enabled() || !env_is_one("VERLATO_TRACE_MODEL_PAYLOADS") {
        return;
    }
    let (safe, redacted) = safe_payload(payload);
    let compact: String = safe
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .take(800)
        .collect();
    trace_model_line(
        &format!(
            "[verlato:model] {stage} chars={} redacted={redacted} excerpt={compact}",
            py_len(payload)
        ),
        &stage.replace('-', "_"),
        Some(Map::from_iter([
            ("chars".to_owned(), json!(py_len(payload))),
            ("redacted".to_owned(), json!(redacted)),
        ])),
        Some(payload),
    );
}

fn trace_filter(stage: &str, detail: &str, title: &str, model: &str) {
    if !trace_model_enabled() {
        return;
    }
    let title_part = if title.is_empty() {
        String::new()
    } else {
        format!(" title={title}")
    };
    let model_part = if model.is_empty() {
        String::new()
    } else {
        format!(" model={model}")
    };
    trace_model_line(
        &format!("[verlato:model] suggestion {stage} {detail}{model_part}{title_part}"),
        &format!("suggestion_{}", stage.replace('-', "_")),
        Some(Map::from_iter([
            ("title".to_owned(), json!(title)),
            ("model".to_owned(), json!(model)),
        ])),
        None,
    );
}

fn trace_suggestion_event(event: &str, attributes: Map<String, Value>) {
    if !env_is_one("VERLATO_TRACE_PROGRESS") {
        return;
    }
    let mut payload = Map::new();
    payload.insert("event".into(), json!(event));
    payload.extend(attributes.into_iter().filter(|(_, v)| !v.is_null()));
    model_log::stderr_lines(&[format!(
        "[verlato:suggestion] json {}",
        pyjson::dumps(&Value::Object(payload), pyjson::DEFAULT_SORTED)
    )]);
}

/// Python `str(value)` for traced provider metadata.
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

/// Python `repr` of JSON data (dict keys in sorted order: the Rust maps are sorted).
fn py_repr(value: &Value) -> String {
    match value {
        Value::String(s) => format!("'{}'", s.replace('\\', "\\\\").replace('\'', "\\'")),
        Value::Array(items) => format!(
            "[{}]",
            items.iter().map(py_repr).collect::<Vec<_>>().join(", ")
        ),
        Value::Object(map) => format!(
            "{{{}}}",
            map.iter()
                .map(|(k, v)| format!("'{k}': {}", py_repr(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
        other => py_str(other),
    }
}

fn elapsed_ms(start: Instant) -> i64 {
    (start.elapsed().as_secs_f64() * 1000.0).round() as i64
}

fn sha12(text: &str) -> String {
    hex::encode(Sha256::digest(text.as_bytes()))[..12].to_owned()
}

// -- scoring ----------------------------------------------------------------

fn summary_tokens(value: &str) -> Vec<String> {
    static RE: OnceLock<Regex> = OnceLock::new();
    let re = RE.get_or_init(|| Regex::new("[a-z0-9]+").unwrap());
    let stopwords = [
        "a", "an", "and", "for", "in", "of", "on", "the", "to", "with",
    ];
    re.find_iter(&value.to_lowercase())
        .map(|m| m.as_str().to_owned())
        .filter(|t| !stopwords.contains(&t.as_str()))
        .collect()
}

/// `_summary_similarity`: Jaccard similarity of content tokens.
pub fn summary_similarity(candidate: &str, current: &str) -> f64 {
    let (a, b) = (summary_tokens(candidate), summary_tokens(current));
    if a.is_empty() || b.is_empty() {
        return 0.0;
    }
    if a.join(" ") == b.join(" ") {
        return 1.0;
    }
    let a: std::collections::BTreeSet<_> = a.into_iter().collect();
    let b: std::collections::BTreeSet<_> = b.into_iter().collect();
    let union = a.union(&b).count();
    if union == 0 {
        0.0
    } else {
        a.intersection(&b).count() as f64 / union as f64
    }
}

fn round_to(value: f64, digits: usize) -> f64 {
    format!("{value:.digits$}").parse().unwrap_or(value)
}

/// `_summary_specificity`: length plus overlap with the change's own evidence words.
pub fn summary_specificity(candidate: &str, group: &ChangeGroup) -> f64 {
    let tokens: std::collections::BTreeSet<String> =
        summary_tokens(candidate).into_iter().collect();
    if tokens.is_empty() {
        return 0.0;
    }
    let pr = group.pr.as_ref();
    let evidence_text = [
        pr.map(|p| p.title.clone()).unwrap_or_default(),
        pr.map(|p| p.body.clone()).unwrap_or_default(),
        pr.map(|p| p.labels.join(" ")).unwrap_or_default(),
        group
            .commits
            .iter()
            .map(|c| c.message.as_str())
            .collect::<Vec<_>>()
            .join(" "),
        changed_files_for_group(group)
            .iter()
            .map(|f| f.path.as_str())
            .collect::<Vec<_>>()
            .join(" "),
    ]
    .join(" ");
    let evidence: std::collections::BTreeSet<String> =
        summary_tokens(&evidence_text).into_iter().collect();
    let overlap = tokens.intersection(&evidence).count();
    let length_score = (tokens.len() as f64 / 12.0).min(1.0) * 0.55;
    let overlap_score = (overlap as f64 / 4.0).min(1.0) * 0.45;
    round_to(length_score + overlap_score, 4)
}

fn numbered_item() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^\s*\d+[\.)]\s+").unwrap())
}

/// `_clean_suggestion_text`: drop code fences and wrapping quotes.
fn clean_suggestion_text(text: &str) -> String {
    let lines: Vec<&str> = splitlines(text.trim())
        .into_iter()
        .filter(|line| !line.trim().starts_with("```"))
        .map(str::trim_end)
        .collect();
    let joined = lines.join("\n");
    joined
        .trim()
        .trim_matches(|c| c == '"' || c == '\'')
        .trim()
        .to_owned()
}

/// `_suggestion_candidates`.
fn suggestion_candidates(normalized: &str, count: i64) -> Vec<String> {
    let re = numbered_item();
    let mut entries: Vec<Vec<String>> = Vec::new();
    for raw in splitlines(normalized) {
        if re.is_match(raw) {
            entries.push(vec![re.replace(raw, "").into_owned()]);
        } else if let Some(last) = entries.last_mut() {
            last.push(raw.to_owned());
        }
    }
    if entries.len() == 1 && re.is_match(normalized) {
        let cleaned = clean_suggestion_text(&entries[0].join("\n"));
        return if cleaned.is_empty() {
            vec![]
        } else {
            vec![cleaned]
        };
    }
    if entries.len() < 2 {
        if count <= 1 {
            let cleaned = clean_suggestion_text(normalized);
            return if cleaned.is_empty() {
                vec![]
            } else {
                vec![cleaned]
            };
        }
        static BLANK: OnceLock<Regex> = OnceLock::new();
        let blank = BLANK.get_or_init(|| Regex::new(r"\n\s*\n").unwrap());
        entries = blank
            .split(normalized)
            .filter(|b| !b.trim().is_empty())
            .map(|b| vec![b.to_owned()])
            .collect();
    }
    let candidates: Vec<String> = entries
        .iter()
        .map(|e| clean_suggestion_text(&e.join("\n")))
        .filter(|c| !c.is_empty())
        .collect();
    if candidates.is_empty() {
        vec![clean_suggestion_text(normalized)]
    } else {
        candidates
    }
}

struct Parsed {
    accepted: Vec<Value>,
    rejected: Vec<Value>,
}

/// `_parse_suggestion_variants`: candidates filtered by rejection rules, banned phrases,
/// similarity to the current summary and the specificity floor.
fn parse_suggestion_variants(content: &str, work: &Work, model: &str) -> Parsed {
    let title = work.item.content.title.as_str();
    let normalized = content.trim();
    if normalized.is_empty() {
        trace_filter("raw-response", "empty", title, model);
        return Parsed {
            accepted: vec![],
            rejected: vec![],
        };
    }
    let candidates = suggestion_candidates(normalized, work.count);
    trace_filter(
        "parse",
        &format!(
            "raw_lines={} candidates={} requested={}",
            splitlines(normalized).len(),
            candidates.len(),
            work.count
        ),
        title,
        model,
    );
    let current = current_summary(&work.item);
    let mut accepted: Vec<Value> = Vec::new();
    let mut rejected: Vec<Value> = Vec::new();
    for line in candidates {
        let mut reason: Option<String> =
            summary_rejection(&line, title, &work.group).map(str::to_owned);
        let lowered = line.to_lowercase();
        let banned = work
            .style
            .banned
            .iter()
            .find(|p| !p.is_empty() && lowered.contains(&p.to_lowercase()));
        if reason.is_none() {
            if let Some(banned) = banned {
                reason = Some(format!("banned-phrase:{banned}"));
            }
        }
        let similarity = summary_similarity(&line, &current);
        let specificity = summary_specificity(&line, &work.group);
        if reason.is_none() && similarity >= 0.82 {
            reason = Some("too-similar-to-current".into());
        }
        if reason.is_none() && specificity < work.style.specificity_floor {
            reason = Some("below-specificity-floor".into());
        }
        if reason.is_none() && accepted.iter().any(|d| d["summary"] == json!(line)) {
            reason = Some("duplicate".into());
        }
        if let Some(reason) = reason {
            trace_filter(
                "reject",
                &format!("reason={reason} similarity_to_current={similarity:.2} specificity={specificity:.2} candidate={line}"),
                title,
                model,
            );
            rejected.push(json!({
                "summary": line,
                "reason": reason,
                "similarity_to_current": similarity,
                "specificity_score": specificity,
            }));
            continue;
        }
        accepted.push(json!({
            "summary": line,
            "similarity_to_current": similarity,
            "specificity_score": specificity,
            "comparison": {"current": current, "suggested": line},
        }));
        trace_filter(
            "accept",
            &format!(
                "index={} similarity_to_current={similarity:.2} specificity={specificity:.2} candidate={line}",
                accepted.len()
            ),
            title,
            model,
        );
        if accepted.len() as i64 >= work.count {
            break;
        }
    }
    Parsed { accepted, rejected }
}

// -- work ---------------------------------------------------------------------

fn current_summary(item: &ChangeItem) -> String {
    item.edited_summary
        .clone()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| item.content.summary.clone())
}

struct Work {
    order: usize,
    item_number: usize,
    batch: usize,
    batch_total: usize,
    total_targets: usize,
    group: ChangeGroup,
    item: ChangeItem,
    prompt: String,
    evidence: Value,
    source_evidence: Vec<Value>,
    prompt_sha: String,
    budget: Map<String, Value>,
    cache_key: String,
    file_count: usize,
    patch_chars: usize,
    missing_patch_count: usize,
    count: i64,
    style: Style,
    provider_label: String,
    evidence_ms: i64,
    prompt_ms: i64,
    started: Instant,
}

struct WorkResult {
    order: usize,
    item: Map<String, Value>,
    model_ms: i64,
    parse_ms: i64,
    model_call_count: i64,
}

impl Work {
    fn privacy(&self) -> Value {
        let get = |key: &str| self.budget.get(key).and_then(Value::as_u64).unwrap_or(0) as usize;
        privacy_summary(
            &self.provider_label,
            self.budget
                .get("privacy_mode")
                .and_then(Value::as_str)
                .unwrap_or("metadata-only"),
            get("raw_patch_chars_available"),
            get("raw_patch_chars_sent"),
        )
    }

    fn request_record(&self) -> Value {
        self.evidence
            .get("request")
            .filter(|r| r.is_object())
            .cloned()
            .unwrap_or_else(|| json!({}))
    }

    fn base_timings(&self, model_ms: i64, parse_ms: i64) -> Map<String, Value> {
        let value = json!({
            "evidence_ms": self.evidence_ms,
            "prompt_ms": self.prompt_ms,
            "model_ms": model_ms,
            "parse_ms": parse_ms,
            "total_ms": elapsed_ms(self.started),
            "strategy": "single",
            "file_count": self.file_count,
            "patch_chars": self.patch_chars,
            "missing_patch_count": self.missing_patch_count,
        });
        value.as_object().cloned().unwrap_or_default()
    }

    /// Fields every result item shares.
    fn common_item(&self) -> Map<String, Value> {
        let value = json!({
            "item_id": self.item.id,
            "title": self.item.content.title,
            "current_summary": current_summary(&self.item),
            "prompt_sha": self.prompt_sha,
            "tone": self.style.tone,
            "style_profile": self.style.profile(),
            "source_evidence": self.source_evidence,
            "prompt_budget": self.budget,
            "privacy": self.privacy(),
            "ai_request": self.request_record(),
            "batch": self.batch,
        });
        value.as_object().cloned().unwrap_or_default()
    }
}

fn source_evidence(
    diff: &evidence::DiffEvidence,
    group: &ChangeGroup,
    include_hunks: bool,
) -> Vec<Value> {
    let mut out: Vec<Value> = Vec::new();
    if let Some(pr) = &group.pr {
        out.push(json!({"kind": "pull-request", "label": format!("PR #{}", pr.number), "detail": pr.title}));
    }
    for commit in group.commits.iter().take(3) {
        let subject_line = splitlines(&commit.message)
            .first()
            .map(|s| s.to_string())
            .unwrap_or_default();
        out.push(json!({
            "kind": "commit",
            "label": commit.sha.chars().take(7).collect::<String>(),
            "detail": subject_line.chars().take(180).collect::<String>(),
        }));
    }
    let mut statuses: Vec<(&String, &String)> = diff.file_statuses.iter().collect();
    statuses.sort();
    for (path, status) in statuses.into_iter().take(6) {
        out.push(json!({"kind": "file", "label": path, "detail": status}));
    }
    if diff.semantic.is_some() {
        for record in semantic_diff_for_prompt(&diff.semantic_diff, include_hunks)
            .iter()
            .take(3)
        {
            let excerpt = render_semantic_diff(std::slice::from_ref(record), include_hunks, None)
                .replace('\n', " ");
            let detail: String = excerpt
                .split_whitespace()
                .collect::<Vec<_>>()
                .join(" ")
                .chars()
                .take(240)
                .collect();
            out.push(json!({
                "kind": "semantic-diff",
                "label": record.get("path").and_then(Value::as_str).unwrap_or(""),
                "detail": detail,
            }));
        }
    } else if include_hunks {
        for (path, hunks) in diff.representative_hunks.iter().take(3) {
            let excerpt = hunks
                .iter()
                .map(|h| h.replace('\n', " "))
                .collect::<Vec<_>>()
                .join(" ");
            out.push(json!({"kind": "diff", "label": path, "detail": excerpt.chars().take(240).collect::<String>()}));
        }
    }
    if !diff.summary.is_empty() {
        out.push(json!({"kind": "summary", "label": "diff summary", "detail": diff.summary.chars().take(300).collect::<String>()}));
    }
    out.truncate(12);
    out
}

/// The counters `_build_full_diff_suggestion_payload_item` contributes on the single path:
/// (file count, patch chars available, patch chars sent, missing patch count).
fn full_diff_counters(
    files: &[ChangedFile],
    diff: &evidence::DiffEvidence,
    include_patches: bool,
) -> (usize, usize, usize, usize) {
    let available: usize = files.iter().map(|f| py_len(&f.patch)).sum();
    if diff.semantic.is_some() {
        let records = semantic_diff_for_prompt(&diff.semantic_diff, include_patches);
        let missing = files
            .iter()
            .filter(|f| {
                !records
                    .iter()
                    .any(|r| r.get("path").and_then(Value::as_str) == Some(f.path.as_str()))
            })
            .count();
        return (files.len(), available, 0, missing);
    }
    let missing = files.iter().filter(|f| f.patch.is_empty()).count();
    (
        files.len(),
        available,
        if include_patches { available } else { 0 },
        missing,
    )
}

/// `_suggestion_cache_key`.
#[allow(clippy::too_many_arguments)]
fn cache_key(
    item: &ChangeItem,
    prompt_sha: &str,
    provider_mode: &str,
    model_id: &str,
    style: &Style,
    count: i64,
    prompt_profile: &str,
    privacy: &str,
) -> String {
    let style_sha = sha12(&pyjson::dumps(
        &style.profile(),
        pyjson::COMPACT_SORTED_ASCII,
    ));
    let payload = json!({
        "schema": "v2",
        "item_id": item.id,
        "checksum_version": item.checksum_version,
        "checksum": item.checksum,
        "current_summary_sha": sha12(&current_summary(item)),
        "prompt_sha": prompt_sha,
        "strategy": "single",
        "prompt_shape": SINGLE_SUGGESTION_PROMPT_SHAPE,
        "privacy_mode": privacy,
        "provider_mode": provider_mode,
        "model_id": model_id,
        "tone": style.tone,
        "style_profile_sha": style_sha,
        "suggestion_count": count,
        "prompt_profile": prompt_profile,
    });
    let serialized = pyjson::dumps(&payload, pyjson::COMPACT_SORTED_ASCII);
    format!(
        "suggestion-v1:{}",
        hex::encode(Sha256::digest(serialized.as_bytes()))
    )
}

/// `_provider_cache_model_id`.
fn provider_cache_model_id(provider: &dyn Provider, mode: &str) -> String {
    let mode = mode.trim().to_lowercase();
    let local = matches!(mode.as_str(), "ollama" | "local");
    let model = if local {
        effective_model(provider, "suggest")
    } else {
        provider
            .model()
            .map(|m| m.trim().to_owned())
            .unwrap_or_default()
    };
    let or_default = |m: &str| {
        if m.is_empty() {
            "default".to_owned()
        } else {
            m.to_owned()
        }
    };
    match mode.as_str() {
        "ollama" | "local" => format!("ollama:{}", or_default(&model)),
        "github-copilot" | "github" | "copilot" => format!("github-copilot:{}", or_default(&model)),
        "codex-cli" | "codex" => format!("codex-cli:{}", or_default(&model)),
        "claude-code" | "claude" => format!("claude-code:{}", or_default(&model)),
        "litellm" | "lite-llm" => format!("litellm:{}", or_default(&model)),
        "plugin" | "external-plugin" | "model-plugin" => {
            format!(
                "plugin:{}",
                if model.is_empty() {
                    provider.class_name().to_owned()
                } else {
                    model
                }
            )
        }
        "mock" => "mock-local".into(),
        "stub" => "stub".into(),
        _ if model.is_empty() => provider.class_name().into(),
        _ => model,
    }
}

/// `_provider_model_context`.
fn provider_model_context(provider: &dyn Provider, mode: &str) -> Map<String, Value> {
    if !matches!(
        mode.trim().to_lowercase().as_str(),
        "ollama" | "local" | "litellm" | "plugin" | "external-plugin"
    ) {
        return Map::new();
    }
    let model = provider.model().unwrap_or_default();
    let suggest_model = provider
        .suggest_model()
        .map(|m| m.trim().to_owned())
        .filter(|m| !m.is_empty());
    let effective = Some(effective_model(provider, "suggest"))
        .filter(|m| !m.is_empty())
        .unwrap_or_else(|| model.clone());
    Map::from_iter([
        ("model".to_owned(), json!(model)),
        ("suggest_model".to_owned(), json!(suggest_model)),
        ("effective_suggest_model".to_owned(), json!(effective)),
    ])
}

/// `_configured_ollama_model_context`.
fn configured_ollama_context(
    config: &ProviderConfig,
    suggest_model: Option<&str>,
) -> Map<String, Value> {
    let model = std::env::var("VERLATO_OLLAMA_MODEL")
        .ok()
        .filter(|m| !m.is_empty())
        .or_else(|| config.model.clone())
        .unwrap_or_else(|| "gemma4:latest".into());
    let resolved = match suggest_model {
        Some(explicit) => Some(explicit.to_owned()),
        None => Some(
            std::env::var("VERLATO_OLLAMA_SUGGEST_MODEL")
                .unwrap_or_else(|_| config.suggest_model.clone().unwrap_or_default()),
        ),
    }
    .map(|m| m.trim().to_owned())
    .filter(|m| !m.is_empty());
    Map::from_iter([
        ("model".to_owned(), json!(model)),
        ("suggest_model".to_owned(), json!(resolved)),
        (
            "effective_suggest_model".to_owned(),
            json!(resolved.clone().unwrap_or(model)),
        ),
    ])
}

/// `_normalized_provider_timings`.
fn provider_timings(metadata: &Map<String, Value>, model_ms: i64) -> Map<String, Value> {
    let number = |key: &str| -> Option<f64> {
        match metadata.get(key) {
            Some(Value::Number(n)) => n.as_f64(),
            Some(Value::Bool(b)) => Some(if *b { 1.0 } else { 0.0 }),
            Some(Value::String(s)) if !s.trim().is_empty() => s.trim().parse().ok(),
            _ => None,
        }
    };
    let mut timings = Map::new();
    for (source, target) in [
        ("total_duration", "ollama_total_ms"),
        ("load_duration", "load_ms"),
        ("prompt_eval_duration", "prompt_eval_ms"),
        ("eval_duration", "eval_ms"),
    ] {
        if let Some(ns) = number(source) {
            timings.insert(
                target.into(),
                json!(((ns / 1_000_000.0).round() as i64).max(0)),
            );
        }
    }
    if let Some(tokens) = number("prompt_eval_count") {
        timings.insert("prompt_tokens".into(), json!(tokens as i64));
    }
    let eval_tokens = number("eval_count").map(|t| t as i64);
    if let Some(tokens) = eval_tokens {
        timings.insert("eval_tokens".into(), json!(tokens));
    }
    if let Some(total) = timings.get("ollama_total_ms").and_then(Value::as_i64) {
        timings.insert("queue_ms".into(), json!((model_ms - total).max(0)));
    }
    if let (Some(tokens), Some(eval_ms)) =
        (eval_tokens, timings.get("eval_ms").and_then(Value::as_i64))
    {
        if eval_ms > 0 {
            timings.insert(
                "tokens_per_second".into(),
                json!(round_to(tokens as f64 / (eval_ms as f64 / 1000.0), 1)),
            );
        }
    }
    timings
}

/// `_run_suggestion_model_work` for one item.
fn run_model_work(provider: &dyn Provider, work: &Work) -> Result<WorkResult> {
    let title = work.item.content.title.as_str();
    if trace_model_enabled() {
        trace_model_line(
            &format!(
                "starting suggest provider={} title={title}",
                work.provider_label
            ),
            "request_started",
            None,
            None,
        );
    }
    trace_progress(
        "suggestion_model_request",
        &format!("Prompting model for {}", work.item.id),
        json!({"current": work.item_number, "total": work.total_targets, "unit": "item", "item_id": work.item.id,
               "batch": work.batch, "batch_total": work.batch_total, "elapsed_ms": elapsed_ms(work.started)}),
    );
    let model_start = Instant::now();
    let response: ProviderResponse = provider.summarize(&ProviderRequest {
        task: "suggest".into(),
        prompt: work.prompt.clone(),
        evidence: work.evidence.clone(),
        response_format: Value::Null,
        metadata: Map::from_iter([
            (
                "privacy_mode".to_owned(),
                work.budget
                    .get("privacy_mode")
                    .cloned()
                    .unwrap_or(Value::Null),
            ),
            (
                "request".to_owned(),
                work.evidence.get("request").cloned().unwrap_or(Value::Null),
            ),
        ]),
    });
    let model_ms = elapsed_ms(model_start);
    let timings_extra = provider_timings(&response.metadata, model_ms);
    if trace_model_enabled() {
        trace_model_line(
            &format!(
                "[verlato:model] finished suggest model={} confidence={:.2} title={title}",
                response.model, response.confidence
            ),
            "request_finished",
            Some(Map::from_iter([
                ("task".to_owned(), json!("suggest")),
                ("model".to_owned(), json!(response.model)),
                ("confidence".to_owned(), json!(response.confidence)),
                ("title".to_owned(), json!(title)),
            ])),
            None,
        );
        let mut keys: Vec<&String> = response.metadata.keys().collect();
        keys.sort();
        let metadata_text = keys
            .iter()
            .map(|k| format!("{k}={}", py_str(&response.metadata[*k])))
            .collect::<Vec<_>>()
            .join(" ");
        let suffix = if metadata_text.is_empty() {
            String::new()
        } else {
            format!(" {metadata_text}")
        };
        let mut attributes = Map::from_iter([
            ("model".to_owned(), json!(response.model)),
            ("content_chars".to_owned(), json!(py_len(&response.content))),
            (
                "reasoning_exposed".to_owned(),
                json!(!response.reasoning.is_empty()),
            ),
            (
                "reasoning_chars".to_owned(),
                json!(py_len(&response.reasoning)),
            ),
        ]);
        attributes.extend(response.metadata.clone());
        trace_model_line(
            &format!(
                "[verlato:model] response-detail model={} content_chars={} reasoning_exposed={} reasoning_chars={}{suffix}",
                response.model,
                py_len(&response.content),
                !response.reasoning.is_empty(),
                py_len(&response.reasoning)
            ),
            "response_detail",
            Some(attributes),
            None,
        );
        trace_payload("response-preview", &response.content);
        if env_is_one("VERLATO_TRACE_MODEL_REASONING") {
            trace_payload("reasoning-preview", &response.reasoning);
        }
    }
    trace_progress(
        "suggestion_model_response",
        &format!("Reading model response for {}", work.item.id),
        json!({"current": work.item_number, "total": work.total_targets, "unit": "item", "item_id": work.item.id,
               "batch": work.batch, "batch_total": work.batch_total, "elapsed_ms": elapsed_ms(work.started), "model_ms": model_ms,
               "queue_ms": timings_extra.get("queue_ms"), "ollama_total_ms": timings_extra.get("ollama_total_ms"),
               "prompt_eval_ms": timings_extra.get("prompt_eval_ms"), "eval_ms": timings_extra.get("eval_ms")}),
    );
    let reasoning = if env_is_one("VERLATO_TRACE_MODEL_REASONING") {
        response.reasoning.clone()
    } else {
        String::new()
    };
    if is_provider_error_marker(&response.content, false) {
        bail!(
            "No model suggestions were produced for draft item {}: {}",
            work.item.id,
            response.content
        );
    }
    let parse_start = Instant::now();
    let parsed = parse_suggestion_variants(&response.content, work, &response.model);
    let parse_ms = elapsed_ms(parse_start);
    if trace_model_enabled() {
        let preview = if parsed.accepted.is_empty() {
            "none".to_owned()
        } else {
            parsed
                .accepted
                .iter()
                .take(3)
                .map(|d| {
                    d["summary"]
                        .as_str()
                        .unwrap_or("")
                        .chars()
                        .take(80)
                        .collect::<String>()
                })
                .collect::<Vec<_>>()
                .join("|")
        };
        trace_model_line(
            &format!(
                "[verlato:model] decision suggestions parsed={} requested={} accepted={preview} rejected={} model={} title={title}",
                parsed.accepted.len(), work.count, parsed.rejected.len(), response.model
            ),
            "suggestion_decision",
            Some(Map::from_iter([
                ("title".to_owned(), json!(title)),
                ("model".to_owned(), json!(response.model)),
                ("parsed".to_owned(), json!(parsed.accepted.len())),
                ("requested".to_owned(), json!(work.count)),
                ("accepted".to_owned(), json!(preview)),
                ("rejected".to_owned(), json!(parsed.rejected.len())),
            ])),
            None,
        );
    }
    if parsed.accepted.is_empty() {
        bail!(
            "No model suggestions were produced for draft item {}.",
            work.item.id
        );
    }
    let mut timings = work.base_timings(model_ms, parse_ms);
    timings.extend(timings_extra);
    let mut item = work.common_item();
    let suggestions: Vec<Value> = parsed
        .accepted
        .iter()
        .map(|d| d["summary"].clone())
        .collect();
    item.insert("provider".into(), json!(response.model));
    item.insert(
        "provider_metadata".into(),
        Value::Object(response.metadata.clone()),
    );
    item.insert("provider_reasoning".into(), json!(reasoning));
    item.insert(
        "ai_response".into(),
        json!({
            "model": response.model,
            "response_chars": py_len(&response.content),
            "parse_result": "accepted",
            "accepted_count": parsed.accepted.len(),
            "rejected_count": parsed.rejected.len(),
        }),
    );
    item.insert("suggestion_details".into(), json!(parsed.accepted));
    item.insert("rejected_candidates".into(), json!(parsed.rejected));
    item.insert("suggestions".into(), json!(suggestions));
    item.insert("cache".into(), json!({"hit": false, "key": work.cache_key}));
    item.insert("timings".into(), Value::Object(timings));
    Ok(WorkResult {
        order: work.order,
        item,
        model_ms,
        parse_ms,
        model_call_count: 1,
    })
}

/// `_cached_suggestion_result`.
fn cached_result(
    work: &Work,
    cached: crate::suggestion_cache::CachedSuggestion,
) -> Option<WorkResult> {
    let mut cached_item = cached.item;
    let suggestions: Vec<String> = cached_item
        .get("suggestions")?
        .as_array()?
        .iter()
        .filter_map(Value::as_str)
        .filter(|s| !s.trim().is_empty())
        .map(str::to_owned)
        .collect();
    if suggestions.is_empty() {
        return None;
    }
    let current = current_summary(&work.item);
    for key in [
        "provider_metadata",
        "provider_reasoning",
        "timings",
        "cache",
        "batch",
    ] {
        cached_item.remove(key);
    }
    let details_by_summary: std::collections::HashMap<String, Map<String, Value>> = cached_item
        .get("suggestion_details")
        .and_then(Value::as_array)
        .map(|details| {
            details
                .iter()
                .filter_map(|d| d.as_object())
                .filter_map(|d| {
                    d.get("summary")
                        .and_then(Value::as_str)
                        .map(|s| (s.to_owned(), d.clone()))
                })
                .collect()
        })
        .unwrap_or_default();
    let details: Vec<Value> = suggestions
        .iter()
        .map(|suggestion| {
            let mut detail = details_by_summary
                .get(suggestion)
                .cloned()
                .unwrap_or_default();
            detail.insert("summary".into(), json!(suggestion));
            detail.insert(
                "similarity_to_current".into(),
                json!(summary_similarity(suggestion, &current)),
            );
            detail.insert(
                "specificity_score".into(),
                json!(summary_specificity(suggestion, &work.group)),
            );
            detail.insert(
                "comparison".into(),
                json!({"current": current, "suggested": suggestion}),
            );
            Value::Object(detail)
        })
        .collect();
    let provider = cached_item
        .get("provider")
        .filter(|p| p.is_string())
        .cloned()
        .unwrap_or_else(|| json!(work.provider_label));
    let rejected = cached_item
        .get("rejected_candidates")
        .filter(|r| r.is_array())
        .cloned()
        .unwrap_or_else(|| json!([]));
    let mut item = cached_item;
    item.extend(work.common_item());
    item.insert("provider".into(), provider);
    item.insert("ai_response".into(), json!({"parse_result": "cache-hit", "accepted_count": suggestions.len(), "rejected_count": 0}));
    item.insert("suggestion_details".into(), json!(details));
    item.insert("rejected_candidates".into(), rejected);
    item.insert("suggestions".into(), json!(suggestions));
    item.insert(
        "cache".into(),
        json!({"hit": true, "key": work.cache_key, "age_seconds": cached.age_seconds}),
    );
    item.insert("timings".into(), Value::Object(work.base_timings(0, 0)));
    Some(WorkResult {
        order: work.order,
        item,
        model_ms: 0,
        parse_ms: 0,
        model_call_count: 0,
    })
}

/// `_preflight_suggestion_result`.
fn preflight_result(work: &Work) -> WorkResult {
    let mut item = work.common_item();
    item.insert("provider".into(), json!(work.provider_label));
    item.insert(
        "request_envelope".into(),
        json!({
            "task": "suggest",
            "prompt": work.prompt,
            "evidence": work.evidence,
            "tools": [],
            "response_format": "suggestion text",
        }),
    );
    item.insert("suggestion_details".into(), json!([]));
    item.insert("rejected_candidates".into(), json!([]));
    item.insert("suggestions".into(), json!([]));
    item.insert("preflight".into(), json!(true));
    item.insert("timings".into(), Value::Object(work.base_timings(0, 0)));
    WorkResult {
        order: work.order,
        item,
        model_ms: 0,
        parse_ms: 0,
        model_call_count: 0,
    }
}

/// `_suggestion_cache_stored_item`.
fn stored_item(item: &Map<String, Value>) -> Map<String, Value> {
    let transient = [
        "batch",
        "cache",
        "provider_metadata",
        "provider_reasoning",
        "timings",
        "ai_request",
        "request_envelope",
    ];
    item.iter()
        .filter(|(k, _)| !transient.contains(&k.as_str()))
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect()
}

fn group_detail(group: &ChangeGroup) -> String {
    if let Some(pr) = &group.pr {
        return format!("PR #{}", pr.number);
    }
    group
        .commits
        .first()
        .map(|c| c.sha.chars().take(12).collect())
        .unwrap_or_else(|| "changeset".into())
}

/// `draft suggest --preview` / `--preflight` for the single strategy (ports
/// `_preview_draft_model_suggestions`).
pub fn preview_suggestions(
    ctx: &Context,
    base: &str,
    head: &str,
    args: &SuggestArgs,
) -> Result<Value> {
    if args.all_visible == args.item_id.as_deref().is_some_and(|i| !i.is_empty()) {
        bail!("Provide exactly one suggestion target: --id or --all-visible");
    }
    if args.no_cache && args.refresh_cache {
        bail!("Use --no-cache or --refresh-cache, not both");
    }
    let total_start = Instant::now();
    let settings = resolve_settings(ctx, args)?;
    let raw = &ctx.config.raw;
    let provider_config = ProviderConfig::from_raw(raw);
    let cache_read = !args.no_cache && !args.refresh_cache;
    let cache_write = !args.no_cache;
    let mut cache = if cache_read || cache_write {
        Some(JsonSuggestionCache::load(
            &settings.cache_path,
            settings.cache_ttl_days,
        )?)
    } else {
        None
    };
    let provider_label = settings.provider.clone();
    let mut model_context = if matches!(
        settings.provider.trim().to_lowercase().as_str(),
        "ollama" | "local"
    ) {
        configured_ollama_context(&provider_config, args.suggest_model.as_deref())
    } else {
        Map::new()
    };
    let batch_size = 1;
    let target_start = Instant::now();
    let groups = ctx.collect_groups(base, head)?;
    let item_id = args.item_id.clone().filter(|i| !i.is_empty());
    trace_stage(
        "target-selection",
        &format!(
            "base={base} head={head} groups={} item_id={}",
            groups.len(),
            item_id.as_deref().unwrap_or("none")
        ),
    );
    trace_progress(
        "selecting_suggestion_targets",
        "Selecting suggestion targets",
        json!({"current": 0, "total": groups.len(), "unit": "changeset"}),
    );
    let mut targets: Vec<(ChangeGroup, ChangeItem)> = Vec::new();
    for (index, group) in groups.iter().enumerate() {
        trace_progress(
            "selecting_suggestion_targets",
            "Selecting suggestion targets",
            json!({"detail": group_detail(group), "current": index, "total": groups.len(), "unit": "changeset"}),
        );
        let baseline = classify(group, base, head, None);
        if item_id.as_deref().is_some_and(|id| id != baseline.id) {
            trace_progress(
                "selecting_suggestion_targets",
                "Checked suggestion target",
                json!({"detail": baseline.id, "current": index + 1, "total": groups.len(), "unit": "changeset"}),
            );
            continue;
        }
        trace_progress(
            "selecting_suggestion_targets",
            "Selected suggestion target",
            json!({"detail": baseline.id, "current": index + 1, "total": groups.len(), "unit": "changeset", "item_id": baseline.id}),
        );
        targets.push((group.clone(), baseline));
    }
    if let (Some(id), true) = (&item_id, targets.is_empty()) {
        bail!("Could not find draft item {id} for range {base}..{head}");
    }
    let merged = ctx.merge_review_state(targets.iter().map(|(_, item)| item.clone()).collect())?;
    let merged_by_id: std::collections::HashMap<String, ChangeItem> =
        merged.iter().map(|i| (i.id.clone(), i.clone())).collect();
    if args.all_visible {
        let visible: Vec<&String> = merged.iter().filter(|i| !i.hidden).map(|i| &i.id).collect();
        targets = targets
            .into_iter()
            .filter(|(_, item)| visible.contains(&&item.id))
            .map(|(group, item)| {
                let merged = merged_by_id.get(&item.id).cloned().unwrap_or(item);
                (group, merged)
            })
            .collect();
        trace_progress(
            "filtering_visible_suggestions",
            "Filtering visible items",
            json!({"current": targets.len(), "total": merged.len(), "unit": "item"}),
        );
    } else {
        targets = targets
            .into_iter()
            .map(|(group, item)| {
                let merged = merged_by_id.get(&item.id).cloned().unwrap_or(item);
                (group, merged)
            })
            .collect();
    }
    let target_selection_ms = elapsed_ms(target_start);

    let max_items = ctx.config.max_model_items();
    let visible_count = targets.len();
    let (fanout, batches): (usize, Vec<Vec<(ChangeGroup, ChangeItem)>>) =
        if args.all_visible && max_items == 0 {
            (0, Vec::new())
        } else {
            let fanout = if args.all_visible && max_items > 0 {
                max_items as usize
            } else {
                targets.len().max(1)
            };
            let mut batches: Vec<Vec<_>> = targets.chunks(fanout).map(|c| c.to_vec()).collect();
            if batches.is_empty() {
                batches.push(Vec::new());
            }
            (fanout, batches)
        };
    if args.all_visible && max_items >= 0 && batches.len() > 1 {
        trace_stage(
            "fanout-batches",
            &format!(
                "visible_items={} fanout_guard={max_items} batch_size={fanout} batches={}",
                targets.len(),
                batches.len()
            ),
        );
    }
    let total_targets: usize = batches.iter().map(Vec::len).sum();
    let batch_total = batches.len();
    trace_progress(
        "planning_suggestions",
        "Planning model suggestions",
        json!({
        "detail": format!("{batch_total} batch(es)"), "current": 0, "total": total_targets, "unit": "item",
        "batch_size": fanout, "batch_total": batch_total, "fanout_guard": max_items}),
    );
    if trace_model_enabled() {
        let scope = if args.all_visible {
            "all-visible"
        } else {
            "single-item"
        };
        trace_model_line(
            &format!(
                "[verlato:model] planning suggest provider={provider_label} scope={scope} base={base} head={head} targets={} suggestions_per_target={} parallelism={} count_source={}",
                targets.len(), settings.count, settings.parallelism, settings.count_source
            ),
            "suggestion_plan",
            Some(Map::from_iter([
                ("provider".to_owned(), json!(provider_label)),
                ("scope".to_owned(), json!(scope)),
                ("base".to_owned(), json!(base)),
                ("head".to_owned(), json!(head)),
                ("targets".to_owned(), json!(targets.len())),
                ("suggestions_per_target".to_owned(), json!(settings.count)),
                ("parallelism".to_owned(), json!(settings.parallelism)),
                ("count_source".to_owned(), json!(settings.count_source)),
            ])),
            None,
        );
    }
    let result_doc = |model_context: &Map<String, Value>,
                      performance: Value,
                      raw_available: usize,
                      raw_sent: usize,
                      items: Vec<Value>| {
        let mut doc = Map::from_iter([
            ("base_ref".to_owned(), json!(base)),
            ("head_ref".to_owned(), json!(head)),
            ("provider".to_owned(), json!(provider_label)),
        ]);
        doc.extend(model_context.clone());
        doc.extend(Map::from_iter([
            (
                "privacy".to_owned(),
                privacy_summary(&provider_label, &settings.privacy, raw_available, raw_sent),
            ),
            ("preflight".to_owned(), json!(args.preflight)),
            ("count".to_owned(), json!(settings.count)),
            ("prompt_profile".to_owned(), json!(settings.prompt_profile)),
            ("tone".to_owned(), json!(settings.style.tone)),
            ("style_profile".to_owned(), settings.style.profile()),
            ("performance".to_owned(), performance),
            ("visible_item_count".to_owned(), json!(visible_count)),
            ("fanout_guard".to_owned(), json!(max_items)),
            ("batch_size".to_owned(), json!(fanout)),
            ("batch_count".to_owned(), json!(batch_total)),
            ("items".to_owned(), json!(items)),
        ]));
        Value::Object(doc)
    };
    #[allow(clippy::too_many_arguments)]
    let performance = |preparation_ms: i64,
                       prompt_chars: usize,
                       file_count: usize,
                       patch_chars: usize,
                       raw_available: usize,
                       model_ms: i64,
                       parse_ms: i64,
                       total_ms: i64,
                       calls: i64,
                       saved: i64| {
        json!({
            "strategy": "single",
            "parallelism": settings.parallelism,
            "target_selection_ms": target_selection_ms,
            "preparation_ms": preparation_ms,
            "prompt_chars": prompt_chars,
            "full_diff_prompt_chars": 0,
            "full_diff_file_count": file_count,
            "full_diff_patch_chars": patch_chars,
            "raw_patch_chars_available": raw_available,
            "raw_patch_chars_sent": patch_chars,
            "privacy_mode": settings.privacy,
            "model_ms": model_ms,
            "parse_ms": parse_ms,
            "total_ms": total_ms,
            "batch_size": batch_size,
            "model_call_count": calls,
            "batched_model_calls": 0,
            "single_model_calls": calls,
            "bulk_model_calls": 0,
            "fallback_single_calls": 0,
            "fallback_item_count": 0,
            "model_calls_saved": saved,
        })
    };
    trace_suggestion_event(
        "suggestion_preview_started",
        Map::from_iter([
            (
                "preview".to_owned(),
                result_doc(
                    &model_context,
                    performance(0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
                    0,
                    0,
                    vec![],
                ),
            ),
            ("total".to_owned(), json!(total_targets)),
            ("batch_size".to_owned(), json!(fanout)),
            ("batch_total".to_owned(), json!(batch_total)),
            ("fanout_guard".to_owned(), json!(max_items)),
        ]),
    );
    if batches.iter().all(Vec::is_empty) {
        trace_progress(
            "suggestions_ready",
            "No model suggestions requested",
            json!({"current": 0, "total": 0, "unit": "item"}),
        );
        if let Some(cache) = cache.as_mut().filter(|c| c.dirty) {
            cache.save()?;
        }
        return Ok(result_doc(
            &model_context,
            performance(0, 0, 0, 0, 0, 0, 0, elapsed_ms(total_start), 0, 0),
            0,
            0,
            vec![],
        ));
    }

    let provider: Option<Box<dyn Provider>>;
    let model_id: String;
    if args.preflight {
        provider = None;
        model_id = args
            .suggest_model
            .as_deref()
            .map(str::trim)
            .filter(|m| !m.is_empty())
            .map(str::to_owned)
            .or_else(|| provider_config.model.clone())
            .unwrap_or_default();
    } else {
        let built = build_provider_with(
            &provider_config,
            Some(&settings.provider),
            args.suggest_model.as_deref(),
        );
        model_context = provider_model_context(built.as_ref(), &settings.provider);
        model_id = provider_cache_model_id(built.as_ref(), &settings.provider);
        provider = Some(built);
    }

    let mut items: Vec<Value> = Vec::new();
    let (
        mut completed,
        mut preparation_ms,
        mut prompt_chars,
        mut file_count_total,
        mut patch_sent_total,
        mut raw_available_total,
    ) = (0usize, 0i64, 0usize, 0usize, 0usize, 0usize);
    let (mut model_ms_total, mut parse_ms_total, mut uncached, mut calls) =
        (0i64, 0i64, 0i64, 0i64);

    for (batch_offset_index, batch) in batches.iter().enumerate() {
        let batch_index = batch_offset_index + 1;
        trace_progress(
            "suggestion_batch",
            "Preparing suggestion batch",
            json!({
            "detail": format!("batch {batch_index}/{batch_total}"), "current": batch_index, "total": batch_total,
            "unit": "batch", "batch": batch_index, "batch_total": batch_total}),
        );
        if batch_total > 1 {
            trace_stage(
                "batch-start",
                &format!(
                    "batch={batch_index}/{batch_total} targets={} fanout_guard={max_items}",
                    batch.len()
                ),
            );
        }
        let base_order = items.len();
        let mut results: Vec<WorkResult> = Vec::new();
        let mut misses: Vec<Work> = Vec::new();
        let record = |result: &WorkResult,
                      cache: &mut Option<JsonSuggestionCache>,
                      completed: &mut usize,
                      model_ms_total: &mut i64,
                      parse_ms_total: &mut i64,
                      calls: &mut i64| {
            *completed += 1;
            *model_ms_total += result.model_ms;
            *parse_ms_total += result.parse_ms;
            *calls += result.model_call_count;
            let timings = result.item.get("timings");
            let item_id = result.item.get("item_id").cloned().unwrap_or(Value::Null);
            let label = if result.item.get("preflight").is_some() {
                format!(
                    "Prepared suggestion preflight for {}",
                    item_id.as_str().unwrap_or("")
                )
            } else {
                format!(
                    "Generated suggestion for {}",
                    item_id.as_str().unwrap_or("")
                )
            };
            trace_progress(
                "suggestion_item_done",
                &label,
                json!({
                "current": *completed, "total": total_targets, "unit": "item", "item_id": item_id,
                "batch": result.item.get("batch"), "batch_total": batch_total,
                "elapsed_ms": timings.and_then(|t| t.get("total_ms")), "model_ms": result.model_ms,
                "queue_ms": timings.and_then(|t| t.get("queue_ms"))}),
            );
            let mut event_item = result.item.clone();
            event_item.remove("batch");
            trace_suggestion_event(
                "suggestion_item_result",
                Map::from_iter([
                    ("order".to_owned(), json!(result.order)),
                    ("item_id".to_owned(), item_id.clone()),
                    ("current".to_owned(), json!(*completed)),
                    ("total".to_owned(), json!(total_targets)),
                    (
                        "batch".to_owned(),
                        result.item.get("batch").cloned().unwrap_or(Value::Null),
                    ),
                    ("batch_total".to_owned(), json!(batch_total)),
                    ("item".to_owned(), Value::Object(event_item)),
                ]),
            );
            if cache_write {
                if let (Some(cache), Some(info)) = (cache.as_mut(), result.item.get("cache")) {
                    if info.get("hit") == Some(&json!(false)) {
                        if let Some(key) = info.get("key").and_then(Value::as_str) {
                            cache.put(key, stored_item(&result.item));
                        }
                    }
                }
            }
        };
        for (offset, (group, item)) in batch.iter().enumerate() {
            let order = base_order + offset;
            let item_number = order + 1;
            let item_start = Instant::now();
            trace_progress(
                "suggestion_evidence",
                &format!("Building suggestion evidence for {}", item.id),
                json!({
                "current": item_number, "total": total_targets, "unit": "item", "item_id": item.id,
                "batch": batch_index, "batch_total": batch_total}),
            );
            let evidence_start = Instant::now();
            let files = changed_files_for_group(group);
            let diff = evidence::build(&files, Some(&group.commits));
            let evidence_ms = elapsed_ms(evidence_start);
            let hunk_count: usize = diff.representative_hunks.values().map(Vec::len).sum();
            let file_count = diff.file_statuses.len();
            trace_stage(
                "evidence-built",
                &format!("item_id={} files={file_count} hunks={hunk_count}", item.id),
            );
            trace_progress(
                "suggestion_prompt",
                &format!("Building model prompt for {}", item.id),
                json!({
                "current": item_number, "total": total_targets, "unit": "item", "item_id": item.id,
                "batch": batch_index, "batch_total": batch_total, "elapsed_ms": elapsed_ms(item_start)}),
            );
            let prompt_start = Instant::now();
            let raw_for_item = raw_patch_chars_available(&files);
            let mut evidence_value = json!({
                "title": item.content.title,
                "item_id": item.id,
                "suggestion_count": settings.count,
                "tone": settings.style.tone,
                "style_profile": settings.style.profile(),
                "change_summary": change_summary_payload(group, Some(item), &diff, &files),
                "file_count": file_count,
                "hunk_count": hunk_count,
                "privacy_mode": settings.privacy,
                "raw_patch_chars_available": raw_for_item,
                "raw_patch_chars_sent": 0,
            });
            let sources = source_evidence(&diff, group, settings.raw_patches_allowed);
            let labels: Vec<String> = group
                .pr
                .as_ref()
                .map(|pr| pr.labels.iter().map(|l| l.to_lowercase()).collect())
                .unwrap_or_default();
            let built = build_summary_prompt(&PromptInput {
                title: &item.content.title,
                labels: &labels,
                pr_body: group.pr.as_ref().map(|p| p.body.as_str()).unwrap_or(""),
                diff: &diff,
                commits: &group.commits,
                suggestion_count: settings.count as usize,
                kind: Some(&item.classification.kind),
                audience: Some(&item.classification.audience),
                style: &settings.style,
                profile: prompt_profile(&settings.prompt_profile),
                include_hunks: settings.raw_patches_allowed,
            });
            let prompt = built.prompt;
            let mut budget = built.budget;
            prompt_chars += budget
                .get("prompt_chars")
                .and_then(Value::as_u64)
                .unwrap_or(py_len(&prompt) as u64) as usize;
            let (fd_files, fd_available, fd_sent, fd_missing) =
                full_diff_counters(&files, &diff, settings.raw_patches_allowed);
            file_count_total += fd_files;
            patch_sent_total += fd_sent;
            raw_available_total += fd_available;
            for (key, value) in [
                ("strategy", json!("single")),
                ("prompt_shape", json!(SINGLE_SUGGESTION_PROMPT_SHAPE)),
                ("full_diff_file_count", json!(fd_files)),
                ("full_diff_patch_chars_available", json!(fd_available)),
                ("full_diff_patch_chars_sent", json!(fd_sent)),
                ("full_diff_missing_patch_count", json!(fd_missing)),
                ("privacy_mode", json!(settings.privacy)),
                ("raw_patch_chars_available", json!(raw_for_item)),
                ("raw_patch_chars_sent", json!(fd_sent)),
                (
                    "omitted_fields",
                    if settings.raw_patches_allowed {
                        json!([])
                    } else {
                        json!(OMITTED_PATCH_EVIDENCE)
                    },
                ),
            ] {
                budget.insert(key.into(), value);
            }
            evidence_value["strategy"] = json!("single");
            evidence_value["raw_patch_chars_sent"] = json!(fd_sent);
            let record_value = model_request_record(
                "suggest-release-note",
                Some(&provider_label),
                &settings.privacy,
                &prompt,
                &evidence_value,
                raw_for_item,
                fd_sent,
                if settings.raw_patches_allowed {
                    &[]
                } else {
                    OMITTED_PATCH_EVIDENCE
                },
                if settings.raw_patches_allowed {
                    &[]
                } else {
                    &["metadata-only mode sends ChangeSummary instead of raw diff text"]
                },
            );
            evidence_value["request"] = record_value;
            let prompt_sha = sha12(&prompt);
            let key = cache_key(
                item,
                &prompt_sha,
                &provider_label,
                &model_id,
                &settings.style,
                settings.count,
                budget
                    .get("profile")
                    .and_then(Value::as_str)
                    .filter(|p| !p.is_empty())
                    .unwrap_or("suggestion-conservative"),
                &settings.privacy,
            );
            let prompt_ms = elapsed_ms(prompt_start);
            preparation_ms += elapsed_ms(item_start);
            let files_text: Vec<&str> = diff
                .file_statuses
                .keys()
                .take(8)
                .map(String::as_str)
                .collect();
            trace_stage(
                "evidence-detail",
                &format!(
                    "item_id={} files={} source_entries={}",
                    item.id,
                    if files_text.is_empty() {
                        "none".to_owned()
                    } else {
                        files_text.join(",")
                    },
                    sources.len()
                ),
            );
            trace_stage(
                "prompt-built",
                &format!(
                    "item_id={} prompt_chars={} prompt_sha={prompt_sha}",
                    item.id,
                    py_len(&prompt)
                ),
            );
            if trace_model_enabled() {
                let get = |k: &str, d: Value| budget.get(k).cloned().unwrap_or(d);
                let s = |v: Value| py_str(&v);
                trace_model_line(
                    &format!(
                        "[verlato:model] prompt-budget profile={} chars={} files={}/{} commits={}/{} hunks={}/{} pr_body_chars={}/{} diff_summary_chars={}/{}",
                        s(get("profile", json!("summary"))), s(get("prompt_chars", json!(0))),
                        s(get("files_included", json!(0))), s(get("files_total", json!(0))),
                        s(get("commits_included", json!(0))), s(get("commits_total", json!(0))),
                        s(get("hunks_included", json!(0))), s(get("hunks_total", json!(0))),
                        s(get("pr_body_chars_included", json!(0))), s(get("pr_body_chars_total", json!(0))),
                        s(get("diff_summary_chars_included", json!(0))), s(get("diff_summary_chars_total", json!(0))),
                    ),
                    "prompt_budget",
                    Some(budget.clone()),
                    None,
                );
            }
            if !prompt.is_empty() {
                trace_payload("prompt-preview", &prompt);
            }
            let work = Work {
                order,
                item_number,
                batch: batch_index,
                batch_total,
                total_targets,
                group: group.clone(),
                item: item.clone(),
                prompt,
                evidence: evidence_value,
                source_evidence: sources,
                prompt_sha,
                budget,
                cache_key: key,
                file_count: fd_files,
                patch_chars: fd_sent,
                missing_patch_count: fd_missing,
                count: settings.count,
                style: settings.style.clone(),
                provider_label: provider_label.clone(),
                evidence_ms,
                prompt_ms,
                started: item_start,
            };
            if args.preflight {
                let result = preflight_result(&work);
                record(
                    &result,
                    &mut cache,
                    &mut completed,
                    &mut model_ms_total,
                    &mut parse_ms_total,
                    &mut calls,
                );
                results.push(result);
                continue;
            }
            if cache_read {
                let hit = cache
                    .as_mut()
                    .and_then(|c| c.get(&work.cache_key))
                    .and_then(|cached| cached_result(&work, cached));
                if let Some(result) = hit {
                    record(
                        &result,
                        &mut cache,
                        &mut completed,
                        &mut model_ms_total,
                        &mut parse_ms_total,
                        &mut calls,
                    );
                    results.push(result);
                    continue;
                }
            }
            uncached += 1;
            misses.push(work);
        }

        if let Some(provider) = provider.as_deref() {
            let fail = |work: &Work, message: &str, completed: usize| {
                trace_suggestion_event(
                    "suggestion_item_failed",
                    Map::from_iter([
                        ("order".to_owned(), json!(work.order)),
                        ("item_id".to_owned(), json!(work.item.id)),
                        ("message".to_owned(), json!(message)),
                        ("current".to_owned(), json!(completed)),
                        ("total".to_owned(), json!(total_targets)),
                        ("batch".to_owned(), json!(work.batch)),
                        ("batch_total".to_owned(), json!(batch_total)),
                    ]),
                );
            };
            if settings.parallelism <= 1 || misses.len() <= 1 {
                for work in &misses {
                    match run_model_work(provider, work) {
                        Ok(result) => {
                            record(
                                &result,
                                &mut cache,
                                &mut completed,
                                &mut model_ms_total,
                                &mut parse_ms_total,
                                &mut calls,
                            );
                            results.push(result);
                        }
                        Err(error) => {
                            fail(work, &error.to_string(), completed);
                            return Err(error);
                        }
                    }
                }
            } else {
                let workers = (settings.parallelism as usize).min(misses.len());
                let next = std::sync::atomic::AtomicUsize::new(0);
                let stop = std::sync::atomic::AtomicBool::new(false);
                let (sender, receiver) = std::sync::mpsc::channel::<(usize, Result<WorkResult>)>();
                let mut failure: Option<anyhow::Error> = None;
                std::thread::scope(|scope| {
                    for _ in 0..workers {
                        let sender = sender.clone();
                        let (misses, next, stop) = (&misses, &next, &stop);
                        scope.spawn(move || loop {
                            if stop.load(std::sync::atomic::Ordering::SeqCst) {
                                break;
                            }
                            let index = next.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                            let Some(work) = misses.get(index) else { break };
                            let outcome = run_model_work(provider, work);
                            if sender.send((index, outcome)).is_err() {
                                break;
                            }
                        });
                    }
                    drop(sender);
                    for (index, outcome) in receiver.iter() {
                        if failure.is_some() {
                            continue;
                        }
                        match outcome {
                            Ok(result) => {
                                record(
                                    &result,
                                    &mut cache,
                                    &mut completed,
                                    &mut model_ms_total,
                                    &mut parse_ms_total,
                                    &mut calls,
                                );
                                results.push(result);
                            }
                            Err(error) => {
                                fail(&misses[index], &error.to_string(), completed);
                                stop.store(true, std::sync::atomic::Ordering::SeqCst);
                                failure = Some(error);
                            }
                        }
                    }
                });
                if let Some(error) = failure {
                    return Err(error);
                }
            }
        }
        results.sort_by_key(|r| r.order);
        for result in results {
            let mut item = result.item;
            item.remove("batch");
            items.push(Value::Object(item));
        }
    }

    if let Some(cache) = cache.as_mut().filter(|c| c.dirty) {
        cache.save()?;
    }
    let total_ms = elapsed_ms(total_start);
    let saved = (uncached - calls).max(0);
    trace_progress(
        "suggestions_ready",
        "Generated suggestions",
        json!({
        "current": items.len(), "total": total_targets, "unit": "item", "elapsed_ms": total_ms, "model_ms": model_ms_total}),
    );
    Ok(result_doc(
        &model_context,
        performance(
            preparation_ms,
            prompt_chars,
            file_count_total,
            patch_sent_total,
            raw_available_total,
            model_ms_total,
            parse_ms_total,
            total_ms,
            calls,
            saved,
        ),
        raw_available_total,
        patch_sent_total,
        items,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn candidates_follow_python_rules() {
        let entry = "**Pagination fix**\n- Fixes paging.\n- Counts unchanged.";
        assert_eq!(suggestion_candidates(entry, 1), vec![entry.to_owned()]);
        assert_eq!(
            suggestion_candidates(
                "1. Adds CSV export.\n   Use --csv.\n2. Adds a CSV option.",
                2
            ),
            vec![
                "Adds CSV export.\n   Use --csv.".to_owned(),
                "Adds a CSV option.".to_owned()
            ]
        );
        assert_eq!(
            suggestion_candidates("1. Only one.", 3),
            vec!["Only one.".to_owned()]
        );
        assert_eq!(
            suggestion_candidates("First para.\n\nSecond para.", 2),
            vec!["First para.".to_owned(), "Second para.".to_owned()]
        );
        assert_eq!(
            suggestion_candidates("```\n\"Quoted.\"\n```", 1),
            vec!["Quoted.".to_owned()]
        );
    }

    #[test]
    fn similarity_matches_python() {
        assert_eq!(summary_similarity("Fixes the export", "fixes export"), 1.0);
        assert_eq!(
            summary_similarity("Adds CSV export", "Adds JSON export"),
            0.5
        );
        assert_eq!(summary_similarity("", "x"), 0.0);
    }

    #[test]
    fn strategy_routing_matches_python() {
        assert_eq!(
            effective_strategy("auto", "ollama", "metadata-only", false),
            "single"
        );
        assert_eq!(
            effective_strategy("auto", "ollama", "trusted-provider-patch", true),
            "tool-retrieval"
        );
        assert_eq!(
            effective_strategy("auto", "litellm", "trusted-provider-patch", true),
            "full-diff-json"
        );
        assert_eq!(
            effective_strategy("auto", "github-copilot", "trusted-provider-patch", true),
            "single"
        );
        assert_eq!(
            effective_strategy("paired", "plugin", "trusted-provider-patch", true),
            "paired"
        );
        assert_eq!(
            effective_strategy("tool-retrieval", "litellm", "trusted-provider-patch", true),
            "single"
        );
    }
}
