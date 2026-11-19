//! Model-free release-note sentences (`summary_style: "prose"`).
//!
//! Without a model, an item's summary used to be the diagnostic evidence summary ("IntentumDiff
//! semantic review: additive changes only; 1 file(s) reviewed; …"). In the prose style it is a
//! release-note sentence built only from what Verlato already knows: the changeset's title
//! (conventional prefix and scope removed, the leading verb put in the past tense) and the facts in
//! the semantic evidence (renamed, removed, re-signatured and added symbols; formatting-only and
//! behaviour-preserving changes). Nothing is claimed that the title or the evidence does not say.
//! The diagnostic summary stays in the item's evidence (`evidence.diff_summary.summary`).

use crate::semantic::SemanticEvidence;
use crate::vocab::{is_test_path, parse_conventional_commit};
use regex::Regex;
use std::sync::OnceLock;

/// How an item's deterministic summary is written.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum SummaryStyle {
    /// A release-note sentence (the default).
    #[default]
    Prose,
    /// The diagnostic evidence summary, as Verlato 0.1 wrote it.
    Evidence,
}

impl SummaryStyle {
    pub fn parse(value: &str) -> Option<Self> {
        match value.trim().to_lowercase().as_str() {
            "prose" => Some(Self::Prose),
            "evidence" => Some(Self::Evidence),
            _ => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Prose => "prose",
            Self::Evidence => "evidence",
        }
    }
}

/// The longest summary the prose style writes; longer ones are cut at a word boundary.
pub const MAX_PROSE_CHARS: usize = 240;
/// At most this many symbols are named per fact ("renamed `a` to `b`, `c` to `d` and 2 more").
const MAX_SYMBOLS: usize = 3;

/// Titles that say nothing about the change, so the sentence comes from the evidence instead.
const VAGUE_TITLES: &[&str] = &[
    "wip",
    "update",
    "updates",
    "updated",
    "change",
    "changes",
    "misc",
    "fix",
    "fixes",
    "stuff",
    "tmp",
    "temp",
    "cleanup",
    "clean up",
    "minor",
    "tweak",
    "tweaks",
    "refactor",
    "work in progress",
    "more changes",
    "minor changes",
    "small changes",
    "small fixes",
    "fix stuff",
    "various fixes",
    "save",
    "commit",
    "test",
    "testing",
];

/// Imperative verb → past tense, for the verbs commit subjects usually start with.
const PAST_TENSE: &[(&str, &str)] = &[
    ("add", "Added"),
    ("adjust", "Adjusted"),
    ("allow", "Allowed"),
    ("avoid", "Avoided"),
    ("bump", "Bumped"),
    ("change", "Changed"),
    ("clarify", "Clarified"),
    ("clean", "Cleaned"),
    ("correct", "Corrected"),
    ("create", "Created"),
    ("delete", "Deleted"),
    ("deprecate", "Deprecated"),
    ("describe", "Described"),
    ("disable", "Disabled"),
    ("document", "Documented"),
    ("drop", "Dropped"),
    ("enable", "Enabled"),
    ("ensure", "Ensured"),
    ("explain", "Explained"),
    ("expose", "Exposed"),
    ("extend", "Extended"),
    ("extract", "Extracted"),
    ("fix", "Fixed"),
    ("format", "Formatted"),
    ("handle", "Handled"),
    ("implement", "Implemented"),
    ("improve", "Improved"),
    ("increase", "Increased"),
    ("introduce", "Introduced"),
    ("make", "Made"),
    ("move", "Moved"),
    ("optimise", "Optimised"),
    ("optimize", "Optimized"),
    ("prevent", "Prevented"),
    ("reduce", "Reduced"),
    ("refactor", "Refactored"),
    ("reformat", "Reformatted"),
    ("remove", "Removed"),
    ("rename", "Renamed"),
    ("replace", "Replaced"),
    ("resolve", "Resolved"),
    ("restore", "Restored"),
    ("revert", "Reverted"),
    ("rewrite", "Rewrote"),
    ("simplify", "Simplified"),
    ("speed", "Sped"),
    ("split", "Split"),
    ("tidy", "Tidied"),
    ("update", "Updated"),
    ("upgrade", "Upgraded"),
    ("use", "Used"),
    ("validate", "Validated"),
    // Verbs fix commits often start with: without them `fix: reject ...` would read "Fixed reject ...".
    ("accept", "Accepted"),
    ("cache", "Cached"),
    ("cap", "Capped"),
    ("catch", "Caught"),
    ("check", "Checked"),
    ("decode", "Decoded"),
    ("encode", "Encoded"),
    ("escape", "Escaped"),
    ("guard", "Guarded"),
    ("hide", "Hid"),
    ("ignore", "Ignored"),
    ("limit", "Limited"),
    ("log", "Logged"),
    ("migrate", "Migrated"),
    ("normalise", "Normalised"),
    ("normalize", "Normalized"),
    ("parse", "Parsed"),
    ("raise", "Raised"),
    ("register", "Registered"),
    ("reject", "Rejected"),
    ("reset", "Reset"),
    ("retry", "Retried"),
    ("return", "Returned"),
    ("skip", "Skipped"),
    ("sort", "Sorted"),
    ("stop", "Stopped"),
    ("strip", "Stripped"),
    ("trim", "Trimmed"),
    ("warn", "Warned"),
];

