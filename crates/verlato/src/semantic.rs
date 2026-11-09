//! IntentumDiff semantic evidence: per-commit digests, group aggregation, verdicts and the
//! deterministic summary. Ports `verlato/core/semantic.py`; the engine call itself lives in
//! `engine.rs`.
//!
//! Only structural metadata and definition names are kept. Source bodies, literal values and
//! the engine's raw change descriptions are never stored, so digests are safe to show in the
//! editor and to place in model prompts.

use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};

use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

use crate::vocab::is_test_path;

pub const DIFF_ENGINE_CHOICES: &[&str] = &["intentumdiff", "git"];
pub const DEFAULT_DIFF_ENGINE: &str = "intentumdiff";
pub const SEMANTIC_DIGEST_VERSION: &str = "v2";
pub const DEFAULT_SEMANTIC_CACHE_PATH: &str = ".verlato_semantic_cache.json";
pub const GROUP_KINDS: &[&str] = &[
    "MEANINGFUL_CHANGE",
    "REFACTORING",
    "MOVED_CODE",
    "IGNORED_STYLE",
    "NOISE_SUPPRESSED",
];
const MAX_SYMBOLS_PER_FILE: usize = 24;
const MAX_EVIDENCE_SYMBOLS: usize = 12;
const MAX_CROSS_FILE: usize = 12;
pub const MAX_REASON_CHARS: usize = 240;

pub fn normalize_diff_engine(value: &str) -> &'static str {
    match value.trim().to_lowercase().as_str() {
        "git" | "legacy" | "text" | "patch" => "git",
        _ => "intentumdiff",
    }
}

/// CLI request > `VERLATO_DIFF_ENGINE` > config > default.
pub fn resolve_diff_engine(configured: Option<&str>, requested: Option<&str>) -> &'static str {
    let env = std::env::var("VERLATO_DIFF_ENGINE")
        .ok()
        .filter(|v| !v.trim().is_empty());
    normalize_diff_engine(
        requested
            .map(str::to_owned)
            .or(env)
            .or(configured.map(str::to_owned))
            .as_deref()
            .unwrap_or(DEFAULT_DIFF_ENGINE),
    )
}

pub fn truncate_chars(value: &str, limit: usize) -> String {
    value.chars().take(limit).collect()
}

// ---------------------------------------------------------------------------
// digest model
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SymbolChange {
    pub name: String,
    pub change: String,
    pub file: String,
    #[serde(default)]
    pub node_type: String,
    #[serde(default)]
    pub old_name: Option<String>,
    #[serde(default)]
    pub refactoring_kind: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct FileSemantics {
    pub path: String,
    pub old_path: String,
    #[serde(default)]
    pub language: String,
    #[serde(default = "modified")]
    pub lifecycle: String,
    #[serde(default)]
    pub group_counts: BTreeMap<String, u64>,
    #[serde(default)]
    pub meaningful_change_types: BTreeMap<String, u64>,
    #[serde(default)]
    pub style_only: bool,
    #[serde(default)]
    pub has_semantic_changes: bool,
    #[serde(default)]
    pub is_fallback: bool,
    #[serde(default)]
    pub refactorings: Vec<String>,
    #[serde(default)]
    pub symbols: Vec<SymbolChange>,
    #[serde(default)]
    pub guardrails: Vec<BTreeMap<String, String>>,
    #[serde(default)]
    pub parse_errors: Vec<String>,
    /// IntentumDiff's review-level changes (the semantic diff LLM prompts use instead of raw
    /// patches); same shape as the Python `semantic_diff_entries`.
    #[serde(default)]
    pub changes: Vec<Value>,
}

fn modified() -> String {
    "modified".to_owned()
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct UnreviewedFile {
    pub path: String,
    pub reason: String,
    #[serde(default = "unreviewed")]
    pub kind: String,
}

fn unreviewed() -> String {
    "unreviewed".to_owned()
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CommitSemantics {
    pub sha: String,
    pub parent_sha: Option<String>,
    #[serde(default = "engine_name")]
    pub engine: String,
    #[serde(default)]
    pub engine_version: String,
    #[serde(default = "digest_version")]
    pub digest_version: String,
    #[serde(default)]
    pub files: Vec<FileSemantics>,
    #[serde(default)]
    pub unreviewed: Vec<UnreviewedFile>,
    #[serde(default)]
    pub cross_file: Vec<Value>,
    #[serde(default)]
    pub cross_file_error: Option<String>,
    #[serde(default)]
    pub error: Option<String>,
}

fn engine_name() -> String {
    "intentumdiff".to_owned()
}
fn digest_version() -> String {
    SEMANTIC_DIGEST_VERSION.to_owned()
}

impl CommitSemantics {
    pub fn new(sha: &str, parent_sha: Option<&str>, engine_version: &str) -> Self {
        Self {
            sha: sha.to_owned(),
            parent_sha: parent_sha.map(str::to_owned),
            engine: engine_name(),
            engine_version: engine_version.to_owned(),
            digest_version: digest_version(),
            files: Vec::new(),
            unreviewed: Vec::new(),
            cross_file: Vec::new(),
            cross_file_error: None,
            error: None,
        }
    }
}

// ---------------------------------------------------------------------------
// digest extraction from an engine review (JSON)
// ---------------------------------------------------------------------------

fn identifier() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^[A-Za-z_$@][\w$@.:\-]{0,119}$").unwrap())
}

fn str_field<'a>(value: &'a Value, key: &str) -> &'a str {
    value.get(key).and_then(Value::as_str).unwrap_or("")
}

