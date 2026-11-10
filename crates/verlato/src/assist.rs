//! Model assists for classification (ports `Classifier._apply_provider_assists` and the
//! helpers it uses from `verlato/core/classifier.py`, `core/change_summary.py` and
//! `core/model_privacy.py`). The model may rewrite the summary and, on the diff-fallback path,
//! the classification; deterministic and IntentumDiff decisions keep precedence.

use regex::Regex;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::sync::OnceLock;

use crate::evidence::DiffEvidence;
use crate::models::{ChangeGroup, ChangedFile, Commit};
use crate::providers::{is_provider_error_marker, Provider, ProviderRequest};
use crate::pyjson::{self, py_len};
use crate::semantic::{render_semantic_diff, semantic_diff_for_prompt, semantic_prompt_payload};
use crate::vocab::{normalize_change_kind, subject, SECTION_ORDER};

pub const DEFAULT_MODEL_PRIVACY: &str = "metadata-only";
const MODEL_PRIVACY_CHOICES: &[&str] = &[
    "metadata-only",
    "local-summary-then-ai",
    "trusted-provider-patch",
];
pub const OMITTED_PATCH_EVIDENCE: &[&str] = &[
    "raw patch text",
    "patch excerpts",
    "representative diff hunks",
];
const RAW_PATCH_FIELDS: &[&str] = &[
    "patch",
    "patch_excerpt",
    "representative_hunks",
    "hunks",
    "diff_hunks",
];
const RAW_PATCH_MARKERS: &[&str] = &["diff --git", "\n@@", "@@ ", "\n+++", "\n---"];

// Summary prompt budget (`SUMMARY_PROMPT_PROFILE`).
const PROMPT_CHANGED_FILES: usize = 12;
const PROMPT_COMMITS: usize = 3;
const PROMPT_PR_BODY_CHARS: usize = 500;
const PROMPT_DIFF_SUMMARY_CHARS: usize = 650;
const PROMPT_HUNK_FILES: usize = 3;
const PROMPT_HUNKS_PER_FILE: usize = 2;
const PROMPT_HUNK_CHARS: usize = 220;

const MAX_CHANGE_SUMMARY_COMMITS: usize = 12;
const MAX_CHANGE_SUMMARY_FILES: usize = 80;
const MAX_PUBLIC_SYMBOLS_PER_FILE: usize = 8;

const LOW_DETAIL_SUGGESTIONS: &[&str] = &[
    "change",
    "changes",
    "update",
    "updates",
    "updated wording",
    "improve",
    "improvements",
    "suggested wording",
];

pub fn normalize_model_privacy(value: &str) -> String {
    let normalized = value.trim().to_lowercase();
    if MODEL_PRIVACY_CHOICES.contains(&normalized.as_str()) {
        normalized
    } else {
        DEFAULT_MODEL_PRIVACY.into()
    }
}

/// `resolve_model_privacy`: remote-patch opt-in > CLI > `VERLATO_MODEL_PRIVACY` > config.
pub fn resolve_model_privacy(
    configured: Option<&str>,
    requested: Option<&str>,
    allow_remote_patches: bool,
) -> String {
    if allow_remote_patches {
        return "trusted-provider-patch".into();
    }
    let env = std::env::var("VERLATO_MODEL_PRIVACY").ok();
    let chosen = [requested, env.as_deref(), configured]
        .into_iter()
        .flatten()
        .find(|v| !v.is_empty())
        .unwrap_or(DEFAULT_MODEL_PRIVACY);
    normalize_model_privacy(chosen)
}

fn sha256_hex(text: &str) -> String {
    hex::encode(Sha256::digest(text.as_bytes()))
}

fn stable_json_hash(value: &Value) -> String {
    sha256_hex(&pyjson::dumps(value, pyjson::COMPACT_SORTED_ASCII))
}

fn estimate_token_count(chars: usize) -> usize {
    chars.div_ceil(4)
}

fn contains_raw_patch_markers(value: &Value) -> bool {
    match value {
        Value::Object(map) => map.iter().any(|(key, item)| {
            RAW_PATCH_FIELDS.contains(&key.as_str()) || contains_raw_patch_markers(item)
        }),
        Value::Array(items) => items.iter().any(contains_raw_patch_markers),
        Value::String(text) => RAW_PATCH_MARKERS.iter().any(|marker| text.contains(marker)),
        _ => false,
    }
}

fn sorted_unique(values: &[&str]) -> Vec<String> {
    let mut values: Vec<String> = values.iter().map(|v| (*v).to_owned()).collect();
    values.sort();
    values.dedup();
    values
}

/// Ports `model_request_record` (the audit record attached to model requests).
#[allow(clippy::too_many_arguments)]
pub fn model_request_record(
    task: &str,
    provider: Option<&str>,
    privacy_mode: &str,
    prompt: &str,
    evidence: &Value,
    raw_patch_chars_available: usize,
    raw_patch_chars_sent: usize,
    omitted_fields: &[&str],
    omission_reasons: &[&str],
) -> Value {
    let privacy = normalize_model_privacy(privacy_mode);
    let prompt_hash = sha256_hex(prompt);
    let evidence_hash = stable_json_hash(evidence);
    let evidence_chars = py_len(&pyjson::dumps(evidence, pyjson::DEFAULT_SORTED));
    let prompt_chars = py_len(prompt);
    json!({
        "task": task,
        "provider": provider.filter(|p| !p.is_empty()).unwrap_or("configured"),
        "model": null,
        "privacy_mode": privacy,
        "request_hash": stable_json_hash(&json!({
            "task": task,
            "provider": provider,
            "model": null,
            "privacy_mode": privacy,
            "prompt_hash": prompt_hash,
            "evidence_hash": evidence_hash,
        })),
        "prompt_hash": prompt_hash,
        "evidence_hash": evidence_hash,
        "prompt_chars": prompt_chars,
        "evidence_chars": evidence_chars,
        "estimated_tokens": estimate_token_count(prompt_chars + evidence_chars),
        "raw_patch_chars_available": raw_patch_chars_available,
        "raw_patch_chars_sent": raw_patch_chars_sent,
        "omitted_fields": sorted_unique(omitted_fields),
        "omission_reasons": sorted_unique(omission_reasons),
        "contains_raw_patch_markers": contains_raw_patch_markers(&json!({"prompt": prompt, "evidence": evidence})),
    })
}

