//! Git collection: commits in a range, per-commit changed files/patches, PR inference from
//! commit subjects, tags, and blob reads for the semantic engine. Ports
//! `verlato/core/{collector,adapters}.py` (standard-git and none adapters).

use anyhow::{bail, Context, Result};
use regex::Regex;
use std::collections::HashMap;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::OnceLock;

use crate::models::{ChangedFile, Commit, PullRequest};
use crate::vocab::subject;

const COMMIT_PATCH_MARKER: &str = "__VERLATO_COMMIT_PATCH__";

#[derive(Debug, Clone)]
pub struct Git {
    pub repo: PathBuf,
}

impl Git {
    pub fn new(repo: impl Into<PathBuf>) -> Self {
        Self { repo: repo.into() }
    }

    pub fn run(&self, args: &[&str]) -> Result<String> {
        let output = Command::new("git")
            .args(args)
            .current_dir(&self.repo)
            .stdin(Stdio::null())
            .output()
            .with_context(|| format!("failed to run git {}", args.join(" ")))?;
        if !output.status.success() {
            bail!(
                "git {} failed: {}",
                args.join(" "),
                String::from_utf8_lossy(&output.stderr).trim()
            );
        }
        Ok(String::from_utf8_lossy(&output.stdout).into_owned())
    }

    /// First-parent commits in `base..head`, oldest first, with changed files and patches.
    pub fn collect_commits(&self, base: &str, head: &str) -> Result<Vec<Commit>> {
        let range = format!("{base}..{head}");
        let log = self.run(&[
            "log",
            "--first-parent",
            "--pretty=format:%H%x09%P%x09%s%x09%an",
            &range,
        ])?;
        let mut commits: Vec<Commit> = log
            .lines()
            .filter(|line| !line.is_empty())
            .map(|line| {
                let mut parts = line.splitn(4, '\t');
                let sha = parts.next().unwrap_or("").to_owned();
                let parents = parts.next().unwrap_or("");
                let message = parts.next().unwrap_or("").to_owned();
                let author = parts.next().unwrap_or("").to_owned();
                Commit {
                    sha,
                    message,
                    author,
                    parent_sha: parents.split_whitespace().next().map(str::to_owned),
                    ..Commit::default()
                }
            })
            .collect();
        let shas: Vec<String> = commits.iter().map(|c| c.sha.clone()).collect();
        let mut files = self.changed_files_for_commits(&shas)?;
        for commit in &mut commits {
            commit.files = files.remove(&commit.sha).unwrap_or_default();
        }
        commits.reverse();
        Ok(commits)
    }

    pub fn changed_files_for_commits(
        &self,
        shas: &[String],
    ) -> Result<HashMap<String, Vec<ChangedFile>>> {
        if shas.is_empty() {
            return Ok(HashMap::new());
        }
        let format = format!("--format={COMMIT_PATCH_MARKER}%H");
        let mut args: Vec<&str> = vec![
            "show",
            "--first-parent",
            &format,
            "--find-renames",
            "--patch-with-raw",
        ];
        args.extend(shas.iter().map(String::as_str));
        Ok(parse_changed_files_by_commit(&self.run(&args)?))
    }

    pub fn latest_tag(&self, head: &str) -> Option<String> {
        self.run(&["describe", "--tags", "--abbrev=0", head])
            .ok()
            .map(|tag| tag.trim().to_owned())
            .filter(|tag| !tag.is_empty())
    }

    pub fn remote_origin_url(&self) -> Option<String> {
        self.run(&["config", "--get", "remote.origin.url"])
            .ok()
            .map(|url| url.trim().to_owned())
    }

    /// `git diff --raw` entries for `old..new`: (status letter, old path, new path).
    pub fn raw_changes(&self, old: &str, new: &str) -> Result<Vec<RawChange>> {
        let raw = self.run(&[
            "diff",
            "--raw",
            "--no-abbrev",
            "--find-renames",
            "-z",
            old,
            new,
        ])?;
        Ok(parse_raw_z(&raw))
    }

