//! Groups commits into PR-level change sets. Ports `verlato/core/grouper.py`.
//!
//! Commit intent inference (model-inferred conventional metadata) is not part of the Rust
//! pipeline yet, so the effective conventional commit is always the authored one.

use std::collections::{BTreeSet, HashMap};

use indexmap::IndexMap;

use crate::models::{ChangeGroup, ChangedFile, Commit, PullRequest};
use crate::vocab::{infer_area, parse_conventional_commit};

const USER_FACING_AREAS: &[&str] = &["api", "frontend", "backend", "infra", "general"];

pub fn group(commits: &[Commit], prs_by_commit: &HashMap<String, PullRequest>) -> Vec<ChangeGroup> {
    let mut grouped: IndexMap<i64, Vec<Commit>> = IndexMap::new();
    let mut no_pr: Vec<Commit> = Vec::new();
    for commit in commits {
        match prs_by_commit.get(&commit.sha) {
            Some(pr) => grouped.entry(pr.number).or_default().push(commit.clone()),
            None => no_pr.push(commit.clone()),
        }
    }
    let mut results = Vec::new();
    for (_, pr_commits) in grouped {
        let pr = prs_by_commit[&pr_commits[0].sha].clone();
        results.extend(split_pr_group(pr, pr_commits));
    }
    results.extend(group_non_pr_commits(no_pr));
    results
}

#[derive(Clone, PartialEq, Eq)]
enum GroupKey {
    Single(String),
    ConventionalScope(String, String),
    FileOverlap(Vec<String>),
}

fn non_pr_group_key(commit: &Commit) -> GroupKey {
    match parse_conventional_commit(&commit.message) {
        Some(parsed) => match parsed.scope {
            Some(scope) => {
                GroupKey::ConventionalScope(parsed.kind.to_lowercase(), scope.to_lowercase())
            }
            None => GroupKey::Single(commit.sha.clone()),
        },
        None => GroupKey::Single(commit.sha.clone()),
    }
}

fn non_pr_group_reason(key: &GroupKey) -> String {
    match key {
        GroupKey::ConventionalScope(kind, scope) => format!(
            "Grouped because these non-PR commits share conventional type `{kind}` and scope `{scope}`."
        ),
        GroupKey::FileOverlap(files) => {
            let shown: Vec<&str> = files.iter().take(3).map(String::as_str).collect();
            let suffix = if shown.is_empty() { ".".to_owned() } else { format!(": {}", shown.join(", ")) };
            format!("Grouped because adjacent non-PR commits touch overlapping substantive files{suffix}")
        }
        GroupKey::Single(_) => "Kept separate because no PR or explicit conventional scope was found.".to_owned(),
    }
}

fn group_non_pr_commits(commits: Vec<Commit>) -> Vec<ChangeGroup> {
    let mut groups = Vec::new();
    let mut bucket: Vec<Commit> = Vec::new();
    let mut bucket_key: Option<GroupKey> = None;
    let mut bucket_paths: BTreeSet<String> = BTreeSet::new();

    let flush =
        |bucket: &mut Vec<Commit>, key: &Option<GroupKey>, groups: &mut Vec<ChangeGroup>| {
            if bucket.is_empty() {
                return;
            }
            let commits = std::mem::take(bucket);
            groups.push(ChangeGroup {
                pr: None,
                files: commit_files(&commits),
                commits,
                grouping_reason: key
                    .as_ref()
                    .map(non_pr_group_reason)
                    .unwrap_or_else(|| "Grouped as an individual non-PR commit.".to_owned()),
            });
        };

    for commit in commits {
        let key = non_pr_group_key(&commit);
        let commit_paths = substantive_paths(&commit.files);
        if bucket.is_empty() {
            bucket_paths = commit_paths;
            bucket.push(commit);
            bucket_key = Some(key);
            continue;
        }
        if bucket_key.as_ref() == Some(&key) && !matches!(key, GroupKey::Single(_)) {
            bucket_paths.extend(commit_paths);
            bucket.push(commit);
            continue;
        }
        let overlap: Vec<String> = bucket_paths.intersection(&commit_paths).cloned().collect();
        if !overlap.is_empty() {
            bucket_paths.extend(commit_paths);
            bucket.push(commit);
            bucket_key = Some(GroupKey::FileOverlap(overlap.into_iter().take(5).collect()));
            continue;
        }
        flush(&mut bucket, &bucket_key, &mut groups);
        bucket_paths = commit_paths;
        bucket.push(commit);
        bucket_key = Some(key);
    }
    flush(&mut bucket, &bucket_key, &mut groups);
    groups
}

