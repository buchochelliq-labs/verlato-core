//! Core data model. JSON shapes match the Python dataclasses (`verlato/core/models.py`)
//! field-for-field so state files, CLI output and the VS Code extension contract are shared.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::semantic::CommitSemantics;

pub const REVIEW_STATUS_VALUES: &[&str] =
    &["needs-review", "ready", "accepted", "edited", "hidden"];

pub fn default_review_status(confidence: f64) -> &'static str {
    if confidence >= 0.8 {
        "ready"
    } else {
        "needs-review"
    }
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ChangedFile {
    pub path: String,
    #[serde(default = "modified")]
    pub status: String,
    #[serde(default)]
    pub patch: String,
}

fn modified() -> String {
    "modified".to_owned()
}

#[derive(Debug, Clone, Default)]
pub struct Commit {
    pub sha: String,
    /// The commit subject (the collector reads `%s`, like the Python implementation).
    pub message: String,
    pub author: String,
    pub parent_sha: Option<String>,
    pub files: Vec<ChangedFile>,
    /// IntentumDiff digest for `parent_sha..sha`, attached by the collector.
    pub semantics: Option<CommitSemantics>,
    /// Model-inferred conventional metadata used for grouping low-signal commits.
    pub inferred_intent: Option<CommitIntent>,
    /// Conventional commit the model derived from the diff alone.
    pub derived_intent: Option<CommitIntent>,
}

/// A model-inferred or model-derived conventional commit (ports `CommitIntent`).
#[derive(Debug, Clone, PartialEq, Default)]
pub struct CommitIntent {
    pub kind: String,
    pub scope: Option<String>,
    pub description: String,
    pub breaking: bool,
    pub confidence: f64,
    pub model: String,
    pub source: String,
    pub rationale: String,
}

impl CommitIntent {
    /// `conventional_commit_message`.
    pub fn message(&self) -> String {
        let scope = self
            .scope
            .as_deref()
            .filter(|s| !s.is_empty())
            .map(|s| format!("({s})"))
            .unwrap_or_default();
        let breaking = if self.breaking { "!" } else { "" };
        format!("{}{scope}{breaking}: {}", self.kind, self.description)
    }

    /// `commit_intent_payload`: the dataclass fields plus `message`.
    pub fn payload(&self) -> Value {
        serde_json::json!({
            "type": self.kind,
            "scope": self.scope,
            "description": self.description,
            "breaking": self.breaking,
            "confidence": self.confidence,
            "model": self.model,
            "source": self.source,
            "rationale": self.rationale,
            "message": self.message(),
        })
    }
}

#[derive(Debug, Clone, Default)]
pub struct PullRequest {
    pub number: i64,
    pub title: String,
    pub body: String,
    pub labels: Vec<String>,
    pub files: Vec<ChangedFile>,
    pub base_ref: String,
    pub head_ref: String,
}

