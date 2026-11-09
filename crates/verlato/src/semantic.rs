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
pub const SEMANTIC_DIGEST_VERSION: &str = "v4";
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

fn facts_count(node: Option<&Value>, key: &str) -> i64 {
    node.and_then(|n| n.get("facts"))
        .and_then(|f| f.get(key))
        .and_then(Value::as_i64)
        .unwrap_or(0)
}

/// Parameter nodes that carry a default or are declared optional (ports
/// `_is_optional_parameter`).
fn is_optional_parameter(node: Option<&Value>) -> bool {
    let node_type = node
        .map(|n| str_field(n, "node_type").to_lowercase())
        .unwrap_or_default();
    node_type.contains("parameter")
        && (node_type.contains("default") || node_type.contains("optional"))
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
    sources: Option<(&str, &str)>,
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
        let mut compatible_signature = true;
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
                if str_field(change, "change_type") != "ADDITION" || !is_optional_parameter(node) {
                    compatible_signature = false;
                }
            }
        }
        if let Some(name) = signature_symbol {
            add_symbol(
                SymbolChange {
                    name,
                    change: if compatible_signature {
                        "extended"
                    } else {
                        "signature"
                    }
                    .into(),
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
                    let added_params = new - old;
                    let added_defaults = facts_count(new_node, "default_count")
                        - facts_count(old_node, "default_count");
                    let change = if added_params > 0 && added_defaults == added_params {
                        "extended"
                    } else {
                        "signature"
                    };
                    add_symbol(
                        symbol(node_name(node), change, node, None, None),
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
    let semantic_changes = semantic_diff_entries(changes, groups, sources);
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
/// Source text of a meaningful edit's old/new node (`before`/`after`), whitespace-collapsed.
const MAX_SEMANTIC_SNIPPET_CHARS: usize = 120;

/// Lines of a source file, split like Python's `bytes.splitlines` (`\n`, `\r\n`, `\r`).
fn source_lines(content: &str) -> Vec<&[u8]> {
    let bytes = content.as_bytes();
    let mut lines = Vec::new();
    let mut start = 0;
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'\n' => {
                lines.push(&bytes[start..index]);
                start = index + 1;
            }
            b'\r' => {
                lines.push(&bytes[start..index]);
                if bytes.get(index + 1) == Some(&b'\n') {
                    index += 1;
                }
                start = index + 1;
            }
            _ => {}
        }
        index += 1;
    }
    if start < bytes.len() {
        lines.push(&bytes[start..]);
    }
    lines
}

/// Whitespace-collapsed source text of `node` (engine positions are 0-based lines and UTF-8
/// byte columns), bounded to `MAX_SEMANTIC_SNIPPET_CHARS`. Ports `_node_source`.
fn node_source(node: Option<&Value>, lines: &[&[u8]]) -> Option<String> {
    let position = node?.get("position")?;
    let field = |key: &str| {
        position
            .get(key)
            .and_then(Value::as_u64)
            .map(|v| v as usize)
    };
    let (start_line, start_col) = (field("start_line")?, field("start_col")?);
    let (end_line, end_col) = (field("end_line")?, field("end_col")?);
    if start_line > end_line || end_line >= lines.len() {
        return None;
    }
    let slice = |line: &[u8], from: usize, to: usize| -> Vec<u8> {
        let to = to.min(line.len());
        line.get(from.min(to)..to).unwrap_or_default().to_vec()
    };
    let raw = if start_line == end_line {
        slice(lines[start_line], start_col, end_col)
    } else {
        let mut parts = vec![slice(lines[start_line], start_col, usize::MAX)];
        parts.extend(lines[start_line + 1..end_line].iter().map(|l| l.to_vec()));
        parts.push(slice(lines[end_line], 0, end_col));
        parts.join(&b'\n')
    };
    let text = String::from_utf8_lossy(&raw)
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    if text.is_empty() {
        return None;
    }
    if text.chars().count() > MAX_SEMANTIC_SNIPPET_CHARS {
        let cut: String = text.chars().take(MAX_SEMANTIC_SNIPPET_CHARS - 3).collect();
        return Some(format!("{}...", cut.trim_end()));
    }
    Some(text)
}

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
type Span = ((u64, u64), (u64, u64));

fn node_span(node: Option<&Value>) -> Option<Span> {
    let position = node?.get("position")?;
    let field = |key: &str| position.get(key).and_then(Value::as_u64);
    Some((
        (field("start_line")?, field("start_col")?),
        (field("end_line")?, field("end_col")?),
    ))
}

fn group_changes<'a>(group: &Value, changes: &'a [Value]) -> Vec<&'a Value> {
    group
        .get("raw_change_indices")
        .and_then(Value::as_array)
        .map(|indices| {
            indices
                .iter()
                .filter_map(|i| i.as_u64().and_then(|i| changes.get(i as usize)))
                .collect()
        })
        .unwrap_or_default()
}