// ---------------------------------------------------------------------------
// change summary payload (ports `build_change_summary_payload` for scan-time evidence)
// ---------------------------------------------------------------------------

const LOCAL_PATCH_PROVIDERS: &[&str] = &["ollama", "local", "stub", "mock"];

/// `can_send_raw_patches`: whether a privacy mode lets this provider see source content.
pub fn can_send_raw_patches(provider_mode: &str, privacy_mode: &str) -> bool {
    match normalize_model_privacy(privacy_mode).as_str() {
        "local-summary-then-ai" => {
            LOCAL_PATCH_PROVIDERS.contains(&provider_mode.trim().to_lowercase().as_str())
        }
        "trusted-provider-patch" => true,
        _ => false,
    }
}

/// `_can_send_raw_patches_for_config`: local-only modes require every provider in the chain
/// (fallbacks included) to be local.
pub fn can_send_raw_patches_for_config(
    config: &crate::providers::ProviderConfig,
    provider_mode: Option<&str>,
    privacy_mode: &str,
) -> bool {
    let mode = provider_mode
        .filter(|m| !m.is_empty())
        .unwrap_or(&config.name)
        .to_owned();
    if !can_send_raw_patches(&mode, privacy_mode) {
        return false;
    }
    if normalize_model_privacy(privacy_mode) != "local-summary-then-ai" {
        return true;
    }
    config.fallbacks.iter().all(|fallback| {
        let name = fallback
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_owned();
        can_send_raw_patches(&name, privacy_mode)
    })
}

/// `privacy_summary`.
pub fn privacy_summary(
    provider: &str,
    privacy_mode: &str,
    raw_patch_chars_available: usize,
    raw_patch_chars_sent: usize,
) -> Value {
    let sends = raw_patch_chars_sent > 0;
    let mut will_send = vec![
        "change summary",
        "PR metadata",
        "commit metadata",
        "file paths and statuses",
        "deterministic diff facts",
    ];
    if sends {
        will_send.push("raw patches");
    }
    json!({
        "mode": normalize_model_privacy(privacy_mode),
        "provider": if provider.is_empty() { "configured" } else { provider },
        "raw_patch_chars_available": raw_patch_chars_available,
        "raw_patch_chars_sent": raw_patch_chars_sent,
        "will_send": will_send,
        "will_not_send": if sends { Vec::<&str>::new() } else { vec!["raw patches", "patch excerpts", "representative hunks"] },
    })
}

pub fn raw_patch_chars_available(files: &[ChangedFile]) -> usize {
    files.iter().map(|f| py_len(&f.patch)).sum()
}

fn looks_like_docs_path(path: &str) -> bool {
    let p = path.to_lowercase();
    p.starts_with("docs/") || p.ends_with(".md") || p.contains("readme")
}

fn looks_like_test_path(path: &str) -> bool {
    let p = path.to_lowercase();
    p.contains("/test") || p.contains("\\test") || p.starts_with("test") || p.contains("_test.")
}

fn looks_like_build_path(path: &str) -> bool {
    let p = path.to_lowercase();
    [
        "package.json",
        "pyproject.toml",
        "cargo.toml",
        "go.mod",
        "pom.xml",
        "build.gradle",
    ]
    .iter()
    .any(|n| p.ends_with(n))
        || p.contains(".github/workflows/")
}

fn looks_like_config_path(path: &str) -> bool {
    let p = path.to_lowercase();
    [".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"]
        .iter()
        .any(|e| p.ends_with(e))
        || p.starts_with(".github/")
}

fn looks_like_api_path(path: &str) -> bool {
    let p = path.to_lowercase();
    ["/api/", "\\api\\", "/sdk/", "\\sdk\\", "client", "provider"]
        .iter()
        .any(|s| p.contains(s))
}

fn sorted(values: &[String]) -> Vec<String> {
    let mut values = values.to_vec();
    values.sort();
    values
}

fn commit_payload(commit: &Commit) -> Value {
    let (effective, inferred_used) = crate::intents::effective_conventional_commit(commit);
    let conventional = effective.map(|parsed| {
        json!({
            "type": parsed.kind,
            "scope": parsed.scope,
            "description": parsed.description,
            "breaking": parsed.breaking,
            "source": if inferred_used { "inferred" } else { "authored" },
        })
    });
    json!({
        "sha": commit.sha,
        "subject": subject(&commit.message),
        "message": commit.message,
        "author": commit.author,
        "previous_sha": commit.parent_sha,
        "initial_commit": commit.parent_sha.is_none(),
        "authored_or_inferred_conventional": conventional,
        "inferred_intent": commit.inferred_intent.as_ref().map(|i| i.payload()),
        "derived_conventional": commit.derived_intent.as_ref().map(|i| i.payload()),
        "files_total": commit.files.len(),
    })
}

fn boundary_payload(commit: &Commit) -> Value {
    json!({
        "sha": commit.sha,
        "subject": subject(&commit.message),
        "previous_sha": commit.parent_sha,
        "initial_commit": commit.parent_sha.is_none(),
        "derived_conventional": commit.derived_intent.as_ref().map(|i| i.message()),
    })
}

fn commit_boundaries(commits: &[Commit]) -> Value {
    match (commits.first(), commits.last()) {
        (Some(first), Some(last)) => {
            json!({"first": boundary_payload(first), "last": boundary_payload(last)})
        }
        _ => json!({"first": null, "last": null}),
    }
}