fn node_name(node: &Value) -> String {
    str_field(node, "label").trim().to_owned()
}

/// IntentumDiff attaches privacy-safe NodeFacts only to definition nodes; data-format nodes
/// whose label is their node type or an index (`pair`, `[5]`) are not named symbols.
fn is_definition(node: Option<&Value>) -> bool {
    let Some(node) = node.filter(|n| n.is_object()) else {
        return false;
    };
    if !node.get("facts").is_some_and(Value::is_object) {
        return false;
    }
    let name = node_name(node);
    identifier().is_match(&name) && name != str_field(node, "node_type")
}

fn param_count(node: Option<&Value>) -> Option<i64> {
    node?.get("facts")?.get("param_count")?.as_i64()
}

fn change_type_group(change_type: &str) -> &'static str {
    match change_type {
        "MOVE" | "MOVE_TO_MODULE" => "MOVED_CODE",
        "REFACTORING" | "CROSS_FILE_RENAME" | "SPLIT_MODULE" => "REFACTORING",
        "STYLE_ONLY" => "IGNORED_STYLE",
        "REORDER" => "NOISE_SUPPRESSED",
        _ => "MEANINGFUL_CHANGE",
    }
}

fn push_unique(values: &mut Vec<String>, value: &str) {
    if !values.iter().any(|v| v == value) {
        values.push(value.to_owned());
    }
}

