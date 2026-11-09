//! Small, dependency-free vocabularies: path areas, conventional commits, changelog kinds and
//! the item checksum. Ported 1:1 from `verlato/core/{areas,conventional,changelog,checksum}.py`.

use regex::Regex;
use sha2::{Digest, Sha256};
use std::sync::OnceLock;

// ---------------------------------------------------------------------------
// areas
// ---------------------------------------------------------------------------

const AREA_RULES: &[(&str, &[&str])] = &[
    ("api", &["api/", "openapi", "schema", "graphql"]),
    ("frontend", &["web/", "ui/", "frontend/"]),
    ("backend", &["server/", "backend/", "service/", "src/"]),
    ("docs", &["docs/", "readme", ".md"]),
    ("tests", &["test/", "tests/", "spec/"]),
    ("infra", &["terraform", "k8s", "helm", ".github/"]),
];

pub fn infer_area(path: &str) -> &'static str {
    let lower = path.to_lowercase();
    for (area, signals) in AREA_RULES {
        if signals.iter().any(|signal| lower.contains(signal)) {
            return area;
        }
    }
    "general"
}

pub fn is_docs_path(path: &str) -> bool {
    let lower = path.to_lowercase();
    lower.ends_with(".md") || lower.contains("docs/")
}

pub fn is_test_path(path: &str) -> bool {
    let lower = path.to_lowercase();
    lower.contains("test") || lower.contains("spec")
}

// ---------------------------------------------------------------------------
// conventional commits
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConventionalCommit {
    pub kind: String,
    pub scope: Option<String>,
    pub description: String,
    pub breaking: bool,
}

fn conventional_pattern() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| {
        Regex::new(
            r"^(?P<type>\w+)(?:\((?P<scope>[\w\-/]+)\))?(?P<breaking>!)?:\s(?P<description>.+)$",
        )
        .unwrap()
    })
}

fn breaking_text() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| Regex::new(r"(?i)BREAKING[- ]CHANGE").unwrap())
}

pub fn parse_conventional_commit(message: &str) -> Option<ConventionalCommit> {
    let trimmed = message.trim();
    let first = trimmed.lines().next().unwrap_or("");
    let captures = conventional_pattern().captures(first)?;
    Some(ConventionalCommit {
        kind: captures["type"].to_lowercase(),
        scope: captures.name("scope").map(|m| m.as_str().to_owned()),
        description: captures["description"].to_owned(),
        breaking: captures.name("breaking").is_some() || breaking_text().is_match(message),
    })
}

/// The first line of a commit message (Python: `message.splitlines()[0] if message else ""`).
pub fn subject(message: &str) -> &str {
    message.lines().next().unwrap_or("")
}

// ---------------------------------------------------------------------------
// changelog kinds
// ---------------------------------------------------------------------------

pub const SECTION_ORDER: &[&str] = &[
    "breaking",
    "added",
    "changed",
    "deprecated",
    "removed",
    "fixed",
    "security",
    "documentation",
    "performance",
    "refactoring",
    "tests",
    "build",
    "internal",
    "other",
];

fn section_title(kind: &str) -> Option<&'static str> {
    Some(match kind {
        "breaking" => "Breaking Changes",
        "added" => "Added",
        "changed" => "Changed",
        "deprecated" => "Deprecated",
        "removed" => "Removed",
        "fixed" => "Fixed",
        "security" => "Security",
        "documentation" => "Documentation",
        "performance" => "Performance",
        "refactoring" => "Refactoring",
        "tests" => "Tests",
        "build" => "Build",
        "internal" => "Internal",
        "other" => "Other",
        _ => return None,
    })
}

fn kind_alias(normalized: &str) -> Option<&'static str> {
    Some(match normalized {
        "add" | "added" | "feat" | "feature" | "features" | "new" => "added",
        "change" | "changed" => "changed",
        "breaking" | "breaking-change" | "breaking-changes" | "major" => "breaking",
        "deprecate" | "deprecated" | "deprecation" | "deprecations" => "deprecated",
        "remove" | "removed" | "removal" | "delete" | "deleted" => "removed",
        "fix" | "fixed" | "bugfix" | "bug-fix" => "fixed",
        "security" | "sec" => "security",
        "doc" | "docs" | "documentation" => "documentation",
        "perf" | "performance" => "performance",
        "refactor" | "refactoring" => "refactoring",
        "test" | "tests" | "testing" => "tests",
        "build" | "ci" | "deps" | "dependencies" => "build",
        "internal" | "chore" | "maintenance" | "style" | "styles" => "internal",
        "other" => "other",
        _ => return None,
    })
}