fn semantic_public_symbols(diff: &DiffEvidence) -> BTreeMap<String, Vec<String>> {
    let mut result: BTreeMap<String, Vec<String>> = BTreeMap::new();
    let Some(semantic) = &diff.semantic else {
        return result;
    };
    for entry in semantic
        .added_symbols
        .iter()
        .chain(&semantic.signature_changes)
        .chain(&semantic.renamed_symbols)
    {
        let (name, path) = entry.split_once(" in ").unwrap_or((entry.as_str(), ""));
        let name = name.rsplit(" -> ").next().unwrap_or(name).to_owned();
        let symbols = result.entry(path.to_owned()).or_default();
        if !symbols.contains(&name) && symbols.len() < MAX_PUBLIC_SYMBOLS_PER_FILE {
            symbols.push(name);
        }
    }
    result
}

fn public_symbols_from_patches(files: &[ChangedFile]) -> BTreeMap<String, Vec<String>> {
    static PATTERNS: OnceLock<Vec<Regex>> = OnceLock::new();
    let patterns = PATTERNS.get_or_init(|| {
        [
            r"^\+\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\b",
            r"^\+\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b",
            r"^\+\s*export\s+(?:default\s+)?(?:async\s+)?(?:function|class|interface|type|const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\b",
            r"^\+\s*(?:public\s+)?(?:static\s+)?(?:async\s+)?[A-Za-z_$][A-Za-z0-9_$<>\[\], ?]*\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(",
        ]
        .iter()
        .map(|p| Regex::new(p).expect("symbol pattern"))
        .collect()
    });
    let mut result = BTreeMap::new();
    let mut files_with_symbols = 0;
    for file in files {
        if files_with_symbols >= 25 {
            break;
        }
        let mut symbols: Vec<String> = Vec::new();
        for line in pyjson::splitlines(&file.patch) {
            if !line.starts_with('+') || line.starts_with("+++") {
                continue;
            }
            if let Some(found) = patterns.iter().find_map(|p| p.captures(line)) {
                let symbol = found[1].to_owned();
                if !symbols.contains(&symbol) {
                    symbols.push(symbol);
                }
            }
            if symbols.len() >= MAX_PUBLIC_SYMBOLS_PER_FILE {
                break;
            }
        }
        if !symbols.is_empty() {
            result.insert(file.path.clone(), symbols);
            files_with_symbols += 1;
        }
    }
    result
}

pub fn change_summary_payload(
    group: &ChangeGroup,
    item: Option<&crate::models::ChangeItem>,
    diff: &DiffEvidence,
    files: &[ChangedFile],
) -> Value {
    let raw_patch_chars = raw_patch_chars_available(files);
    let mut status_counts: BTreeMap<String, usize> = BTreeMap::new();
    for file in files {
        let status = if file.status.is_empty() {
            "modified"
        } else {
            &file.status
        };
        *status_counts.entry(status.to_owned()).or_default() += 1;
    }
    let symbols = if diff.semantic.is_some() {
        semantic_public_symbols(diff)
    } else {
        public_symbols_from_patches(files)
    };
    let area_for = |path: &str| {
        diff.files_by_area
            .iter()
            .find(|(_, paths)| paths.iter().any(|p| p == path))
            .map(|(area, _)| area.clone())
    };
    let commits: Vec<Value> = group
        .commits
        .iter()
        .take(MAX_CHANGE_SUMMARY_COMMITS)
        .map(commit_payload)
        .collect();
    let file_payloads: Vec<Value> = files
        .iter()
        .take(MAX_CHANGE_SUMMARY_FILES)
        .map(|file| {
            json!({
                "path": file.path,
                "status": file.status,
                "area": area_for(&file.path),
                "patch_chars_available": py_len(&file.patch),
                "public_symbols": symbols.get(&file.path).cloned().unwrap_or_default(),
                "signals": {
                    "docs": looks_like_docs_path(&file.path),
                    "tests": looks_like_test_path(&file.path),
                    "build": looks_like_build_path(&file.path),
                    "config": looks_like_config_path(&file.path) || diff.config_changes.contains(&file.path),
                    "migration": diff.migration_changes.contains(&file.path),
                    "api": diff.api_changes.contains(&file.path) || looks_like_api_path(&file.path),
                },
            })
        })
        .collect();
    let statuses = |names: &[&str]| {
        let mut paths: Vec<String> = files
            .iter()
            .filter(|f| names.contains(&f.status.to_lowercase().as_str()))
            .map(|f| f.path.clone())
            .collect();
        paths.sort();
        paths
    };
    let paths: Vec<&str> = files.iter().map(|f| f.path.as_str()).collect();
    let facts = json!({
        "docs_only": diff.docs_only,
        "tests_only": diff.tests_only,
        "api_changes": sorted(&diff.api_changes),
        "migration_changes": sorted(&diff.migration_changes),
        "config_changes": sorted(&diff.config_changes),
        "added_files": statuses(&["added", "a"]),
        "removed_files": statuses(&["removed", "deleted", "d"]),
        "renamed_files": statuses(&["renamed", "r"]),
        "docs_changed": paths.iter().any(|p| looks_like_docs_path(p)),
        "tests_changed": paths.iter().any(|p| looks_like_test_path(p)),
        "build_changed": paths.iter().any(|p| looks_like_build_path(p)),
        "config_changed": !diff.config_changes.is_empty() || paths.iter().any(|p| looks_like_config_path(p)),
        "migration_changed": !diff.migration_changes.is_empty(),
        "api_looking_changed": !diff.api_changes.is_empty() || paths.iter().any(|p| looks_like_api_path(p)),
        "raw_patch_chars_available": raw_patch_chars,
        "raw_patch_chars_sent": 0,
    });
    let mut areas: Vec<(&String, &Vec<String>)> = diff.files_by_area.iter().collect();
    areas.sort_by(|a, b| a.0.cmp(b.0));
    let areas: Map<String, Value> = areas
        .into_iter()
        .map(|(area, paths)| (area.clone(), json!(sorted(paths))))
        .collect();
    let title = match (item, &group.pr) {
        (Some(item), _) => item.content.title.clone(),
        (None, Some(pr)) => pr.title.clone(),
        (None, None) => group
            .commits
            .first()
            .map(|c| subject(&c.message).to_owned())
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "Untitled change".into()),
    };
    let mut payload = json!({
        "schema": "verlato.change-summary.v1",
        "title": title,
        "current_summary": item.map(|i| i.content.summary.as_str()).unwrap_or(""),
        "classification": item.map(|i| json!({
            "kind": i.classification.kind,
            "area": i.classification.area,
            "audience": i.classification.audience,
            "semver": i.classification.semver,
        })),
        "pull_request": group.pr.as_ref().map(|pr| json!({
            "number": pr.number, "title": pr.title, "body": pr.body, "labels": pr.labels,
        })),
        "commit_boundaries": commit_boundaries(&group.commits),
        "commits_total": group.commits.len(),
        "commits_omitted": group.commits.len().saturating_sub(commits.len()),
        "commits": commits,
        "files_total": files.len(),
        "files_omitted": files.len().saturating_sub(file_payloads.len()),
        "files": file_payloads,
        "file_status_counts": status_counts,
        "areas": areas,
        "deterministic_facts": facts,
        "local_summary": null,
        "evidence_source": if diff.semantic.is_some() { "intentumdiff-semantic-diff" } else { "git-patch" },
        "semantic_review": diff.semantic.as_ref().map(semantic_prompt_payload),
        // Structure-level semantic diff (no literal values): this payload is privacy-filtered.
        "semantic_diff": diff.semantic.as_ref().map(|_| semantic_diff_for_prompt(&diff.semantic_diff, false)),
        "privacy": {
            "raw_patch_chars_available": raw_patch_chars,
            "raw_patch_chars_sent": 0,
            "omitted_fields": OMITTED_PATCH_EVIDENCE,
            "omission_reasons": [
                "metadata-only evidence is the default",
                "raw patch text is reserved for local or explicitly trusted patch modes",
            ],
        },
    });
    let json_chars = py_len(&pyjson::dumps(&payload, pyjson::DEFAULT_SORTED));
    payload["size"] =
        json!({"json_chars": json_chars, "estimated_tokens": estimate_token_count(json_chars)});
    payload
}