/// Reduce an engine `SemanticDiff`/`Review` JSON object to Verlato's digest.
pub fn digest_file_diff(
    review: &Value,
    path: &str,
    old_path: &str,
    lifecycle: Option<&str>,
) -> FileSemantics {
    let empty = Vec::new();
    let changes = review
        .get("changes")
        .and_then(Value::as_array)
        .unwrap_or(&empty);
    let groups = review
        .get("change_groups")
        .and_then(Value::as_array)
        .unwrap_or(&empty);
    let mut group_counts: BTreeMap<String, u64> = BTreeMap::new();
    let mut meaningful_types: BTreeMap<String, u64> = BTreeMap::new();
    let mut refactorings: Vec<String> = Vec::new();
    let mut symbols: Vec<SymbolChange> = Vec::new();
    let mut seen: Vec<(String, String)> = Vec::new();

    let mut add_symbol = |symbol: SymbolChange, symbols: &mut Vec<SymbolChange>| {
        let identity = if symbol.change == "renamed" {
            format!(
                "{}->{}",
                symbol.old_name.clone().unwrap_or_default(),
                symbol.name
            )
        } else {
            symbol.name.clone()
        };
        let key = (symbol.change.clone(), identity);
        if seen.contains(&key) || symbols.len() >= MAX_SYMBOLS_PER_FILE {
            return;
        }
        seen.push(key);
        symbols.push(symbol);
    };

    for group in groups {
        let kind = str_field(group, "kind").to_owned();
        *group_counts.entry(kind.clone()).or_default() += 1;
        let group_refactoring = str_field(group, "refactoring_kind");
        if !group_refactoring.is_empty() {
            push_unique(&mut refactorings, group_refactoring);
        }
        if kind != "MEANINGFUL_CHANGE" {
            continue;
        }
        let mut signature_symbol: Option<String> = None;
        for index in group
            .get("raw_change_indices")
            .and_then(Value::as_array)
            .unwrap_or(&empty)
        {
            let Some(change) = index.as_u64().and_then(|i| changes.get(i as usize)) else {
                continue;
            };
            *meaningful_types
                .entry(str_field(change, "change_type").to_owned())
                .or_default() += 1;
            let node = change
                .get("new_node")
                .filter(|n| n.is_object())
                .or_else(|| change.get("old_node"));
            if node.is_some_and(|n| {
                str_field(n, "node_type")
                    .to_lowercase()
                    .contains("parameter")
            }) {
                let labels = group
                    .get("new_labels")
                    .and_then(Value::as_array)
                    .filter(|l| !l.is_empty())
                    .or_else(|| group.get("old_labels").and_then(Value::as_array));
                if let Some(first) = labels
                    .and_then(|l| l.first())
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
                {
                    signature_symbol = Some(first.to_owned());
                }
            }
        }
        if let Some(name) = signature_symbol {
            add_symbol(
                SymbolChange {
                    name,
                    change: "signature".into(),
                    file: path.into(),
                    node_type: String::new(),
                    old_name: None,
                    refactoring_kind: None,
                },
                &mut symbols,
            );
        }
    }

    if groups.is_empty() && !changes.is_empty() {
        // Whole-file additions/deletions carry raw changes without review-level groups.
        for change in changes {
            let change_type = str_field(change, "change_type");
            let kind = change_type_group(change_type);
            *group_counts.entry(kind.to_owned()).or_default() += 1;
            if kind == "MEANINGFUL_CHANGE" {
                *meaningful_types.entry(change_type.to_owned()).or_default() += 1;
            }
        }
    }

    for change in changes {
        let change_type = str_field(change, "change_type");
        let old_node = change.get("old_node").filter(|n| n.is_object());
        let new_node = change.get("new_node").filter(|n| n.is_object());
        let refactoring_kind = str_field(change, "refactoring_kind");
        let symbol = |name: String,
                      change: &str,
                      node: &Value,
                      old_name: Option<String>,
                      refactoring: Option<String>| SymbolChange {
            name,
            change: change.into(),
            file: path.into(),
            node_type: str_field(node, "node_type").into(),
            old_name,
            refactoring_kind: refactoring,
        };
        if change_type == "ADDITION" && is_definition(new_node) {
            let node = new_node.unwrap();
            add_symbol(
                symbol(node_name(node), "added", node, None, None),
                &mut symbols,
            );
        } else if change_type == "DELETION" && is_definition(old_node) {
            let node = old_node.unwrap();
            add_symbol(
                symbol(node_name(node), "removed", node, None, None),
                &mut symbols,
            );
        } else if let (true, Some(old), Some(new)) =
            (refactoring_kind.starts_with("RENAME"), old_node, new_node)
        {
            if node_name(old) != node_name(new) {
                add_symbol(
                    symbol(
                        node_name(new),
                        "renamed",
                        new,
                        Some(node_name(old)),
                        Some(refactoring_kind.into()),
                    ),
                    &mut symbols,
                );
            }
        } else if refactoring_kind == "CHANGE_SIGNATURE" && is_definition(new_node) {
            let node = new_node.unwrap();
            add_symbol(
                symbol(
                    node_name(node),
                    "signature",
                    node,
                    None,
                    Some(refactoring_kind.into()),
                ),
                &mut symbols,
            );
        } else if change_type == "MODIFICATION"
            && is_definition(old_node)
            && is_definition(new_node)
        {
            if let (Some(old), Some(new)) = (param_count(old_node), param_count(new_node)) {
                if old != new {
                    let node = new_node.unwrap();
                    add_symbol(
                        symbol(node_name(node), "signature", node, None, None),
                        &mut symbols,
                    );
                }
            }
        }
    }

    let guardrails = review
        .get("guardrail_violations")
        .and_then(Value::as_array)
        .unwrap_or(&empty)
        .iter()
        .map(|violation| {
            let file = str_field(violation, "file");
            BTreeMap::from([
                (
                    "rule_id".to_owned(),
                    str_field(violation, "rule_id").to_owned(),
                ),
                (
                    "severity".to_owned(),
                    str_field(violation, "severity").to_owned(),
                ),
                (
                    "file".to_owned(),
                    if file.is_empty() {
                        path.to_owned()
                    } else {
                        file.to_owned()
                    },
                ),
                (
                    "semantic_path".to_owned(),
                    str_field(violation, "semantic_path").to_owned(),
                ),
                (
                    "message".to_owned(),
                    truncate_chars(str_field(violation, "message"), MAX_REASON_CHARS),
                ),
            ])
        })
        .collect();
    let semantic_changes = semantic_diff_entries(changes, groups);
    let metadata_lifecycle = review
        .get("metadata")
        .and_then(|m| m.get("file_lifecycle"))
        .and_then(Value::as_str);
    FileSemantics {
        path: path.into(),
        old_path: old_path.into(),
        language: str_field(review, "language").into(),
        lifecycle: metadata_lifecycle
            .or(lifecycle)
            .unwrap_or("modified")
            .into(),
        group_counts,
        meaningful_change_types: meaningful_types,
        style_only: review
            .get("is_style_only")
            .and_then(Value::as_bool)
            .unwrap_or(false),
        has_semantic_changes: review
            .get("has_semantic_changes")
            .and_then(Value::as_bool)
            .unwrap_or(false),
        is_fallback: review
            .get("is_fallback")
            .and_then(Value::as_bool)
            .unwrap_or(false),
        refactorings,
        symbols,
        guardrails,
        parse_errors: review
            .get("parse_errors")
            .and_then(Value::as_array)
            .unwrap_or(&empty)
            .iter()
            .take(5)
            .map(|e| truncate_chars(e.as_str().unwrap_or(&e.to_string()), MAX_REASON_CHARS))
            .collect(),
        changes: semantic_changes,
    }
}

const MAX_SEMANTIC_ENTRIES_PER_FILE: usize = 40;
const MAX_SEMANTIC_EDITS_PER_ENTRY: usize = 4;
const MAX_SEMANTIC_DETAIL_CHARS: usize = 160;

fn semantic_group(kind: &str) -> Option<&'static str> {
    match kind {
        "MEANINGFUL_CHANGE" => Some("meaningful"),
        "REFACTORING" => Some("refactoring"),
        "MOVED_CODE" => Some("moved"),
        _ => None,
    }
}

fn identifier_or_null(value: &str, node_type: &str) -> Value {
    let text = value.trim();
    if identifier().is_match(text) && text != node_type {
        json!(text)
    } else {
        Value::Null
    }
}

