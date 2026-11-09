//! Markdown rendering and draft documents. Ports `verlato/core/{generator,drafts}.py`.

use anyhow::{anyhow, Result};
use indexmap::IndexMap;
use regex::Regex;
use serde::Serialize;
use serde_json::{Map, Value};
use std::path::Path;

use crate::config::{resolve_target_path, Config, OutputTarget};
use crate::git::{inside_git_worktree, normalize_remote_url, Git};
use crate::models::ChangeItem;
use crate::vocab::{changelog_section_title, normalize_change_kind, ordered_change_kinds};

pub fn today() -> String {
    chrono::Local::now()
        .date_naive()
        .format("%Y-%m-%d")
        .to_string()
}

fn item_kind(item: &ChangeItem) -> String {
    normalize_change_kind(&item.classification.kind)
        .map(str::to_owned)
        .unwrap_or_else(|| item.classification.kind.clone())
}

fn render_sections(
    items: &[ChangeItem],
    include_summary: bool,
    include_confidence: bool,
    include_provenance: bool,
) -> Vec<String> {
    let mut by_kind: IndexMap<String, Vec<&ChangeItem>> = IndexMap::new();
    for item in items {
        by_kind.entry(item_kind(item)).or_default().push(item);
    }
    let mut lines = Vec::new();
    for key in ordered_change_kinds(by_kind.keys().map(String::as_str)) {
        let Some(section) = by_kind.get(&key).filter(|s| !s.is_empty()) else {
            continue;
        };
        lines.push(format!("### {}", changelog_section_title(&key)));
        for item in section {
            let source = item
                .source
                .pr_number
                .map(|n| format!(" (PR #{n})"))
                .unwrap_or_default();
            let mut extras = Vec::new();
            if include_confidence {
                extras.push(format!("confidence {:.2}", item.confidence));
            }
            if include_provenance {
                extras.push(format!(
                    "source {}..{}",
                    item.source.base_ref, item.source.head_ref
                ));
                if !item.lineage_releases.is_empty() {
                    extras.push(format!("prior {}", item.lineage_releases.join(", ")));
                }
            }
            let suffix = if extras.is_empty() {
                String::new()
            } else {
                format!(" [{}]", extras.join("; "))
            };
            if include_summary || !item.content.summary.is_empty() {
                lines.push(format!(
                    "- {}{source}: {}{suffix}",
                    item.content.title, item.content.summary
                ));
            } else {
                lines.push(format!("- {}{source}{suffix}", item.content.title));
            }
        }
        lines.push(String::new());
    }
    if lines.is_empty() {
        lines.extend([
            "### No Changes".to_owned(),
            "- No notable changes were detected for this range.".to_owned(),
            String::new(),
        ]);
    }
    lines
}

fn finish(header: String, body: Vec<String>) -> String {
    let mut lines = vec![header, String::new()];
    lines.extend(body);
    format!("{}\n", lines.join("\n").trim())
}

pub fn render_github_release(
    items: &[ChangeItem],
    version: &str,
    confidence: bool,
    provenance: bool,
) -> String {
    finish(
        format!("## {version} - {}", today()),
        render_sections(items, false, confidence, provenance),
    )
}

pub fn render_changelog_entry(
    items: &[ChangeItem],
    version: &str,
    confidence: bool,
    provenance: bool,
) -> String {
    finish(
        format!("### {version} ({})", today()),
        render_sections(items, true, confidence, provenance),
    )
}