// ---------------------------------------------------------------------------
// prompts
// ---------------------------------------------------------------------------

fn truncate_prompt_text(value: &str, limit: usize) -> String {
    let normalized = value.trim();
    if py_len(normalized) <= limit {
        return normalized.to_owned();
    }
    let head: String = normalized.chars().take(limit).collect();
    format!("{}...", head.trim_end())
}

fn commit_boundary_line(commit: &Commit) -> String {
    let base = match &commit.parent_sha {
        None => "initial commit".to_owned(),
        Some(parent) => format!("previous {}", parent.chars().take(12).collect::<String>()),
    };
    let derived = commit
        .derived_intent
        .as_ref()
        .map(|i| format!("; derived {}", i.message()))
        .unwrap_or_default();
    format!(
        "{} ({base}) {}{derived}",
        commit.sha.chars().take(12).collect::<String>(),
        subject(&commit.message)
    )
}

/// `_commit_prompt_line`: the subject, annotated with inferred and derived intents.
fn commit_prompt_line(commit: &Commit) -> String {
    let mut annotations = Vec::new();
    if let Some(intent) = &commit.inferred_intent {
        annotations.push(format!("inferred {}", intent.message()));
    }
    if let Some(intent) = &commit.derived_intent {
        annotations.push(format!("derived {}", intent.message()));
    }
    let subject = subject(&commit.message);
    if annotations.is_empty() {
        subject.to_owned()
    } else {
        format!("{subject} [{}]", annotations.join(" | "))
    }
}

fn tone_instruction(tone: &str) -> &'static str {
    match tone.trim().to_lowercase().as_str() {
        "concise" => "short, direct wording with no extra qualifiers.",
        "technical" => "precise engineering wording that names APIs, files, protocols, or internal behavior when useful.",
        "customer-facing" => "plain customer-facing wording that emphasizes user-visible value and avoids internal jargon.",
        _ => "polished release-note wording for a broad product audience.",
    }
}

/// Style settings for prompts (`suggestion_tone` / `suggestion_style`, with env overrides).
#[derive(Debug, Clone, Default)]
pub struct Style {
    pub tone: String,
    pub guidance: String,
    pub vocabulary: Vec<String>,
    pub banned: Vec<String>,
    pub specificity_floor: f64,
}

fn env_list(name: &str) -> Vec<String> {
    std::env::var(name)
        .ok()
        .map(|raw| {
            raw.split(',')
                .map(|p| p.trim().to_owned())
                .filter(|p| !p.is_empty())
                .collect()
        })
        .unwrap_or_default()
}

impl Style {
    pub fn from_config(raw: &Value) -> Style {
        Self::resolve(raw, None)
    }

    /// The `style_profile` record shown with suggestion prompts.
    pub fn profile(&self) -> Value {
        json!({
            "guidance": self.guidance,
            "project_vocabulary": self.vocabulary,
            "banned_phrases": self.banned,
            "specificity_floor": self.specificity_floor,
        })
    }

