//! Classification of a change group into a `ChangeItem` (ports `verlato/core/classifier.py`).
//! Model assists (summary rewrite, diff-fallback classification) live in `crate::assist`.

use regex::Regex;
use serde_json::{json, Map, Value};
use std::sync::OnceLock;

use crate::assist::{self, ModelAssist};
use crate::evidence::{self, DiffEvidence};
use crate::models::{
    changed_files_for_group, default_review_status, ChangeGroup, ChangeItem, Classification,
    Commit, Content, Evidence, SourceRef,
};
use crate::semantic::SemanticEvidence;
use crate::vocab::{
    build_checksum, normalize_change_kind, parse_conventional_commit, subject, ConventionalCommit,
    CHECKSUM_VERSION,
};

fn label_area(label: &str) -> Option<&'static str> {
    Some(match label {
        "frontend" | "ui" => "frontend",
        "backend" | "server" => "backend",
        "api" => "api",
        "docs" | "documentation" => "docs",
        "infra" | "ops" => "infra",
        "test" | "testing" => "tests",
        _ => return None,
    })
}

const INTERNAL_LABELS: &[&str] = &[
    "internal",
    "chore",
    "maintenance",
    "docs",
    "documentation",
    "test",
    "testing",
    "build",
    "ci",
];

fn breaking_change_text() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"(?i)\bbreaking[- ]change\b").unwrap())
}

pub fn semver_for_kind(kind: &str) -> &'static str {
    match kind {
        "breaking" => "major",
        "added" | "deprecated" => "minor",
        "documentation" | "tests" | "build" | "internal" | "other" => "none",
        _ => "patch",
    }
}

struct Decision {
    kind: String,
    audience: String,
    semver: String,
    confidence: f64,
}

/// Removed/renamed/re-signatured public symbols or protected-config edits always deserve a
/// human look before publishing, whatever the commit message or a model claims.
fn cap_for_semantic_review(
    semantic: Option<&SemanticEvidence>,
    kind: &str,
    confidence: f64,
) -> f64 {
    match semantic {
        Some(semantic)
            if (!semantic.api_break_candidates().is_empty() || !semantic.guardrails.is_empty())
                && kind != "breaking" =>
        {
            confidence.min(0.79)
        }
        _ => confidence,
    }
}

