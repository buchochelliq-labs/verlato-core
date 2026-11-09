//! Change evidence for a group: IntentumDiff semantic evidence plus path/status/patch signals.
//! Ports `verlato/core/diff_evidence.py`.

use indexmap::IndexMap;

use crate::models::{ChangedFile, Commit};
use crate::semantic::{build_semantic_diff, build_semantic_evidence, SemanticEvidence};
use crate::vocab::{infer_area, is_docs_path, is_test_path};

#[derive(Debug, Clone)]
pub struct DiffEvidence {
    pub files_by_area: IndexMap<String, Vec<String>>,
    pub file_statuses: IndexMap<String, String>,
    pub api_changes: Vec<String>,
    pub migration_changes: Vec<String>,
    pub config_changes: Vec<String>,
    pub docs_only: bool,
    pub tests_only: bool,
    pub representative_hunks: IndexMap<String, Vec<String>>,
    pub summary: String,
    pub semantic: Option<SemanticEvidence>,
    /// IntentumDiff's semantic diff records for the group (empty without semantic evidence).
    pub semantic_diff: Vec<serde_json::Value>,
}

pub fn build(files: &[ChangedFile], commits: Option<&[Commit]>) -> DiffEvidence {
    let mut files_by_area: IndexMap<String, Vec<String>> = IndexMap::new();
    let mut file_statuses: IndexMap<String, String> = IndexMap::new();
    let (mut api_changes, mut migration_changes, mut config_changes) =
        (Vec::new(), Vec::new(), Vec::new());
    let mut representative_hunks: IndexMap<String, Vec<String>> = IndexMap::new();

    for file in files {
        files_by_area
            .entry(infer_area(&file.path).to_owned())
            .or_default()
            .push(file.path.clone());
        file_statuses.insert(file.path.clone(), file.status.clone());
        let lower = file.path.to_lowercase();
        if ["public", "api", "interface", "sdk"]
            .iter()
            .any(|k| lower.contains(k))
        {
            api_changes.push(file.path.clone());
        }
        if ["migration", "migrations", "alembic"]
            .iter()
            .any(|k| lower.contains(k))
        {
            migration_changes.push(file.path.clone());
        }
        if ["config", ".env", "settings", "yaml", "yml", "toml"]
            .iter()
            .any(|k| lower.contains(k))
        {
            config_changes.push(file.path.clone());
        }
        let hunks = representative_hunk_lines(&file.patch, 2);
        if !hunks.is_empty() {
            representative_hunks.insert(file.path.clone(), hunks);
        }
    }

    let docs_only = !files.is_empty() && files.iter().all(|f| is_docs_path(&f.path));
    let tests_only = !files.is_empty() && files.iter().all(|f| is_test_path(&f.path));
    let mut summary = build_summary(
        &files_by_area,
        &file_statuses,
        &api_changes,
        &migration_changes,
        &config_changes,
        &representative_hunks,
    );
    let paths: Vec<String> = files.iter().map(|f| f.path.clone()).collect();
    let semantic = commits.filter(|c| !c.is_empty()).and_then(|c| {
        build_semantic_evidence(
            c.iter().filter_map(|commit| commit.semantics.as_ref()),
            Some(&paths),
        )
    });
    let semantic_diff = match (&semantic, commits) {
        (Some(_), Some(commits)) => build_semantic_diff(
            commits
                .iter()
                .filter_map(|commit| commit.semantics.as_ref()),
            Some(&paths),
        ),
        _ => Vec::new(),
    };
    if let Some(semantic) = &semantic {
        // The semantic diff replaces raw hunk excerpts, so the summary does not point at them.
        let without_hunks = build_summary(
            &files_by_area,
            &file_statuses,
            &api_changes,
            &migration_changes,
            &config_changes,
            &IndexMap::new(),
        );
        summary = format!("{}; {without_hunks}", semantic.summary);
    }
    DiffEvidence {
        files_by_area,
        file_statuses,
        api_changes,
        migration_changes,
        config_changes,
        docs_only,
        tests_only,
        representative_hunks,
        summary,
        semantic,
        semantic_diff,
    }
}

/// Python slices strings by code point; keep that behaviour.
fn take_chars(value: &str, limit: usize) -> &str {
    match value.char_indices().nth(limit) {
        Some((index, _)) => &value[..index],
        None => value,
    }
}