    /// Read many blobs (`rev:path` specs) in one `git cat-file --batch` process.
    /// Missing objects map to `None`.
    pub fn read_blobs(&self, specs: &[String]) -> Result<Vec<Option<Vec<u8>>>> {
        if specs.is_empty() {
            return Ok(Vec::new());
        }
        let mut child = Command::new("git")
            .args(["cat-file", "--batch"])
            .current_dir(&self.repo)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .context("failed to start git cat-file")?;
        let mut stdin = child.stdin.take().expect("piped stdin");
        let input: String = specs.iter().map(|spec| format!("{spec}\n")).collect();
        let writer = std::thread::spawn(move || stdin.write_all(input.as_bytes()));
        let output = child.wait_with_output().context("git cat-file failed")?;
        writer
            .join()
            .map_err(|_| anyhow::anyhow!("git cat-file writer panicked"))??;
        parse_cat_file_batch(&output.stdout, specs.len())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RawChange {
    pub status: char,
    pub old_path: String,
    pub new_path: String,
}

fn parse_raw_z(raw: &str) -> Vec<RawChange> {
    let mut changes = Vec::new();
    let mut fields = raw.split('\0').filter(|f| !f.is_empty());
    while let Some(meta) = fields.next() {
        let status = meta
            .rsplit(' ')
            .next()
            .and_then(|s| s.chars().next())
            .unwrap_or('M');
        let Some(first) = fields.next() else { break };
        let (old_path, new_path) = if matches!(status, 'R' | 'C') {
            let second = fields.next().unwrap_or(first);
            (first.to_owned(), second.to_owned())
        } else {
            (first.to_owned(), first.to_owned())
        };
        changes.push(RawChange {
            status,
            old_path,
            new_path,
        });
    }
    changes
}

fn parse_cat_file_batch(stdout: &[u8], expected: usize) -> Result<Vec<Option<Vec<u8>>>> {
    let mut results = Vec::with_capacity(expected);
    let mut cursor = 0usize;
    while results.len() < expected && cursor < stdout.len() {
        let line_end = stdout[cursor..]
            .iter()
            .position(|b| *b == b'\n')
            .map(|i| cursor + i)
            .ok_or_else(|| anyhow::anyhow!("truncated git cat-file header"))?;
        let header = String::from_utf8_lossy(&stdout[cursor..line_end]).into_owned();
        cursor = line_end + 1;
        if header.ends_with(" missing") || header.ends_with(" ambiguous") {
            results.push(None);
            continue;
        }
        let size: usize = header
            .rsplit(' ')
            .next()
            .and_then(|s| s.parse().ok())
            .ok_or_else(|| anyhow::anyhow!("unexpected git cat-file header: {header}"))?;
        let end = cursor + size;
        if end > stdout.len() {
            bail!("truncated git cat-file body");
        }
        results.push(Some(stdout[cursor..end].to_vec()));
        cursor = end + 1; // trailing newline
    }
    while results.len() < expected {
        results.push(None);
    }
    Ok(results)
}

pub fn parse_changed_files_by_commit(raw: &str) -> HashMap<String, Vec<ChangedFile>> {
    let mut by_commit = HashMap::new();
    let mut current_sha: Option<String> = None;
    let mut current: Vec<&str> = Vec::new();
    for line in raw.lines() {
        if let Some(sha) = line.strip_prefix(COMMIT_PATCH_MARKER) {
            if let Some(previous) = current_sha.take() {
                by_commit.insert(previous, parse_git_show_changed_files(&current.join("\n")));
            }
            current_sha = Some(sha.trim().to_owned());
            current.clear();
            continue;
        }
        current.push(line);
    }
    if let Some(previous) = current_sha {
        by_commit.insert(previous, parse_git_show_changed_files(&current.join("\n")));
    }
    by_commit
}

/// Parse `git show --patch-with-raw` output into changed files with statuses and patches.
pub fn parse_git_show_changed_files(raw: &str) -> Vec<ChangedFile> {
    let mut files: Vec<ChangedFile> = Vec::new();
    let mut patch_order: Vec<String> = Vec::new();
    let mut patch_by_path: HashMap<String, Vec<String>> = HashMap::new();
    let mut current_path: Option<String> = None;

    let push_patch = |order: &mut Vec<String>,
                      map: &mut HashMap<String, Vec<String>>,
                      path: &str,
                      line: &str| {
        if !map.contains_key(path) {
            order.push(path.to_owned());
        }
        map.entry(path.to_owned())
            .or_default()
            .push(line.to_owned());
    };

    for line in raw.lines() {
        if line.starts_with("diff --git ") {
            current_path = path_from_diff_header(line);
            if let Some(path) = &current_path {
                if !patch_by_path.contains_key(path) {
                    push_patch(&mut patch_order, &mut patch_by_path, path, line);
                }
            }
            continue;
        }
        if let Some(path) = current_path.clone() {
            push_patch(&mut patch_order, &mut patch_by_path, &path, line);
            if let Some(next) = line.strip_prefix("+++ b/") {
                current_path = Some(next.to_owned());
                push_patch(&mut patch_order, &mut patch_by_path, next, line);
            }
            continue;
        }
        if let Some((status, path)) =
            parse_name_status_line(line).or_else(|| parse_raw_diff_line(line))
        {
            files.push(ChangedFile {
                path,
                status,
                patch: String::new(),
            });
        }
    }

    for path in patch_order {
        let patch = patch_by_path
            .remove(&path)
            .unwrap_or_default()
            .join("\n")
            .trim()
            .to_owned();
        match files.iter_mut().rev().find(|f| f.path == path) {
            Some(file) => file.patch = patch,
            None => files.push(ChangedFile {
                path,
                status: "modified".into(),
                patch,
            }),
        }
    }
    files
}

fn normalize_status(code: &str) -> Option<String> {
    Some(
        match code.chars().next()? {
            'A' => "added",
            'M' => "modified",
            'D' => "removed",
            'R' => "renamed",
            'C' => "copied",
            _ => return None,
        }
        .to_owned(),
    )
}

fn parse_name_status_line(line: &str) -> Option<(String, String)> {
    if line.trim().is_empty() || !line.contains('\t') {
        return None;
    }
    let parts: Vec<&str> = line.split('\t').collect();
    let code = parts[0];
    if code.is_empty() || code.starts_with("@@") {
        return None;
    }
    Some((normalize_status(code)?, parts.last()?.to_string()))
}

fn parse_raw_diff_line(line: &str) -> Option<(String, String)> {
    if !line.starts_with(':') || !line.contains('\t') {
        return None;
    }
    let mut parts = line.split('\t');
    let metadata = parts.next()?;
    let paths: Vec<&str> = parts.collect();
    let code = metadata.rsplit(' ').next()?;
    Some((normalize_status(code)?, paths.last()?.to_string()))
}

fn path_from_diff_header(line: &str) -> Option<String> {
    let parts: Vec<&str> = line.split_whitespace().collect();
    if parts.len() < 4 {
        return None;
    }
    let path = parts[3];
    Some(path.strip_prefix("b/").unwrap_or(path).to_owned())
}

// ---------------------------------------------------------------------------
// PR mapping
// ---------------------------------------------------------------------------

/// The selected forge adapter. `StandardGit`, `None` and `GitHub` are core's built-ins
/// ([`crate::extensions::BUILTIN_FORGES`]); `Extension` is one registered by an embedding
/// binary through [`crate::extensions::Extensions::forge`].
#[derive(Clone)]
pub enum PrAdapter {
    StandardGit,
    None,
    /// Read-only PRs from the GitHub REST API (`VERLATO_PR_ADAPTER=github`).
    GitHub(GitHubApi),
    Extension(std::sync::Arc<dyn crate::extensions::ForgeAdapter>),
}

impl PrAdapter {
    pub fn name(&self) -> &str {
        match self {
            PrAdapter::StandardGit => "standard-git",
            PrAdapter::None => "none",
            PrAdapter::GitHub(_) => "github",
            PrAdapter::Extension(adapter) => adapter.name(),
        }
    }

    /// Whether the adapter makes network calls.
    pub fn is_remote(&self) -> bool {
        match self {
            PrAdapter::StandardGit | PrAdapter::None => false,
            PrAdapter::GitHub(_) => true,
            PrAdapter::Extension(adapter) => adapter.is_remote(),
        }
    }
}

impl std::fmt::Debug for PrAdapter {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "PrAdapter({})", self.name())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GitHubApi {
    pub repo: String,
    pub token: String,
    pub api_base: String,
}

impl GitHubApi {
    /// `GitHubApiAdapter.from_env`: needs `VERLATO_GITHUB_REPO` and `GITHUB_TOKEN`/`GH_TOKEN`.
    pub fn from_env() -> Option<GitHubApi> {
        let env = |name: &str| std::env::var(name).ok().filter(|v| !v.is_empty());
        Some(GitHubApi {
            repo: env("VERLATO_GITHUB_REPO")?,
            token: env("GITHUB_TOKEN").or_else(|| env("GH_TOKEN"))?,
            api_base: env("VERLATO_GITHUB_API_URL")
                .unwrap_or_else(|| "https://api.github.com".into())
                .trim_end_matches('/')
                .to_owned(),
        })
    }

    fn request_json(
        &self,
        path: &str,
        params: &[(&str, String)],
    ) -> anyhow::Result<serde_json::Value> {
        let query = if params.is_empty() {
            String::new()
        } else {
            format!(
                "?{}",
                params
                    .iter()
                    .map(|(k, v)| format!("{k}={}", url_encode(v)))
                    .collect::<Vec<_>>()
                    .join("&")
            )
        };
        let url = format!("{}{path}{query}", self.api_base);
        let headers = [
            ("Accept", "application/vnd.github+json".to_owned()),
            ("Authorization", format!("Bearer {}", self.token)),
            ("X-GitHub-Api-Version", "2022-11-28".to_owned()),
            ("User-Agent", "verlato".to_owned()),
        ];
        let text =
            crate::providers::http::get_text(&url, &headers, std::time::Duration::from_secs(60))
                .map_err(|e| anyhow::anyhow!("GitHub API request {path} failed: {e}"))?;
        Ok(serde_json::from_str(&text)?)
    }

    fn paginate(&self, path: &str) -> anyhow::Result<Vec<serde_json::Value>> {
        let mut results = Vec::new();
        for page in 1.. {
            let data = self.request_json(
                path,
                &[("page", page.to_string()), ("per_page", "100".into())],
            )?;
            let items = match data {
                serde_json::Value::Array(items) => items,
                serde_json::Value::Null => break,
                serde_json::Value::Object(ref o) if o.is_empty() => break,
                other => anyhow::bail!("Expected list payload for {path}, got {other}"),
            };
            if items.is_empty() {
                break;
            }
            let full = items.len() >= 100;
            results.extend(items);
            if !full {
                break;
            }
        }
        Ok(results)
    }

    fn pull_request(&self, number: i64) -> anyhow::Result<PullRequest> {
        let data = self.request_json(&format!("/repos/{}/pulls/{number}", self.repo), &[])?;
        anyhow::ensure!(
            data.is_object(),
            "Expected object payload for pull request details"
        );
        let text = |value: Option<&serde_json::Value>| {
            value
                .and_then(serde_json::Value::as_str)
                .unwrap_or("")
                .to_owned()
        };
        let files = self.paginate(&format!("/repos/{}/pulls/{number}/files", self.repo))?;
        Ok(PullRequest {
            number,
            title: text(data.get("title")).trim().to_owned(),
            body: text(data.get("body")),
            labels: data
                .get("labels")
                .and_then(serde_json::Value::as_array)
                .map(|labels| {
                    labels
                        .iter()
                        .map(|l| text(l.get("name")))
                        .filter(|n| !n.is_empty())
                        .collect()
                })
                .unwrap_or_default(),
            files: files
                .iter()
                .filter(|f| !text(f.get("filename")).is_empty())
                .map(|f| ChangedFile {
                    path: text(f.get("filename")),
                    status: f
                        .get("status")
                        .and_then(serde_json::Value::as_str)
                        .unwrap_or("modified")
                        .to_owned(),
                    patch: text(f.get("patch")),
                })
                .collect(),
            base_ref: text(data.get("base").and_then(|b| b.get("ref"))),
            head_ref: text(data.get("head").and_then(|h| h.get("ref"))),
        })
    }

    /// PRs for each commit; a failed commit lookup means "no PR", a failed PR fetch is an error.
    /// Public so forge adapters for other GitHub deployments can reuse the read-only lookup.
    pub fn map_commits(&self, commits: &[Commit]) -> anyhow::Result<HashMap<String, PullRequest>> {
        let mut by_commit: HashMap<String, Option<PullRequest>> = HashMap::new();
        let mut by_number: HashMap<i64, PullRequest> = HashMap::new();
        let mut mapping = HashMap::new();
        for commit in commits {
            if !by_commit.contains_key(&commit.sha) {
                let pulls = self
                    .request_json(
                        &format!("/repos/{}/commits/{}/pulls", self.repo, commit.sha),
                        &[],
                    )
                    .ok();
                let number = pulls
                    .as_ref()
                    .and_then(serde_json::Value::as_array)
                    .and_then(|p| p.first())
                    .and_then(|p| p.get("number"))
                    .and_then(serde_json::Value::as_i64);
                let pr = match number {
                    None => None,
                    Some(number) => Some(match by_number.get(&number) {
                        Some(pr) => pr.clone(),
                        None => {
                            let pr = self.pull_request(number)?;
                            by_number.insert(number, pr.clone());
                            pr
                        }
                    }),
                };
                by_commit.insert(commit.sha.clone(), pr);
            }
            if let Some(Some(pr)) = by_commit.get(&commit.sha) {
                mapping.insert(commit.sha.clone(), pr.clone());
            }
        }
        Ok(mapping)
    }
}

/// `urllib.parse.urlencode` value quoting (`quote_plus`).
fn url_encode(value: &str) -> String {
    value
        .bytes()
        .map(|b| match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'_' | b'.' | b'-' | b'~' => {
                (b as char).to_string()
            }
            b' ' => "+".into(),
            other => format!("%{other:02X}"),
        })
        .collect()
}

fn pr_patterns() -> &'static [Regex; 3] {
    static PATTERNS: OnceLock<[Regex; 3]> = OnceLock::new();
    PATTERNS.get_or_init(|| {
        [
            Regex::new(r"\(#(?P<num>\d+)\)").unwrap(),
            Regex::new(r"(?i)pull request #(?P<num>\d+)").unwrap(),
            Regex::new(r"(?i)merge pr #(?P<num>\d+)").unwrap(),
        ]
    })
}

