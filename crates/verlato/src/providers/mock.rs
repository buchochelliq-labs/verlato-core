//! Deterministic local provider for tests and demos (ports `verlato/providers/mock.py`).
//! Confidences and classifications come from Python's `random.Random` seeded with a prompt
//! hash, so this ships a Mersenne Twister seeded exactly as CPython seeds it from an int.

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;

use super::{Provider, ProviderRequest, ProviderResponse};
use crate::pyjson::{self, splitlines};

/// CPython's MT19937 (`random.Random`).
struct PyRandom {
    state: [u32; 624],
    index: usize,
}

impl PyRandom {
    fn init_genrand(seed: u32) -> [u32; 624] {
        let mut mt = [0u32; 624];
        mt[0] = seed;
        for i in 1..624 {
            mt[i] = 1812433253u32
                .wrapping_mul(mt[i - 1] ^ (mt[i - 1] >> 30))
                .wrapping_add(i as u32);
        }
        mt
    }

    /// `Random(seed)` for a non-negative int seed below 2**32 (`init_by_array([seed])`).
    fn new(seed: u32) -> PyRandom {
        let key = [seed];
        let mut mt = Self::init_genrand(19650218);
        let (mut i, mut j) = (1usize, 0usize);
        for _ in 0..624.max(key.len()) {
            mt[i] = (mt[i] ^ (mt[i - 1] ^ (mt[i - 1] >> 30)).wrapping_mul(1664525))
                .wrapping_add(key[j])
                .wrapping_add(j as u32);
            i += 1;
            j += 1;
            if i >= 624 {
                mt[0] = mt[623];
                i = 1;
            }
            if j >= key.len() {
                j = 0;
            }
        }
        for _ in 0..623 {
            mt[i] = (mt[i] ^ (mt[i - 1] ^ (mt[i - 1] >> 30)).wrapping_mul(1566083941))
                .wrapping_sub(i as u32);
            i += 1;
            if i >= 624 {
                mt[0] = mt[623];
                i = 1;
            }
        }
        mt[0] = 0x8000_0000;
        PyRandom {
            state: mt,
            index: 624,
        }
    }

    fn next_u32(&mut self) -> u32 {
        if self.index >= 624 {
            for k in 0..624 {
                let y = (self.state[k] & 0x8000_0000) | (self.state[(k + 1) % 624] & 0x7fff_ffff);
                let mut next = self.state[(k + 397) % 624] ^ (y >> 1);
                if y & 1 != 0 {
                    next ^= 0x9908_b0df;
                }
                self.state[k] = next;
            }
            self.index = 0;
        }
        let mut y = self.state[self.index];
        self.index += 1;
        y ^= y >> 11;
        y ^= (y << 7) & 0x9d2c_5680;
        y ^= (y << 15) & 0xefc6_0000;
        y ^ (y >> 18)
    }

    /// `random.random()`.
    fn random(&mut self) -> f64 {
        let a = (self.next_u32() >> 5) as f64;
        let b = (self.next_u32() >> 6) as f64;
        (a * 67108864.0 + b) / 9007199254740992.0
    }

    /// `randrange(n)` for small `n` (`_randbelow_with_getrandbits`).
    fn randrange(&mut self, n: u32) -> u32 {
        let k = 32 - n.leading_zeros();
        loop {
            let r = self.next_u32() >> (32 - k);
            if r < n {
                return r;
            }
        }
    }
}

/// Python `round(value, 2)` (correctly rounded from the binary value).
fn round2(value: f64) -> f64 {
    format!("{value:.2}").parse().unwrap_or(value)
}

fn seed(request: &ProviderRequest) -> u32 {
    let digest = hex::encode(Sha256::digest(
        format!("{}:{}", request.task, request.prompt).as_bytes(),
    ));
    u32::from_str_radix(&digest[..8], 16).unwrap_or(0)
}

fn text(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Bool(false)) => String::new(),
        Some(other) => other.to_string(),
    }
}

fn first_line(value: &str) -> String {
    splitlines(value)
        .first()
        .map(|s| (*s).to_owned())
        .unwrap_or_default()
}