#[derive(Debug, Clone, Serialize)]
pub struct DraftTarget {
    pub name: String,
    pub path: String,
    pub format: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct DraftItem {
    pub id: String,
    pub title: String,
    pub summary: String,
    pub original_summary: String,
    pub summary_source: String,
    pub classification_source: String,
    pub evidence_summary: String,
    pub kind: String,
    pub area: String,
    pub audience: String,
    pub semver: String,
    pub confidence: f64,
    pub pr_number: Option<i64>,
    pub commit_shas: Vec<String>,
    pub base_ref: String,
    pub head_ref: String,
    pub files: Vec<String>,
    pub commit_signals: Vec<String>,
    pub derived_conventional_commits: Vec<Value>,
    pub grouping_reason: String,
    pub status: String,
    pub reviewed_by: Option<String>,
    pub reviewed_at: Option<String>,
    pub edited_summary: Option<String>,
    pub summary_provenance: Option<Map<String, Value>>,
    pub hidden: bool,
    pub semantic: Option<Value>,
    pub diff_engine: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct DraftSection {
    pub kind: String,
    pub title: String,
    pub items: Vec<DraftItem>,
}

#[derive(Debug, Clone, Serialize)]
pub struct DraftDocument {
    pub version: String,
    pub generated_on: String,
    pub repository_url: Option<String>,
    pub base_ref: String,
    pub head_ref: String,
    pub targets: Vec<DraftTarget>,
    pub sections: Vec<DraftSection>,
    pub rendered: IndexMap<String, String>,
    pub item_count: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct ApplyResult {
    pub target: String,
    pub path: String,
    pub format: String,
    pub action: String,
    pub bytes_written: usize,
}

fn with_review_content(item: &ChangeItem) -> ChangeItem {
    let mut item = item.clone();
    if let Some(edited) = item.edited_summary.clone().filter(|s| !s.is_empty()) {
        item.content.summary = edited;
    }
    item
}

#[allow(clippy::too_many_arguments)]
pub fn build_document(
    config: &Config,
    items: &[ChangeItem],
    base_ref: &str,
    head_ref: &str,
    version: &str,
    include_confidence: bool,
    include_provenance: bool,
    cwd: &Path,
) -> DraftDocument {
    let mut targets: Vec<DraftTarget> = config
        .targets
        .iter()
        .map(|(name, t)| DraftTarget {
            name: name.clone(),
            path: t.path.clone(),
            format: t.format.clone(),
        })
        .collect();
    targets.sort_by(|a, b| a.name.cmp(&b.name));
    let render_items: Vec<ChangeItem> = items
        .iter()
        .filter(|i| !i.hidden)
        .map(with_review_content)
        .collect();
    let mut rendered = IndexMap::new();
    rendered.insert(
        "github-release".to_owned(),
        render_github_release(
            &render_items,
            version,
            include_confidence,
            include_provenance,
        ),
    );
    rendered.insert(
        "changelog".to_owned(),
        render_changelog_entry(
            &render_items,
            version,
            include_confidence,
            include_provenance,
        ),
    );
    DraftDocument {
        version: version.into(),
        generated_on: today(),
        repository_url: repository_url(cwd),
        base_ref: base_ref.into(),
        head_ref: head_ref.into(),
        targets,
        sections: build_sections(items),
        rendered,
        item_count: items.len(),
    }
}

fn repository_url(cwd: &Path) -> Option<String> {
    if !inside_git_worktree(cwd) {
        return None;
    }
    normalize_remote_url(&Git::new(cwd).remote_origin_url()?)
}

fn build_sections(items: &[ChangeItem]) -> Vec<DraftSection> {
    let mut grouped: IndexMap<String, Vec<DraftItem>> = IndexMap::new();
    for item in items {
        let kind = item_kind(item);
        let semantic = item
            .evidence
            .diff_summary
            .get("semantic")
            .filter(|v| v.is_object())
            .cloned();
        grouped.entry(kind.clone()).or_default().push(DraftItem {
            id: item.id.clone(),
            title: item.content.title.clone(),
            summary: item
                .edited_summary
                .clone()
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| item.content.summary.clone()),
            original_summary: item.content.summary.clone(),
            summary_source: summary_source(item, semantic.is_some()),
            classification_source: classification_source(item),
            evidence_summary: evidence_summary(item),
            kind,
            area: item.classification.area.clone(),
            audience: item.classification.audience.clone(),
            semver: item.classification.semver.clone(),
            confidence: item.confidence,
            pr_number: item.source.pr_number,
            commit_shas: item.source.commit_shas.clone(),
            base_ref: item.source.base_ref.clone(),
            head_ref: item.source.head_ref.clone(),
            files: item.evidence.files.clone(),
            commit_signals: item.evidence.commit_signals.clone(),
            derived_conventional_commits: item.evidence.derived_conventional_commits.clone(),
            grouping_reason: item
                .evidence
                .diff_summary
                .get("grouping_reason")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned(),
            status: if item.hidden {
                "hidden".into()
            } else {
                item.status.clone()
            },
            reviewed_by: item.reviewed_by.clone(),
            reviewed_at: item.reviewed_at.clone(),
            edited_summary: item.edited_summary.clone(),
            summary_provenance: item.summary_provenance.clone(),
            hidden: item.hidden,
            diff_engine: if semantic.is_some() {
                "intentumdiff".into()
            } else {
                "git".into()
            },
            semantic,
        });
    }
    ordered_change_kinds(grouped.keys().map(String::as_str))
        .into_iter()
        .filter_map(|key| {
            grouped.get(&key).map(|items| DraftSection {
                title: changelog_section_title(&key),
                kind: key.clone(),
                items: items.clone(),
            })
        })
        .collect()
}

fn signal_suffix<'a>(item: &'a ChangeItem, prefix: &str) -> Option<&'a str> {
    item.evidence
        .commit_signals
        .iter()
        .find(|s| s.starts_with(prefix))
        .map(|s| s.split_once(':').map_or("", |(_, rest)| rest))
}