pub fn classify(
    group: &ChangeGroup,
    base_ref: &str,
    head_ref: &str,
    model: Option<&ModelAssist>,
) -> ChangeItem {
    let pr = group.pr.as_ref();
    let labels: Vec<String> = pr
        .map(|p| p.labels.iter().map(|l| l.to_lowercase()).collect())
        .unwrap_or_default();
    let changed_files = changed_files_for_group(group);
    let diff = evidence::build(&changed_files, Some(&group.commits));
    let semantic = diff.semantic.as_ref();

    let mut d = Decision {
        kind: "changed".into(),
        audience: "external".into(),
        semver: "patch".into(),
        confidence: 0.55,
    };
    let mut area = classify_area(&labels, &diff);
    let mut commit_signals: Vec<String> = Vec::new();
    let label_kind = labels
        .iter()
        .find_map(|l| normalize_change_kind(l))
        .map(str::to_owned);

    let parsed: Vec<ConventionalCommit> = group
        .commits
        .iter()
        .filter_map(|c| parse_conventional_commit(&c.message))
        .collect();
    if let Some(best) = parsed.first() {
        commit_signals.push(format!("conventional:{}", best.kind));
        apply_conventional_type(best, &mut d);
        d.confidence = 0.85;
    } else if let Some(kind) = &label_kind {
        d.kind = kind.clone();
        d.semver = semver_for_kind(kind).into();
        if ["documentation", "tests", "build", "internal"].contains(&kind.as_str()) {
            d.audience = "internal".into();
        }
        d.confidence = d.confidence.max(0.78);
        commit_signals.push(format!("label:{kind}"));
    } else if let Some(kind) = kind_from_statuses(&diff) {
        d.kind = kind.into();
        d.semver = semver_for_kind(kind).into();
    }

    let mut semantic_decided = false;
    if let Some(semantic) = semantic {
        semantic_decided = apply_semantic_evidence(
            semantic,
            &mut d,
            &mut commit_signals,
            parsed.first().map(|p| p.kind.as_str()),
            label_kind.as_deref(),
        );
    }

    let general_files = diff
        .files_by_area
        .get("general")
        .cloned()
        .unwrap_or_default();
    if d.kind == "breaking" || has_breaking_signal(group, &labels) {
        d.kind = "breaking".into();
        d.semver = "major".into();
        d.confidence = d.confidence.max(0.86);
    }
    let has_security = d.kind == "security"
        || labels.iter().any(|l| l.contains("security"))
        || general_files
            .iter()
            .any(|p| p.to_lowercase().contains("security"));
    if has_security && d.kind != "breaking" {
        d.kind = "security".into();
        d.semver = "patch".into();
        d.confidence = d.confidence.max(0.8);
    }

    if diff.docs_only && !["breaking", "security"].contains(&d.kind.as_str()) {
        d.kind = "documentation".into();
        d.audience = "internal".into();
        d.semver = "none".into();
        area = "docs".into();
    } else if diff.tests_only && !["breaking", "security"].contains(&d.kind.as_str()) {
        d.kind = "tests".into();
        d.audience = "internal".into();
        d.semver = "none".into();
        area = "tests".into();
    }

    if labels.iter().any(|l| INTERNAL_LABELS.contains(&l.as_str())) {
        d.audience = "internal".into();
        d.confidence = d.confidence.max(0.78);
        if !commit_signals.iter().any(|s| s.starts_with("label:")) {
            commit_signals.push("label:internal".into());
        }
    }

    if parsed.is_empty() {
        d.confidence = d.confidence.max(0.62);
        commit_signals.push("diff-fallback".into());
    }
    d.confidence = cap_for_semantic_review(semantic, &d.kind, d.confidence);

    let title = pr
        .map(|p| p.title.clone())
        .unwrap_or_else(|| subject(&group.commits[0].message).to_owned())
        .trim()
        .to_owned();
    let mut summary = diff.summary.clone();
    if let Some(model) = model {
        let mut decision = assist::Decision {
            kind: d.kind.clone(),
            area: area.clone(),
            audience: d.audience.clone(),
            semver: d.semver.clone(),
            summary: summary.clone(),
            confidence: d.confidence,
        };
        let allow_classification_override = !model.summary_only
            && parsed.is_empty()
            && !semantic_decided
            && !has_security
            && !(diff.docs_only || diff.tests_only);
        model.apply(
            group,
            &labels,
            &diff,
            &changed_files,
            &title,
            &mut decision,
            &mut commit_signals,
            allow_classification_override,
        );
        d.kind = decision.kind;
        area = decision.area;
        d.audience = decision.audience;
        d.semver = decision.semver;
        summary = decision.summary;
        // A confident model summary or classification must not lift the review cap.
        d.confidence = cap_for_semantic_review(semantic, &d.kind, decision.confidence);
    }
    let source = SourceRef {
        pr_number: pr.map(|p| p.number),
        commit_shas: group.commits.iter().map(|c| c.sha.clone()).collect(),
        base_ref: base_ref.into(),
        head_ref: head_ref.into(),
    };
    let evidence = Evidence {
        labels: pr.map(|p| p.labels.clone()).unwrap_or_default(),
        files: changed_files.iter().map(|f| f.path.clone()).collect(),
        commit_signals,
        derived_conventional_commits: Vec::new(),
        diff_summary: diff_summary(&diff, group),
    };
    let checksum = build_checksum(source.pr_number, &source.commit_shas, &title, &area);
    ChangeItem {
        id: format!("ci_{}", &checksum[..12]),
        source,
        classification: Classification {
            kind: d.kind,
            area,
            audience: d.audience,
            semver: d.semver,
        },
        content: Content { title, summary },
        evidence,
        confidence: d.confidence,
        published: false,
        published_in: None,
        checksum,
        checksum_version: CHECKSUM_VERSION.into(),
        lineage_releases: Vec::new(),
        status: default_review_status(d.confidence).into(),
        reviewed_by: None,
        reviewed_at: None,
        edited_summary: None,
        summary_provenance: None,
        hidden: false,
    }
}

fn apply_conventional_type(best: &ConventionalCommit, d: &mut Decision) {
    let set = |d: &mut Decision, kind: &str, audience: Option<&str>, semver: Option<&str>| {
        d.kind = kind.into();
        if let Some(a) = audience {
            d.audience = a.into();
        }
        if let Some(s) = semver {
            d.semver = s.into();
        }
    };
    if best.breaking {
        set(d, "breaking", None, Some("major"));
        return;
    }
    match best.kind.as_str() {
        "feat" => set(d, "added", None, Some("minor")),
        "fix" => set(d, "fixed", None, Some("patch")),
        "docs" => set(d, "documentation", Some("internal"), Some("none")),
        "test" => set(d, "tests", Some("internal"), Some("none")),
        "build" | "ci" => set(d, "build", Some("internal"), Some("none")),
        "perf" => set(d, "performance", None, Some("patch")),
        "refactor" => set(d, "refactoring", None, Some("patch")),
        "deprecate" | "deprecated" => set(d, "deprecated", None, Some("minor")),
        "remove" | "removed" | "delete" => set(d, "removed", None, Some("patch")),
        "security" | "sec" => set(d, "security", None, Some("patch")),
        "chore" | "style" => set(d, "internal", Some("internal"), None),
        _ => {}
    }
}