fn object_node<'a>(change: &'a Value, key: &str) -> Option<&'a Value> {
    change.get(key).filter(|n| n.is_object())
}

/// A moved sub-expression whose old and new nodes lie inside meaningful edits is part of that
/// edit, not a move (ports `_is_moved_within_meaningful`).
fn is_moved_within_meaningful(
    group_changes: &[&Value],
    old_spans: &[Span],
    new_spans: &[Span],
) -> bool {
    let inside = |node: Option<&Value>, containers: &[Span]| {
        node_span(node).is_some_and(|span| {
            containers
                .iter()
                .any(|outer| outer.0 <= span.0 && span.1 <= outer.1)
        })
    };
    !group_changes.is_empty()
        && group_changes.iter().all(|change| {
            let old = object_node(change, "old_node");
            let new = object_node(change, "new_node");
            (old.is_none() || inside(old, old_spans)) && (new.is_none() || inside(new, new_spans))
        })
}

pub fn semantic_diff_entries(
    changes: &[Value],
    groups: &[Value],
    sources: Option<(&str, &str)>,
) -> Vec<Value> {
    let (old_lines, new_lines) = match sources {
        Some((old, new)) => (Some(source_lines(old)), Some(source_lines(new))),
        None => (None, None),
    };
    let (mut old_spans, mut new_spans) = (Vec::new(), Vec::new());
    for group in groups {
        if semantic_group(str_field(group, "kind")) != Some("meaningful") {
            continue;
        }
        for change in group_changes(group, changes) {
            old_spans.extend(node_span(object_node(change, "old_node")));
            new_spans.extend(node_span(object_node(change, "new_node")));
        }
    }
    let mut entries = Vec::new();
    for group in groups {
        let Some(group_name) = semantic_group(str_field(group, "kind")) else {
            continue;
        };
        if group_name == "moved"
            && is_moved_within_meaningful(&group_changes(group, changes), &old_spans, &new_spans)
        {
            continue;
        }
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
            let mut edit = edit_of(change);
            if group_name == "meaningful" {
                let side = |key: &str, lines: &Option<Vec<&[u8]>>| {
                    lines.as_deref().and_then(|lines| {
                        node_source(change.get(key).filter(|n| n.is_object()), lines)
                    })
                };
                let before = side("old_node", &old_lines);
                let after = side("new_node", &new_lines);
                if before != after {
                    if let Some(before) = before {
                        edit["before"] = json!(before);
                    }
                    if let Some(after) = after {
                        edit["after"] = json!(after);
                    }
                }
            }
            edits.push(edit);
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
    /// Definitions whose only signature change is added optional (defaulted) parameters.
    #[serde(default)]
    pub extended_signatures: Vec<String>,
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
    let mut extended: Vec<String> = Vec::new();
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
                    "extended" => push_unique(&mut extended, &label),
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
            // Every revision counts: a path reviewed in one commit but not another is still
            // partially unreviewed, so the group must not be reported as complete.
            if owned(&item.path, "") {
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
        extended_signatures: truncate(
            extended
                .into_iter()
                .filter(|e| !signatures.contains(e))
                .collect(),
        ),
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
        (
            "optional parameters added (compatible)",
            &evidence.extended_signatures,
        ),
        (
            "possible API break (public symbol removed, renamed or re-signatured; callers may need updating)",
            &evidence.api_break_candidates(),
        ),
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
        "extended_signatures": names(&evidence.extended_signatures),
        "api_break_candidates": names(&evidence.api_break_candidates()),
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

// ---------------------------------------------------------------------------
// semantic diff records (what LLM prompts receive instead of raw patches)
// ---------------------------------------------------------------------------

/// Edit fields that can contain source content: kept only at the detailed privacy level.
const SOURCE_EDIT_KEYS: &[&str] = &["detail", "before", "after"];

/// IntentumDiff's semantic diff for a change group: one record per (commit, file), plus the
/// files the engine could not review (ports `build_semantic_diff`).
pub fn build_semantic_diff<'a>(
    digests: impl IntoIterator<Item = &'a CommitSemantics>,
    paths: Option<&[String]>,
) -> Vec<Value> {
    let owned = |path: &str, old_path: &str| match paths {
        None => true,
        Some(paths) => {
            paths.iter().any(|p| p == path)
                || (!old_path.is_empty() && paths.iter().any(|p| p == old_path))
        }
    };
    let mut records = Vec::new();
    for digest in digests {
        let short: String = digest.sha.chars().take(12).collect();
        for file in &digest.files {
            if !owned(&file.path, &file.old_path) {
                continue;
            }
            let mut record = Map::new();
            record.insert("path".into(), json!(file.path));
            record.insert("commit".into(), json!(short));
            record.insert("language".into(), json!(file.language));
            record.insert("lifecycle".into(), json!(file.lifecycle));
            record.insert(
                "style_only".into(),
                json!(file.style_only || !file.has_semantic_changes),
            );
            record.insert("group_counts".into(), json!(file.group_counts));
            record.insert("changes".into(), json!(file.changes));
            if !file.old_path.is_empty() && file.old_path != file.path {
                record.insert("old_path".into(), json!(file.old_path));
            }
            if !file.guardrails.is_empty() {
                let mut rules: Vec<String> = file
                    .guardrails
                    .iter()
                    .map(|g| g.get("rule_id").cloned().unwrap_or_else(|| "None".into()))
                    .collect();
                rules.sort();
                rules.dedup();
                record.insert("guardrails".into(), json!(rules));
            }
            records.push(Value::Object(record));
        }
        for item in &digest.unreviewed {
            if owned(&item.path, "") {
                records.push(json!({
                    "path": item.path,
                    "commit": short,
                    "unreviewed": item.kind,
                    "reason": item.reason,
                }));
            }
        }
    }
    records
}

fn present(value: &Value) -> bool {
    !matches!(value, Value::Null) && value.as_str() != Some("")
}

/// Apply the privacy detail level (ports `semantic_diff_for_prompt`): `detailed` keeps the
/// engine's change descriptions and source snippets; otherwise only structure and names remain.
pub fn semantic_diff_for_prompt(records: &[Value], detailed: bool) -> Vec<Value> {
    records
        .iter()
        .map(|record| {
            let Some(object) = record.as_object() else {
                return record.clone();
            };
            let mut copy: Map<String, Value> = object
                .iter()
                .filter(|(k, _)| k.as_str() != "changes")
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect();
            if let Some(changes) = object.get("changes").and_then(Value::as_array) {
                let prepared: Vec<Value> = changes
                    .iter()
                    .map(|entry| {
                        let empty = Map::new();
                        let entry = entry.as_object().unwrap_or(&empty);
                        let edits: Vec<Value> = entry
                            .get("edits")
                            .and_then(Value::as_array)
                            .map(|edits| {
                                edits
                                    .iter()
                                    .map(|edit| {
                                        let kept: Map<String, Value> = edit
                                            .as_object()
                                            .map(|e| {
                                                e.iter()
                                                    .filter(|(k, v)| {
                                                        present(v)
                                                            && (detailed
                                                                || !SOURCE_EDIT_KEYS
                                                                    .contains(&k.as_str()))
                                                    })
                                                    .map(|(k, v)| (k.clone(), v.clone()))
                                                    .collect()
                                            })
                                            .unwrap_or_default();
                                        Value::Object(kept)
                                    })
                                    .collect()
                            })
                            .unwrap_or_default();
                        let mut header: Map<String, Value> = entry
                            .iter()
                            .filter(|(k, v)| k.as_str() != "edits" && present(v))
                            .map(|(k, v)| (k.clone(), v.clone()))
                            .collect();
                        header.insert("edits".into(), Value::Array(edits));
                        Value::Object(header)
                    })
                    .collect();
                copy.insert("changes".into(), Value::Array(prepared));
            }
            Value::Object(copy)
        })
        .collect()
}

fn text_of<'a>(value: &'a Value, key: &str) -> &'a str {
    value.get(key).and_then(Value::as_str).unwrap_or("")
}