    /// Style with a requested tone (`--tone`) taking precedence (`_resolve_suggestion_tone`).
    pub fn resolve(raw: &Value, requested_tone: Option<&str>) -> Style {
        let tone_choices = ["release-note", "concise", "technical", "customer-facing"];
        let configured_tone = raw
            .get("suggestion_tone")
            .filter(|v| !v.is_null())
            .map(|v| {
                v.as_str()
                    .map(str::to_owned)
                    .unwrap_or_else(|| v.to_string())
                    .trim()
                    .to_owned()
            })
            .filter(|v| !v.is_empty())
            .unwrap_or_else(|| "release-note".into());
        let tone = requested_tone
            .filter(|v| !v.is_empty())
            .map(str::to_owned)
            .or_else(|| {
                std::env::var("VERLATO_SUGGESTION_TONE")
                    .ok()
                    .filter(|v| !v.is_empty())
            })
            .unwrap_or(configured_tone)
            .trim()
            .to_lowercase();
        let style = raw.get("suggestion_style").and_then(Value::as_object);
        let list = |key: &str| -> Vec<String> {
            style
                .and_then(|s| s.get(key))
                .and_then(Value::as_array)
                .map(|items| {
                    items
                        .iter()
                        .map(|v| {
                            v.as_str()
                                .map(str::to_owned)
                                .unwrap_or_else(|| v.to_string())
                                .trim()
                                .to_owned()
                        })
                        .filter(|v| !v.is_empty())
                        .collect()
                })
                .unwrap_or_default()
        };
        let guidance = std::env::var("VERLATO_STYLE_GUIDANCE")
            .ok()
            .filter(|v| !v.is_empty())
            .or_else(|| {
                style
                    .and_then(|s| s.get("guidance"))
                    .and_then(Value::as_str)
                    .map(|g| g.trim().to_owned())
            })
            .unwrap_or_default();
        let vocabulary = Some(env_list("VERLATO_PROJECT_VOCABULARY"))
            .filter(|v| !v.is_empty())
            .unwrap_or_else(|| list("project_vocabulary"));
        let banned = Some(env_list("VERLATO_BANNED_PHRASES"))
            .filter(|v| !v.is_empty())
            .unwrap_or_else(|| list("banned_phrases"));
        Style {
            tone: if tone_choices.contains(&tone.as_str()) {
                tone
            } else {
                "release-note".into()
            },
            guidance: guidance.trim().to_owned(),
            vocabulary,
            banned,
            specificity_floor: {
                let configured = style
                    .and_then(|s| s.get("specificity_floor"))
                    .and_then(Value::as_f64)
                    .unwrap_or(0.0);
                std::env::var("VERLATO_SPECIFICITY_FLOOR")
                    .ok()
                    .map(|raw| raw.parse::<f64>().unwrap_or(configured))
                    .unwrap_or(configured)
                    .clamp(0.0, 1.0)
            },
        }
    }

    fn lines(&self) -> String {
        let mut lines = Vec::new();
        if !self.guidance.trim().is_empty() {
            lines.push(format!(
                "Project style guidance: {}",
                truncate_prompt_text(&self.guidance, 300)
            ));
        }
        let vocabulary: Vec<&str> = self
            .vocabulary
            .iter()
            .map(|t| t.trim())
            .filter(|t| !t.is_empty())
            .take(20)
            .collect();
        if !vocabulary.is_empty() {
            lines.push(format!(
                "Preferred project vocabulary: {}",
                vocabulary.join(", ")
            ));
        }
        let banned: Vec<&str> = self
            .banned
            .iter()
            .map(|t| t.trim())
            .filter(|t| !t.is_empty())
            .take(20)
            .collect();
        if !banned.is_empty() {
            lines.push(format!(
                "Avoid these phrases exactly: {}",
                banned.join(", ")
            ));
        }
        lines.join("\n")
    }
}

/// A prompt budget profile (ports `PromptBudgetProfile`).
#[derive(Debug, Clone, Copy)]
pub struct PromptProfile {
    pub name: &'static str,
    pub changed_files: usize,
    pub commits: usize,
    pub pr_body_chars: usize,
    pub diff_summary_chars: usize,
    pub hunk_files: usize,
    pub hunks_per_file: usize,
    pub hunk_chars: usize,
}

pub const SUMMARY_PROFILE: PromptProfile = PromptProfile {
    name: "summary",
    changed_files: PROMPT_CHANGED_FILES,
    commits: PROMPT_COMMITS,
    pr_body_chars: PROMPT_PR_BODY_CHARS,
    diff_summary_chars: PROMPT_DIFF_SUMMARY_CHARS,
    hunk_files: PROMPT_HUNK_FILES,
    hunks_per_file: PROMPT_HUNKS_PER_FILE,
    hunk_chars: PROMPT_HUNK_CHARS,
};

/// `_prompt_budget_profile`.
pub fn prompt_profile(name: &str) -> PromptProfile {
    let profile = |name,
                   changed_files,
                   commits,
                   pr_body_chars,
                   diff_summary_chars,
                   hunk_files,
                   hunks_per_file,
                   hunk_chars| PromptProfile {
        name,
        changed_files,
        commits,
        pr_body_chars,
        diff_summary_chars,
        hunk_files,
        hunks_per_file,
        hunk_chars,
    };
    match name.trim().to_lowercase().as_str() {
        "suggest" | "suggestion" | "conservative" | "suggestion-conservative" => {
            profile("suggestion-conservative", 8, 2, 240, 420, 2, 1, 160)
        }
        "lean" | "suggestion-lean" => profile("suggestion-lean", 5, 1, 160, 280, 1, 1, 120),
        "minimal" | "suggestion-minimal" => profile("suggestion-minimal", 3, 1, 80, 180, 1, 1, 80),
        _ => SUMMARY_PROFILE,
    }
}

/// Inputs of `_build_summary_prompt_with_budget`.
pub struct PromptInput<'a> {
    pub title: &'a str,
    pub labels: &'a [String],
    pub pr_body: &'a str,
    pub diff: &'a DiffEvidence,
    pub commits: &'a [Commit],
    pub suggestion_count: usize,
    pub kind: Option<&'a str>,
    pub audience: Option<&'a str>,
    pub style: &'a Style,
    pub profile: PromptProfile,
    pub include_hunks: bool,
}

pub struct SummaryPrompt {
    pub prompt: String,
    pub budget: Map<String, Value>,
}