fn representative_hunk_lines(patch: &str, max_hunks: usize) -> Vec<String> {
    if patch.is_empty() {
        return Vec::new();
    }
    let mut hunks: Vec<String> = Vec::new();
    let mut current: Vec<String> = Vec::new();
    for line in patch.lines() {
        if line.starts_with("@@") {
            if !current.is_empty() {
                hunks.push(current.join("\n").trim().to_owned());
            }
            current = vec![line.to_owned()];
            continue;
        }
        if current.is_empty() || line.starts_with("+++") || line.starts_with("---") {
            continue;
        }
        if (line.starts_with('+') || line.starts_with('-')) && !matches!(line.trim(), "+" | "-") {
            current.push(take_chars(line, 160).to_owned());
        }
    }
    if !current.is_empty() {
        hunks.push(current.join("\n").trim().to_owned());
    }
    hunks
        .into_iter()
        .filter(|h| !h.is_empty())
        .map(|h| take_chars(&h, 500).to_owned())
        .take(max_hunks)
        .collect()
}

fn build_summary(
    files_by_area: &IndexMap<String, Vec<String>>,
    file_statuses: &IndexMap<String, String>,
    api_changes: &[String],
    migration_changes: &[String],
    config_changes: &[String],
    representative_hunks: &IndexMap<String, Vec<String>>,
) -> String {
    if file_statuses.is_empty() {
        return "No changed-file evidence was available from PR or commit metadata".to_owned();
    }
    let mut sorted: Vec<(&String, &String)> = file_statuses.iter().collect();
    sorted.sort();
    let mut changed_files: String = sorted
        .iter()
        .take(5)
        .map(|(path, status)| format!("{status} {path}"))
        .collect::<Vec<_>>()
        .join(", ");
    if file_statuses.len() > 5 {
        changed_files.push_str(&format!(", and {} more", file_statuses.len() - 5));
    }
    let mut status_counts: std::collections::BTreeMap<&str, usize> = Default::default();
    for status in file_statuses.values() {
        *status_counts.entry(status).or_default() += 1;
    }
    let status_text = status_counts
        .iter()
        .map(|(s, c)| format!("{s}:{c}"))
        .collect::<Vec<_>>()
        .join(", ");
    let mut areas: Vec<(&String, &Vec<String>)> = files_by_area.iter().collect();
    areas.sort_by(|a, b| a.0.cmp(b.0));
    let area_text = areas
        .iter()
        .map(|(a, p)| format!("{a}({})", p.len()))
        .collect::<Vec<_>>()
        .join(", ");
    let mut parts = vec![
        format!("changed files: {changed_files}"),
        format!("statuses: {status_text}"),
        format!("areas: {area_text}"),
    ];
    let first3 = |v: &[String]| v.iter().take(3).cloned().collect::<Vec<_>>().join(", ");
    if !api_changes.is_empty() {
        parts.push(format!("public-api: {}", first3(api_changes)));
    }
    if !migration_changes.is_empty() {
        parts.push(format!("migrations: {}", first3(migration_changes)));
    }
    if !config_changes.is_empty() {
        parts.push(format!("config: {}", first3(config_changes)));
    }
    let hunk_paths: Vec<String> = representative_hunks.keys().take(3).cloned().collect();
    if !hunk_paths.is_empty() {
        parts.push(format!("diff excerpts: {}", hunk_paths.join(", ")));
    }
    parts.join("; ")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn summary_and_flags() {
        let files = vec![
            ChangedFile {
                path: "src/app.py".into(),
                status: "modified".into(),
                patch: "@@ -1 +1 @@\n-a\n+b".into(),
            },
            ChangedFile {
                path: "config/settings.yaml".into(),
                status: "added".into(),
                patch: String::new(),
            },
        ];
        let evidence = build(&files, None);
        assert_eq!(
            evidence.summary,
            "changed files: added config/settings.yaml, modified src/app.py; statuses: added:1, modified:1; areas: backend(1), general(1); config: config/settings.yaml; diff excerpts: src/app.py"
        );
        assert_eq!(
            evidence.representative_hunks["src/app.py"],
            vec!["@@ -1 +1 @@\n-a\n+b".to_owned()]
        );
        assert!(!evidence.docs_only);
    }
}