fn node_identifier(node: Option<&Value>) -> Value {
    match node {
        Some(node) => identifier_or_null(&node_name(node), str_field(node, "node_type")),
        None => Value::Null,
    }
}

fn change_detail(change: &Value) -> String {
    let text_diff = str_field(change, "text_diff");
    let raw = if text_diff.is_empty() {
        str_field(change, "description")
    } else {
        text_diff
    };
    truncate_chars(
        &raw.split_whitespace().collect::<Vec<_>>().join(" "),
        MAX_SEMANTIC_DETAIL_CHARS,
    )
}

fn edit_of(change: &Value) -> Value {
    let old_node = change.get("old_node").filter(|n| n.is_object());
    let new_node = change.get("new_node").filter(|n| n.is_object());
    let node = new_node.or(old_node);
    let mut edit = json!({
        "change": str_field(change, "change_type"),
        "node": node.map(|n| str_field(n, "node_type")).unwrap_or(""),
        "name": node_identifier(node),
        "detail": change_detail(change),
    });
    if old_node.is_some() && new_node.is_some() {
        let old_name = node_identifier(old_node);
        if !old_name.is_null() && old_name != edit["name"] {
            edit["old_name"] = old_name;
        }
    }
    edit
}

/// IntentumDiff's review-level changes as compact semantic-diff entries (ports
/// `semantic_diff_entries` in `verlato/core/semantic.py`).
pub fn semantic_diff_entries(changes: &[Value], groups: &[Value]) -> Vec<Value> {
    let mut entries = Vec::new();
    for group in groups {
        let Some(group_name) = semantic_group(str_field(group, "kind")) else {
            continue;
        };
        let labels_of = |key: &str| {
            group
                .get(key)
                .and_then(Value::as_array)
                .filter(|l| !l.is_empty())
                .cloned()
        };
        let labels = labels_of("new_labels")
            .or_else(|| labels_of("old_labels"))
            .unwrap_or_default();
        let old_labels = labels_of("old_labels").unwrap_or_default();
        let mut edits = Vec::new();
        for index in group
            .get("raw_change_indices")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default()
        {
            let Some(change) = index.as_u64().and_then(|i| changes.get(i as usize)) else {
                continue;
            };
            edits.push(edit_of(change));
            if edits.len() >= MAX_SEMANTIC_EDITS_PER_ENTRY {
                break;
            }
        }
        let first_label = |labels: &[Value]| {
            labels
                .first()
                .and_then(Value::as_str)
                .map(|l| identifier_or_null(l, ""))
                .unwrap_or(Value::Null)
        };
        let symbol = first_label(&labels);
        let old_symbol = first_label(&old_labels);
        let refactoring = str_field(group, "refactoring_kind");
        entries.push(json!({
            "group": group_name,
            "old_symbol": if !old_symbol.is_null() && old_symbol != symbol { old_symbol } else { Value::Null },
            "symbol": symbol,
            "refactoring": if refactoring.is_empty() { Value::Null } else { json!(refactoring) },
            "edits": edits,
        }));
        if entries.len() >= MAX_SEMANTIC_ENTRIES_PER_FILE {
            break;
        }
    }
    if groups.is_empty() {
        for change in changes.iter().take(MAX_SEMANTIC_ENTRIES_PER_FILE) {
            entries.push(json!({
                "group": "meaningful",
                "symbol": null,
                "old_symbol": null,
                "refactoring": null,
                "edits": [edit_of(change)],
            }));
        }
    }
    entries
}

// ---------------------------------------------------------------------------
// group-level evidence
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SemanticEvidence {
    pub engine: String,
    pub engine_version: String,
    pub verdict: String,
    pub complete: bool,
    pub files_reviewed: usize,
    pub files_unreviewed: usize,
    pub group_counts: BTreeMap<String, u64>,
    pub languages: Vec<String>,
    pub style_only_files: Vec<String>,
    pub refactor_only_files: Vec<String>,
    pub meaningful_files: Vec<String>,
    pub added_symbols: Vec<String>,
    pub removed_symbols: Vec<String>,
    pub renamed_symbols: Vec<String>,
    pub signature_changes: Vec<String>,
    pub refactorings: Vec<String>,
    pub cross_file: Vec<Value>,
    pub guardrails: Vec<BTreeMap<String, String>>,
    pub unreviewed: Vec<BTreeMap<String, String>>,
    pub errors: Vec<String>,
    pub notes: Vec<String>,
    pub summary: String,
}

impl SemanticEvidence {
    /// Public-looking symbols removed, renamed or re-signatured outside tests.
    pub fn api_break_candidates(&self) -> Vec<String> {
        self.removed_symbols
            .iter()
            .chain(&self.renamed_symbols)
            .chain(&self.signature_changes)
            .filter(|entry| {
                let (symbol, path) = entry.split_once(" in ").unwrap_or((entry.as_str(), ""));
                let bare = symbol
                    .rsplit(" -> ")
                    .next()
                    .unwrap_or("")
                    .rsplit('.')
                    .next()
                    .unwrap_or("")
                    .trim();
                !bare.is_empty()
                    && !bare.starts_with('_')
                    && !(!path.is_empty() && is_test_path(path))
            })
            .cloned()
            .collect()
    }
}