pub fn extract_pr_number(subject: &str) -> Option<i64> {
    pr_patterns().iter().find_map(|pattern| {
        pattern
            .captures(subject)
            .and_then(|c| c["num"].parse().ok())
    })
}

/// Map commit SHAs to PRs inferred from commit subjects (Python `StandardGitAdapter`).
pub fn map_commits_to_prs(
    adapter: &PrAdapter,
    commits: &[Commit],
) -> anyhow::Result<HashMap<String, PullRequest>> {
    let mut mapping = HashMap::new();
    match adapter {
        PrAdapter::None => return Ok(mapping),
        PrAdapter::GitHub(api) => return api.map_commits(commits),
        PrAdapter::Extension(adapter) => return adapter.map_commits(commits),
        PrAdapter::StandardGit => {}
    }
    for commit in commits {
        let title = subject(&commit.message);
        if let Some(number) = extract_pr_number(title) {
            mapping.insert(
                commit.sha.clone(),
                PullRequest {
                    number,
                    title: title.to_owned(),
                    files: commit.files.clone(),
                    ..PullRequest::default()
                },
            );
        }
    }
    Ok(mapping)
}

/// GitHub-style URL for a git remote (Python `normalize_remote_url`).
pub fn normalize_remote_url(remote_url: &str) -> Option<String> {
    if remote_url.is_empty() {
        return None;
    }
    let strip_git = |s: &str| s.strip_suffix(".git").unwrap_or(s).to_owned();
    if let Some(path) = remote_url.strip_prefix("git@github.com:") {
        return Some(format!("https://github.com/{}", strip_git(path)));
    }
    if remote_url.starts_with("https://github.com/") {
        return Some(strip_git(remote_url));
    }
    if let Some(rest) = remote_url.strip_prefix("http://") {
        if rest.starts_with("github.com/") {
            return Some(format!("https://{}", strip_git(rest)));
        }
    }
    Some(strip_git(remote_url))
}

