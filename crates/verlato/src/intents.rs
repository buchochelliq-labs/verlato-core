//! Commit intents (ports `verlato/core/commit_intent.py`): conventional commits a model derives
//! from a commit's diff, used to group low-signal commits, and drafted Conventional Commits
//! messages (`commit conventional`).

use indexmap::IndexMap;
use regex::Regex;
use serde_json::{json, Map, Value};
use std::sync::OnceLock;

use crate::assist::{can_send_raw_patches, model_request_record, privacy_summary};
use crate::models::{ChangedFile, Commit, CommitIntent};
use crate::providers::{Provider, ProviderRequest};
use crate::pyjson::{self, py_len, splitlines};
use crate::semantic::{
    build_semantic_diff, build_semantic_evidence, semantic_diff_for_prompt, semantic_prompt_payload,
};
use crate::vocab::{infer_area, parse_conventional_commit, subject, ConventionalCommit};

pub const COMMIT_INTENT_MIN_CONFIDENCE: f64 = 0.65;
const MAX_COMMIT_INTENT_FILES: usize = 8;
const MAX_COMMIT_INTENT_PATCH_CHARS: usize = 900;
const MAX_CONVENTIONAL_DRAFT_FILES: usize = 12;
const MAX_CONVENTIONAL_DRAFT_PATCH_FILES: usize = 16;
const MAX_CONVENTIONAL_DRAFT_PATCH_CHARS: usize = 1800;
const MAX_CONVENTIONAL_DRAFT_BODY_ITEMS: usize = 8;
const MAX_CONVENTIONAL_DRAFT_FACTS: usize = 14;
const MAX_CONVENTIONAL_DRAFT_SYMBOLS: usize = 18;
const MAX_CONVENTIONAL_DRAFT_FILE_TERMS: usize = 4;
const MAX_CONVENTIONAL_DRAFT_FILE_SYMBOLS: usize = 3;
pub const DERIVED_COMMIT_SOURCE: &str = "model-derived-diff";
pub const CONVENTIONAL_DRAFT_SOURCE: &str = "model-drafted-commit";
pub const OMITTED_COMMIT_PATCH_EVIDENCE: &[&str] = &["raw patch text", "patch excerpts"];
const ALLOWED_CONVENTIONAL_TYPES: &[&str] = &[
    "build",
    "chore",
    "ci",
    "delete",
    "deprecate",
    "deprecated",
    "docs",
    "feat",
    "fix",
    "perf",
    "refactor",
    "remove",
    "removed",
    "sec",
    "security",
    "style",
    "test",
];
const CONFIG_LANGUAGES: &[&str] = &[
    "json",
    "yaml",
    "toml",
    "ini",
    "xml",
    "properties",
    "terraform",
    "hcl",
];
const SEMANTIC_SOURCE_TEXT: &str = ", and the IntentumDiff semantic diff (semantic_review and each file's semantic_diff: meaningful changes \
with their enclosing symbols, behavior-preserving refactorings, moved code and ignored formatting). \
Treat the semantic diff as the source of truth; never describe moved code, refactorings or formatting \
as new behavior";

/// `normalize_commit_intent_mode`.
pub fn normalize_mode(value: &str) -> Option<&'static str> {
    match value.trim().to_lowercase().as_str() {
        "off" => Some("off"),
        "low-signal" => Some("low-signal"),
        _ => None,
    }
}

/// `normalize_conventional_draft_evidence_mode` (default `subject-hint`).
pub fn normalize_evidence_mode(value: &str) -> &'static str {
    match value.trim().to_lowercase().as_str() {
        "diff-only" => "diff-only",
        _ => "subject-hint",
    }
}

pub fn is_low_signal_commit(commit: &Commit) -> bool {
    parse_conventional_commit(&commit.message).is_none_or(|parsed| parsed.scope.is_none())
}

/// `effective_conventional_commit`: the authored conventional commit, completed or replaced by
/// the inferred intent; the flag says whether inferred metadata was used.
pub fn effective_conventional_commit(commit: &Commit) -> (Option<ConventionalCommit>, bool) {
    let parsed = parse_conventional_commit(&commit.message);
    let intent = commit.inferred_intent.as_ref();
    match (parsed, intent) {
        (Some(parsed), None) => (Some(parsed), false),
        (Some(parsed), Some(_)) if parsed.scope.is_some() => (Some(parsed), false),
        (Some(parsed), Some(intent)) => {
            let used = intent.scope.as_deref().is_some_and(|s| !s.is_empty()) || intent.breaking;
            (
                Some(ConventionalCommit {
                    kind: parsed.kind,
                    scope: intent.scope.clone(),
                    description: parsed.description,
                    breaking: parsed.breaking || intent.breaking,
                }),
                used,
            )
        }
        (None, Some(intent)) => (
            Some(ConventionalCommit {
                kind: intent.kind.clone(),
                scope: intent.scope.clone(),
                description: intent.description.clone(),
                breaking: intent.breaking,
            }),
            true,
        ),
        (None, None) => (None, false),
    }
}

/// `commit_semantic_payload`: the semantic review block for one commit.
pub fn commit_semantic_payload(commit: &Commit) -> Option<Value> {
    let digest = commit.semantics.as_ref()?;
    build_semantic_evidence([digest], None).map(|e| semantic_prompt_payload(&e))
}

fn patch_chars(files: &[ChangedFile]) -> usize {
    files.iter().map(|f| py_len(&f.patch)).sum()
}

fn take_chars(text: &str, limit: usize) -> String {
    text.chars().take(limit).collect()
}

fn sent_chars(files: &[ChangedFile], max_files: usize, max_chars: usize) -> usize {
    files
        .iter()
        .take(max_files)
        .map(|f| py_len(&take_chars(&f.patch, max_chars)))
        .sum()
}

fn with_semantics(commit: &Commit, payload: &mut Map<String, Value>, include_patches: bool) {
    if let Some(semantic) = commit_semantic_payload(commit) {
        payload.insert("semantic_review".into(), semantic);
        payload.insert(
            "evidence_source".into(),
            json!("intentumdiff-semantic-diff"),
        );
        payload.insert(
            "semantic_diff_detail".into(),
            json!(if include_patches {
                "detailed"
            } else {
                "structure"
            }),
        );
    }
}