pub fn normalize_change_kind(value: &str) -> Option<&'static str> {
    if value.is_empty() {
        return None;
    }
    let lower = value.trim().to_lowercase();
    let mut normalized = String::with_capacity(lower.len());
    let mut last_dash = false;
    for ch in lower.chars() {
        if ch.is_ascii_lowercase() || ch.is_ascii_digit() {
            normalized.push(ch);
            last_dash = false;
        } else if !last_dash {
            normalized.push('-');
            last_dash = true;
        }
    }
    kind_alias(normalized.trim_matches('-'))
}

pub fn is_valid_change_kind(kind: &str) -> bool {
    SECTION_ORDER.contains(&kind)
}

pub fn changelog_section_title(kind: &str) -> String {
    let normalized = normalize_change_kind(kind).unwrap_or(kind);
    if let Some(title) = section_title(normalized) {
        return title.to_owned();
    }
    // Python: re.sub(r"[-_]+", " ", kind).strip().title() or "Other"
    let spaced: String = kind
        .chars()
        .map(|c| if c == '-' || c == '_' { ' ' } else { c })
        .collect();
    let titled = title_case(
        spaced
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ")
            .trim(),
    );
    if titled.is_empty() {
        "Other".to_owned()
    } else {
        titled
    }
}

fn title_case(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut start = true;
    for ch in text.chars() {
        if ch.is_alphabetic() {
            if start {
                out.extend(ch.to_uppercase());
            } else {
                out.extend(ch.to_lowercase());
            }
            start = false;
        } else {
            out.push(ch);
            start = true;
        }
    }
    out
}

/// Known kinds in section order, then any unknown kinds sorted.
pub fn ordered_change_kinds<'a>(kinds: impl IntoIterator<Item = &'a str>) -> Vec<String> {
    let mut unique: Vec<&str> = kinds.into_iter().filter(|k| !k.is_empty()).collect();
    unique.sort();
    unique.dedup();
    let mut ordered: Vec<String> = SECTION_ORDER
        .iter()
        .filter(|k| unique.contains(k))
        .map(|k| (*k).to_owned())
        .collect();
    ordered.extend(
        unique
            .iter()
            .filter(|k| !SECTION_ORDER.contains(k))
            .map(|k| (*k).to_owned()),
    );
    ordered
}

// ---------------------------------------------------------------------------
// checksum
// ---------------------------------------------------------------------------

pub const CHECKSUM_VERSION: &str = "v2";

/// Versioned checksum that includes classification area (Python `build_checksum`).
pub fn build_checksum(
    pr_number: Option<i64>,
    commit_shas: &[String],
    title: &str,
    area: &str,
) -> String {
    let pr = pr_number
        .map(|n| n.to_string())
        .unwrap_or_else(|| "None".to_owned());
    let identity = format!("{pr}:{}:{title}:{area}", commit_shas.join(","));
    hex::encode(Sha256::digest(identity.as_bytes()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn areas() {
        assert_eq!(infer_area("src/app.py"), "backend");
        assert_eq!(infer_area("web/app.ts"), "frontend");
        assert_eq!(infer_area("README.md"), "docs");
        assert_eq!(infer_area("api/openapi.yaml"), "api");
        assert_eq!(infer_area("Cargo.toml"), "general");
        assert!(is_test_path("tests/test_x.py"));
        assert!(is_docs_path("docs/a.txt"));
    }

    #[test]
    fn conventional() {
        let parsed = parse_conventional_commit("feat(api)!: add thing").unwrap();
        assert_eq!(parsed.kind, "feat");
        assert_eq!(parsed.scope.as_deref(), Some("api"));
        assert!(parsed.breaking);
        assert!(parse_conventional_commit("update stuff").is_none());
        assert!(
            parse_conventional_commit("fix: x\n\nBREAKING CHANGE: y")
                .unwrap()
                .breaking
        );
        assert_eq!(parse_conventional_commit("FIX: x").unwrap().kind, "fix");
    }

    #[test]
    fn kinds() {
        assert_eq!(normalize_change_kind("Feature"), Some("added"));
        assert_eq!(normalize_change_kind(" bug fix "), Some("fixed"));
        assert_eq!(normalize_change_kind("nope"), None);
        assert_eq!(changelog_section_title("feat"), "Added");
        assert_eq!(changelog_section_title("custom_kind"), "Custom Kind");
        assert_eq!(
            ordered_change_kinds(["zeta", "fixed", "added", "fixed"]),
            vec!["added".to_owned(), "fixed".to_owned(), "zeta".to_owned()]
        );
    }

    #[test]
    fn checksum_matches_python() {
        // python3 -c "import hashlib;print(hashlib.sha256(b'None:a,b:Title:backend').hexdigest())"
        assert_eq!(
            build_checksum(None, &["a".into(), "b".into()], "Title", "backend"),
            "82379fc0d4afd79c3d064606d6b2a4d3aa3769e6dce2f44d9780b70f342b7f66"
        );
    }
}