fn render_edit(edit: &Value, detailed: bool) -> String {
    let mut text = text_of(edit, "change").to_lowercase();
    let node = text_of(edit, "node");
    if !node.is_empty() {
        text.push(' ');
        text.push_str(node);
    }
    let (name, old_name) = (text_of(edit, "name"), text_of(edit, "old_name"));
    if !old_name.is_empty() && !name.is_empty() {
        text.push_str(&format!(" {old_name} -> {name}"));
    } else if !name.is_empty() {
        text.push_str(&format!(" {name}"));
    }
    let (before, after, detail) = (
        text_of(edit, "before"),
        text_of(edit, "after"),
        text_of(edit, "detail"),
    );
    if detailed && !before.is_empty() && !after.is_empty() {
        text.push_str(&format!(" (was `{before}`, now `{after}`)"));
    } else if detailed && !after.is_empty() {
        text.push_str(&format!(" (added `{after}`)"));
    } else if detailed && !before.is_empty() {
        text.push_str(&format!(" (removed `{before}`)"));
    } else if detailed && !detail.is_empty() {
        text.push_str(&format!(" [{detail}]"));
    }
    text
}

/// Prompt-friendly text rendering of a semantic diff (ports `render_semantic_diff`).
pub fn render_semantic_diff(records: &[Value], detailed: bool, max_chars: Option<usize>) -> String {
    let mut lines: Vec<String> = Vec::new();
    for record in records {
        let path = text_of(record, "path");
        let commit = text_of(record, "commit");
        if present(record.get("unreviewed").unwrap_or(&Value::Null)) {
            lines.push(format!(
                "{path} @{commit}: not semantically reviewed ({})",
                text_of(record, "unreviewed")
            ));
            continue;
        }
        let old_path = text_of(record, "old_path");
        let rename = if old_path.is_empty() {
            String::new()
        } else {
            format!(" (renamed from {old_path})")
        };
        let language = Some(text_of(record, "language"))
            .filter(|l| !l.is_empty())
            .unwrap_or("text");
        let lifecycle = match record.get("lifecycle") {
            Some(Value::String(s)) => s.clone(),
            _ => "None".into(),
        };
        lines.push(format!(
            "{path}{rename} ({language}, {lifecycle}) @{commit}:"
        ));
        let changes = record
            .get("changes")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        for entry in &changes {
            let mut header = text_of(entry, "group").to_owned();
            let refactoring = text_of(entry, "refactoring");
            if !refactoring.is_empty() {
                header.push_str(&format!(" {refactoring}"));
            }
            let (symbol, old_symbol) = (text_of(entry, "symbol"), text_of(entry, "old_symbol"));
            if !old_symbol.is_empty() && !symbol.is_empty() {
                header.push_str(&format!(": {old_symbol} -> {symbol}"));
            } else if !symbol.is_empty() {
                header.push_str(&format!(" in {symbol}"));
            }
            let edits: Vec<String> = entry
                .get("edits")
                .and_then(Value::as_array)
                .map(|edits| edits.iter().map(|e| render_edit(e, detailed)).collect())
                .unwrap_or_default();
            if edits.is_empty() {
                lines.push(format!("  - {header}"));
            } else {
                lines.push(format!("  - {header}: {}", edits.join("; ")));
            }
        }
        let count = |kind: &str| {
            record
                .get("group_counts")
                .and_then(|c| c.get(kind))
                .and_then(Value::as_u64)
                .unwrap_or(0)
        };
        let ignored: Vec<String> = ["IGNORED_STYLE", "NOISE_SUPPRESSED"]
            .iter()
            .filter(|k| count(k) > 0)
            .map(|k| format!("{}:{}", group_label(k), count(k)))
            .collect();
        if !ignored.is_empty() {
            lines.push(format!(
                "  - ignored as formatting/noise ({})",
                ignored.join(", ")
            ));
        }
        if changes.is_empty() && ignored.is_empty() {
            let style_only = record
                .get("style_only")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            lines.push(
                if style_only {
                    "  - no semantic change"
                } else {
                    "  - changed (no structural detail)"
                }
                .into(),
            );
        }
        if let Some(rules) = record.get("guardrails").and_then(Value::as_array) {
            if !rules.is_empty() {
                let rules: Vec<&str> = rules.iter().filter_map(Value::as_str).collect();
                lines.push(format!(
                    "  - protected-config guardrails: {}",
                    rules.join(", ")
                ));
            }
        }
    }
    let text = lines.join("\n");
    match max_chars {
        Some(max) if text.chars().count() > max => {
            let head: String = text.chars().take(max).collect();
            let cut = match head.rfind('\n') {
                Some(index) => head[..index].to_owned(),
                None => head,
            };
            let remaining = text[cut.len()..].matches('\n').count();
            format!("{cut}\n... ({remaining} more semantic diff line(s) omitted)")
        }
        _ => text,
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
                {"change_type": "ADDITION", "new_node": node("pair", "pair", None, true), "old_node": null},
                {"change_type": "DELETION", "old_node": node("flag", "default_parameter", None, false), "new_node": null}
            ],
            "change_groups": [
                {"kind": "MEANINGFUL_CHANGE", "raw_change_indices": [0], "old_labels": ["greet"], "new_labels": ["greet", "punct"]},
                {"kind": "MEANINGFUL_CHANGE", "raw_change_indices": [6], "old_labels": ["fetch"], "new_labels": ["fetch"]},
                {"kind": "MEANINGFUL_CHANGE", "raw_change_indices": [1]},
                {"kind": "MEANINGFUL_CHANGE", "raw_change_indices": [2]},
                {"kind": "REFACTORING", "raw_change_indices": [3], "refactoring_kind": "RENAME_SYMBOL"},
                {"kind": "IGNORED_STYLE", "raw_change_indices": []}
            ]
        });
        let digest = digest_file_diff(&review, "src/app.py", "src/app.py", None, None);
        let found: Vec<(String, String)> = digest
            .symbols
            .iter()
            .map(|s| (s.change.clone(), s.name.clone()))
            .collect();
        for expected in [
            ("added", "fresh"),
            ("removed", "gone"),
            ("extended", "greet"),
            ("signature", "fetch"),
            ("renamed", "baz"),
        ] {
            assert!(
                found.contains(&(expected.0.into(), expected.1.into())),
                "{expected:?} in {found:?}"
            );
        }
        assert!(!found.iter().any(|(_, n)| n == "[5]" || n == "pair"));
        assert_eq!(digest.group_counts.get("MEANINGFUL_CHANGE"), Some(&4));
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

    #[test]
    fn unreviewed_revision_is_not_hidden_by_another_reviewed_revision() {
        let mut style = CommitSemantics::new("s1", Some("p0"), "x");
        style
            .files
            .push(file("src/app.py", &[("IGNORED_STYLE", 1)], true));
        let mut failed = CommitSemantics::new("f1", Some("s1"), "x");
        failed.unreviewed.push(UnreviewedFile {
            path: "src/app.py".into(),
            reason: "boom".into(),
            kind: "pipeline_error".into(),
        });
        let evidence = build_semantic_evidence([&style, &failed], None).unwrap();
        assert!(!evidence.complete);
        assert_eq!(evidence.files_unreviewed, 1);
    }

    #[test]
    fn meaningful_edits_carry_bounded_source_snippets() {
        let at = |line: u64, start: u64, end: u64, label: &str, node_type: &str| {
            let mut n = node(label, node_type, None, false);
            n["position"] =
                json!({"start_line": line, "start_col": start, "end_line": line, "end_col": end});
            n
        };
        let old = "def paginate(page, per_page):\r\n    start = page * per_page + 1\r\n    return start\r\n";
        let new = "def paginate(page, per_page):\n    start = page * per_page\n    return start\n";
        let changes = vec![
            json!({"change_type": "MODIFICATION", "old_node": at(1, 4, 31, "start", "assignment"), "new_node": at(1, 4, 27, "start", "assignment")}),
            json!({"change_type": "MOVE", "old_node": at(1, 12, 27, "page", "binary_operator"), "new_node": at(1, 12, 27, "page", "binary_operator")}),
        ];
        let groups = vec![
            json!({"kind": "MEANINGFUL_CHANGE", "raw_change_indices": [0], "new_labels": ["paginate"]}),
            json!({"kind": "MOVED_CODE", "raw_change_indices": [1], "new_labels": ["page"]}),
        ];
        let entries = semantic_diff_entries(&changes, &groups, Some((old, new)));
        // The moved `page * per_page` lies inside the modified assignment.
        assert_eq!(entries.len(), 1);
        assert_eq!(
            entries[0]["edits"][0]["before"],
            "start = page * per_page + 1"
        );
        assert_eq!(entries[0]["edits"][0]["after"], "start = page * per_page");
        let plain = semantic_diff_entries(&changes, &groups, None);
        assert!(plain[0]["edits"][0].get("before").is_none());
        let mut moved_elsewhere = changes.clone();
        moved_elsewhere[1]["old_node"] = at(5, 0, 15, "page", "binary_operator");
        let kept = semantic_diff_entries(&moved_elsewhere, &groups, Some((old, new)));
        assert_eq!(kept[1]["group"], "moved");
        assert!(kept[1]["edits"][0].get("before").is_none());

        let long = format!("x = [{}]", "1, ".repeat(60));
        let lines = source_lines(&long);
        let snippet = node_source(
            Some(&at(0, 0, long.len() as u64, "x", "assignment")),
            &lines,
        )
        .unwrap();
        assert_eq!(snippet.chars().count(), MAX_SEMANTIC_SNIPPET_CHARS);
        assert!(snippet.ends_with("..."));
    }
}