fn semantic_records_by_path(commit: &Commit, detailed: bool) -> IndexMap<String, Vec<Value>> {
    let mut by_path: IndexMap<String, Vec<Value>> = IndexMap::new();
    let Some(digest) = commit.semantics.as_ref() else {
        return by_path;
    };
    for record in semantic_diff_for_prompt(&build_semantic_diff([digest], None), detailed) {
        let path = record
            .get("path")
            .map(|p| {
                p.as_str()
                    .map(str::to_owned)
                    .unwrap_or_else(|| p.to_string())
            })
            .unwrap_or_default();
        let trimmed: Map<String, Value> = record
            .as_object()
            .map(|o| {
                o.iter()
                    .filter(|(k, _)| k.as_str() != "path" && k.as_str() != "commit")
                    .map(|(k, v)| (k.clone(), v.clone()))
                    .collect()
            })
            .unwrap_or_default();
        by_path
            .entry(path)
            .or_default()
            .push(Value::Object(trimmed));
    }
    by_path
}

fn file_payload(file: &ChangedFile, include_patch: bool) -> Value {
    let mut payload = json!({
        "path": file.path,
        "status": file.status,
        "patch_chars_available": py_len(&file.patch),
        "patch_sent": include_patch,
    });
    if include_patch {
        let excerpt = take_chars(&file.patch, MAX_COMMIT_INTENT_PATCH_CHARS);
        payload["patch_chars_sent"] = json!(py_len(&excerpt));
        payload["patch_excerpt"] = json!(excerpt);
        payload["patch_truncated"] = json!(py_len(&file.patch) > MAX_COMMIT_INTENT_PATCH_CHARS);
    }
    payload
}

fn commit_file_payloads(commit: &Commit, include_patches: bool) -> Vec<Value> {
    let files = commit.files.iter().take(MAX_COMMIT_INTENT_FILES);
    if commit.semantics.is_none() {
        return files.map(|f| file_payload(f, include_patches)).collect();
    }
    let by_path = semantic_records_by_path(commit, include_patches);
    files
        .map(|file| {
            json!({
                "path": file.path,
                "status": file.status,
                "patch_chars_available": py_len(&file.patch),
                "patch_sent": false,
                "semantic_diff": by_path.get(&file.path).cloned().unwrap_or_default(),
            })
        })
        .collect()
}

fn file_status_counts(files: &[ChangedFile]) -> Value {
    let mut counts: std::collections::BTreeMap<String, usize> = Default::default();
    for file in files {
        let status = if file.status.is_empty() {
            "modified"
        } else {
            &file.status
        };
        *counts.entry(status.to_owned()).or_default() += 1;
    }
    json!(counts)
}

/// `commit_diff_intent_evidence`.
pub fn commit_diff_intent_evidence(commit: &Commit, include_patches: bool) -> Value {
    let semantic = commit.semantics.is_some();
    let mut payload = json!({
        "schema": "verlato.commit-diff-intent-evidence.v1",
        "sha": commit.sha,
        "previous_sha": commit.parent_sha,
        "initial_commit": commit.parent_sha.is_none(),
        "files": commit_file_payloads(commit, include_patches),
        "files_total": commit.files.len(),
        "file_status_counts": file_status_counts(&commit.files),
        "raw_patch_chars_available": patch_chars(&commit.files),
        "raw_patch_chars_sent": if include_patches && !semantic {
            sent_chars(&commit.files, MAX_COMMIT_INTENT_FILES, MAX_COMMIT_INTENT_PATCH_CHARS)
        } else {
            0
        },
        "omitted_fields": if include_patches && !semantic { Vec::<&str>::new() } else { OMITTED_COMMIT_PATCH_EVIDENCE.to_vec() },
    });
    with_semantics(commit, payload.as_object_mut().unwrap(), include_patches);
    payload
}

fn source_text(base: &str, payload: &Value, include_patches: bool, patches: &str) -> String {
    let mut text = base.to_owned();
    if payload.get("evidence_source").and_then(Value::as_str) == Some("intentumdiff-semantic-diff")
    {
        text.push_str(SEMANTIC_SOURCE_TEXT);
    } else if include_patches {
        text.push_str(patches);
    }
    text
}

/// `build_diff_conventional_commit_prompt`.
pub fn build_diff_conventional_commit_prompt(
    payload: &Value,
    include_patches: bool,
    privacy_mode: &str,
) -> String {
    let source = source_text(
        "changed file paths, statuses, patch sizes, and deterministic diff metadata",
        payload,
        include_patches,
        ", including bounded patch excerpts",
    );
    format!(
        "Derive the conventional commit that best describes this exact commit diff.\n\
         Privacy mode: {privacy_mode}. Use only the provided {source}.\n\
         The authored git commit message is intentionally not provided; do not infer from commit titles or PR text.\n\
         The diff is from previous_sha to sha. If initial_commit is true, treat the diff as the initial tree.\n\
         Return strict JSON matching the schema. Prefer a concise scope such as api, ui, cli, docs, tests, infra, \
         mcp, vscode, config, auth, release, or cache when supported by the diff evidence.\n\
         Do not invent product behavior that is not supported by the diff evidence.\n\n\
         COMMIT_DIFF_EVIDENCE:\n{}",
        pyjson::dumps_indented(payload, 2)
    )
}