fn summary_source(item: &ChangeItem, has_semantic: bool) -> String {
    if let Some(provenance) = &item.summary_provenance {
        let source = provenance
            .get("source")
            .and_then(Value::as_str)
            .unwrap_or("");
        let model = provenance
            .get("model")
            .and_then(Value::as_str)
            .filter(|m| !m.is_empty())
            .or_else(|| {
                provenance
                    .get("provider")
                    .and_then(Value::as_str)
                    .filter(|m| !m.is_empty())
            })
            .unwrap_or("unknown");
        match source {
            "human-edited-model" => return format!("Human edited summary, originally generated by LLM ({model})"),
            "model-generated" | "model-suggestion" => return format!("LLM summary generated from diff evidence ({model})"),
            "human-edited" => return "Human edited summary, originally generated from deterministic diff evidence".into(),
            _ => {}
        }
    }
    let provider = signal_suffix(item, "provider-summary:");
    if item
        .edited_summary
        .as_deref()
        .is_some_and(|s| !s.is_empty())
    {
        return match provider {
            Some(model) => format!("Human edited summary, originally generated by LLM ({model})"),
            None => {
                "Human edited summary, originally generated from deterministic diff evidence".into()
            }
        };
    }
    if let Some(model) = provider {
        return format!("LLM summary generated from diff evidence ({model})");
    }
    if has_semantic {
        return "Deterministic summary generated from IntentumDiff semantic evidence".into();
    }
    "Deterministic summary generated from changed-file evidence".into()
}

fn classification_source(item: &ChangeItem) -> String {
    let signals = &item.evidence.commit_signals;
    if let Some(model) = signal_suffix(item, "provider-classification:") {
        return format!("LLM classification assist ({model})");
    }
    if signals
        .iter()
        .any(|s| s.starts_with("inferred-conventional:"))
    {
        if signals.iter().any(|s| s.starts_with("conventional:")) {
            return "Deterministic classification from conventional commit signal with inferred scope".into();
        }
        return "Deterministic classification from inferred conventional commit evidence".into();
    }
    if signals.iter().any(|s| s.starts_with("conventional:")) {
        return "Deterministic classification from conventional commit signal".into();
    }
    if signals.iter().any(|s| s.starts_with("label:")) {
        return "Deterministic classification from labels and changed files".into();
    }
    if signals.iter().any(|s| s == "semantic-classified") {
        return "Deterministic classification from IntentumDiff semantic review".into();
    }
    "Deterministic classification from changed-file evidence".into()
}

fn evidence_summary(item: &ChangeItem) -> String {
    if let Some(summary) = item
        .evidence
        .diff_summary
        .get("summary")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        return summary.to_owned();
    }
    if !item.evidence.files.is_empty() {
        return format!(
            "Changed files: {}",
            item.evidence
                .files
                .iter()
                .take(5)
                .cloned()
                .collect::<Vec<_>>()
                .join(", ")
        );
    }
    "No changed-file evidence recorded".into()
}