/// Aggregate per-commit digests into one evidence block restricted to `paths`.
/// Returns `None` when no commit carries a digest (the git engine was used).
pub fn build_semantic_evidence<'a>(
    digests: impl IntoIterator<Item = &'a CommitSemantics>,
    paths: Option<&[String]>,
) -> Option<SemanticEvidence> {
    let digests: Vec<&CommitSemantics> = digests.into_iter().collect();
    if digests.is_empty() {
        return None;
    }
    let owned = |path: &str, old_path: &str| match paths {
        None => true,
        Some(filter) => filter
            .iter()
            .any(|p| p == path || (!old_path.is_empty() && p == old_path)),
    };

    let mut group_counts: BTreeMap<String, u64> =
        GROUP_KINDS.iter().map(|k| ((*k).to_owned(), 0)).collect();
    let mut meaningful_types: BTreeMap<String, u64> = BTreeMap::new();
    let mut reviewed: indexmap::IndexMap<String, &FileSemantics> = indexmap::IndexMap::new();
    let mut unreviewed_files: indexmap::IndexMap<String, &UnreviewedFile> =
        indexmap::IndexMap::new();
    let (mut added, mut removed, mut renamed, mut signatures) =
        (Vec::new(), Vec::new(), Vec::new(), Vec::new());
    let mut refactorings: Vec<String> = Vec::new();
    let mut cross_file: Vec<Value> = Vec::new();
    let mut guardrails: Vec<BTreeMap<String, String>> = Vec::new();
    let (mut errors, mut notes) = (Vec::new(), Vec::new());
    let mut file_meaningful: HashMap<String, u64> = HashMap::new();
    let mut file_structural: HashMap<String, u64> = HashMap::new();
    let mut file_style: HashMap<String, bool> = HashMap::new();

    for digest in &digests {
        let short: String = digest.sha.chars().take(12).collect();
        if let Some(error) = &digest.error {
            errors.push(format!("{short}: {error}"));
        }
        if let Some(note) = &digest.cross_file_error {
            notes.push(format!("{short}: cross-file analysis: {note}"));
        }
        for file in &digest.files {
            if !owned(&file.path, &file.old_path) {
                continue;
            }
            reviewed.insert(file.path.clone(), file);
            unreviewed_files.shift_remove(&file.path);
            for (kind, count) in &file.group_counts {
                *group_counts.entry(kind.clone()).or_default() += count;
            }
            for (kind, count) in &file.meaningful_change_types {
                *meaningful_types.entry(kind.clone()).or_default() += count;
            }
            let count = |k: &str| file.group_counts.get(k).copied().unwrap_or(0);
            *file_meaningful.entry(file.path.clone()).or_default() += count("MEANINGFUL_CHANGE");
            *file_structural.entry(file.path.clone()).or_default() +=
                count("REFACTORING") + count("MOVED_CODE");
            let style = file.style_only || !file.has_semantic_changes;
            let entry = file_style.entry(file.path.clone()).or_insert(true);
            *entry = *entry && style;
            for symbol in &file.symbols {
                let label = format!("{} in {}", symbol.name, symbol.file);
                match symbol.change.as_str() {
                    "added" => push_unique(&mut added, &label),
                    "removed" => push_unique(&mut removed, &label),
                    "renamed" => push_unique(
                        &mut renamed,
                        &format!(
                            "{} -> {} in {}",
                            symbol.old_name.as_deref().unwrap_or("?"),
                            symbol.name,
                            symbol.file
                        ),
                    ),
                    "signature" => push_unique(&mut signatures, &label),
                    _ => {}
                }
            }
            for kind in &file.refactorings {
                push_unique(&mut refactorings, kind);
            }
            for violation in &file.guardrails {
                let mut violation = violation.clone();
                if violation.get("file").is_none_or(String::is_empty) {
                    violation.insert("file".into(), file.path.clone());
                }
                guardrails.push(violation);
            }
        }
        for item in &digest.unreviewed {
            if owned(&item.path, "") && !reviewed.contains_key(&item.path) {
                unreviewed_files.insert(item.path.clone(), item);
            }
        }
        for change in &digest.cross_file {
            let old_file = str_field(change, "old_file");
            let new_file = str_field(change, "new_file");
            if (owned(old_file, "") || owned(new_file, "")) && cross_file.len() < MAX_CROSS_FILE {
                cross_file.push(change.clone());
            }
        }
    }

    let churn: Vec<String> = added
        .iter()
        .filter(|a| removed.contains(a))
        .cloned()
        .collect();
    added.retain(|a| !churn.contains(a));
    removed.retain(|r| !churn.contains(r));

    let mut meaningful_files: Vec<String> = file_meaningful
        .iter()
        .filter(|(_, c)| **c > 0)
        .map(|(p, _)| p.clone())
        .collect();
    meaningful_files.sort();
    let mut refactor_only_files: Vec<String> = reviewed
        .keys()
        .filter(|p| {
            file_meaningful.get(*p).copied().unwrap_or(0) == 0
                && file_structural.get(*p).copied().unwrap_or(0) > 0
        })
        .cloned()
        .collect();
    refactor_only_files.sort();
    let mut style_only_files: Vec<String> = reviewed
        .keys()
        .filter(|p| {
            file_meaningful.get(*p).copied().unwrap_or(0) == 0
                && file_structural.get(*p).copied().unwrap_or(0) == 0
                && file_style.get(*p).copied().unwrap_or(false)
        })
        .cloned()
        .collect();
    style_only_files.sort();

    let structural =
        group_counts["REFACTORING"] + group_counts["MOVED_CODE"] + cross_file.len() as u64;
    let verdict = verdict(
        reviewed.len(),
        group_counts["MEANINGFUL_CHANGE"],
        structural,
        &meaningful_types,
        &removed,
        &signatures,
    );
    let mut languages: Vec<String> = reviewed
        .values()
        .map(|f| f.language.clone())
        .filter(|l| !l.is_empty())
        .collect();
    languages.sort();
    languages.dedup();
    let truncate = |mut v: Vec<String>| {
        v.truncate(MAX_EVIDENCE_SYMBOLS);
        v
    };
    let mut evidence = SemanticEvidence {
        engine: "intentumdiff".into(),
        engine_version: digests
            .iter()
            .map(|d| d.engine_version.clone())
            .find(|v| !v.is_empty())
            .unwrap_or_default(),
        verdict: verdict.into(),
        complete: unreviewed_files.is_empty() && digests.iter().all(|d| d.error.is_none()),
        files_reviewed: reviewed.len(),
        files_unreviewed: unreviewed_files.len(),
        group_counts: group_counts.into_iter().filter(|(_, c)| *c > 0).collect(),
        languages,
        style_only_files,
        refactor_only_files,
        meaningful_files,
        added_symbols: truncate(added),
        removed_symbols: truncate(removed),
        renamed_symbols: truncate(renamed),
        signature_changes: truncate(signatures),
        refactorings,
        cross_file,
        guardrails,
        unreviewed: unreviewed_files
            .values()
            .map(|u| {
                BTreeMap::from([
                    ("path".to_owned(), u.path.clone()),
                    ("kind".to_owned(), u.kind.clone()),
                    ("reason".to_owned(), u.reason.clone()),
                ])
            })
            .collect(),
        errors,
        notes,
        summary: String::new(),
    };
    evidence.summary = render_semantic_summary(&evidence);
    Some(evidence)
}