/// Ports `Classifier._build_summary_prompt_with_budget`.
pub fn build_summary_prompt(input: &PromptInput) -> SummaryPrompt {
    let profile = input.profile;
    let (commits, diff) = (input.commits, input.diff);
    let commit_subjects: Vec<String> = commits
        .iter()
        .take(profile.commits)
        .map(commit_prompt_line)
        .collect();
    let mut commit_lines = if commit_subjects.is_empty() {
        "none".to_owned()
    } else {
        commit_subjects.join("; ")
    };
    if commits.len() > profile.commits {
        commit_lines.push_str(&format!(
            "; ... and {} more",
            commits.len() - profile.commits
        ));
    }
    let boundary_text = match (commits.first(), commits.last()) {
        (Some(first), Some(last)) => format!(
            "First commit: {}\nLast commit: {}",
            commit_boundary_line(first),
            commit_boundary_line(last)
        ),
        _ => "First commit: none\nLast commit: none".into(),
    };
    let label_text = if input.labels.is_empty() {
        "none".to_owned()
    } else {
        input.labels.join(", ")
    };
    let mut file_statuses: Vec<(&String, &String)> = diff.file_statuses.iter().collect();
    file_statuses.sort();
    let included_files = file_statuses.len().min(profile.changed_files);
    let mut file_lines = file_statuses
        .iter()
        .take(profile.changed_files)
        .map(|(path, status)| format!("- {status} {path}"))
        .collect::<Vec<_>>()
        .join("\n");
    if file_lines.is_empty() {
        file_lines = "- none".into();
    }
    if file_statuses.len() > profile.changed_files {
        file_lines.push_str(&format!(
            "\n- ... and {} more",
            file_statuses.len() - profile.changed_files
        ));
    }
    let total_hunk_files = diff.representative_hunks.len();
    let total_hunks: usize = diff.representative_hunks.values().map(Vec::len).sum();
    let total_hunk_chars: usize = diff
        .representative_hunks
        .values()
        .flatten()
        .map(|h| py_len(h))
        .sum();
    let (mut included_hunk_files, mut included_hunks, mut included_hunk_chars) = (0, 0, 0);
    let use_semantic_diff = diff.semantic.is_some();
    let semantic_detail = if input.include_hunks {
        "detailed"
    } else {
        "structure"
    };
    let mut semantic_text = String::new();
    let hunk_text = if use_semantic_diff {
        let budget = (profile.hunk_files * profile.hunks_per_file * profile.hunk_chars).max(1200);
        semantic_text =
            render_semantic_diff(&diff.semantic_diff, input.include_hunks, Some(budget));
        if semantic_text.is_empty() {
            "no reviewed changes".to_owned()
        } else {
            semantic_text.clone()
        }
    } else if input.include_hunks {
        let mut lines: Vec<String> = Vec::new();
        for (path, hunks) in diff.representative_hunks.iter().take(profile.hunk_files) {
            included_hunk_files += 1;
            lines.push(format!("{path}:"));
            for hunk in hunks.iter().take(profile.hunks_per_file) {
                let compact = truncate_prompt_text(hunk, profile.hunk_chars);
                included_hunks += 1;
                included_hunk_chars += py_len(&compact);
                lines.push(format!("  {compact}"));
            }
            if hunks.len() > profile.hunks_per_file {
                lines.push(format!(
                    "  ... and {} more hunk(s)",
                    hunks.len() - profile.hunks_per_file
                ));
            }
        }
        if lines.is_empty() {
            "none".to_owned()
        } else {
            lines.join("\n")
        }
    } else {
        "omitted by privacy mode; use the deterministic change summary and file/status signals"
            .into()
    };
    let raw_hunks_sent = input.include_hunks && !use_semantic_diff;
    let pr_body = input.pr_body.trim();
    let pr_body_text = Some(truncate_prompt_text(pr_body, profile.pr_body_chars))
        .filter(|t| !t.is_empty())
        .unwrap_or_else(|| "none".into());
    let diff_summary = truncate_prompt_text(&diff.summary, profile.diff_summary_chars);
    let mut context = Vec::new();
    if let Some(kind) = input.kind.filter(|k| !k.is_empty()) {
        context.push(format!("kind={kind}"));
    }
    if let Some(audience) = input.audience.filter(|a| !a.is_empty()) {
        context.push(format!("audience={audience}"));
    }
    let context_text = if context.is_empty() {
        "unspecified".to_owned()
    } else {
        context.join(", ")
    };
    let output_instruction = if input.suggestion_count <= 1 {
        "Return one complete release-note entry. Use a short paragraph or focused bullet points when that helps \
         downstream consumers understand the change; avoid padding."
            .to_owned()
    } else {
        format!(
            "Return exactly {} distinct complete release-note options as a numbered list. \
             Each option may use a short paragraph or focused bullet points when that helps downstream consumers \
             understand the change; avoid padding.",
            input.suggestion_count
        )
    };
    let style_lines = input.style.lines();
    let style_text = if style_lines.is_empty() {
        String::new()
    } else {
        format!("{style_lines}\n")
    };
    let evidence_section = if use_semantic_diff {
        format!(
            "Base the entry on the IntentumDiff semantic diff below. It lists, per file, the meaningful \
             changes (with the enclosing symbol), behavior-preserving refactorings and moved code, and \
             formatting that was ignored. Describe meaningful changes; never present moved code, \
             refactorings or formatting as new behavior.\n\
             Semantic diff (IntentumDiff, {semantic_detail}):\n{hunk_text}"
        )
    } else {
        format!("Representative hunks:\n{hunk_text}")
    };
    let prompt = format!(
        "Write a useful Verlato release-note description based on privacy-filtered change evidence.\n\
         Verlato renders the final release note as Markdown with a version heading and change-type \
         subheadings; return only the entry body for this change, without top-level Markdown headings.\n\
         Lead with what changed and why it matters. Name the observable behavior, workflow, API, docs, \
         test, or maintenance impact.\n\
         Prefer concrete active verbs such as Adds, Fixes, Clarifies, Reduces, Preserves, Documents, \
         or Validates.\n\
         Avoid generic filler, file-name summaries, and text that just repeats the title or commits.\n\
         For external/user-facing changes, use polished release-note wording. For internal, docs, tests, \
         or maintenance changes, use concise commit-like wording.\n\
         Tone: {tone}\n\
         {style_text}\
         {output_instruction}\n\
         Title: {title}\n\
         Release context: {context_text}\n\
         Labels: {label_text}\n\
         {boundary_text}\n\
         Commits: {commit_lines}\n\
         PR body: {pr_body_text}\n\
         Changed files:\n{file_lines}\n\
         Diff summary: {diff_summary}\n\
         {evidence_section}",
        tone = tone_instruction(&input.style.tone),
        title = input.title,
    );
    let pr_body_included = if pr_body_text == "none" {
        0
    } else {
        py_len(&pr_body_text)
    };
    let (pr_total, summary_total, summary_included) = (
        py_len(pr_body),
        py_len(&diff.summary),
        py_len(&diff_summary),
    );
    let budget = json!({
        "profile": profile.name,
        "prompt_chars": py_len(&prompt),
        "files_included": included_files,
        "files_total": file_statuses.len(),
        "files_skipped": file_statuses.len() - included_files,
        "commits_included": commit_subjects.len(),
        "commits_total": commits.len(),
        "commits_skipped": commits.len() - commit_subjects.len(),
        "commit_boundaries_included": !commits.is_empty(),
        "pr_body_chars_included": pr_body_included,
        "pr_body_chars_total": pr_total,
        "pr_body_chars_skipped": pr_total.saturating_sub(pr_body_included),
        "diff_summary_chars_included": summary_included,
        "diff_summary_chars_total": summary_total,
        "diff_summary_chars_skipped": summary_total.saturating_sub(summary_included),
        "hunk_files_included": included_hunk_files,
        "hunk_files_total": total_hunk_files,
        "hunk_files_skipped": total_hunk_files.saturating_sub(included_hunk_files),
        "hunks_included": included_hunks,
        "hunks_total": total_hunks,
        "hunks_skipped": total_hunks.saturating_sub(included_hunks),
        "hunk_chars_included": included_hunk_chars,
        "hunk_chars_total": total_hunk_chars,
        "hunk_chars_skipped": total_hunk_chars.saturating_sub(included_hunk_chars),
        "evidence_source": if use_semantic_diff { "intentumdiff-semantic-diff" } else { "git-patch" },
        "semantic_diff_detail": if use_semantic_diff { json!(semantic_detail) } else { Value::Null },
        "semantic_diff_chars_included": py_len(&semantic_text),
        "semantic_diff_files": diff.semantic_diff.len(),
        "raw_patch_text_included": raw_hunks_sent,
        "raw_patch_chars_sent": if raw_hunks_sent { included_hunk_chars } else { 0 },
        "omitted_fields": if raw_hunks_sent { String::new() } else { OMITTED_PATCH_EVIDENCE.join(", ") },
    });
    SummaryPrompt {
        prompt,
        budget: budget.as_object().cloned().unwrap_or_default(),
    }
}