pub fn inside_git_worktree(path: &Path) -> bool {
    let Ok(resolved) = path.canonicalize() else {
        return false;
    };
    resolved
        .ancestors()
        .any(|candidate| candidate.join(".git").exists())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_patch_with_raw() {
        let raw = ":100644 100644 aaa bbb M\tsrc/app.py\n:000000 100644 000 ccc A\tdocs/new.md\n\ndiff --git a/src/app.py b/src/app.py\nindex aaa..bbb 100644\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new\ndiff --git a/docs/new.md b/docs/new.md\nnew file mode 100644\n--- /dev/null\n+++ b/docs/new.md\n@@ -0,0 +1 @@\n+hello";
        let files = parse_git_show_changed_files(raw);
        assert_eq!(files.len(), 2);
        assert_eq!(files[0].path, "src/app.py");
        assert_eq!(files[0].status, "modified");
        assert!(files[0].patch.starts_with("diff --git a/src/app.py"));
        assert!(files[0].patch.ends_with("+new"));
        assert_eq!(files[1].status, "added");
        assert!(files[1].patch.contains("+hello"));
    }

    #[test]
    fn pr_numbers() {
        assert_eq!(extract_pr_number("Add thing (#12)"), Some(12));
        assert_eq!(extract_pr_number("Merge pull request #7 from x/y"), Some(7));
        assert_eq!(extract_pr_number("plain"), None);
    }

    #[test]
    fn raw_z_with_renames() {
        let raw = ":100644 100644 a b M\0src/a.py\0:100644 100644 a b R091\0old.py\0new.py\0";
        let changes = parse_raw_z(raw);
        assert_eq!(
            changes[0],
            RawChange {
                status: 'M',
                old_path: "src/a.py".into(),
                new_path: "src/a.py".into()
            }
        );
        assert_eq!(
            changes[1],
            RawChange {
                status: 'R',
                old_path: "old.py".into(),
                new_path: "new.py".into()
            }
        );
    }

    #[test]
    fn remote_urls() {
        assert_eq!(
            normalize_remote_url("git@github.com:o/r.git").as_deref(),
            Some("https://github.com/o/r")
        );
        assert_eq!(
            normalize_remote_url("http://github.com/o/r.git").as_deref(),
            Some("https://github.com/o/r")
        );
    }
}