fn verdict(
    reviewed: usize,
    meaningful: u64,
    structural: u64,
    meaningful_types: &BTreeMap<String, u64>,
    removed: &[String],
    signatures: &[String],
) -> &'static str {
    if reviewed == 0 {
        return "unreviewed";
    }
    if meaningful == 0 && structural == 0 {
        return "style-only";
    }
    if meaningful == 0 {
        return "refactor-only";
    }
    let non_additive = meaningful_types
        .iter()
        .any(|(kind, count)| kind != "ADDITION" && *count > 0);
    if !non_additive && removed.is_empty() && signatures.is_empty() {
        "additive"
    } else {
        "behavioral"
    }
}

fn verdict_text(verdict: &str) -> &str {
    match verdict {
        "style-only" => "formatting/style-only (no semantic change)",
        "refactor-only" => "behavior-preserving refactoring/moves only",
        "additive" => "additive changes only",
        "behavioral" => "behavioral changes",
        "unreviewed" => "not semantically reviewed",
        other => other,
    }
}

fn group_label(kind: &str) -> &str {
    match kind {
        "MEANINGFUL_CHANGE" => "meaningful",
        "REFACTORING" => "refactoring",
        "MOVED_CODE" => "moved",
        "IGNORED_STYLE" => "style",
        "NOISE_SUPPRESSED" => "noise",
        other => other,
    }
}

fn symbol_list(entries: &[String]) -> String {
    let limit = 5;
    let names: Vec<&str> = entries
        .iter()
        .take(limit)
        .map(|e| e.split(" in ").next().unwrap_or(e))
        .collect();
    let mut text = names.join(", ");
    if entries.len() > limit {
        text.push_str(&format!(" (+{} more)", entries.len() - limit));
    }
    text
}