fn resolve_target(
    config: &Config,
    name: &str,
    explicit_path: Option<&str>,
) -> Result<OutputTarget> {
    if let Some(path) = explicit_path {
        let format = if name == "github-release" {
            "github-release"
        } else {
            "changelog"
        };
        return Ok(OutputTarget {
            path: path.into(),
            format: format.into(),
        });
    }
    config
        .targets
        .get(name)
        .cloned()
        .ok_or_else(|| anyhow!("Unknown draft target: {name}"))
}

pub fn apply_document(
    config: &Config,
    document: &DraftDocument,
    target_name: &str,
    target_path: Option<&str>,
    cwd: &Path,
) -> Result<ApplyResult> {
    let target = resolve_target(config, target_name, target_path)?;
    let path = resolve_target_path(&target.path, cwd);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let rendered = document
        .rendered
        .get(&target.format)
        .ok_or_else(|| anyhow!("Unsupported draft target format: {}", target.format))?;
    let (updated, action) = if target.format == "changelog" {
        let existing = std::fs::read_to_string(&path).unwrap_or_default();
        apply_changelog(&existing, rendered, &document.version)
    } else {
        (rendered.clone(), "replace".to_owned())
    };
    std::fs::write(&path, &updated)?;
    Ok(ApplyResult {
        target: target_name.into(),
        path: path.to_string_lossy().into_owned(),
        format: target.format,
        action,
        bytes_written: updated.len(),
    })
}

/// Insert or replace a version entry in an existing changelog (Python `_apply_changelog`).
pub fn apply_changelog(existing: &str, rendered: &str, version: &str) -> (String, String) {
    let heading = Regex::new(&format!(r"(?m)^### {} \([^)]+\)$", regex::escape(version))).unwrap();
    if let Some(found) = heading.find(existing) {
        // Replace from this heading up to the next "### " heading (or end of file).
        // Replace through the next *version* heading; entries contain "### Added"-style
        // subsections that belong to the entry being replaced.
        let after = &existing[found.end()..];
        let next = Regex::new(r"(?m)^### [^\n]* \([^)\n]+\)$").unwrap();
        let end = next
            .find(after)
            .map(|m| found.end() + m.start())
            .unwrap_or(existing.len());
        let updated = format!(
            "{}{}\n\n{}",
            &existing[..found.start()],
            rendered.trim_end(),
            &existing[end..]
        );
        return (updated, "replace".into());
    }
    if existing.trim().is_empty() {
        return (rendered.to_owned(), "create".into());
    }
    let lines: Vec<&str> = existing.lines().collect();
    if lines.first().is_some_and(|l| l.starts_with('#')) {
        let prefix = format!("{}\n\n", lines[0]);
        let suffix = lines[1..].join("\n");
        let suffix = suffix.trim_start_matches('\n');
        let mut updated = prefix + rendered.trim_end() + "\n";
        if !suffix.is_empty() {
            updated.push('\n');
            updated.push_str(suffix);
        }
        return (updated, "insert".into());
    }
    (
        format!(
            "{}\n\n{}",
            rendered.trim_end(),
            existing.trim_start_matches('\n')
        ),
        "prepend".into(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn changelog_apply_modes() {
        let rendered = "### v2 (2026-01-02)\n\n### Added\n- New\n";
        assert_eq!(
            apply_changelog("", rendered, "v2"),
            (rendered.to_owned(), "create".to_owned())
        );
        let (updated, action) = apply_changelog(
            "# Changelog\n\n### v1 (2026-01-01)\n- Old\n",
            rendered,
            "v2",
        );
        assert_eq!(action, "insert");
        assert_eq!(
            updated,
            "# Changelog\n\n### v2 (2026-01-02)\n\n### Added\n- New\n\n### v1 (2026-01-01)\n- Old"
        );
        let existing = "### v2 (2025-12-31)\n\n### Fixed\n- Stale\n\n### v1 (2026-01-01)\n- Old\n";
        let (updated, action) = apply_changelog(existing, rendered, "v2");
        assert_eq!(action, "replace");
        assert!(updated.starts_with("### v2 (2026-01-02)"));
        assert!(updated.contains("### v1 (2026-01-01)\n- Old"));
        assert!(!updated.contains("Stale"));
    }
}