/// Fold IntentumDiff's verdict into the deterministic classification (see the Python
/// `Classifier._apply_semantic_evidence`). Authored conventional commits and labels keep
/// precedence; the verdict decides kind only on the diff-fallback path.
fn apply_semantic_evidence(
    semantic: &SemanticEvidence,
    d: &mut Decision,
    signals: &mut Vec<String>,
    authored_type: Option<&str>,
    label_kind: Option<&str>,
) -> bool {
    signals.push(format!("semantic:{}", semantic.verdict));
    if !semantic.complete {
        signals.push("semantic:partial".into());
    }
    let mut decided = false;
    if authored_type.is_none() && label_kind.is_none() {
        match semantic.verdict.as_str() {
            "style-only" if semantic.complete => {
                d.kind = "internal".into();
                d.audience = "internal".into();
                d.semver = "none".into();
                d.confidence = d.confidence.max(0.88);
                decided = true;
            }
            "refactor-only" if semantic.complete => {
                d.kind = "refactoring".into();
                d.semver = "patch".into();
                d.confidence = d.confidence.max(0.82);
                decided = true;
            }
            "additive" if !semantic.added_symbols.is_empty() => {
                d.kind = "added".into();
                d.semver = "minor".into();
                d.confidence = d.confidence.max(0.74);
                decided = true;
            }
            "behavioral" | "additive" => d.confidence = d.confidence.max(0.7),
            _ => {}
        }
    } else if matches!(authored_type, Some("feat" | "fix" | "perf"))
        && semantic.verdict == "style-only"
        && semantic.complete
    {
        signals.push("semantic:conflict".into());
        d.confidence = d.confidence.min(0.7);
    }
    if decided {
        signals.push("semantic-classified".into());
    }
    if !semantic.api_break_candidates().is_empty() {
        signals.push("semantic:api-break-candidate".into());
    }
    if !semantic.guardrails.is_empty() {
        signals.push("semantic:guardrail".into());
    }
    if !semantic.cross_file.is_empty() {
        signals.push("semantic:cross-file".into());
    }
    decided
}

fn classify_area(labels: &[String], diff: &DiffEvidence) -> String {
    if let Some(area) = labels.iter().find_map(|l| label_area(l.trim())) {
        return area.into();
    }
    let mut areas: Vec<(&String, usize)> = diff
        .files_by_area
        .iter()
        .map(|(a, p)| (a, p.len()))
        .collect();
    if areas.is_empty() {
        return "general".into();
    }
    areas.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(b.0)));
    areas[0].0.clone()
}

fn kind_from_statuses(diff: &DiffEvidence) -> Option<&'static str> {
    let statuses: Vec<String> = diff
        .file_statuses
        .values()
        .filter(|s| !s.is_empty())
        .map(|s| s.to_lowercase())
        .collect();
    if statuses.is_empty() {
        return None;
    }
    if statuses.iter().all(|s| s == "added") {
        return Some("added");
    }
    if statuses.iter().all(|s| s == "removed") {
        return Some("removed");
    }
    None
}

fn has_breaking_signal(group: &ChangeGroup, labels: &[String]) -> bool {
    if labels
        .iter()
        .any(|l| normalize_change_kind(l) == Some("breaking"))
    {
        return true;
    }
    let pr = group.pr.as_ref();
    let haystack = [
        pr.map(|p| p.title.as_str()).unwrap_or(""),
        pr.map(|p| p.body.as_str()).unwrap_or(""),
        &group
            .commits
            .iter()
            .map(|c| c.message.as_str())
            .collect::<Vec<_>>()
            .join("\n"),
    ]
    .join("\n");
    breaking_change_text().is_match(&haystack)
}

fn boundary(commit: &Commit) -> Value {
    json!({
        "sha": commit.sha,
        "subject": subject(&commit.message),
        "previous_sha": commit.parent_sha,
        "initial_commit": commit.parent_sha.is_none(),
        "derived_conventional": Value::Null,
    })
}

fn diff_summary(diff: &DiffEvidence, group: &ChangeGroup) -> Map<String, Value> {
    let boundaries = match (group.commits.first(), group.commits.last()) {
        (Some(first), Some(last)) => json!({"first": boundary(first), "last": boundary(last)}),
        _ => json!({"first": null, "last": null}),
    };
    let mut map = Map::new();
    map.insert("summary".into(), json!(diff.summary));
    map.insert("files_by_area".into(), json!(diff.files_by_area));
    map.insert("file_statuses".into(), json!(diff.file_statuses));
    map.insert("commit_boundaries".into(), boundaries);
    map.insert("api_changes".into(), json!(diff.api_changes));
    map.insert("migrations".into(), json!(diff.migration_changes));
    map.insert("config_changes".into(), json!(diff.config_changes));
    map.insert(
        "representative_hunks".into(),
        json!(diff.representative_hunks),
    );
    map.insert("grouping_reason".into(), json!(group.grouping_reason));
    map.insert(
        "engine".into(),
        json!(if diff.semantic.is_some() {
            "intentumdiff"
        } else {
            "git"
        }),
    );
    map.insert(
        "semantic".into(),
        diff.semantic
            .as_ref()
            .map(|s| json!(s))
            .unwrap_or(Value::Null),
    );
    map
}
