//! Evidence tools over the suggestion items of a git range (ports
//! `verlato/core/suggestion_tools.py`, `SuggestionEvidenceToolProvider`). The MCP server
//! exposes them to agents: item manifests, the IntentumDiff semantic diff of a file and, for the
//! legacy `git` engine only, bounded raw patches.

use serde_json::{json, Map, Value};

use crate::evidence::{self, DiffEvidence};
use crate::models::{changed_files_for_group, ChangeGroup, ChangeItem, ChangedFile, Commit};
use crate::pipeline::ServiceError;
use crate::pyjson::{py_len, splitlines};
use crate::semantic::{render_semantic_diff, semantic_diff_for_prompt, semantic_prompt_payload};

pub const DEFAULT_PATCH_CHUNK_CHARS: i64 = 12_000;
pub const MAX_PATCH_CHUNK_CHARS: i64 = 24_000;
pub const MAX_SEARCH_RESULTS: i64 = 8;

struct Entry {
    group: ChangeGroup,
    item: ChangeItem,
    files: Vec<ChangedFile>,
    diff: DiffEvidence,
    patch_chars: usize,
    missing_patch_count: usize,
}

pub struct SuggestionTools {
    entries: Vec<Entry>,
    /// Whether IntentumDiff's change descriptions (which can contain literals) may be returned.
    semantic_detail: bool,
}

type ToolResult = anyhow::Result<Value>;

fn key_error(message: String) -> anyhow::Error {
    ServiceError {
        code: "KeyError",
        message: crate::mcp::py_repr_str(&message),
    }
    .into()
}

impl SuggestionTools {
    /// `SuggestionEvidenceToolProvider.from_groups`.
    pub fn from_groups(
        groups: Vec<ChangeGroup>,
        items: Vec<ChangeItem>,
        semantic_detail: bool,
    ) -> Self {
        let entries = groups
            .into_iter()
            .zip(items)
            .map(|(group, item)| {
                let files = changed_files_for_group(&group);
                let diff = evidence::build(&files, Some(&group.commits));
                let patch_chars = files.iter().map(|f| py_len(&f.patch)).sum();
                let missing_patch_count = files.iter().filter(|f| f.patch.is_empty()).count();
                Entry {
                    group,
                    item,
                    files,
                    diff,
                    patch_chars,
                    missing_patch_count,
                }
            })
            .collect();
        SuggestionTools {
            entries,
            semantic_detail,
        }
    }

    fn entry(&self, item_id: &str) -> anyhow::Result<&Entry> {
        self.entries
            .iter()
            .find(|e| e.item.id == item_id)
            .ok_or_else(|| key_error(format!("Unknown suggestion item id: {item_id}")))
    }