fn pr_ref() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| Regex::new(r"\s*\(#\d+\)").unwrap())
}

/// The ` [area]` / ` [scope]` the grouper appends to the title of each part of a split PR.
fn split_suffix() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| Regex::new(r"\s\[([\w./-]+)\]$").unwrap())
}

fn merge_title() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| {
        Regex::new(r"(?i)^merge (pull request #\d+ from \S+|branch '[^']*'|remote-tracking branch)")
            .unwrap()
    })
}

/// What the sentence is built from.
pub struct ProseInput<'a> {
    /// The PR title, or the first commit's subject.
    pub title: &'a str,
    /// The PR number when the changeset maps to a PR (the renderer prints it as `(PR #n)`).
    pub pr_number: Option<i64>,
    /// The item's final change kind (`added`, `fixed`, `breaking`, ...).
    pub kind: &'a str,
    pub semantic: Option<&'a SemanticEvidence>,
    /// The changed files and their statuses (`added`, `modified`, `deleted`, `renamed`): the
    /// fallback facts when the title is vague and there is no semantic evidence.
    pub files: &'a [(String, String)],
}

/// The release-note sentence for one item.
pub fn release_note(input: &ProseInput<'_>) -> String {
    let mut title = input.title.trim().to_owned();
    // A split PR's part: "Title [backend]" reads "Title (backend)".
    let part = split_suffix().captures(&title).map(|c| c[1].to_owned());
    if part.is_some() {
        title = split_suffix().replace(&title, "").into_owned();
    }
    if input.pr_number.is_some() {
        // The renderer prints the PR number as "(PR #n)".
        title = pr_ref().replace_all(&title, "").trim().to_owned();
    }
    let conventional = parse_conventional_commit(&title);
    let (description, commit_type, explicit_breaking) = match &conventional {
        Some(c) => (
            c.description.trim().to_owned(),
            Some(c.kind.as_str()),
            c.breaking,
        ),
        None => (title.clone(), None, false),
    };
    let breaking = explicit_breaking || input.kind == "breaking";
    let vague = is_vague(&description) || merge_title().is_match(&title);

    let facts = Facts::from(
        input.semantic,
        &description,
        vague || commit_type == Some("feat") || input.kind == "added",
    );
    let mut sentences: Vec<String> = Vec::new();
    if vague {
        if let Some(clause) = facts.clause() {
            sentences.push(sentence(&clause));
        } else if facts.style_only {
            sentences.push("Formatting changes only.".into());
        } else if facts.refactor_only {
            sentences.push("Behaviour-preserving refactoring only.".into());
        } else if let Some(clause) = file_clause(input.files) {
            sentences.push(sentence(&clause));
        } else {
            sentences.push(sentence(&lead(&description, commit_type)));
        }
    } else {
        let mut first = lead(&description, commit_type);
        if let Some(part) = &part {
            first.push_str(&format!(" ({part})"));
        }
        if facts.style_only && !facts.has_symbols() {
            first.push_str(" (formatting only)");
        } else if facts.refactor_only && !facts.has_symbols() {
            first.push_str(" (no behaviour change)");
        }
        sentences.push(sentence(&first));
        if let Some(clause) = facts.clause() {
            sentences.push(sentence(&clause));
        }
    }
    if breaking {
        let first = sentences.remove(0);
        sentences.insert(0, before_period(&first, " (breaking change)"));
    } else if facts.possible_break && !facts.marks_break() {
        let last = sentences.pop().unwrap_or_default();
        sentences.push(before_period(&last, " (possible breaking change)"));
    }
    bound(&sentences.join(" "))
}

