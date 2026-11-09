//! `verlato.json` loading. Ports the parts of `verlato/config.py` the native pipeline uses;
//! the raw document is kept so delegated (Python) features see the same file.

use anyhow::{Context, Result};
use indexmap::IndexMap;
use serde_json::Value;
use std::path::{Path, PathBuf};

use crate::semantic::{normalize_diff_engine, DEFAULT_DIFF_ENGINE, DEFAULT_SEMANTIC_CACHE_PATH};

#[derive(Debug, Clone, PartialEq)]
pub struct OutputTarget {
    pub path: String,
    pub format: String,
}

#[derive(Debug, Clone)]
pub struct Config {
    pub state_path: String,
    pub diff_engine: String,
    pub semantic_cache_path: String,
    pub semantic_parallelism: Option<usize>,
    pub commit_intent_inference: String,
    pub provider_name: String,
    pub max_items_per_scan: i64,
    pub feedback_include_text: bool,
    pub targets: IndexMap<String, OutputTarget>,
    pub raw: Value,
}

impl Default for Config {
    fn default() -> Self {
        let mut targets = IndexMap::new();
        targets.insert(
            "changelog".into(),
            OutputTarget {
                path: "CHANGELOG.md".into(),
                format: "changelog".into(),
            },
        );
        targets.insert(
            "github-release".into(),
            OutputTarget {
                path: ".github/release.md".into(),
                format: "github-release".into(),
            },
        );
        Self {
            state_path: ".verlato_state.json".into(),
            diff_engine: DEFAULT_DIFF_ENGINE.into(),
            semantic_cache_path: DEFAULT_SEMANTIC_CACHE_PATH.into(),
            semantic_parallelism: None,
            commit_intent_inference: "off".into(),
            provider_name: "ollama".into(),
            max_items_per_scan: 25,
            feedback_include_text: false,
            targets,
            raw: Value::Null,
        }
    }
}

pub fn config_path(cwd: &Path) -> Option<PathBuf> {
    if let Ok(path) = std::env::var("VERLATO_CONFIG_PATH") {
        if !path.is_empty() {
            return Some(PathBuf::from(path));
        }
    }
    let candidate = cwd.join("verlato.json");
    candidate.exists().then_some(candidate)
}

pub fn load(cwd: &Path) -> Result<Config> {
    let mut config = Config::default();
    let Some(path) = config_path(cwd) else {
        return Ok(config);
    };
    let text = std::fs::read_to_string(&path)
        .with_context(|| format!("Unable to load config from {}", path.display()))?;
    let raw: Value = serde_json::from_str(&text)
        .with_context(|| format!("Unable to load config from {}", path.display()))?;
    let str_of = |key: &str| raw.get(key).and_then(Value::as_str).map(str::to_owned);
    if let Some(v) = str_of("state_path").filter(|v| !v.is_empty()) {
        config.state_path = v;
    }
    if let Some(v) = str_of("diff_engine") {
        config.diff_engine = normalize_diff_engine(&v).into();
    }
    if let Some(v) = str_of("semantic_cache_path") {
        config.semantic_cache_path = v;
    }
    if let Some(v) = raw.get("semantic_parallelism").and_then(Value::as_u64) {
        config.semantic_parallelism = Some(v as usize);
    }
    if let Some(v) = str_of("commit_intent_inference") {
        let v = v.trim().to_lowercase();
        config.commit_intent_inference = if v == "low-signal" { v } else { "off".into() };
    }
    if let Some(v) = raw.get("feedback_include_text").and_then(Value::as_bool) {
        config.feedback_include_text = v;
    }
    if let Some(provider) = raw.get("provider").and_then(Value::as_object) {
        if let Some(name) = provider
            .get("name")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|n| !n.is_empty())
        {
            config.provider_name = name.to_owned();
        }
        if let Some(max) = provider.get("max_items_per_scan").and_then(Value::as_i64) {
            config.max_items_per_scan = max;
        }
    }
    if let Some(targets) = raw.get("targets").and_then(Value::as_object) {
        let mut parsed = IndexMap::new();
        for (name, target) in targets {
            let target = target
                .as_object()
                .with_context(|| format!("Invalid target config for {name:?}: expected object"))?;
            let path = target
                .get("path")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim()
                .to_owned();
            let format = target
                .get("format")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim()
                .to_owned();
            anyhow::ensure!(
                !path.is_empty() && !format.is_empty(),
                "Invalid target config for {name:?}: require path and format"
            );
            parsed.insert(name.clone(), OutputTarget { path, format });
        }
        if !parsed.is_empty() {
            config.targets = parsed;
        }
    }
    config.raw = raw;
    Ok(config)
}

impl Config {
    pub fn state_path(&self) -> String {
        std::env::var("VERLATO_STATE_PATH")
            .ok()
            .filter(|p| !p.is_empty())
            .unwrap_or_else(|| self.state_path.clone())
    }

    /// The model provider a scan would use: CLI request > `VERLATO_MODEL_PROVIDER` > config.
    pub fn effective_provider(&self, requested: Option<&str>) -> String {
        requested
            .map(str::to_owned)
            .or_else(|| {
                std::env::var("VERLATO_MODEL_PROVIDER")
                    .ok()
                    .filter(|v| !v.trim().is_empty())
            })
            .unwrap_or_else(|| self.provider_name.clone())
            .trim()
            .to_lowercase()
    }

    pub fn max_model_items(&self) -> i64 {
        std::env::var("VERLATO_MODEL_MAX_ITEMS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(self.max_items_per_scan)
    }

    /// CLI flag > `VERLATO_COMMIT_INTENT_INFERENCE` > config.
    pub fn commit_intent_inference(&self, requested: Option<bool>) -> String {
        if let Some(flag) = requested {
            return if flag {
                "low-signal".into()
            } else {
                "off".into()
            };
        }
        match std::env::var("VERLATO_COMMIT_INTENT_INFERENCE").map(|v| v.trim().to_lowercase()) {
            Ok(v) if v == "low-signal" || v == "off" => v,
            _ => self.commit_intent_inference.clone(),
        }
    }

    pub fn feedback_include_text(&self) -> bool {
        match std::env::var("VERLATO_FEEDBACK_INCLUDE_TEXT") {
            Ok(v) => matches!(
                v.trim().to_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            ),
            Err(_) => self.feedback_include_text,
        }
    }
}

pub fn resolve_target_path(path: &str, cwd: &Path) -> PathBuf {
    let target = PathBuf::from(path);
    if target.is_absolute() {
        target
    } else {
        cwd.join(target)
    }
}