/// `commit_intent_response_format`.
pub fn commit_intent_response_format() -> Value {
    json!({
        "type": "object",
        "additionalProperties": false,
        "properties": {
            "type": {"type": "string", "enum": ALLOWED_CONVENTIONAL_TYPES},
            "scope": {"type": ["string", "null"]},
            "description": {"type": "string"},
            "breaking": {"type": "boolean"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "rationale": {"type": "string"},
        },
        "required": ["type", "scope", "description", "breaking", "confidence", "rationale"],
    })
}

/// `conventional_commit_draft_response_format`.
pub fn conventional_commit_draft_response_format() -> Value {
    let mut schema = commit_intent_response_format();
    schema["properties"]["body"] = json!({
        "type": "array",
        "items": {"type": "string"},
        "maxItems": MAX_CONVENTIONAL_DRAFT_BODY_ITEMS,
    });
    schema["required"] = json!([
        "type",
        "scope",
        "description",
        "body",
        "breaking",
        "confidence",
        "rationale"
    ]);
    schema
}

// ---------------------------------------------------------------------------
// response parsing
// ---------------------------------------------------------------------------

/// Python `str(value)` for JSON values the parsers coerce.
fn py_str(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::String(s) => s.clone(),
        Value::Number(n) if n.is_f64() => pyjson::float_repr(n.as_f64().unwrap_or(0.0)),
        other => other.to_string(),
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

/// `str(value or "")`.
fn text_or_empty(value: Option<&Value>) -> String {
    match value {
        Some(v) if truthy(v) => py_str(v),
        _ => String::new(),
    }
}

fn parse_json_object(content: &str) -> Option<Map<String, Value>> {
    static OPEN: OnceLock<Regex> = OnceLock::new();
    static CLOSE: OnceLock<Regex> = OnceLock::new();
    let mut text = content.trim().to_owned();
    if text.starts_with("```") {
        let open = OPEN.get_or_init(|| Regex::new(r"(?i)^```(?:json)?\s*").unwrap());
        let close = CLOSE.get_or_init(|| Regex::new(r"\s*```$").unwrap());
        text = open.replace(&text, "").trim().to_owned();
        text = close.replace(&text, "").trim().to_owned();
    }
    match serde_json::from_str::<Value>(&text) {
        Ok(Value::Object(map)) => Some(map),
        _ => None,
    }
}

fn normalized_type(value: Option<&Value>) -> Option<String> {
    let normalized = text_or_empty(value).trim().to_lowercase();
    ALLOWED_CONVENTIONAL_TYPES
        .contains(&normalized.as_str())
        .then_some(normalized)
}

fn normalized_scope(value: Option<&Value>) -> Option<String> {
    static SCOPE: OnceLock<Regex> = OnceLock::new();
    let value = value.filter(|v| !v.is_null())?;
    let normalized = py_str(value).trim().to_lowercase();
    if normalized.is_empty() {
        return None;
    }
    let normalized = normalized.replace('\\', "/");
    SCOPE
        .get_or_init(|| Regex::new(r"^[a-z0-9][a-z0-9/_-]{0,48}$").unwrap())
        .is_match(&normalized)
        .then_some(normalized)
}

fn normalized_description(value: Option<&Value>) -> Option<String> {
    let description = text_or_empty(value).trim().to_owned();
    (!description.is_empty()).then(|| take_chars(&description, 160))
}

fn coerce_confidence(value: Option<&Value>) -> f64 {
    match value {
        Some(Value::Bool(b)) => {
            if *b {
                1.0
            } else {
                0.0
            }
        }
        Some(Value::Number(n)) => n.as_f64().unwrap_or(0.0).clamp(0.0, 1.0),
        Some(Value::String(s)) => s
            .trim()
            .parse::<f64>()
            .map(|v| v.clamp(0.0, 1.0))
            .unwrap_or(0.0),
        _ => 0.0,
    }
}

fn coerce_bool(value: Option<&Value>) -> bool {
    match value {
        Some(Value::Bool(b)) => *b,
        Some(Value::String(s)) => matches!(s.trim().to_lowercase().as_str(), "1" | "true" | "yes"),
        Some(other) => truthy(other),
        None => false,
    }
}

/// `_unique_preserve_order`: stripped, case-insensitively unique, in order.
fn unique_preserve_order(values: &[String]) -> Vec<String> {
    let mut seen = std::collections::HashSet::new();
    let mut result = Vec::new();
    for value in values {
        let normalized = value.trim();
        if normalized.is_empty() || !seen.insert(normalized.to_lowercase()) {
            continue;
        }
        result.push(normalized.to_owned());
    }
    result
}

fn normalized_body(value: Option<&Value>) -> Vec<String> {
    static BULLET: OnceLock<Regex> = OnceLock::new();
    static SPACES: OnceLock<Regex> = OnceLock::new();
    let candidates: Vec<String> = match value {
        Some(Value::String(s)) => splitlines(s).into_iter().map(str::to_owned).collect(),
        Some(Value::Array(items)) => items.iter().map(py_str).collect(),
        _ => Vec::new(),
    };
    let bullet = BULLET.get_or_init(|| Regex::new(r"^\s*[-*]\s+").unwrap());
    let spaces = SPACES.get_or_init(|| Regex::new(r"\s+").unwrap());
    let body: Vec<String> = candidates
        .iter()
        .map(|c| {
            let stripped = bullet.replace(c, "").trim().to_owned();
            take_chars(&spaces.replace_all(&stripped, " "), 220)
        })
        .filter(|c| !c.is_empty())
        .collect();
    let mut unique = unique_preserve_order(&body);
    unique.truncate(MAX_CONVENTIONAL_DRAFT_BODY_ITEMS);
    unique
}

fn parse_intent(
    raw: &Map<String, Value>,
    model: &str,
    min_confidence: f64,
    source: &str,
) -> Option<CommitIntent> {
    let kind = normalized_type(raw.get("type"))?;
    let description = normalized_description(raw.get("description"))?;
    let confidence = coerce_confidence(raw.get("confidence"));
    if confidence < min_confidence {
        return None;
    }
    let raw_scope = raw.get("scope");
    let scope = normalized_scope(raw_scope);
    if let Some(raw_scope) = raw_scope.filter(|v| !v.is_null()) {
        if !py_str(raw_scope).trim().is_empty() && scope.is_none() {
            return None;
        }
    }
    Some(CommitIntent {
        kind,
        scope,
        description,
        breaking: coerce_bool(raw.get("breaking")),
        confidence,
        model: model.to_owned(),
        source: source.to_owned(),
        rationale: take_chars(text_or_empty(raw.get("rationale")).trim(), 300),
    })
}

/// `parse_commit_intent_response`.
pub fn parse_commit_intent_response(
    content: &str,
    model: &str,
    min_confidence: f64,
    source: &str,
) -> Option<CommitIntent> {
    parse_intent(&parse_json_object(content)?, model, min_confidence, source)
}

/// `_is_provider_error` (commit-intent flavour).
fn is_provider_error(content: &str) -> bool {
    let lowered = content.trim().to_lowercase();
    lowered.starts_with('[')
        && ["error", "unavailable", "empty"]
            .iter()
            .any(|m| lowered.contains(m))
}

/// `DiffConventionalCommitDeriver.derive`: the conventional commit the model derives from the
/// diff alone (no authored message).
pub fn derive_conventional_commit(
    commit: &Commit,
    provider: &dyn Provider,
    privacy_mode: &str,
    provider_mode: &str,
) -> Option<CommitIntent> {
    let provider_mode = provider_mode.trim().to_lowercase();
    let include_patches = can_send_raw_patches(&provider_mode, privacy_mode);
    let evidence = commit_diff_intent_evidence(commit, include_patches);
    let prompt = build_diff_conventional_commit_prompt(&evidence, include_patches, privacy_mode);
    let raw_available = patch_chars(&commit.files);
    let raw_sent = if include_patches {
        sent_chars(
            &commit.files,
            MAX_COMMIT_INTENT_FILES,
            MAX_COMMIT_INTENT_PATCH_CHARS,
        )
    } else {
        0
    };
    let request = model_request_record(
        "derive-conventional",
        Some(&provider_mode),
        privacy_mode,
        &prompt,
        &evidence,
        raw_available,
        raw_sent,
        if include_patches {
            &[]
        } else {
            OMITTED_COMMIT_PATCH_EVIDENCE
        },
        if include_patches {
            &[]
        } else {
            &["derived conventional commit is using metadata-only diff evidence"]
        },
    );
    let response = provider.summarize(&ProviderRequest {
        task: "derive-conventional".into(),
        prompt,
        evidence,
        response_format: commit_intent_response_format(),
        metadata: Map::from_iter([
            ("privacy_mode".to_owned(), json!(privacy_mode)),
            ("request".to_owned(), request),
        ]),
        ..Default::default()
    });
    if is_provider_error(&response.content) {
        return None;
    }
    parse_commit_intent_response(
        &response.content,
        &response.model,
        COMMIT_INTENT_MIN_CONFIDENCE,
        DERIVED_COMMIT_SOURCE,
    )
}

// ---------------------------------------------------------------------------
// drafted Conventional Commits messages (`commit conventional`)
// ---------------------------------------------------------------------------

fn regexes() -> &'static Patterns {
    static PATTERNS: OnceLock<Patterns> = OnceLock::new();
    PATTERNS.get_or_init(|| Patterns {
        setting: Regex::new(
            r#"(?:"(?P<quoted>[A-Za-z][A-Za-z0-9_.-]{2,})"\s*[:=]\s*(?P<quoted_value>true|false|null|-?\d+(?:\.\d+)?|"[^"]*")|(?P<bare>[A-Za-z][A-Za-z0-9_]{2,})\s*[:=]\s*(?P<bare_value>true|false|null|-?\d+(?:\.\d+)?|"[^"]*"))"#,
        )
        .unwrap(),
        string_literal: Regex::new(r#""([^"\n]{3,80})"|`([^`\n]{3,80})`|'([^'\n]{3,80})'"#).unwrap(),
        identifier: Regex::new(r"\b[A-Za-z][A-Za-z0-9_]{3,}\b").unwrap(),
        public_symbols: [
            r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\b",
            r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b",
            r"^\s*export\s+(?:default\s+)?(?:async\s+)?(?:function|class|interface|type|const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\b",
            r"^\s*(?:public\s+)?(?:static\s+)?(?:async\s+)?[A-Za-z_$][A-Za-z0-9_$<>\[\], ?]*\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(",
        ]
        .iter()
        .map(|p| Regex::new(p).unwrap())
        .collect(),
        test_names: [
            r"^\s*(?:async\s+)?def\s+(test_[A-Za-z0-9_]+)\b",
            r#"^\s*(?:test|it)\(\s*["'`]([^"'`]{3,120})["'`]"#,
        ]
        .iter()
        .map(|p| Regex::new(p).unwrap())
        .collect(),
        ui_action: Regex::new(r#"(?:data-action=|action\s*===|type:\s*)["'`]([A-Za-z][A-Za-z0-9_-]{2,80})["'`]"#).unwrap(),
        config_key: Regex::new(r#"["'`]((?:verlato|VERLATO)[A-Za-z0-9_.-]{2,})["'`]"#).unwrap(),
        whitespace: Regex::new(r"\s+").unwrap(),
    })
}

struct Patterns {
    setting: Regex,
    string_literal: Regex,
    identifier: Regex,
    public_symbols: Vec<Regex>,
    test_names: Vec<Regex>,
    ui_action: Regex,
    config_key: Regex,
    whitespace: Regex,
}

fn patch_lines(patch: &str, prefix: char) -> Vec<String> {
    let other = if prefix == '+' { '-' } else { '+' };
    let doubled = format!("{prefix}{prefix}");
    let mixed = format!("{prefix}{other}");
    let mut lines = Vec::new();
    for line in splitlines(patch) {
        if !line.starts_with(prefix) || line.starts_with(&doubled) || line.starts_with(&mixed) {
            continue;
        }
        let content = line[prefix.len_utf8()..].trim();
        if !content.is_empty() {
            lines.push(take_chars(content, 240));
        }
    }
    lines.truncate(200);
    lines
}

fn settings_from_lines(lines: &[String]) -> IndexMap<String, String> {
    let mut settings = IndexMap::new();
    for line in lines {
        for found in regexes().setting.captures_iter(line) {
            let Some(name) = found.name("quoted").or_else(|| found.name("bare")) else {
                continue;
            };
            let value = found
                .name("quoted_value")
                .or_else(|| found.name("bare_value"))
                .map(|m| m.as_str())
                .unwrap_or("");
            let value = take_chars(value.trim().trim_matches('"'), 80);
            settings.insert(name.as_str().to_owned(), value);
        }
    }
    settings
}

const NOTABLE_MARKERS: &[&str] = &[
    "suggest",
    "strategy",
    "batch",
    "cache",
    "warm",
    "instant",
    "provisional",
    "confidence",
    "provider",
    "model",
    "mcp",
    "release",
    "control",
    "setting",
    "privacy",
    "prompt",
    "preview",
    "remote",
    "patch",
    "raw",
    "preflight",
    "request",
    "envelope",
    "allow",
    "full-diff",
    "paired",
    "single",
    "auto",
];

fn is_notable_term(term: &str) -> bool {
    let normalized = term.trim();
    if normalized.is_empty() || py_len(normalized) > 80 {
        return false;
    }
    let lowered = normalized.to_lowercase();
    let noise = [
        "const",
        "return",
        "string",
        "number",
        "boolean",
        "undefined",
        "private",
        "public",
        "function",
    ];
    if noise.contains(&lowered.as_str()) {
        return false;
    }
    NOTABLE_MARKERS.iter().any(|m| lowered.contains(m))
}

fn notable_terms(lines: &[String]) -> Vec<String> {
    let patterns = regexes();
    let mut terms = Vec::new();
    for line in lines {
        for found in patterns.string_literal.captures_iter(line) {
            for index in 1..=3 {
                if let Some(group) = found.get(index).filter(|g| !g.as_str().is_empty()) {
                    terms.push(group.as_str().to_owned());
                }
            }
        }
        terms.extend(
            patterns
                .identifier
                .find_iter(line)
                .map(|m| m.as_str().to_owned()),
        );
    }
    let mut notable: Vec<String> = unique_preserve_order(&terms)
        .into_iter()
        .filter(|t| is_notable_term(t))
        .collect();
    notable.truncate(MAX_CONVENTIONAL_DRAFT_SYMBOLS);
    notable
}

fn first_match(patterns: &[Regex], line: &str) -> Option<String> {
    patterns.iter().find_map(|p| {
        p.captures(line)
            .and_then(|c| c.get(1))
            .map(|m| m.as_str().to_owned())
    })
}

fn public_symbols(lines: &[String]) -> Vec<String> {
    let found: Vec<String> = lines
        .iter()
        .filter_map(|l| first_match(&regexes().public_symbols, l))
        .collect();
    unique_preserve_order(&found)
}

fn test_names(lines: &[String]) -> Vec<String> {
    let found: Vec<String> = lines
        .iter()
        .filter_map(|l| first_match(&regexes().test_names, l))
        .map(|n| regexes().whitespace.replace_all(&n, " ").trim().to_owned())
        .collect();
    unique_preserve_order(&found)
}

fn config_keys(lines: &[String]) -> Vec<String> {
    let patterns = regexes();
    let mut keys = Vec::new();
    for line in lines {
        keys.extend(
            patterns
                .config_key
                .captures_iter(line)
                .map(|c| c[1].to_owned()),
        );
        for identifier in patterns.identifier.find_iter(line) {
            let lowered = identifier.as_str().to_lowercase();
            if ["privacy", "patch", "provider"]
                .iter()
                .any(|m| lowered.contains(m))
            {
                keys.push(identifier.as_str().to_owned());
            }
        }
        for found in patterns.setting.captures_iter(line) {
            if let Some(name) = found.name("quoted").or_else(|| found.name("bare")) {
                let lowered = name.as_str().to_lowercase();
                if lowered.contains("privacy")
                    || lowered.contains("patch")
                    || lowered.contains("provider")
                {
                    keys.push(name.as_str().to_owned());
                }
            }
        }
    }
    unique_preserve_order(&keys)
}

fn ui_actions(lines: &[String]) -> Vec<String> {
    let mut actions = Vec::new();
    for line in lines {
        actions.extend(
            regexes()
                .ui_action
                .captures_iter(line)
                .map(|c| c[1].to_owned()),
        );
        if line.contains("showWarningMessage") {
            actions.push("showWarningMessage".into());
        }
        if line.contains("Prompt Preview") || line.to_lowercase().contains("prompt preview") {
            actions.push("promptPreview".into());
        }
    }
    unique_preserve_order(&actions)
}

fn truncated(mut values: Vec<String>, limit: usize) -> Vec<String> {
    values.truncate(limit);
    values
}

fn theme_facts(
    files: &[&ChangedFile],
    added: &[String],
    removed: &[String],
    settings: &[Value],
) -> Vec<&'static str> {
    let paths = files
        .iter()
        .map(|f| f.path.to_lowercase())
        .collect::<Vec<_>>()
        .join(" ");
    let added = added
        .iter()
        .map(|t| t.to_lowercase())
        .collect::<Vec<_>>()
        .join(" ");
    let removed = removed
        .iter()
        .map(|t| t.to_lowercase())
        .collect::<Vec<_>>()
        .join(" ");
    let setting_names = settings
        .iter()
        .map(|f| f.get("name").map(py_str).unwrap_or_default().to_lowercase())
        .collect::<Vec<_>>()
        .join(" ");
    let combined = format!("{paths} {added} {removed} {setting_names}");
    let has = |text: &str, needles: &[&str]| needles.iter().any(|n| text.contains(n));
    let mut themes = Vec::new();
    if has(
        &combined,
        &[
            "modelprivacy",
            "trusted-provider-patch",
            "local-summary-then-ai",
        ],
    ) {
        themes.push("adds model privacy settings for AI evidence");
    }
    if has(
        &combined,
        &[
            "allowremotepatches",
            "raw_patch_chars_sent",
            "patch_excerpt",
        ],
    ) {
        themes.push("gates raw patch evidence by privacy and provider mode");
    }
    if has(&combined, &["--privacy", "allow-remote-patches"]) {
        themes.push("adds CLI privacy flags");
    }
    if has(&combined, &["preflight", "request_envelope"]) {
        themes.push("adds preflight request envelope preview");
    }
    if has(
        &combined,
        &["draftpromptpreview", "promptpreview", "prompt preview"],
    ) {
        themes.push("updates prompt preview with AI request and privacy details");
    }
    if has(
        &combined,
        &[
            "showwarningmessage",
            "send patch evidence",
            "allow remote patches",
        ],
    ) {
        themes.push("adds confirmation for sending raw patch evidence");
    }
    if has(
        &combined,
        &["ai_request", "model_request_record", "request_hash"],
    ) {
        themes.push("records model request privacy metadata");
    }
    if format!("{added}{setting_names}").contains("suggestionstrategy")
        || added.contains("full-diff-json")
    {
        themes.push("adds configurable suggestion strategy modes");
    }
    if setting_names.contains("suggestionbatchsize") {
        themes.push("changes suggestion batch-size defaults or settings");
    }
    if removed.contains("instant") || removed.contains("provisional") {
        themes.push("removes instant provisional suggestion behavior");
    }
    if has(&paths, &["releasecontrolview", "releasecontrolmarkup"]) {
        themes.push("updates the VS Code release control UI");
    }
    if has(&paths, &["commands", "registercommands"]) {
        themes.push("updates VS Code commands");
    }
    if paths.contains("test") {
        themes.push("updates tests for the changed behavior");
    }
    if has(&paths, &["readme", "architecture.md"]) {
        themes.push("updates documentation");
    }
    themes.truncate(MAX_CONVENTIONAL_DRAFT_FACTS);
    themes
}

fn diff_facts(files: &[&ChangedFile]) -> Value {
    let mut added_settings: IndexMap<String, String> = IndexMap::new();
    let mut removed_settings: IndexMap<String, String> = IndexMap::new();
    let (mut added_terms, mut removed_terms, mut symbols, mut tests, mut configs, mut actions) = (
        Vec::new(),
        Vec::new(),
        Vec::new(),
        Vec::new(),
        Vec::new(),
        Vec::new(),
    );
    let mut patch_files = 0;
    for file in files {
        if file.patch.is_empty() {
            continue;
        }
        patch_files += 1;
        let added = patch_lines(&file.patch, '+');
        let removed = patch_lines(&file.patch, '-');
        for (k, v) in settings_from_lines(&added) {
            added_settings.insert(k, v);
        }
        for (k, v) in settings_from_lines(&removed) {
            removed_settings.insert(k, v);
        }
        added_terms.extend(notable_terms(&added));
        removed_terms.extend(notable_terms(&removed));
        symbols.extend(
            public_symbols(&added)
                .into_iter()
                .map(|s| format!("{}:{s}", file.path)),
        );
        tests.extend(test_names(&added));
        configs.extend(config_keys(&added));
        actions.extend(ui_actions(&added));
    }
    let mut keys: Vec<&String> = added_settings
        .keys()
        .chain(removed_settings.keys())
        .collect();
    keys.sort();
    keys.dedup();
    let mut changed: Vec<Value> = Vec::new();
    for key in keys {
        match (removed_settings.get(key), added_settings.get(key)) {
            (Some(before), Some(after)) if before != after => {
                changed.push(json!({"name": key, "from": before, "to": after}))
            }
            (None, Some(after)) => changed.push(json!({"name": key, "added": after})),
            (Some(before), _) => changed.push(json!({"name": key, "removed": before})),
            _ => {}
        }
    }
    let themes = theme_facts(files, &added_terms, &removed_terms, &changed);
    json!({
        "patch_files": patch_files,
        "changed_settings": changed.iter().take(MAX_CONVENTIONAL_DRAFT_FACTS).collect::<Vec<_>>(),
        "added_terms": truncated(unique_preserve_order(&added_terms), MAX_CONVENTIONAL_DRAFT_FACTS),
        "removed_terms": truncated(unique_preserve_order(&removed_terms), MAX_CONVENTIONAL_DRAFT_FACTS),
        "public_symbols": truncated(unique_preserve_order(&symbols), MAX_CONVENTIONAL_DRAFT_FACTS),
        "test_names": truncated(unique_preserve_order(&tests), MAX_CONVENTIONAL_DRAFT_FACTS),
        "config_keys": truncated(unique_preserve_order(&configs), MAX_CONVENTIONAL_DRAFT_FACTS),
        "ui_actions": truncated(unique_preserve_order(&actions), MAX_CONVENTIONAL_DRAFT_FACTS),
        "themes": themes,
    })
}

fn semantic_diff_facts(commit: &Commit, files: &[&ChangedFile], detailed: bool) -> Value {
    let digest = commit.semantics.as_ref().expect("semantic commit");
    let (mut added_terms, mut removed_terms, mut symbols, mut tests, mut configs) =
        (Vec::new(), Vec::new(), Vec::new(), Vec::new(), Vec::new());
    let mut changed: Vec<Value> = Vec::new();
    for file in &digest.files {
        let is_test = file.path.to_lowercase().contains("test");
        for symbol in &file.symbols {
            if ["added", "signature", "renamed"].contains(&symbol.change.as_str()) {
                if is_test {
                    tests.push(symbol.name.clone());
                } else {
                    symbols.push(format!("{}:{}", file.path, symbol.name));
                }
            }
        }
        for entry in &file.changes {
            let edits = entry
                .get("edits")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            for edit in edits {
                let name = edit
                    .get("name")
                    .filter(|v| truthy(v))
                    .or_else(|| entry.get("symbol").filter(|v| truthy(v)));
                let Some(name) = name.map(py_str) else {
                    continue;
                };
                let change = edit.get("change").map(py_str).unwrap_or_default();
                if change == "ADDITION" {
                    added_terms.push(name.clone());
                } else if change == "DELETION" {
                    removed_terms.push(name.clone());
                }
                if CONFIG_LANGUAGES.contains(&file.language.as_str())
                    && entry.get("group").and_then(Value::as_str) == Some("meaningful")
                {
                    configs.push(name.clone());
                    let mut setting = json!({"name": name, "change": change.to_lowercase()});
                    if let Some(detail) = edit.get("detail").filter(|v| detailed && truthy(v)) {
                        setting["detail"] = json!(py_str(detail));
                    }
                    changed.push(setting);
                }
            }
        }
    }
    let themes = theme_facts(files, &added_terms, &removed_terms, &changed);
    json!({
        "source": "intentumdiff",
        "semantic_files": digest.files.len(),
        "changed_settings": changed.iter().take(MAX_CONVENTIONAL_DRAFT_FACTS).collect::<Vec<_>>(),
        "added_terms": truncated(unique_preserve_order(&added_terms), MAX_CONVENTIONAL_DRAFT_FACTS),
        "removed_terms": truncated(unique_preserve_order(&removed_terms), MAX_CONVENTIONAL_DRAFT_FACTS),
        "public_symbols": truncated(unique_preserve_order(&symbols), MAX_CONVENTIONAL_DRAFT_FACTS),
        "test_names": truncated(unique_preserve_order(&tests), MAX_CONVENTIONAL_DRAFT_FACTS),
        "config_keys": truncated(unique_preserve_order(&configs), MAX_CONVENTIONAL_DRAFT_FACTS),
        "ui_actions": [],
        "themes": themes,
    })
}

fn is_low_value_draft_file(path: &str) -> bool {
    let normalized = path.replace('\\', "/").to_lowercase();
    let name = normalized.rsplit('/').next().unwrap_or("");
    normalized.starts_with(".verlato_")
        || normalized.contains("/.verlato_")
        || name == ".verlato_state.json"
        || name == ".verlato_suggestion_cache.json.lock"
        || name.ends_with(".vsix")
}

fn draft_file_rank(file: &ChangedFile) -> (i64, String) {
    let path = file.path.replace('\\', "/").to_lowercase();
    let mut rank = 0;
    if path.contains("vscode-extension/src") {
        rank -= 50;
    }
    if path.starts_with("verlato/") {
        rank -= 45;
    }
    if [
        "commit_intent",
        "model_privacy",
        "releasecontrol",
        "commands",
        "config",
    ]
    .iter()
    .any(|p| path.contains(p))
    {
        rank -= 25;
    }
    if path.contains("test") {
        rank -= 10;
    }
    if path.ends_with(".md") || path.ends_with(".lock") || path.contains("changelog") {
        rank += 20;
    }
    if is_low_value_draft_file(&file.path) {
        rank += 80;
    }
    (rank, path)
}

fn notable_paths(files: &[&ChangedFile]) -> Vec<String> {
    let score = |file: &ChangedFile| {
        let path = file.path.to_lowercase();
        let mut rank = 0i64;
        if path.contains("vscode-extension/src") {
            rank -= 30;
        }
        if path.contains("releasecontrol") || path.contains("settings") || path.contains("config") {
            rank -= 20;
        }
        if path.contains("test") {
            rank -= 8;
        }
        if path.ends_with(".md")
            || path.ends_with(".lock")
            || path.ends_with(".vsix")
            || path.contains("release.md")
        {
            rank += 25;
        }
        (rank, path)
    };
    let mut sorted: Vec<&&ChangedFile> = files.iter().collect();
    sorted.sort_by_key(|f| score(f));
    sorted
        .into_iter()
        .take(30)
        .map(|f| f.path.clone())
        .collect()
}

fn draft_file_payload(file: &ChangedFile, include_patch: bool) -> Value {
    let added = patch_lines(&file.patch, '+');
    let removed = patch_lines(&file.patch, '-');
    let mut payload = json!({
        "path": file.path,
        "area": infer_area(&file.path),
        "status": file.status,
        "patch_chars_available": py_len(&file.patch),
        "patch_sent": include_patch,
        "added_terms": truncated(notable_terms(&added), MAX_CONVENTIONAL_DRAFT_FILE_TERMS),
        "removed_terms": truncated(notable_terms(&removed), MAX_CONVENTIONAL_DRAFT_FILE_TERMS),
        "public_symbols": truncated(public_symbols(&added), MAX_CONVENTIONAL_DRAFT_FILE_SYMBOLS),
        "test_names": truncated(test_names(&added), MAX_CONVENTIONAL_DRAFT_FILE_SYMBOLS),
        "config_keys": truncated(config_keys(&added), MAX_CONVENTIONAL_DRAFT_FILE_SYMBOLS),
        "ui_actions": truncated(ui_actions(&added), MAX_CONVENTIONAL_DRAFT_FILE_SYMBOLS),
    });
    if include_patch {
        let excerpt = take_chars(&file.patch, MAX_CONVENTIONAL_DRAFT_PATCH_CHARS);
        payload["patch_chars_sent"] = json!(py_len(&excerpt));
        payload["patch_excerpt"] = json!(excerpt);
        payload["patch_truncated"] =
            json!(py_len(&file.patch) > MAX_CONVENTIONAL_DRAFT_PATCH_CHARS);
    }
    payload
}

fn semantic_draft_file_payloads(
    commit: &Commit,
    files: &[&ChangedFile],
    detailed: bool,
) -> Vec<Value> {
    let by_path = semantic_records_by_path(commit, detailed);
    let mut symbols_by_path: IndexMap<String, Vec<String>> = IndexMap::new();
    if let Some(digest) = &commit.semantics {
        for file in &digest.files {
            symbols_by_path.insert(
                file.path.clone(),
                file.symbols
                    .iter()
                    .filter(|s| ["added", "signature", "renamed"].contains(&s.change.as_str()))
                    .map(|s| s.name.clone())
                    .collect(),
            );
        }
    }
    files
        .iter()
        .map(|file| {
            json!({
                "path": file.path,
                "area": infer_area(&file.path),
                "status": file.status,
                "patch_chars_available": py_len(&file.patch),
                "patch_sent": false,
                "public_symbols": truncated(symbols_by_path.get(&file.path).cloned().unwrap_or_default(), MAX_CONVENTIONAL_DRAFT_FILE_SYMBOLS),
                "semantic_diff": by_path.get(&file.path).cloned().unwrap_or_default(),
            })
        })
        .collect()
}

/// `conventional_commit_draft_evidence`.
pub fn conventional_commit_draft_evidence(
    commit: &Commit,
    include_patches: bool,
    evidence_mode: &str,
) -> Value {
    let evidence_mode = normalize_evidence_mode(evidence_mode);
    let signal: Vec<&ChangedFile> = commit
        .files
        .iter()
        .filter(|f| !is_low_value_draft_file(&f.path))
        .collect();
    let fact_files: Vec<&ChangedFile> = if signal.is_empty() {
        commit.files.iter().collect()
    } else {
        signal
    };
    let mut ranked = fact_files.clone();
    ranked.sort_by_key(|f| draft_file_rank(f));
    ranked.truncate(MAX_CONVENTIONAL_DRAFT_FILES);
    let semantic = commit.semantics.is_some();
    let mut by_area: std::collections::BTreeMap<&str, Vec<String>> = Default::default();
    for file in &fact_files {
        by_area
            .entry(infer_area(&file.path))
            .or_default()
            .push(file.path.clone());
    }
    let by_area: Map<String, Value> = by_area
        .into_iter()
        .map(|(area, paths)| (area.to_owned(), json!(truncated(paths, 20))))
        .collect();
    let fact_owned: Vec<ChangedFile> = fact_files.iter().map(|f| (*f).clone()).collect();
    let mut payload = json!({
        "schema": "verlato.conventional-commit-draft-evidence.v1",
        "evidence_mode": evidence_mode,
        "sha": commit.sha,
        "previous_sha": commit.parent_sha,
        "initial_commit": commit.parent_sha.is_none(),
        "authored_message_omitted": evidence_mode == "diff-only",
        "files": if semantic {
            semantic_draft_file_payloads(commit, &ranked, include_patches)
        } else {
            ranked
                .iter()
                .enumerate()
                .map(|(index, file)| draft_file_payload(file, include_patches && index < MAX_CONVENTIONAL_DRAFT_PATCH_FILES))
                .collect()
        },
        "files_total": commit.files.len(),
        "files_analyzed": fact_files.len(),
        "files_included": ranked.len(),
        "omitted_files_count": commit.files.len().saturating_sub(ranked.len()),
        "file_status_counts": file_status_counts(&fact_owned),
        "files_by_area": by_area,
        "notable_paths": notable_paths(&fact_files),
        "diff_facts": if semantic { semantic_diff_facts(commit, &fact_files, include_patches) } else { diff_facts(&fact_files) },
        "raw_patch_chars_available": patch_chars(&commit.files),
        "raw_patch_chars_sent": if include_patches && !semantic {
            sent_chars(&commit.files, MAX_CONVENTIONAL_DRAFT_PATCH_FILES, MAX_CONVENTIONAL_DRAFT_PATCH_CHARS)
        } else {
            0
        },
        "omitted_fields": if include_patches && !semantic { Vec::<&str>::new() } else { OMITTED_COMMIT_PATCH_EVIDENCE.to_vec() },
    });
    if evidence_mode == "subject-hint" {
        payload["authored_subject"] = json!(subject(&commit.message));
        payload["authored_message"] = json!(commit.message);
    }
    with_semantics(commit, payload.as_object_mut().unwrap(), include_patches);
    payload
}

/// `build_conventional_commit_draft_prompt`.
pub fn build_conventional_commit_draft_prompt(
    payload: &Value,
    include_patches: bool,
    privacy_mode: &str,
    evidence_mode: &str,
) -> String {
    let evidence_mode = normalize_evidence_mode(evidence_mode);
    let base = if evidence_mode == "subject-hint" {
        "authored subject, changed file paths, statuses, and deterministic facts extracted from the diff"
    } else {
        "changed file paths, statuses, and deterministic facts extracted from the diff"
    };
    let source = source_text(
        base,
        payload,
        include_patches,
        ", plus bounded patch excerpts",
    );
    let subject_instruction = if evidence_mode == "diff-only" {
        "The authored git commit message is intentionally not provided; infer from commit ID and diff evidence only.\n"
    } else {
        "Use the authored subject as a hint, but let the commit diff decide the type, scope, description, and body.\n"
    };
    format!(
        "Draft a full Conventional Commits message for this exact existing git commit.\n\
         Privacy mode: {privacy_mode}. Use only the provided {source}.\n\
         Evidence mode: {evidence_mode}.\n\
         {subject_instruction}\
         Return strict JSON matching the schema. The body array should contain 3-7 concrete, supported bullets for \
         non-trivial commits and [] for tiny commits. Do not prefix body strings with '-' or '*'.\n\
         Prefer `refactor` when behavior is reorganized, removed, or renamed without a clear user-facing feature; \
         prefer `feat` for new user-facing capability, `fix` for bug repairs, `docs` for documentation-only changes, \
         and `test` only when the commit is primarily tests.\n\
         Use a concise scope such as api, cli, config, docs, mcp, release, tests, ui, or vscode when supported.\n\
         Do not invent behavior that is not supported by the evidence.\n\n\
         COMMIT_DRAFT_EVIDENCE:\n{}",
        pyjson::dumps(payload, pyjson::COMPACT_SORTED_ASCII)
    )
}

/// `draft_conventional_commit`: a full Conventional Commits message for one commit, as the
/// `ConventionalCommitDraft.to_dict()` payload, or `None` when the model gave no usable draft.
pub fn draft_conventional_commit(
    commit: &Commit,
    provider: &dyn Provider,
    privacy_mode: &str,
    provider_mode: &str,
    evidence_mode: &str,
) -> Option<Value> {
    let provider_mode = provider_mode.trim().to_lowercase();
    let evidence_mode = normalize_evidence_mode(evidence_mode);
    let include_patches = can_send_raw_patches(&provider_mode, privacy_mode);
    let evidence = conventional_commit_draft_evidence(commit, include_patches, evidence_mode);
    let prompt = build_conventional_commit_draft_prompt(
        &evidence,
        include_patches,
        privacy_mode,
        evidence_mode,
    );
    let raw_available = patch_chars(&commit.files);
    let raw_sent = if include_patches {
        sent_chars(
            &commit.files,
            MAX_CONVENTIONAL_DRAFT_PATCH_FILES,
            MAX_CONVENTIONAL_DRAFT_PATCH_CHARS,
        )
    } else {
        0
    };
    let mut request = model_request_record(
        "draft-conventional",
        Some(&provider_mode),
        privacy_mode,
        &prompt,
        &evidence,
        raw_available,
        raw_sent,
        if include_patches {
            &[]
        } else {
            OMITTED_COMMIT_PATCH_EVIDENCE
        },
        if include_patches {
            &[]
        } else {
            &["conventional commit drafting is using structured diff facts"]
        },
    );
    request["evidence_mode"] = json!(evidence_mode);
    request["authored_message_omitted"] = json!(evidence_mode == "diff-only");
    let response = provider.summarize(&ProviderRequest {
        task: "draft-conventional".into(),
        prompt,
        evidence,
        response_format: conventional_commit_draft_response_format(),
        metadata: Map::from_iter([
            ("privacy_mode".to_owned(), json!(privacy_mode)),
            ("request".to_owned(), request.clone()),
        ]),
        ..Default::default()
    });
    if is_provider_error(&response.content) {
        return None;
    }
    let raw = parse_json_object(&response.content)?;
    let intent = parse_intent(
        &raw,
        &response.model,
        COMMIT_INTENT_MIN_CONFIDENCE,
        CONVENTIONAL_DRAFT_SOURCE,
    )?;
    let body_value = raw.get("body").or_else(|| raw.get("body_bullets"));
    let body = normalized_body(body_value);
    let header = intent.message();
    let message = if body.is_empty() {
        header.clone()
    } else {
        format!(
            "{header}\n\n{}",
            body.iter()
                .map(|l| format!("- {l}"))
                .collect::<Vec<_>>()
                .join("\n")
        )
    };
    let mut payload = json!({
        "sha": commit.sha,
        "previous_sha": commit.parent_sha,
        "initial_commit": commit.parent_sha.is_none(),
        "authored_subject": if evidence_mode == "subject-hint" { subject(&commit.message) } else { "" },
        "evidence_mode": evidence_mode,
    });
    if let Value::Object(intent_payload) = intent.payload() {
        for (key, value) in intent_payload {
            payload[key] = value;
        }
    }
    payload["header"] = json!(header);
    payload["message"] = json!(message);
    payload["body"] = json!(body);
    payload["privacy"] = privacy_summary(&provider_mode, privacy_mode, raw_available, raw_sent);
    payload["ai_request"] = request;
    payload["provider_metadata"] = Value::Object(response.metadata.clone());
    payload["provider_reasoning"] = json!(response.reasoning);
    Some(payload)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_intents_like_python() {
        let intent = parse_commit_intent_response(
            "```json\n{\"type\": \"Feat\", \"scope\": \"API\", \"description\": \" adds export \", \"breaking\": \"yes\", \"confidence\": 0.9, \"rationale\": \"r\"}\n```",
            "m",
            0.65,
            DERIVED_COMMIT_SOURCE,
        )
        .unwrap();
        assert_eq!(intent.message(), "feat(api)!: adds export");
        assert!(parse_commit_intent_response("{\"type\": \"feat\", \"scope\": \"Bad Scope\", \"description\": \"x\", \"confidence\": 1}", "m", 0.65, "s").is_none());
        assert!(parse_commit_intent_response(
            "{\"type\": \"feat\", \"description\": \"x\", \"confidence\": 0.5}",
            "m",
            0.65,
            "s"
        )
        .is_none());
    }

    #[test]
    fn patch_lines_skip_headers() {
        let lines = patch_lines("+++ b/x\n+ added line\n++x\n+-y\n- removed", '+');
        assert_eq!(lines, vec!["added line".to_owned()]);
    }
}