fn split_pr_group(pr: PullRequest, commits: Vec<Commit>) -> Vec<ChangeGroup> {
    if let Some(scoped) = split_pr_group_by_scope(&pr, &commits) {
        return scoped;
    }
    let files = commit_files(&commits);
    let whole = |pr: PullRequest, commits: Vec<Commit>, files: Vec<ChangedFile>| {
        let reason = format!("Grouped by PR #{}.", pr.number);
        vec![ChangeGroup {
            pr: Some(pr),
            commits,
            files,
            grouping_reason: reason,
        }]
    };
    if pr.files.is_empty() {
        return whole(pr, commits, files);
    }
    let mut areas: Vec<&str> = Vec::new();
    for file in &pr.files {
        let area = infer_area(&file.path);
        if USER_FACING_AREAS.contains(&area) && !areas.contains(&area) {
            areas.push(area);
        }
    }
    if areas.len() <= 1 {
        return whole(pr, commits, files);
    }
    areas.sort();
    let mut split = Vec::new();
    for area in areas {
        let area_files: Vec<ChangedFile> = pr
            .files
            .iter()
            .filter(|f| infer_area(&f.path) == area)
            .cloned()
            .collect();
        if area_files.is_empty() {
            continue;
        }
        let area_commit_files: Vec<ChangedFile> = files
            .iter()
            .filter(|f| infer_area(&f.path) == area)
            .cloned()
            .collect();
        let area_pr = PullRequest {
            title: format!("{} [{area}]", pr.title),
            files: area_files,
            ..pr.clone()
        };
        split.push(ChangeGroup {
            pr: Some(area_pr),
            commits: commits.clone(),
            files: area_commit_files,
            grouping_reason: format!(
                "Grouped by PR #{}, then split by changed area `{area}`.",
                pr.number
            ),
        });
    }
    if split.is_empty() {
        let reason = format!("Grouped by PR #{}.", pr.number);
        return vec![ChangeGroup {
            pr: Some(pr),
            commits,
            files: Vec::new(),
            grouping_reason: reason,
        }];
    }
    split
}

fn split_pr_group_by_scope(pr: &PullRequest, commits: &[Commit]) -> Option<Vec<ChangeGroup>> {
    if commits.len() < 2 {
        return None;
    }
    let mut buckets: IndexMap<String, Vec<Commit>> = IndexMap::new();
    for commit in commits {
        let parsed = parse_conventional_commit(&commit.message)?;
        let scope = parsed.scope?;
        buckets
            .entry(scope.to_lowercase())
            .or_default()
            .push(commit.clone());
    }
    if buckets.len() <= 1 {
        return None;
    }
    Some(
        buckets
            .into_iter()
            .map(|(scope, scope_commits)| {
                let files = commit_files(&scope_commits);
                let scope_pr = PullRequest { title: format!("{} [{scope}]", pr.title), files: files.clone(), ..pr.clone() };
                ChangeGroup {
                    pr: Some(scope_pr),
                    commits: scope_commits,
                    files,
                    grouping_reason: format!(
                        "Grouped by PR #{}, then split because the PR contains multiple explicit conventional \
                         scopes; this changeset uses scope `{scope}`.",
                        pr.number
                    ),
                }
            })
            .collect(),
    )
}

/// Union of commit files in first-seen order, concatenating distinct patches.
pub fn commit_files(commits: &[Commit]) -> Vec<ChangedFile> {
    let mut files: Vec<ChangedFile> = Vec::new();
    for commit in commits {
        for file in &commit.files {
            if let Some(existing) = files.iter_mut().find(|f| f.path == file.path) {
                if !file.patch.is_empty() && !existing.patch.contains(&file.patch) {
                    existing.patch = [existing.patch.as_str(), file.patch.as_str()]
                        .into_iter()
                        .filter(|p| !p.is_empty())
                        .collect::<Vec<_>>()
                        .join("\n");
                }
                if !file.status.is_empty() {
                    existing.status = file.status.clone();
                }
                continue;
            }
            files.push(file.clone());
        }
    }
    files
}

fn substantive_paths(files: &[ChangedFile]) -> BTreeSet<String> {
    files
        .iter()
        .filter(|f| !f.path.is_empty() && !is_generated_release_artifact(&f.path))
        .map(|f| f.path.clone())
        .collect()
}

pub fn is_generated_release_artifact(path: &str) -> bool {
    let normalized = path.replace('\\', "/").to_lowercase();
    matches!(
        normalized.as_str(),
        "changelog.md"
            | ".github/release.md"
            | ".verlato_state.json"
            | ".verlato_suggestion_cache.json"
            | ".verlato_semantic_cache.json"
    ) || normalized.ends_with(".vsix")
        || normalized.ends_with(".tgz")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn commit(sha: &str, message: &str, paths: &[&str]) -> Commit {
        Commit {
            sha: sha.into(),
            message: message.into(),
            files: paths
                .iter()
                .map(|p| ChangedFile {
                    path: (*p).into(),
                    status: "modified".into(),
                    patch: String::new(),
                })
                .collect(),
            ..Commit::default()
        }
    }

    #[test]
    fn non_pr_grouping_by_scope_and_overlap() {
        let commits = vec![
            commit("a", "feat(api): one", &["api/a.py"]),
            commit("b", "fix(api): two", &["api/b.py"]),
            commit("c", "feat(api): three", &["api/c.py"]),
            commit("d", "tidy", &["api/c.py"]),
            commit("e", "unrelated", &["web/x.ts"]),
        ];
        let groups = group(&commits, &HashMap::new());
        let shas: Vec<Vec<&str>> = groups
            .iter()
            .map(|g| g.commits.iter().map(|c| c.sha.as_str()).collect())
            .collect();
        assert_eq!(shas, vec![vec!["a"], vec!["b"], vec!["c", "d"], vec!["e"]]);
        assert!(groups[2]
            .grouping_reason
            .contains("overlapping substantive files: api/c.py"));
    }

    #[test]
    fn pr_area_split() {
        let commits = vec![commit("a", "Big (#3)", &["web/a.ts", "server/b.py"])];
        let prs = crate::git::map_commits_to_prs(crate::git::PrAdapter::StandardGit, &commits);
        let groups = group(&commits, &prs);
        assert_eq!(groups.len(), 2);
        assert_eq!(groups[0].pr.as_ref().unwrap().title, "Big (#3) [backend]");
        assert_eq!(groups[1].pr.as_ref().unwrap().title, "Big (#3) [frontend]");
    }
}