/// Deterministic, privacy-safe rendering of the evidence (matches the Python text).
pub fn render_semantic_summary(evidence: &SemanticEvidence) -> String {
    let mut parts = vec![
        format!(
            "IntentumDiff semantic review: {}",
            verdict_text(&evidence.verdict)
        ),
        format!(
            "{} file(s) reviewed{}",
            evidence.files_reviewed,
            if evidence.files_unreviewed > 0 {
                format!(", {} unreviewed", evidence.files_unreviewed)
            } else {
                String::new()
            }
        ),
    ];
    let groups: Vec<String> = GROUP_KINDS
        .iter()
        .filter_map(|kind| {
            evidence
                .group_counts
                .get(*kind)
                .filter(|c| **c > 0)
                .map(|c| format!("{}:{c}", group_label(kind)))
        })
        .collect();
    if !groups.is_empty() {
        parts.push(format!("groups: {}", groups.join(", ")));
    }
    for (label, entries) in [
        ("added", &evidence.added_symbols),
        ("removed", &evidence.removed_symbols),
        ("renamed", &evidence.renamed_symbols),
        ("signature changed", &evidence.signature_changes),
    ] {
        if !entries.is_empty() {
            parts.push(format!("{label}: {}", symbol_list(entries)));
        }
    }
    if !evidence.cross_file.is_empty() {
        let moved: Vec<String> = evidence
            .cross_file
            .iter()
            .take(4)
            .map(|c| {
                format!(
                    "{} ({})",
                    str_field(c, "symbol_name"),
                    str_field(c, "change_type").to_lowercase().replace('_', " ")
                )
            })
            .collect();
        parts.push(format!("cross-file: {}", moved.join(", ")));
    }
    if !evidence.style_only_files.is_empty() {
        parts.push(format!(
            "style-only files: {}",
            evidence
                .style_only_files
                .iter()
                .take(3)
                .cloned()
                .collect::<Vec<_>>()
                .join(", ")
        ));
    }
    if !evidence.guardrails.is_empty() {
        let mut rules: Vec<String> = evidence
            .guardrails
            .iter()
            .map(|g| {
                format!(
                    "{} ({})",
                    g.get("rule_id").cloned().unwrap_or_default(),
                    g.get("severity").cloned().unwrap_or_default()
                )
            })
            .collect();
        rules.sort();
        rules.dedup();
        rules.truncate(3);
        parts.push(format!("guardrails: {}", rules.join(", ")));
    }
    parts.join("; ")
}

/// Compact block for model-facing evidence payloads (Python `semantic_prompt_payload`).
pub fn semantic_prompt_payload(evidence: &SemanticEvidence) -> Value {
    let names = |entries: &[String]| -> Vec<String> {
        entries
            .iter()
            .map(|e| e.split(" in ").next().unwrap_or(e).to_owned())
            .collect()
    };
    let mut rules: Vec<String> = evidence
        .guardrails
        .iter()
        .filter_map(|g| g.get("rule_id").cloned())
        .collect();
    rules.sort();
    rules.dedup();
    json!({
        "engine": evidence.engine,
        "engine_version": evidence.engine_version,
        "verdict": evidence.verdict,
        "complete": evidence.complete,
        "group_counts": evidence.group_counts,
        "added_symbols": names(&evidence.added_symbols),
        "removed_symbols": names(&evidence.removed_symbols),
        "renamed_symbols": names(&evidence.renamed_symbols),
        "signature_changes": names(&evidence.signature_changes),
        "refactorings": evidence.refactorings,
        "style_only_files": evidence.style_only_files,
        "guardrail_rules": rules,
        "summary": evidence.summary,
    })
}

// ---------------------------------------------------------------------------
// cache
// ---------------------------------------------------------------------------

/// JSON cache of commit digests keyed by `parent..sha@engine-version` (shared format with the
/// Python implementation; keys differ because the engine versions differ).
pub struct SemanticCache {
    path: Option<PathBuf>,
    entries: Mutex<Option<Map<String, Value>>>,
    dirty: Mutex<bool>,
}

impl SemanticCache {
    pub fn new(path: Option<PathBuf>) -> Self {
        Self {
            path,
            entries: Mutex::new(None),
            dirty: Mutex::new(false),
        }
    }

    fn load(&self) -> std::sync::MutexGuard<'_, Option<Map<String, Value>>> {
        let mut guard = self.entries.lock().unwrap();
        if guard.is_none() {
            let mut entries = Map::new();
            if let Some(path) = &self.path {
                if let Ok(text) = std::fs::read_to_string(path) {
                    if let Ok(raw) = serde_json::from_str::<Value>(&text) {
                        if raw.get("digest_version").and_then(Value::as_str)
                            == Some(SEMANTIC_DIGEST_VERSION)
                        {
                            if let Some(stored) = raw.get("entries").and_then(Value::as_object) {
                                entries = stored.clone();
                            }
                        }
                    }
                }
            }
            *guard = Some(entries);
        }
        guard
    }

    pub fn get(&self, key: &str) -> Option<CommitSemantics> {
        let guard = self.load();
        serde_json::from_value(guard.as_ref()?.get(key)?.clone()).ok()
    }

    pub fn put(&self, key: &str, digest: &CommitSemantics) {
        let mut guard = self.load();
        if let (Some(entries), Ok(value)) = (guard.as_mut(), serde_json::to_value(digest)) {
            entries.insert(key.to_owned(), value);
            *self.dirty.lock().unwrap() = true;
        }
    }

    pub fn flush(&self) {
        let Some(path) = &self.path else { return };
        if !*self.dirty.lock().unwrap() {
            return;
        }
        let guard = self.load();
        let payload = json!({"digest_version": SEMANTIC_DIGEST_VERSION, "entries": guard.as_ref().cloned().unwrap_or_default()});
        if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
            let _ = std::fs::create_dir_all(parent);
        }
        let tmp = path.with_extension(format!("tmp-{}", std::process::id()));
        if std::fs::write(&tmp, payload.to_string()).is_ok() && std::fs::rename(&tmp, path).is_ok()
        {
            *self.dirty.lock().unwrap() = false;
        } else {
            let _ = std::fs::remove_file(&tmp);
        }
    }
}