#[derive(Debug, Clone)]
pub struct ChangeGroup {
    pub pr: Option<PullRequest>,
    pub commits: Vec<Commit>,
    pub files: Vec<ChangedFile>,
    pub grouping_reason: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SourceRef {
    pub pr_number: Option<i64>,
    pub commit_shas: Vec<String>,
    pub base_ref: String,
    pub head_ref: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Classification {
    pub kind: String,
    pub area: String,
    pub audience: String,
    pub semver: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Content {
    pub title: String,
    pub summary: String,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Evidence {
    #[serde(default)]
    pub labels: Vec<String>,
    #[serde(default)]
    pub files: Vec<String>,
    #[serde(default)]
    pub commit_signals: Vec<String>,
    #[serde(default)]
    pub diff_summary: Map<String, Value>,
    #[serde(default)]
    pub derived_conventional_commits: Vec<Value>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ChangeItem {
    pub id: String,
    pub source: SourceRef,
    pub classification: Classification,
    pub content: Content,
    pub evidence: Evidence,
    pub confidence: f64,
    pub published: bool,
    pub published_in: Option<String>,
    pub checksum: String,
    pub checksum_version: String,
    pub lineage_releases: Vec<String>,
    pub status: String,
    pub reviewed_by: Option<String>,
    pub reviewed_at: Option<String>,
    pub edited_summary: Option<String>,
    pub summary_provenance: Option<Map<String, Value>>,
    pub hidden: bool,
}

impl ChangeItem {
    /// Lenient loader mirroring Python `ChangeItem.from_dict` (defaults + status normalization).
    pub fn from_value(value: &Value) -> anyhow::Result<ChangeItem> {
        let object = value
            .as_object()
            .ok_or_else(|| anyhow::anyhow!("change item must be an object"))?;
        let get = |key: &str| object.get(key).cloned().unwrap_or(Value::Null);
        let text = |key: &str| object.get(key).and_then(Value::as_str).map(str::to_owned);
        let confidence = object
            .get("confidence")
            .and_then(Value::as_f64)
            .unwrap_or(0.5);
        let mut status = text("status")
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| default_review_status(confidence).into());
        if !REVIEW_STATUS_VALUES.contains(&status.as_str()) {
            status = default_review_status(confidence).into();
        }
        let hidden = object
            .get("hidden")
            .and_then(Value::as_bool)
            .unwrap_or(false)
            || status == "hidden";
        let evidence: Evidence = match object.get("evidence") {
            Some(Value::Object(_)) => serde_json::from_value(get("evidence"))?,
            _ => Evidence::default(),
        };
        Ok(ChangeItem {
            id: text("id").ok_or_else(|| anyhow::anyhow!("change item is missing id"))?,
            source: serde_json::from_value(get("source"))?,
            classification: serde_json::from_value(get("classification"))?,
            content: serde_json::from_value(get("content"))?,
            evidence,
            confidence,
            published: object
                .get("published")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            published_in: text("published_in"),
            checksum: text("checksum").unwrap_or_default(),
            checksum_version: text("checksum_version").unwrap_or_else(|| "v1".into()),
            lineage_releases: object
                .get("lineage_releases")
                .and_then(Value::as_array)
                .map(|items| {
                    items
                        .iter()
                        .filter_map(|v| v.as_str().map(str::to_owned))
                        .collect()
                })
                .unwrap_or_default(),
            status,
            reviewed_by: text("reviewed_by"),
            reviewed_at: text("reviewed_at"),
            edited_summary: text("edited_summary"),
            summary_provenance: object
                .get("summary_provenance")
                .and_then(Value::as_object)
                .cloned(),
            hidden,
        })
    }

    pub fn checksum_key(&self) -> (&str, &str) {
        (&self.checksum_version, &self.checksum)
    }
}

/// Merge PR-reported files with commit files, PR first (Python `merge_changed_files`).
pub fn merge_changed_files(primary: &[ChangedFile], fallback: &[ChangedFile]) -> Vec<ChangedFile> {
    if primary.is_empty() {
        return fallback.to_vec();
    }
    let mut merged = Vec::with_capacity(primary.len() + fallback.len());
    for file in primary {
        let fallback_file = fallback.iter().find(|f| f.path == file.path);
        merged.push(ChangedFile {
            path: file.path.clone(),
            status: if file.status.is_empty() {
                fallback_file
                    .map(|f| f.status.clone())
                    .unwrap_or_else(modified)
            } else {
                file.status.clone()
            },
            patch: if file.patch.is_empty() {
                fallback_file.map(|f| f.patch.clone()).unwrap_or_default()
            } else {
                file.patch.clone()
            },
        });
    }
    for file in fallback {
        if !primary.iter().any(|p| p.path == file.path) {
            merged.push(file.clone());
        }
    }
    merged
}

pub fn changed_files_for_group(group: &ChangeGroup) -> Vec<ChangedFile> {
    match &group.pr {
        None => group.files.clone(),
        Some(pr) => merge_changed_files(&pr.files, &group.files),
    }
}