fn summarize_from_evidence(evidence: &Value) -> String {
    let empty = Map::new();
    let evidence = evidence.as_object().unwrap_or(&empty);
    if let Some(change_summary) = evidence.get("change_summary").and_then(Value::as_object) {
        if let Some(files) = change_summary
            .get("files")
            .and_then(Value::as_array)
            .filter(|f| !f.is_empty())
        {
            let mut statuses: BTreeMap<String, usize> = BTreeMap::new();
            let mut paths: Vec<String> = Vec::new();
            for file in files.iter().filter_map(Value::as_object) {
                let status = Some(text(file.get("status")))
                    .filter(|s| !s.is_empty())
                    .unwrap_or_else(|| "modified".into());
                *statuses.entry(status).or_default() += 1;
                let path = text(file.get("path")).trim().to_owned();
                if !path.is_empty() {
                    paths.push(path);
                }
            }
            if !paths.is_empty() {
                let status_text = statuses
                    .iter()
                    .map(|(s, c)| format!("{s} {c}"))
                    .collect::<Vec<_>>()
                    .join(", ");
                let mut shown = paths.clone();
                shown.sort();
                shown.truncate(6);
                let suffix = if paths.len() > shown.len() {
                    format!(", plus {} more", paths.len() - shown.len())
                } else {
                    String::new()
                };
                return format!(
                    "Updates {} changed file(s) ({status_text}): {}{suffix}.",
                    paths.len(),
                    shown.join(", ")
                );
            }
        }
        if let Some(first) = change_summary
            .get("commits")
            .and_then(Value::as_array)
            .and_then(|c| c.first())
        {
            let subject = match first.as_object() {
                Some(commit) => {
                    let subject = text(commit.get("subject"));
                    first_line(&if subject.is_empty() {
                        text(commit.get("message"))
                    } else {
                        subject
                    })
                }
                None => first_line(&text(Some(first))),
            };
            if !subject.is_empty() {
                return format!("Updates release scope from commit evidence: {subject}.");
            }
        }
    }
    if let Some(statuses_map) = evidence
        .get("file_statuses")
        .and_then(Value::as_object)
        .filter(|m| !m.is_empty())
    {
        let mut statuses: BTreeMap<String, usize> = BTreeMap::new();
        for status in statuses_map.values() {
            let status = Some(text(Some(status)))
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| "modified".into());
            *statuses.entry(status).or_default() += 1;
        }
        let status_text = statuses
            .iter()
            .map(|(s, c)| format!("{s} {c}"))
            .collect::<Vec<_>>()
            .join(", ");
        let mut paths: Vec<&String> = statuses_map.keys().collect();
        paths.sort();
        paths.truncate(6);
        let suffix = if statuses_map.len() > paths.len() {
            format!(", plus {} more", statuses_map.len() - paths.len())
        } else {
            String::new()
        };
        return format!(
            "Updates {} changed file(s) ({status_text}): {}{suffix}.",
            statuses_map.len(),
            paths
                .iter()
                .map(|p| p.as_str())
                .collect::<Vec<_>>()
                .join(", ")
        );
    }
    let commits = evidence
        .get("commit_subjects")
        .filter(|v| !v.is_null() && v.as_array().is_some_and(|a| !a.is_empty()))
        .or_else(|| evidence.get("commits"));
    if let Some(first) = commits.and_then(Value::as_array).and_then(|c| c.first()) {
        let raw = match first {
            Value::String(s) => s.clone(),
            other => pyjson::dumps(other, pyjson::DEFAULT),
        };
        return format!(
            "Updates release scope from commit evidence: {}.",
            first_line(&raw)
        );
    }
    "No changed-file evidence was available from PR or commit metadata.".into()
}

fn file_payloads(evidence: &Map<String, Value>) -> Vec<&Map<String, Value>> {
    evidence
        .get("files")
        .and_then(Value::as_array)
        .map(|files| files.iter().filter_map(Value::as_object).collect())
        .unwrap_or_default()
}