/// "updated `src/app.py`; added `docs/x.md`" from the changed files.
fn file_clause(files: &[(String, String)]) -> Option<String> {
    let mut parts = Vec::new();
    for (status, verb) in [
        ("added", "added"),
        ("modified", "updated"),
        ("deleted", "removed"),
        ("renamed", "renamed"),
    ] {
        let paths: Vec<String> = files
            .iter()
            .filter(|(_, s)| s == status)
            .map(|(path, _)| format!("`{path}`"))
            .collect();
        if !paths.is_empty() {
            parts.push(format!("{verb} {}", enumerate(&paths)));
        }
    }
    (!parts.is_empty()).then(|| parts.join("; "))
}

fn is_vague(description: &str) -> bool {
    let normal = description
        .trim()
        .trim_end_matches(['.', '!'])
        .to_lowercase();
    normal.is_empty() || VAGUE_TITLES.contains(&normal.as_str())
}

/// The description as a past-tense clause: "add export endpoint" → "Added export endpoint".
fn lead(description: &str, commit_type: Option<&str>) -> String {
    let description = description.trim().trim_end_matches('.');
    let (first, rest) = match description.split_once(char::is_whitespace) {
        Some((first, rest)) => (first, rest.trim_start()),
        None => (description, ""),
    };
    let word = first.to_lowercase();
    let join = |head: &str| {
        if rest.is_empty() {
            head.to_owned()
        } else {
            format!("{head} {rest}")
        }
    };
    if commit_type == Some("fix") && (word == "handle" || word == "handles") && !rest.is_empty() {
        return format!("Fixed handling of {rest}");
    }
    if (word == "support" || word == "supports") && !rest.is_empty() {
        return format!("Added support for {rest}");
    }
    if let Some(past) = past_tense(&word) {
        return join(past);
    }
    match commit_type {
        Some("feat") if !looks_past(&word) => format!("Added {}", lower_first(description)),
        Some("fix") if !looks_past(&word) => format!("Fixed {}", lower_first(description)),
        _ => upper_first(description),
    }
}

fn past_tense(word: &str) -> Option<&'static str> {
    let lookup = |w: &str| {
        PAST_TENSE
            .iter()
            .find(|(base, _)| *base == w)
            .map(|(_, p)| *p)
    };
    lookup(word)
        .or_else(|| {
            word.strip_suffix("ies")
                .and_then(|stem| lookup(&format!("{stem}y")))
        })
        .or_else(|| word.strip_suffix("es").and_then(lookup))
        .or_else(|| word.strip_suffix('s').and_then(lookup))
}

fn looks_past(word: &str) -> bool {
    word.ends_with("ed") || PAST_TENSE.iter().any(|(_, p)| p.eq_ignore_ascii_case(word))
}

fn upper_first(text: &str) -> String {
    let mut chars = text.chars();
    match chars.next() {
        Some(c) => c.to_uppercase().chain(chars).collect(),
        None => String::new(),
    }
}

fn lower_first(text: &str) -> String {
    // Leave acronyms and identifiers ("API", "CSV", "`foo`") as written.
    let mut chars = text.chars();
    match (chars.next(), chars.next()) {
        (Some(a), Some(b)) if a.is_uppercase() && b.is_lowercase() => a
            .to_lowercase()
            .chain(std::iter::once(b))
            .chain(chars)
            .collect(),
        _ => text.to_owned(),
    }
}

/// A clause as a sentence: capitalised and ending in a full stop.
fn sentence(clause: &str) -> String {
    let clause = upper_first(clause.trim());
    if clause.ends_with(['.', '!', '?']) {
        clause
    } else {
        format!("{clause}.")
    }
}

