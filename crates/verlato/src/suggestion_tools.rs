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

fn tool_definition(name: &str, description: &str, schema: Value) -> Value {
    json!({
        "type": "function",
        "function": {"name": name, "description": description, "parameters": schema},
    })
}

fn item_id_property() -> Value {
    json!({"type": "string", "description": "Suggestion item id."})
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

/// `_required_string`.
fn required_string(arguments: &Map<String, Value>, key: &str) -> Result<String, String> {
    match arguments.get(key) {
        Some(Value::String(value)) if !value.trim().is_empty() => Ok(value.clone()),
        _ => Err(format!("Missing required string argument: {key}")),
    }
}

/// `_coerce_int`.
fn coerce_int(value: Option<&Value>, default: i64, minimum: i64, maximum: Option<i64>) -> i64 {
    let parsed = match value {
        None | Some(Value::Null) => Some(default),
        Some(Value::Bool(b)) => Some(i64::from(*b)),
        Some(Value::Number(n)) => n.as_i64().or_else(|| n.as_f64().map(|f| f.trunc() as i64)),
        Some(Value::String(s)) => s.trim().replace('_', "").parse().ok(),
        Some(_) => None,
    };
    let result = parsed.unwrap_or(default).max(minimum);
    maximum.map_or(result, |m| result.min(m))
}

impl SuggestionTools {
    /// Whether any item was reviewed by IntentumDiff (`uses_semantic_diff`).
    pub fn uses_semantic_diff(&self) -> bool {
        self.entries.iter().any(|e| e.diff.semantic.is_some())
    }

    /// `ollama_tools()`: the tool definitions in Ollama's function-calling format.
    pub fn ollama_tools(&self) -> Vec<Value> {
        let mut tools = vec![
            tool_definition(
                "verlato_list_suggestion_items",
                "List compact release-note suggestion items and changed-file manifests for the active git range.",
                json!({
                    "type": "object",
                    "properties": {
                        "include_files": {
                            "type": "boolean",
                            "description": "Include per-file path, status, and patch-size metadata.",
                        }
                    },
                }),
            ),
            tool_definition(
                "verlato_get_suggestion_item",
                "Return metadata, commits, file manifest, and deterministic diff summary for one suggestion item.",
                json!({
                    "type": "object",
                    "required": ["item_id"],
                    "properties": {"item_id": item_id_property()},
                }),
            ),
        ];
        if self.uses_semantic_diff() {
            tools.push(tool_definition(
                "verlato_get_file_semantic_diff",
                "Return the IntentumDiff semantic diff for one file in one suggestion item: meaningful \
                 changes with their enclosing symbols, refactorings, moved code and ignored formatting.",
                json!({
                    "type": "object",
                    "required": ["item_id", "path"],
                    "properties": {
                        "item_id": item_id_property(),
                        "path": {"type": "string", "description": "Changed file path."},
                    },
                }),
            ));
            tools.push(tool_definition(
                "verlato_search_semantic_diff",
                "Search the IntentumDiff semantic diff of one suggestion item for a symbol or term.",
                json!({
                    "type": "object",
                    "required": ["item_id", "query"],
                    "properties": {
                        "item_id": item_id_property(),
                        "query": {"type": "string", "description": "Case-insensitive symbol or term."},
                        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_RESULTS},
                    },
                }),
            ));
            return tools;
        }
        tools.push(tool_definition(
            "verlato_get_file_patch",
            "Return a bounded patch chunk for one file in one suggestion item.",
            json!({
                "type": "object",
                "required": ["item_id", "path"],
                "properties": {
                    "item_id": item_id_property(),
                    "path": {"type": "string", "description": "Changed file path."},
                    "offset": {"type": "integer", "minimum": 0, "description": "Character offset into the patch."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PATCH_CHUNK_CHARS},
                },
            }),
        ));
        tools.push(tool_definition(
            "verlato_search_item_diff",
            "Search patch text for one suggestion item and return small matching snippets.",
            json!({
                "type": "object",
                "required": ["item_id", "query"],
                "properties": {
                    "item_id": item_id_property(),
                    "query": {"type": "string", "description": "Case-insensitive text to search in patches."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_RESULTS},
                },
            }),
        ));
        tools
    }

    /// `call_tool`: run a model tool call; errors are Python's `str(exception)`.
    pub fn call_tool(&self, name: &str, arguments: &Map<String, Value>) -> Result<Value, String> {
        let text = |e: anyhow::Error| e.to_string();
        match name {
            "verlato_list_suggestion_items" => {
                Ok(self.list_suggestion_items(arguments.get("include_files").is_some_and(truthy)))
            }
            "verlato_get_suggestion_item" => self
                .get_suggestion_item(&required_string(arguments, "item_id")?)
                .map_err(text),
            "verlato_get_file_patch" => {
                let item_id = required_string(arguments, "item_id")?;
                let path = required_string(arguments, "path")?;
                let offset = coerce_int(arguments.get("offset"), 0, 0, None);
                let limit = coerce_int(
                    arguments.get("limit"),
                    DEFAULT_PATCH_CHUNK_CHARS,
                    1,
                    Some(MAX_PATCH_CHUNK_CHARS),
                );
                self.get_file_patch(&item_id, &path, offset, limit)
                    .map_err(text)
            }
            "verlato_get_file_semantic_diff" => {
                let item_id = required_string(arguments, "item_id")?;
                let path = required_string(arguments, "path")?;
                self.get_file_semantic_diff(&item_id, &path).map_err(text)
            }
            "verlato_search_semantic_diff" | "verlato_search_item_diff" => {
                let item_id = required_string(arguments, "item_id")?;
                let query = required_string(arguments, "query")?;
                let limit = coerce_int(arguments.get("limit"), 5, 1, Some(MAX_SEARCH_RESULTS));
                if name == "verlato_search_semantic_diff" {
                    self.search_semantic_diff(&item_id, &query, limit)
                } else {
                    self.search_item_diff(&item_id, &query, limit)
                }
                .map_err(text)
            }
            _ => Err(crate::mcp::py_repr_str(&format!(
                "Unknown suggestion evidence tool: {name}"
            ))),
        }
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

pub(crate) fn commit_boundaries(commits: &[Commit]) -> Value {
    match (commits.first(), commits.last()) {
        (Some(first), Some(last)) => {
            json!({"first": boundary_payload(first), "last": boundary_payload(last)})
        }
        _ => json!({"first": null, "last": null}),
    }
}