fn infer_conventional(evidence: &Value) -> Value {
    let empty = Map::new();
    let evidence = evidence.as_object().unwrap_or(&empty);
    let subject = text(evidence.get("subject")).to_lowercase();
    let files = file_payloads(evidence);
    let paths: Vec<String> = files.iter().map(|f| text(f.get("path"))).collect();
    let statuses: std::collections::BTreeSet<String> = files
        .iter()
        .map(|f| text(f.get("status")).to_lowercase())
        .collect();
    let joined = paths.join(" ").to_lowercase();
    let kind = if subject.contains("remove") || subject.contains("delete") {
        "remove"
    } else if subject.contains("fix") || subject.contains("bug") {
        "fix"
    } else if subject.contains("doc") || joined.contains("readme") {
        "docs"
    } else if subject.contains("test") || joined.contains("/test") {
        "test"
    } else if !statuses.is_empty() && statuses.iter().all(|s| s == "added") {
        "feat"
    } else if statuses.contains("removed") || statuses.contains("deleted") {
        "remove"
    } else if joined.contains("api") || subject.contains("add") || subject.contains("new") {
        "feat"
    } else {
        "chore"
    };
    let scope = if joined.contains("api") {
        "api"
    } else if joined.contains("vscode") || joined.contains("extension") {
        "vscode"
    } else if joined.contains("/ui") || joined.contains("web/") || joined.contains("frontend") {
        "ui"
    } else if joined.contains("mcp") {
        "mcp"
    } else if joined.contains("doc") || joined.contains("readme") {
        "docs"
    } else if joined.contains("test") {
        "tests"
    } else {
        "general"
    };
    let description = if subject.is_empty() {
        match files.first() {
            None => "update commit diff".to_owned(),
            Some(first) => {
                let status = Some(text(first.get("status")))
                    .filter(|s| !s.is_empty())
                    .unwrap_or_else(|| "modify".into())
                    .to_lowercase();
                let path = Some(text(first.get("path")))
                    .filter(|s| !s.is_empty())
                    .unwrap_or_else(|| "files".into());
                let verb = match status.as_str() {
                    "added" => "add",
                    "modified" => "update",
                    "removed" | "deleted" => "remove",
                    "renamed" => "rename",
                    _ => "update",
                };
                format!("{verb} {path}")
            }
        }
    } else {
        subject.clone()
    };
    json!({
        "type": kind,
        "scope": scope,
        "description": description,
        "breaking": subject.contains("breaking"),
        "confidence": 0.82,
        "rationale": "Deterministic mock intent derived from changed paths.",
    })
}

fn terms(facts: &Map<String, Value>, key: &str) -> String {
    facts
        .get(key)
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .map(|t| text(Some(t)).to_lowercase())
                .collect::<Vec<_>>()
                .join(" ")
        })
        .unwrap_or_default()
}

fn draft_conventional(evidence: &Value) -> Value {
    let empty = Map::new();
    let evidence = evidence.as_object().unwrap_or(&empty);
    let subject = {
        let authored = text(evidence.get("authored_subject"));
        if authored.is_empty() {
            text(evidence.get("subject"))
        } else {
            authored
        }
    }
    .trim()
    .to_owned();
    let lowered = subject.to_lowercase();
    let files = file_payloads(evidence);
    let paths: Vec<String> = files.iter().map(|f| text(f.get("path"))).collect();
    let joined = paths.join(" ").to_lowercase();
    let facts = evidence
        .get("diff_facts")
        .and_then(Value::as_object)
        .unwrap_or(&empty);
    let themes: Vec<String> = facts
        .get("themes")
        .and_then(Value::as_array)
        .map(|t| t.iter().map(|v| text(Some(v))).collect())
        .unwrap_or_default();
    let theme_text = themes.join(" ");
    let theme_lower = theme_text.to_lowercase();
    let added = terms(facts, "added_terms");
    let removed = terms(facts, "removed_terms");
    let kind = if lowered.contains("fix") || lowered.contains("repair") {
        "fix"
    } else if lowered.contains("refactor")
        || removed.contains("remove")
        || theme_text.contains("removes instant provisional")
    {
        "refactor"
    } else if lowered.contains("doc")
        && !(joined.contains("vscode-extension/src") || joined.contains("/src/"))
    {
        "docs"
    } else if lowered.contains("test") && !joined.contains("src") {
        "test"
    } else if lowered.contains("add")
        || theme_text.contains("adds configurable")
        || theme_text.contains("adds model privacy")
    {
        "feat"
    } else {
        "chore"
    };
    let scope = if joined.contains("commit_intent") || theme_text.contains("commit-intent") {
        "commit-intent"
    } else if joined.contains("vscode-extension") {
        "vscode"
    } else if joined.contains("mcp") {
        "mcp"
    } else if joined.contains("api") {
        "api"
    } else if joined.contains("test") {
        "tests"
    } else if joined.contains("readme") || joined.contains(".md") {
        "docs"
    } else {
        "general"
    };
    let description =
        if theme_lower.contains("model privacy") || theme_lower.contains("raw patch evidence") {
            "enhance commit intent inference with privacy settings".to_owned()
        } else if theme_lower.contains("suggestion strategy") || lowered.contains("suggestion") {
            "revise suggestion strategy settings".to_owned()
        } else if let Some(rest) = lowered.strip_prefix("refactor ") {
            rest.replace("verlato ", "").chars().take(120).collect()
        } else if lowered.is_empty() {
            "update commit diff".to_owned()
        } else {
            lowered.clone()
        };
    let mut body: Vec<&str> = Vec::new();
    if theme_lower.contains("model privacy") {
        body.push("Added model privacy modes for metadata-only, local-summary-then-ai, and trusted-provider-patch inference.");
    }
    if theme_lower.contains("raw patch evidence") {
        body.push("Gated raw patch evidence by privacy mode and provider trust.");
    }
    if theme_lower.contains("model request privacy metadata") {
        body.push("Recorded privacy request metadata for model-backed commit inference.");
    }
    if theme_lower.contains("prompt preview") {
        body.push("Updated prompt previews to show AI request and privacy details.");
    }
    if theme_lower.contains("confirmation") {
        body.push("Added confirmation before sending raw patch evidence to trusted providers.");
    }
    if theme_lower.contains("batch-size") || added.contains("suggestionbatchsize") {
        body.push("Updated suggestion batch-size defaults and configuration.");
    }
    if theme_lower.contains("strategy") || added.contains("full-diff-json") {
        body.push("Added configurable suggestion strategy modes for auto, single, paired, and full-diff-json.");
    }
    if removed.contains("instant") || removed.contains("provisional") {
        body.push("Removed instant provisional suggestion behavior and related settings.");
    }
    if paths
        .iter()
        .any(|p| p.contains("releaseControlView") || p.contains("releaseControlMarkup"))
    {
        body.push("Updated the release control view and markup to match the new suggestion flow.");
    }
    if paths.iter().any(|p| p.to_lowercase().contains("test")) {
        body.push("Updated tests to cover the revised suggestion handling.");
    }
    body.truncate(6);
    if body.is_empty() {
        body.push("Updated files according to the commit-scoped diff evidence.");
    }
    json!({
        "type": kind,
        "scope": scope,
        "description": description,
        "body": body,
        "breaking": lowered.contains("breaking"),
        "confidence": 0.84,
        "rationale": "Deterministic mock draft derived from commit-scoped evidence.",
    })
}