/// `text` with `insert` placed before its final full stop.
fn before_period(text: &str, insert: &str) -> String {
    match text.strip_suffix('.') {
        Some(stem) => format!("{stem}{insert}."),
        None => format!("{text}{insert}"),
    }
}

fn bound(text: &str) -> String {
    if text.chars().count() <= MAX_PROSE_CHARS {
        return text.to_owned();
    }
    let cut: String = text.chars().take(MAX_PROSE_CHARS - 1).collect();
    let cut = cut.rsplit_once(' ').map_or(cut.as_str(), |(head, _)| head);
    format!("{}…", cut.trim_end_matches([',', ';', ' ']))
}

/// The facts from the semantic evidence worth a release note.
#[derive(Default)]
struct Facts {
    renamed: Vec<String>,
    removed: Vec<String>,
    signature: Vec<String>,
    extended: Vec<String>,
    added: Vec<String>,
    possible_break: bool,
    style_only: bool,
    refactor_only: bool,
}

impl Facts {
    fn from(semantic: Option<&SemanticEvidence>, description: &str, name_additions: bool) -> Self {
        let Some(semantic) = semantic else {
            return Self::default();
        };
        let mentioned = |name: &str| {
            let name = name.to_lowercase();
            description
                .to_lowercase()
                .split(|c: char| !(c.is_alphanumeric() || c == '_'))
                .any(|word| word == name)
        };
        let public = |entries: &[String]| -> Vec<(String, String)> {
            entries
                .iter()
                .filter_map(|entry| {
                    let (symbol, path) = entry.split_once(" in ").unwrap_or((entry.as_str(), ""));
                    let bare = symbol.rsplit('.').next().unwrap_or("").trim();
                    let private = bare.starts_with('_') || bare.is_empty();
                    (!private && !(!path.is_empty() && is_test_path(path)))
                        .then(|| (symbol.trim().to_owned(), path.to_owned()))
                })
                .collect()
        };
        let names = |entries: &[String]| -> Vec<String> {
            public(entries)
                .into_iter()
                .map(|(symbol, _)| symbol)
                .collect()
        };
        let renamed = public(&semantic.renamed_symbols)
            .into_iter()
            .map(|(symbol, _)| match symbol.split_once(" -> ") {
                Some((old, new)) => format!("`{}` to `{}`", old.trim(), new.trim()),
                None => format!("`{symbol}`"),
            })
            .collect::<Vec<_>>();
        // Additions are named for features, vague titles, and next to other symbol facts (so a
        // removal is not reported without the addition that replaced it).
        let other_facts = !semantic.renamed_symbols.is_empty()
            || !semantic.removed_symbols.is_empty()
            || !semantic.signature_changes.is_empty()
            || !semantic.extended_signatures.is_empty();
        let added = if name_additions || other_facts {
            names(&semantic.added_symbols)
                .into_iter()
                .filter(|name| !mentioned(name.rsplit('.').next().unwrap_or(name)))
                .map(|name| format!("`{name}`"))
                .collect()
        } else {
            Vec::new()
        };
        let quote = |list: Vec<String>| list.into_iter().map(|n| format!("`{n}`")).collect();
        Self {
            possible_break: !semantic.api_break_candidates().is_empty(),
            renamed,
            removed: quote(names(&semantic.removed_symbols)),
            signature: quote(names(&semantic.signature_changes)),
            extended: quote(names(&semantic.extended_signatures)),
            added,
            style_only: semantic.verdict == "style-only",
            refactor_only: semantic.verdict == "refactor-only",
        }
    }

    fn has_symbols(&self) -> bool {
        !(self.renamed.is_empty()
            && self.removed.is_empty()
            && self.signature.is_empty()
            && self.extended.is_empty()
            && self.added.is_empty())
    }