pub fn cache_from_env(configured: &str) -> Option<SemanticCache> {
    let path =
        std::env::var("VERLATO_SEMANTIC_CACHE_PATH").unwrap_or_else(|_| configured.to_owned());
    if path.is_empty() || path.eq_ignore_ascii_case("off") {
        None
    } else {
        Some(SemanticCache::new(Some(Path::new(&path).to_path_buf())))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn node(label: &str, node_type: &str, params: Option<i64>, definition: bool) -> Value {
        let facts = if definition {
            json!({"param_count": params})
        } else {
            Value::Null
        };
        json!({"label": label, "node_type": node_type, "facts": facts})
    }

    #[test]
    fn digest_symbols_signatures_renames() {
        let review = json!({
            "language": "python",
            "changes": [
                {"change_type": "ADDITION", "new_node": node("punct", "default_parameter", None, false), "old_node": null},
                {"change_type": "DELETION", "old_node": node("gone", "function_definition", Some(1), true), "new_node": null},
                {"change_type": "ADDITION", "new_node": node("fresh", "function_definition", Some(2), true), "old_node": null},
                {"change_type": "REFACTORING", "refactoring_kind": "RENAME_SYMBOL", "old_node": node("bar", "function_definition", Some(2), true), "new_node": node("baz", "function_definition", Some(2), true)},
                {"change_type": "ADDITION", "new_node": node("[5]", "pair", None, true), "old_node": null},
                {"change_type": "ADDITION", "new_node": node("pair", "pair", None, true), "old_node": null}
            ],
            "change_groups": [
                {"kind": "MEANINGFUL_CHANGE", "raw_change_indices": [0], "old_labels": ["greet"], "new_labels": ["greet", "punct"]},
                {"kind": "MEANINGFUL_CHANGE", "raw_change_indices": [1]},
                {"kind": "MEANINGFUL_CHANGE", "raw_change_indices": [2]},
                {"kind": "REFACTORING", "raw_change_indices": [3], "refactoring_kind": "RENAME_SYMBOL"},
                {"kind": "IGNORED_STYLE", "raw_change_indices": []}
            ]
        });
        let digest = digest_file_diff(&review, "src/app.py", "src/app.py", None);
        let found: Vec<(String, String)> = digest
            .symbols
            .iter()
            .map(|s| (s.change.clone(), s.name.clone()))
            .collect();
        for expected in [
            ("added", "fresh"),
            ("removed", "gone"),
            ("signature", "greet"),
            ("renamed", "baz"),
        ] {
            assert!(
                found.contains(&(expected.0.into(), expected.1.into())),
                "{expected:?} in {found:?}"
            );
        }
        assert!(!found.iter().any(|(_, n)| n == "[5]" || n == "pair"));
        assert_eq!(digest.group_counts.get("MEANINGFUL_CHANGE"), Some(&3));
        assert_eq!(digest.refactorings, vec!["RENAME_SYMBOL".to_owned()]);
    }

    fn file(path: &str, counts: &[(&str, u64)], style_only: bool) -> FileSemantics {
        FileSemantics {
            path: path.into(),
            old_path: path.into(),
            language: "python".into(),
            group_counts: counts.iter().map(|(k, v)| ((*k).into(), *v)).collect(),
            style_only,
            has_semantic_changes: !style_only,
            ..FileSemantics::default()
        }
    }

    #[test]
    fn verdicts_and_summary() {
        let mut digest = CommitSemantics::new("abc", Some("p"), "x");
        digest
            .files
            .push(file("a.py", &[("IGNORED_STYLE", 2)], true));
        let evidence = build_semantic_evidence([&digest], None).unwrap();
        assert_eq!(evidence.verdict, "style-only");
        assert_eq!(evidence.summary, "IntentumDiff semantic review: formatting/style-only (no semantic change); 1 file(s) reviewed; groups: style:2; style-only files: a.py");

        let mut digest = CommitSemantics::new("abc", Some("p"), "x");
        let mut f = file("src/api.py", &[("MEANINGFUL_CHANGE", 1)], false);
        f.meaningful_change_types.insert("DELETION".into(), 1);
        f.symbols.push(SymbolChange {
            name: "public_fn".into(),
            change: "removed".into(),
            file: "src/api.py".into(),
            node_type: String::new(),
            old_name: None,
            refactoring_kind: None,
        });
        f.symbols.push(SymbolChange {
            name: "_private".into(),
            change: "removed".into(),
            file: "src/api.py".into(),
            node_type: String::new(),
            old_name: None,
            refactoring_kind: None,
        });
        digest.files.push(f);
        digest.unreviewed.push(UnreviewedFile {
            path: "x.bin".into(),
            reason: "binary".into(),
            kind: "no_parser".into(),
        });
        let evidence = build_semantic_evidence([&digest], None).unwrap();
        assert_eq!(evidence.verdict, "behavioral");
        assert!(!evidence.complete);
        assert_eq!(
            evidence.api_break_candidates(),
            vec!["public_fn in src/api.py".to_owned()]
        );
        let scoped = build_semantic_evidence([&digest], Some(&["x.bin".to_owned()])).unwrap();
        assert_eq!(scoped.verdict, "unreviewed");
    }
}