pub struct MockProvider;

fn response(content: String, confidence: f64) -> ProviderResponse {
    ProviderResponse {
        content,
        confidence,
        model: "mock-local".into(),
        metadata: Map::new(),
        reasoning: String::new(),
    }
}

impl Provider for MockProvider {
    fn summarize(&self, request: &ProviderRequest) -> ProviderResponse {
        match request.task.as_str() {
            "draft-conventional" => response(
                pyjson::dumps(
                    &draft_conventional(&request.evidence),
                    pyjson::DEFAULT_SORTED,
                ),
                0.84,
            ),
            "infer-conventional" | "derive-conventional" => response(
                pyjson::dumps(
                    &infer_conventional(&request.evidence),
                    pyjson::DEFAULT_SORTED,
                ),
                0.82,
            ),
            _ => {
                let mut rng = PyRandom::new(seed(request));
                let confidence = round2(0.6 + rng.random() * 0.35);
                response(summarize_from_evidence(&request.evidence), confidence)
            }
        }
    }

    fn classify(&self, request: &ProviderRequest) -> ProviderResponse {
        let classes = [
            "added/api/external/minor",
            "fixed/backend/external/patch",
            "internal/docs/internal/none",
            "changed/general/external/patch",
        ];
        let mut rng = PyRandom::new(seed(request));
        let choice = classes[rng.randrange(classes.len() as u32) as usize];
        let confidence = round2(0.55 + rng.random() * 0.4);
        response(format!("[mock-classification] {choice}"), confidence)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mersenne_twister_matches_cpython() {
        // random.Random(12345): random() and randrange(4) values from CPython.
        let mut rng = PyRandom::new(12345);
        assert_eq!(rng.random(), 0.41661987254534116);
        assert_eq!(rng.random(), 0.010169169457068361);
        let mut rng = PyRandom::new(42);
        assert_eq!(rng.randrange(4), 0);
        assert_eq!(rng.randrange(4), 0);
        assert_eq!(rng.randrange(4), 2);
        let mut rng = PyRandom::new(0xdead_beef);
        assert_eq!(round2(0.6 + rng.random() * 0.35), 0.61);
    }
}