    /// "renamed `a` to `b`; changed the signature of `c` (possible breaking change); added `d`".
    /// A possible API break is marked on the facts that can break callers (renames, removals,
    /// signature changes), not on the compatible ones after them.
    fn clause(&self) -> Option<String> {
        let part = |verb: &str, list: &[String]| {
            (!list.is_empty()).then(|| format!("{verb} {}", enumerate(list)))
        };
        let mut parts: Vec<String> = [
            part("renamed", &self.renamed),
            part("removed", &self.removed),
            part("changed the signature of", &self.signature),
        ]
        .into_iter()
        .flatten()
        .collect();
        if self.marks_break() {
            if let Some(last) = parts.last_mut() {
                last.push_str(" (possible breaking change)");
            }
        }
        parts.extend(
            [
                part("added optional parameters to", &self.extended),
                part("added", &self.added),
            ]
            .into_iter()
            .flatten(),
        );
        (!parts.is_empty()).then(|| parts.join("; "))
    }

    /// Whether `clause` carries the possible-break marker itself.
    fn marks_break(&self) -> bool {
        self.possible_break
            && !(self.renamed.is_empty() && self.removed.is_empty() && self.signature.is_empty())
    }
}

/// "`a`", "`a` and `b`", "`a`, `b` and `c`", "`a`, `b`, `c` and 2 more".
fn enumerate(list: &[String]) -> String {
    let shown: Vec<&str> = list.iter().take(MAX_SYMBOLS).map(String::as_str).collect();
    let more = list.len().saturating_sub(MAX_SYMBOLS);
    match (shown.as_slice(), more) {
        ([only], 0) => (*only).to_owned(),
        (items, 0) => {
            let (last, head) = items.split_last().expect("at least two items");
            format!("{} and {last}", head.join(", "))
        }
        (items, more) => format!("{} and {more} more", items.join(", ")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn evidence(verdict: &str) -> SemanticEvidence {
        SemanticEvidence {
            engine: "intentumdiff".into(),
            engine_version: "test".into(),
            verdict: verdict.into(),
            complete: true,
            files_reviewed: 1,
            files_unreviewed: 0,
            group_counts: Default::default(),
            languages: vec!["python".into()],
            style_only_files: Vec::new(),
            refactor_only_files: Vec::new(),
            meaningful_files: Vec::new(),
            added_symbols: Vec::new(),
            removed_symbols: Vec::new(),
            renamed_symbols: Vec::new(),
            signature_changes: Vec::new(),
            refactorings: Vec::new(),
            cross_file: Vec::new(),
            guardrails: Vec::new(),
            unreviewed: Vec::new(),
            errors: Vec::new(),
            notes: Vec::new(),
            summary: String::new(),
            extended_signatures: Vec::new(),
        }
    }

    fn note(title: &str, kind: &str, semantic: Option<&SemanticEvidence>) -> String {
        release_note(&ProseInput {
            title,
            pr_number: None,
            kind,
            semantic,
            files: &[],
        })
    }

    #[test]
    fn conventional_titles_become_past_tense_sentences() {
        let mut added = evidence("additive");
        added.added_symbols = vec!["export in api/export.py".into()];
        assert_eq!(
            note("feat(api): add export endpoint", "added", Some(&added)),
            "Added export endpoint."
        );
        assert_eq!(
            note("fix(api): handle empty export", "fixed", None),
            "Fixed handling of empty export."
        );
        assert_eq!(
            note("docs: explain export", "documentation", None),
            "Explained export."
        );
        assert_eq!(note("Add more tests", "tests", None), "Added more tests.");
        assert_eq!(note("feat: CSV export", "added", None), "Added CSV export.");
        assert_eq!(
            note("fix: crash on empty input", "fixed", None),
            "Fixed crash on empty input."
        );
        assert_eq!(
            note("feat: supports dark mode", "added", None),
            "Added support for dark mode."
        );
        assert_eq!(note("chore: Bumps deps", "changed", None), "Bumped deps.");
        assert_eq!(
            note("fix: reject tasks with empty titles", "fixed", None),
            "Rejected tasks with empty titles."
        );
        assert_eq!(
            note("fix: retries on timeout", "fixed", None),
            "Retried on timeout."
        );
    }

    #[test]
    fn breaking_changes_are_marked() {
        assert_eq!(
            note("refactor!: drop legacy greet", "breaking", None),
            "Dropped legacy greet (breaking change)."
        );
        let mut renamed = evidence("behavioral");
        renamed.renamed_symbols = vec!["open_tasks -> pending in tasks.py".into()];
        assert_eq!(
            note("wip", "changed", Some(&renamed)),
            "Renamed `open_tasks` to `pending` (possible breaking change)."
        );
        assert_eq!(
            note("Update task helpers", "changed", Some(&renamed)),
            "Updated task helpers. Renamed `open_tasks` to `pending` (possible breaking change)."
        );
    }

    #[test]
    fn evidence_facts_are_named_and_bounded() {
        let mut mixed = evidence("behavioral");
        mixed.signature_changes = vec!["render in web/ui.ts".into()];
        mixed.added_symbols = vec!["handle in server/handler.py".into()];
        mixed.extended_signatures = vec!["greet in src/app.py".into()];
        mixed.removed_symbols = vec![
            "_private in src/app.py".into(),
            "helper in tests/test_a.py".into(),
        ];
        assert_eq!(
            note("Big change across areas (#12)", "changed", Some(&mixed)),
            "Big change across areas (#12). Changed the signature of `render` (possible breaking \
             change); added optional parameters to `greet`; added `handle`."
        );
        // A feature names the symbols it adds unless the title already does.
        assert_eq!(
            note("feat: request routing", "added", Some(&mixed)),
            "Added request routing. Changed the signature of `render` (possible breaking change); \
             added optional parameters to `greet`; added `handle`."
        );
        let mut many = evidence("additive");
        many.added_symbols = (1..=5).map(|i| format!("f{i} in m.py")).collect();
        assert_eq!(
            note("wip", "added", Some(&many)),
            "Added `f1`, `f2`, `f3` and 2 more."
        );
        let long = format!("feat: {}", "very long words ".repeat(40));
        let out = note(&long, "added", None);
        assert!(out.chars().count() <= MAX_PROSE_CHARS, "{out}");
        assert!(out.ends_with('…'), "{out}");
    }

    #[test]
    fn verdicts_without_symbols() {
        let style = evidence("style-only");
        assert_eq!(
            note("Tidy formatting", "changed", Some(&style)),
            "Tidied formatting (formatting only)."
        );
        assert_eq!(
            note("wip", "changed", Some(&style)),
            "Formatting changes only."
        );
        let refactor = evidence("refactor-only");
        assert_eq!(
            note("Reorganise modules", "changed", Some(&refactor)),
            "Reorganise modules (no behaviour change)."
        );
        // No evidence and a vague title: the cleaned title is all there is.
        assert_eq!(note("wip", "changed", None), "Wip.");
    }

    #[test]
    fn pr_numbers_are_left_to_the_renderer() {
        let with_pr = |title: &str| {
            release_note(&ProseInput {
                title,
                pr_number: Some(12),
                kind: "changed",
                semantic: None,
                files: &[],
            })
        };
        assert_eq!(with_pr("Export API (#12)"), "Export API.");
        // Each part of a split PR names its area.
        assert_eq!(
            with_pr("Big change across areas (#12) [backend]"),
            "Big change across areas (backend)."
        );
        assert_eq!(
            note(
                "Merge pull request #11 from someone/feature",
                "changed",
                None
            ),
            "Merge pull request #11 from someone/feature."
        );
    }

    #[test]
    fn vague_titles_without_evidence_fall_back_to_the_files() {
        let files = vec![
            ("src/app.py".to_owned(), "modified".to_owned()),
            ("docs/a.md".to_owned(), "added".to_owned()),
            ("old.txt".to_owned(), "deleted".to_owned()),
        ];
        let input = ProseInput {
            title: "wip",
            pr_number: None,
            kind: "changed",
            semantic: None,
            files: &files,
        };
        assert_eq!(
            release_note(&input),
            "Added `docs/a.md`; updated `src/app.py`; removed `old.txt`."
        );
        // An informative title is used as written, without the file list.
        let input = ProseInput {
            title: "Refresh the landing page",
            ..input
        };
        assert_eq!(release_note(&input), "Refresh the landing page.");
    }

    #[test]
    fn styles_parse() {
        assert_eq!(SummaryStyle::parse(" Prose "), Some(SummaryStyle::Prose));
        assert_eq!(
            SummaryStyle::parse("evidence"),
            Some(SummaryStyle::Evidence)
        );
        assert_eq!(SummaryStyle::parse("other"), None);
        assert_eq!(SummaryStyle::default().as_str(), "prose");
    }
}