#[allow(clippy::too_many_arguments)]
fn summary_prompt(
    title: &str,
    labels: &[String],
    pr_body: &str,
    diff: &DiffEvidence,
    commits: &[Commit],
    kind: &str,
    audience: &str,
    style: &Style,
) -> String {
    build_summary_prompt(&PromptInput {
        title,
        labels,
        pr_body,
        diff,
        commits,
        suggestion_count: 1,
        kind: Some(kind),
        audience: Some(audience),
        style,
        profile: SUMMARY_PROFILE,
        include_hunks: false,
    })
    .prompt
}

fn classification_prompt(
    title: &str,
    labels: &[String],
    diff_summary: &str,
    commits: &[Commit],
) -> String {
    let commit_lines = commits
        .iter()
        .map(commit_prompt_line)
        .collect::<Vec<_>>()
        .join("; ");
    let label_text = if labels.is_empty() {
        "none".to_owned()
    } else {
        labels.join(", ")
    };
    let mut kinds: Vec<&str> = SECTION_ORDER.to_vec();
    kinds.sort();
    format!(
        "Classify this change as kind/area/audience/semver.\n\
         Valid changelog kinds: {}.\n\
         Title: {title}\n\
         Labels: {label_text}\n\
         Commits: {commit_lines}\n\
         Diff: {diff_summary}",
        kinds.join(", ")
    )
}

fn parse_classification(content: &str) -> Option<(String, String, String, String)> {
    static RE: OnceLock<Regex> = OnceLock::new();
    let re = RE.get_or_init(|| {
        Regex::new(
            r"(?P<kind>[a-z-]+)/(?P<area>[a-z0-9_-]+)/(?P<audience>[a-z-]+)/(?P<semver>[a-z-]+)",
        )
        .unwrap()
    });
    let lowered = content.to_lowercase();
    let found = re.captures(&lowered)?;
    let kind = normalize_change_kind(&found["kind"])?;
    let audience = &found["audience"];
    let semver = &found["semver"];
    if !SECTION_ORDER.contains(&kind)
        || !["external", "internal"].contains(&audience)
        || !["major", "minor", "patch", "none"].contains(&semver)
    {
        return None;
    }
    Some((
        kind.into(),
        found["area"].into(),
        audience.into(),
        semver.into(),
    ))
}

pub fn summary_rejection(content: &str, title: &str, group: &ChangeGroup) -> Option<&'static str> {
    let normalized = content.trim();
    if normalized.is_empty() {
        return Some("empty");
    }
    let lowered = normalized.to_lowercase();
    if is_provider_error_marker(&lowered, true) {
        return Some("provider-error-marker");
    }
    if lowered.starts_with("mock summary:") {
        return Some("mock-prompt-echo");
    }
    let stripped = lowered.trim_end_matches('.');
    let mut candidates = vec![title.trim().to_lowercase()];
    candidates.extend(
        group
            .commits
            .iter()
            .map(|c| subject(&c.message).trim().to_lowercase()),
    );
    if candidates
        .iter()
        .filter(|c| !c.is_empty())
        .any(|c| c.trim_end_matches('.') == stripped)
    {
        return Some("repeats-title-or-commit");
    }
    static WORDS: OnceLock<Regex> = OnceLock::new();
    let words = WORDS
        .get_or_init(|| Regex::new("[a-z0-9]+").unwrap())
        .find_iter(&lowered)
        .count();
    if words < 3 || LOW_DETAIL_SUGGESTIONS.contains(&stripped) {
        return Some("too-short-or-generic");
    }
    None
}