    fn file<'a>(entry: &'a Entry, path: &str) -> anyhow::Result<&'a ChangedFile> {
        let normalized = path.replace('\\', "/");
        entry
            .files
            .iter()
            .find(|f| f.path.replace('\\', "/") == normalized)
            .ok_or_else(|| {
                key_error(format!(
                    "Unknown file path for suggestion item {}: {path}",
                    entry.item.id
                ))
            })
    }

    fn manifest(entry: &Entry, include_files: bool) -> Map<String, Value> {
        let item = &entry.item;
        let mut payload = Map::new();
        payload.insert("item_id".into(), json!(item.id));
        payload.insert("title".into(), json!(item.content.title));
        let current = item
            .edited_summary
            .clone()
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| item.content.summary.clone());
        payload.insert("current_summary".into(), json!(current));
        payload.insert(
            "classification".into(),
            json!({
                "kind": item.classification.kind,
                "area": item.classification.area,
                "audience": item.classification.audience,
                "semver": item.classification.semver,
            }),
        );
        payload.insert("file_count".into(), json!(entry.files.len()));
        payload.insert("patch_chars".into(), json!(entry.patch_chars));
        payload.insert(
            "missing_patch_count".into(),
            json!(entry.missing_patch_count),
        );
        payload.insert("diff_summary".into(), json!(entry.diff.summary));
        if include_files {
            let files: Vec<Value> = entry
                .files
                .iter()
                .map(|f| {
                    json!({
                        "path": f.path,
                        "status": f.status,
                        "patch_chars": py_len(&f.patch),
                        "patch_available": !f.patch.is_empty(),
                    })
                })
                .collect();
            payload.insert("files".into(), Value::Array(files));
        }
        payload
    }

    pub fn list_suggestion_items(&self, include_files: bool) -> Value {
        let items: Vec<Value> = self
            .entries
            .iter()
            .map(|e| Value::Object(Self::manifest(e, include_files)))
            .collect();
        json!({ "items": items })
    }

    pub fn get_suggestion_item(&self, item_id: &str) -> ToolResult {
        let entry = self.entry(item_id)?;
        let semantic = entry.diff.semantic.as_ref();
        let mut payload = Self::manifest(entry, true);
        payload.insert(
            "evidence_source".into(),
            json!(if semantic.is_some() {
                "intentumdiff-semantic-diff"
            } else {
                "git-patch"
            }),
        );
        payload.insert(
            "semantic_review".into(),
            semantic.map(semantic_prompt_payload).unwrap_or(Value::Null),
        );
        let pr = entry.group.pr.as_ref();
        payload.insert(
            "labels".into(),
            json!(pr.map(|p| p.labels.clone()).unwrap_or_default()),
        );
        payload.insert(
            "pr".into(),
            pr.map(|p| {
                json!({
                    "number": p.number,
                    "title": p.title,
                    "body": p.body,
                    "base_ref": p.base_ref,
                    "head_ref": p.head_ref,
                })
            })
            .unwrap_or(Value::Null),
        );
        payload.insert(
            "commit_boundaries".into(),
            commit_boundaries(&entry.group.commits),
        );
        payload.insert(
            "commits".into(),
            Value::Array(entry.group.commits.iter().map(commit_payload).collect()),
        );
        let diff = &entry.diff;
        payload.insert(
            "diff_summary".into(),
            json!({
                "summary": diff.summary,
                "files_by_area": diff.files_by_area,
                "file_statuses": diff.file_statuses,
                "api_changes": diff.api_changes,
                "migration_changes": diff.migration_changes,
                "config_changes": diff.config_changes,
            }),
        );
        Ok(Value::Object(payload))
    }

    fn semantic_records(&self, entry: &Entry) -> Vec<Value> {
        semantic_diff_for_prompt(&entry.diff.semantic_diff, self.semantic_detail)
    }

    pub fn get_file_semantic_diff(&self, item_id: &str, path: &str) -> ToolResult {
        let entry = self.entry(item_id)?;
        let file = Self::file(entry, path)?;
        let records: Vec<Value> = self
            .semantic_records(entry)
            .into_iter()
            .filter(|r| r.get("path").and_then(Value::as_str) == Some(file.path.as_str()))
            .collect();
        Ok(json!({
            "item_id": item_id,
            "path": file.path,
            "status": file.status,
            "semantic_diff_available": !records.is_empty(),
            "detail": if self.semantic_detail { "detailed" } else { "structure" },
            "text": render_semantic_diff(&records, self.semantic_detail, None),
            "records": records,
        }))
    }

    pub fn search_semantic_diff(&self, item_id: &str, query: &str, limit: i64) -> ToolResult {
        let entry = self.entry(item_id)?;
        let needle = query.trim().to_lowercase();
        let mut results: Vec<Value> = Vec::new();
        if !needle.is_empty() {
            'records: for record in self.semantic_records(entry) {
                let rendered =
                    render_semantic_diff(std::slice::from_ref(&record), self.semantic_detail, None);
                for line in splitlines(&rendered).into_iter().skip(1) {
                    if line.to_lowercase().contains(&needle) {
                        results.push(json!({
                            "path": record.get("path").cloned().unwrap_or(Value::Null),
                            "commit": record.get("commit").cloned().unwrap_or(Value::Null),
                            "change": line.trim(),
                        }));
                        if results.len() as i64 >= limit {
                            break 'records;
                        }
                    }
                }
            }
        }
        Ok(json!({"item_id": item_id, "query": query, "results": results}))
    }

    pub fn get_file_patch(&self, item_id: &str, path: &str, offset: i64, limit: i64) -> ToolResult {
        let entry = self.entry(item_id)?;
        let file = Self::file(entry, path)?;
        let chars: Vec<char> = file.patch.chars().collect();
        let total = chars.len() as i64;
        let offset = offset.max(0).min(total);
        let limit = limit.clamp(1, MAX_PATCH_CHUNK_CHARS);
        let end = (offset + limit).min(total);
        let chunk: String = chars[offset as usize..end as usize].iter().collect();
        let more = end < total;
        Ok(json!({
            "item_id": item_id,
            "path": file.path,
            "status": file.status,
            "patch_available": !file.patch.is_empty(),
            "patch_chars": total,
            "offset": offset,
            "limit": limit,
            "next_offset": if more { json!(end) } else { Value::Null },
            "truncated": more,
            "patch": chunk,
        }))
    }

    pub fn search_item_diff(&self, item_id: &str, query: &str, limit: i64) -> ToolResult {
        let entry = self.entry(item_id)?;
        let needle = query.trim().to_lowercase();
        let mut results: Vec<Value> = Vec::new();
        if !needle.is_empty() {
            'files: for file in &entry.files {
                let lines = splitlines(&file.patch);
                for (index, line) in lines.iter().enumerate() {
                    if !line.to_lowercase().contains(&needle) {
                        continue;
                    }
                    let start = index.saturating_sub(2);
                    let end = (index + 3).min(lines.len());
                    let snippet: String = lines[start..end].join("\n").chars().take(1200).collect();
                    results.push(json!({"path": file.path, "line": index + 1, "snippet": snippet}));
                    if results.len() as i64 >= limit {
                        break 'files;
                    }
                }
            }
        }
        Ok(json!({"item_id": item_id, "query": query, "results": results}))
    }
}

fn first_line(message: &str) -> String {
    splitlines(message)
        .first()
        .map(|s| (*s).to_owned())
        .unwrap_or_default()
}

fn commit_payload(commit: &Commit) -> Value {
    let (effective, used_inferred) = crate::intents::effective_conventional_commit(commit);
    let mut payload = Map::new();
    payload.insert("sha".into(), json!(commit.sha));
    payload.insert("subject".into(), json!(first_line(&commit.message)));
    payload.insert("message".into(), json!(commit.message));
    payload.insert("author".into(), json!(commit.author));
    payload.insert("previous_sha".into(), json!(commit.parent_sha));
    payload.insert("initial_commit".into(), json!(commit.parent_sha.is_none()));
    if let Some(intent) = &commit.inferred_intent {
        payload.insert("inferred_conventional".into(), intent.payload());
    }
    if let Some(intent) = &commit.derived_intent {
        payload.insert("derived_conventional".into(), intent.payload());
    }
    if let (Some(effective), true) = (effective, used_inferred) {
        payload.insert(
            "effective_conventional".into(),
            json!({
                "type": effective.kind,
                "scope": effective.scope,
                "description": effective.description,
                "breaking": effective.breaking,
                "source": "inferred",
            }),
        );
    }
    Value::Object(payload)
}

fn boundary_payload(commit: &Commit) -> Value {
    json!({
        "sha": commit.sha,
        "subject": first_line(&commit.message),
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