fn trace(line: impl FnOnce() -> String) {
    if std::env::var("VERLATO_TRACE_MODEL_CALLS").as_deref() == Ok("1") {
        eprintln!("[verlato:model] {}", line());
    }
}

/// The classification decision the model may refine.
pub struct Decision {
    pub kind: String,
    pub area: String,
    pub audience: String,
    pub semver: String,
    pub summary: String,
    pub confidence: f64,
}

pub struct ModelAssist {
    pub provider: Box<dyn Provider>,
    /// The provider mode recorded in request audits (`--model-provider` or the config name).
    pub provider_mode: Option<String>,
    pub privacy: String,
    pub style: Style,
    /// Only rewrite the summary, never the classification (`classify(..., summary_only=True)`).
    pub summary_only: bool,
}

impl ModelAssist {
    #[allow(clippy::too_many_arguments)]
    pub fn apply(
        &self,
        group: &ChangeGroup,
        labels: &[String],
        diff: &DiffEvidence,
        files: &[ChangedFile],
        title: &str,
        decision: &mut Decision,
        signals: &mut Vec<String>,
        allow_classification_override: bool,
    ) {
        let mut evidence = json!({
            "title": title,
            "labels": labels,
            "change_summary": change_summary_payload(group, None, diff, files),
            "inferred_commit_intents": group
                .commits
                .iter()
                .filter_map(|c| c.inferred_intent.as_ref().map(|i| i.payload()))
                .collect::<Vec<_>>(),
        });
        let prompt = summary_prompt(
            title,
            labels,
            group.pr.as_ref().map(|p| p.body.as_str()).unwrap_or(""),
            diff,
            &group.commits,
            &decision.kind,
            &decision.audience,
            &self.style,
        );
        let record = model_request_record(
            "summarize",
            self.provider_mode.as_deref(),
            &self.privacy,
            &prompt,
            &evidence,
            raw_patch_chars_available(files),
            0,
            OMITTED_PATCH_EVIDENCE,
            &["classifier provider assist uses ChangeSummary evidence by default"],
        );
        evidence["request"] = record.clone();
        let metadata = Map::from_iter([
            ("privacy_mode".to_owned(), json!(self.privacy)),
            ("request".to_owned(), record),
        ]);
        trace(|| format!("starting summarize title={title}"));
        let response = self.provider.summarize(&ProviderRequest {
            task: "summarize".into(),
            prompt,
            evidence: evidence.clone(),
            response_format: Value::Null,
            metadata: metadata.clone(),
            ..Default::default()
        });
        trace(|| {
            format!(
                "summarize model={} confidence={:.2} title={title}",
                response.model, response.confidence
            )
        });
        let rejection = summary_rejection(&response.content, title, group);
        if response.confidence >= 0.5 && rejection.is_none() {
            decision.summary = response.content.clone();
            decision.confidence = decision.confidence.max(response.confidence.min(0.95));
            signals.push(format!("provider-summary:{}", response.model));
            trace(|| {
                format!(
                    "decision summary accepted model={} title={title}",
                    response.model
                )
            });
        } else if is_provider_error_marker(&response.content, false) {
            signals.push(format!(
                "provider-error:{}:{}",
                response.model, response.content
            ));
            trace(|| {
                format!(
                    "decision summary provider-error deterministic-fallback model={} title={title}",
                    response.model
                )
            });
        } else {
            let reason = rejection.unwrap_or("low-confidence");
            trace(|| {
                format!("decision summary rejected reason={reason} deterministic-fallback model={} title={title}", response.model)
            });
        }

        if !allow_classification_override {
            trace(|| {
                format!("decision classification skipped deterministic-rule-confidence model=deterministic title={title}")
            });
            return;
        }
        trace(|| format!("starting classify title={title}"));
        let response = self.provider.classify(&ProviderRequest {
            task: "classify".into(),
            prompt: classification_prompt(title, labels, &diff.summary, &group.commits),
            evidence,
            response_format: Value::Null,
            metadata,
            ..Default::default()
        });
        trace(|| {
            format!(
                "classify model={} confidence={:.2} title={title}",
                response.model, response.confidence
            )
        });
        match parse_classification(&response.content) {
            Some((kind, area, audience, semver)) if response.confidence >= 0.65 => {
                trace(|| {
                    format!("decision classification accepted value={kind}/{area}/{audience}/{semver} model={} title={title}", response.model)
                });
                decision.kind = kind;
                decision.area = area;
                decision.audience = audience;
                decision.semver = semver;
                decision.confidence = decision.confidence.max(response.confidence.min(0.98));
                signals.push(format!("provider-classification:{}", response.model));
            }
            _ => trace(|| {
                format!("decision classification rejected deterministic-fallback model={} title={title}", response.model)
            }),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classification_parsing_matches_python() {
        assert_eq!(
            parse_classification("[mock-classification] fixed/backend/external/patch"),
            Some((
                "fixed".into(),
                "backend".into(),
                "external".into(),
                "patch".into()
            ))
        );
        assert_eq!(parse_classification("changed/general/everyone/patch"), None);
    }

    #[test]
    fn truncation_counts_code_points() {
        assert_eq!(truncate_prompt_text("  ééééé  ", 3), "ééé...");
        assert_eq!(truncate_prompt_text("abc", 3), "abc");
    }

    #[test]
    fn raw_patch_markers_are_detected() {
        assert!(contains_raw_patch_markers(
            &json!({"prompt": "x\n@@ -1 +1 @@"})
        ));
        assert!(contains_raw_patch_markers(
            &json!({"evidence": {"patch": ""}})
        ));
        assert!(!contains_raw_patch_markers(
            &json!({"evidence": {"patch_chars_available": 3}})
        ));
    }
}
