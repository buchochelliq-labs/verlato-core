//! The IntentumDiff engine, linked in-process through its supported Rust API
//! (`intentumdiff_rust_core::api::review_sources`). No Python is involved.
//!
//! For every commit the engine reviews `parent..sha` file by file: the changed paths come from
//! `git diff --raw`, both blob versions are read with one `git cat-file --batch`, and each file
//! is reviewed in parallel. Files the engine cannot review are recorded with the engine's
//! reason instead of failing the commit. Digests are cached per commit.

use anyhow::{bail, Result};
use std::path::{Path, PathBuf};

use crate::git::{Git, RawChange};
use crate::models::Commit;
use crate::semantic::{
    cache_from_env, CommitSemantics, SemanticCache, UnreviewedFile, MAX_REASON_CHARS,
};
#[cfg(feature = "intentumdiff")]
use crate::semantic::{digest_file_diff, truncate_chars};

/// The engine revision this build links (pinned in Cargo.toml).
pub const ENGINE_REVISION: &str = "1c4bdbc5dfbe6e738b3d70d8964e7f148d711821";

pub fn engine_version() -> String {
    format!("intentumdiff-core@{}", &ENGINE_REVISION[..12])
}

pub const WASM_DIR_HINT: &str = "Set VERLATO_INTENTUMDIFF_WASM_DIR (or INTENTUMDIFF_WASM_DIR) to a directory containing \
     parser_manifest.json and the parser .wasm components, or place a `wasm/` directory next to the verlato \
     executable. `scripts/fetch_intentumdiff_parsers.sh` downloads the pinned parser bundle. To use legacy \
     text-patch evidence instead, set diff_engine to \"git\" (VERLATO_DIFF_ENGINE=git).";

/// Locate the IntentumDiff parser bundle (`parser_manifest.json` + `*.wasm`).
pub fn resolve_wasm_dir() -> Result<PathBuf> {
    let has_manifest = |dir: &Path| dir.join("parser_manifest.json").is_file();
    let mut searched = Vec::new();
    for var in ["VERLATO_INTENTUMDIFF_WASM_DIR", "INTENTUMDIFF_WASM_DIR"] {
        if let Ok(dir) = std::env::var(var) {
            if !dir.is_empty() {
                let dir = PathBuf::from(dir);
                if has_manifest(&dir) {
                    return Ok(dir);
                }
                bail!(
                    "{var}={} has no parser_manifest.json. {WASM_DIR_HINT}",
                    dir.display()
                );
            }
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(bin_dir) = exe.parent() {
            for candidate in [
                bin_dir.join("wasm"),
                bin_dir.join("../share/verlato/wasm"),
                bin_dir.join("../../wasm"),
            ] {
                if has_manifest(&candidate) {
                    return Ok(candidate);
                }
                searched.push(candidate.display().to_string());
            }
        }
    }
    bail!(
        "Verlato is powered by the IntentumDiff semantic engine, but its parser bundle was not found (searched: {}). {WASM_DIR_HINT}",
        searched.join(", ")
    )
}

pub fn engine_linked() -> bool {
    cfg!(feature = "intentumdiff")
}

pub struct Engine {
    git: Git,
    repo_root: PathBuf,
    #[cfg_attr(not(feature = "intentumdiff"), allow(dead_code))]
    wasm_dir: PathBuf,
    cache: Option<SemanticCache>,
    parallelism: usize,
    options: EngineOptions,
}

/// Engine settings beyond the cache and parallelism (`verlato.json`, overridden by environment).
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct EngineOptions {
    /// Commit-wide cross-file move/rename detection (`semantic_cross_file`,
    /// `VERLATO_SEMANTIC_CROSS_FILE`).
    pub cross_file: bool,
    /// Raised size limit for very large files, in tree nodes (`semantic_max_nodes`,
    /// `VERLATO_SEMANTIC_MAX_NODES`); `None` keeps the engine's defaults.
    pub max_nodes: Option<usize>,
}

impl EngineOptions {
    /// Apply the environment overrides to the configured values.
    pub fn resolve(cross_file: bool, max_nodes: Option<usize>) -> EngineOptions {
        let env = |name: &str| std::env::var(name).ok().filter(|v| !v.trim().is_empty());
        let cross_file = env("VERLATO_SEMANTIC_CROSS_FILE")
            .map(|v| {
                matches!(
                    v.trim().to_lowercase().as_str(),
                    "1" | "true" | "yes" | "on"
                )
            })
            .unwrap_or(cross_file);
        let max_nodes = match env("VERLATO_SEMANTIC_MAX_NODES") {
            Some(v) => v.trim().parse::<usize>().ok().filter(|n| *n > 0),
            None => max_nodes,
        };
        EngineOptions {
            cross_file,
            max_nodes,
        }
    }
}

/// What reviewing one commit range produced.
#[cfg_attr(not(feature = "intentumdiff"), allow(dead_code))]
#[derive(Default)]
struct RangeReview {
    files: Vec<crate::semantic::FileSemantics>,
    unreviewed: Vec<UnreviewedFile>,
    cross_file: Vec<serde_json::Value>,
    cross_file_error: Option<String>,
}

/// Engine bytes allowed per tree node when `max_nodes` is raised: the serialized tree takes
/// roughly 170 bytes per node, so the byte limits must grow with the node limit.
#[cfg(feature = "intentumdiff")]
const BYTES_PER_NODE: usize = 512;
/// Languages left out of the cross-file symbol index.
#[cfg(feature = "intentumdiff")]
const NON_CODE_LANGUAGES: &[&str] = &[
    "markdown",
    "mdx",
    "asciidoc",
    "latex",
    "json",
    "yaml",
    "toml",
    "ini",
    "xml",
    "html",
    "css",
    "scss",
    "text",
    "plaintext",
    "po",
    "gitignore",
    "csv",
    "generic",
];
/// The engine's default byte limits (16 MiB); raised limits never go below them.
#[cfg(feature = "intentumdiff")]
const DEFAULT_ENGINE_BYTE_LIMIT: usize = 16 * 1024 * 1024;

pub type Progress<'a> = &'a (dyn Fn(usize, usize, &str) + Sync);

impl Engine {
    /// Build the engine for a repository, failing with an actionable message when it is not
    /// linked into this build or its parser bundle is missing.
    pub fn new(
        repo: &Path,
        cache_path: &str,
        parallelism: Option<usize>,
        options: EngineOptions,
    ) -> Result<Engine> {
        if !engine_linked() {
            bail!(
                "This verlato build does not include the IntentumDiff engine (built without the `intentumdiff` \
                 feature). Rebuild with default features, or set diff_engine to \"git\"."
            );
        }
        let wasm_dir = resolve_wasm_dir()?;
        let git = Git::new(repo);
        let repo_root = git
            .run(&["rev-parse", "--show-toplevel"])
            .map(|p| PathBuf::from(p.trim()))
            .unwrap_or_else(|_| repo.to_path_buf());
        Ok(Engine {
            git,
            repo_root,
            wasm_dir,
            cache: cache_from_env(cache_path),
            parallelism: resolve_parallelism(parallelism),
            options,
        })
    }

    /// Digests depend on the options that change what is reviewed, so those are in the key.
    fn cache_key(&self, parent: Option<&str>, sha: &str) -> String {
        let mut scope = String::new();
        if self.options.cross_file {
            scope.push_str("+xf");
        }
        if let Some(nodes) = self.options.max_nodes {
            scope.push_str(&format!("+n{nodes}"));
        }
        format!(
            "{}..{sha}@{}{scope}",
            parent.unwrap_or("root"),
            engine_version()
        )
    }

    /// Attach a digest to every commit (cached commits are not re-reviewed).
    pub fn annotate(&self, commits: &mut [Commit], progress: Progress<'_>) {
        use rayon::prelude::*;
        let total = commits.len();
        let done = std::sync::atomic::AtomicUsize::new(0);
        let pool = rayon::ThreadPoolBuilder::new()
            .num_threads(self.parallelism)
            .build();
        let work = |commit: &mut Commit| {
            let key = self.cache_key(commit.parent_sha.as_deref(), &commit.sha);
            let digest = match self.cache.as_ref().and_then(|c| c.get(&key)) {
                Some(cached) => cached,
                None => {
                    let digest = self.review_commit(commit);
                    if let (Some(cache), None) = (&self.cache, &digest.error) {
                        cache.put(&key, &digest);
                    }
                    digest
                }
            };
            commit.semantics = Some(digest);
            let current = done.fetch_add(1, std::sync::atomic::Ordering::SeqCst) + 1;
            progress(current, total, &commit.sha);
        };
        match pool {
            Ok(pool) => pool.install(|| commits.par_iter_mut().for_each(work)),
            Err(_) => commits.iter_mut().for_each(work),
        }
        if let Some(cache) = &self.cache {
            cache.flush();
        }
    }

    pub fn review_commit(&self, commit: &Commit) -> CommitSemantics {
        let mut digest =
            CommitSemantics::new(&commit.sha, commit.parent_sha.as_deref(), &engine_version());
        let Some(parent) = commit.parent_sha.as_deref() else {
            // The root commit has no parent revision to compare against; report rather than guess.
            digest.unreviewed = commit
                .files
                .iter()
                .map(|f| UnreviewedFile {
                    path: f.path.clone(),
                    kind: "root-commit".into(),
                    reason: "Root commit: no parent revision to diff against.".into(),
                })
                .collect();
            return digest;
        };
        match self.review_range(parent, &commit.sha) {
            Ok(review) => {
                digest.files = review.files;
                digest.unreviewed = review.unreviewed;
                digest.cross_file = review.cross_file;
                digest.cross_file_error = review.cross_file_error;
            }
            Err(error) => {
                digest.error = Some(error.to_string().chars().take(MAX_REASON_CHARS).collect())
            }
        }
        digest
    }

    #[cfg(not(feature = "intentumdiff"))]
    fn review_range(&self, _parent: &str, _sha: &str) -> Result<RangeReview> {
        bail!("IntentumDiff engine not linked")
    }

    #[cfg(feature = "intentumdiff")]
    fn review_range(&self, parent: &str, sha: &str) -> Result<RangeReview> {
        use intentumdiff_rust_core::api::review_sources;
        use rayon::prelude::*;

        let changes: Vec<RawChange> = self.git.raw_changes(parent, sha)?;
        let mut specs = Vec::with_capacity(changes.len() * 2);
        for change in &changes {
            specs.push(format!("{parent}:{}", change.old_path));
            specs.push(format!("{sha}:{}", change.new_path));
        }
        let blobs = self.git.read_blobs(&specs)?;
        let options = self.review_options();

        enum Outcome {
            Reviewed(crate::semantic::FileSemantics),
            Unreviewed(UnreviewedFile),
            Skipped,
        }
        // The text sources of every reviewable change, for the cross-file index.
        let sources: Vec<Option<(String, String)>> = changes
            .iter()
            .enumerate()
            .map(|(index, change)| {
                let text = |blob: &Option<Vec<u8>>, present: bool| -> Option<String> {
                    if !present {
                        return Some(String::new());
                    }
                    let bytes = blob.as_ref()?;
                    if bytes.contains(&0) {
                        return None;
                    }
                    String::from_utf8(bytes.clone()).ok()
                };
                let (old_present, new_present) = (change.status != 'A', change.status != 'D');
                Some((
                    text(&blobs[index * 2], old_present)?,
                    text(&blobs[index * 2 + 1], new_present)?,
                ))
            })
            .collect();
        let outcomes: Vec<Outcome> = changes
            .par_iter()
            .enumerate()
            .map(|(index, change)| {
                // Binary blobs and gitlinks are not text sources; the engine skips them too.
                let Some((old, new)) = &sources[index] else {
                    return Outcome::Skipped;
                };
                let new_present = change.status != 'D';
                let filename = if new_present {
                    &change.new_path
                } else {
                    &change.old_path
                };
                let lifecycle = match change.status {
                    'A' => "added",
                    'D' => "deleted",
                    _ => "modified",
                };
                let reviewed = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    review_sources(
                        &self.repo_root,
                        &self.wasm_dir,
                        filename,
                        old,
                        new,
                        &options,
                    )
                }));
                match reviewed {
                    // The engine could not explain this file's change (core #252): never report
                    // it as a clean review.
                    Ok(Ok(review)) if review.completeness.as_deref() == Some("incomplete") => {
                        Outcome::Unreviewed(UnreviewedFile {
                            path: filename.clone(),
                            kind: "incomplete".into(),
                            reason: truncate_chars(
                                review
                                    .incomplete
                                    .as_ref()
                                    .map(|i| i.message.as_str())
                                    .unwrap_or("IntentumDiff could not explain this change"),
                                MAX_REASON_CHARS,
                            ),
                        })
                    }
                    Ok(Ok(review)) => match serde_json::to_value(&review) {
                        Ok(value) => Outcome::Reviewed(digest_file_diff(
                            &value,
                            filename,
                            &change.old_path,
                            Some(lifecycle),
                            Some((old, new)),
                        )),
                        Err(error) => Outcome::Unreviewed(UnreviewedFile {
                            path: filename.clone(),
                            kind: "pipeline_error".into(),
                            reason: truncate_chars(&error.to_string(), MAX_REASON_CHARS),
                        }),
                    },
                    Ok(Err(error)) => Outcome::Unreviewed(UnreviewedFile {
                        path: filename.clone(),
                        kind: unreviewed_kind(&error).into(),
                        reason: truncate_chars(&error.to_string(), MAX_REASON_CHARS),
                    }),
                    Err(_) => Outcome::Unreviewed(UnreviewedFile {
                        path: filename.clone(),
                        kind: "pipeline_error".into(),
                        reason: "IntentumDiff engine panicked while reviewing this file".into(),
                    }),
                }
            })
            .collect();
        let mut files = Vec::new();
        let mut unreviewed = Vec::new();
        for outcome in outcomes {
            match outcome {
                Outcome::Reviewed(file) => files.push(file),
                Outcome::Unreviewed(file) => unreviewed.push(file),
                Outcome::Skipped => {}
            }
        }
        let mut review = RangeReview {
            files,
            unreviewed,
            ..RangeReview::default()
        };
        if self.options.cross_file {
            self.attach_cross_file(&changes, &sources, &mut review);
        }
        Ok(review)
    }

    #[cfg(feature = "intentumdiff")]
    fn review_options(&self) -> intentumdiff_rust_core::api::ReviewOptions {
        let mut options = intentumdiff_rust_core::api::ReviewOptions::default();
        if let Some(nodes) = self.options.max_nodes {
            // A larger tree also needs room in the parser-output and tree byte limits.
            let bytes = nodes
                .saturating_mul(BYTES_PER_NODE)
                .max(DEFAULT_ENGINE_BYTE_LIMIT);
            options.max_nodes = Some(nodes);
            options
                .settings
                .insert("max_cst_bytes".into(), serde_json::json!(bytes));
            options
                .settings
                .insert("max_plugin_output_bytes".into(), serde_json::json!(bytes));
        }
        options
    }

    /// Commit-wide cross-file analysis (symbols moved or renamed between files). Each changed
    /// file's sides are parsed with the engine's parsers and handed to its commit index, which
    /// keeps going when a file can't be indexed and reports that file instead (core #298).
    #[cfg(feature = "intentumdiff")]
    fn attach_cross_file(
        &self,
        changes: &[RawChange],
        sources: &[Option<(String, String)>],
        review: &mut RangeReview,
    ) {
        use intentumdiff_rust_core::{c_abi::dispatch, parse_to_tree};
        use rayon::prelude::*;
        use serde_json::{json, Value};

        let config = self.review_options();
        let config = serde_json::to_string(&config).unwrap_or_else(|_| "{}".into());
        let wasm_dir = self.wasm_dir.to_string_lossy().into_owned();
        let files: Vec<Value> = changes
            .par_iter()
            .zip(sources.par_iter())
            .filter_map(|(change, source)| {
                let (old, new) = source.as_ref()?;
                let side = |present: bool, path: &str, content: &str| -> Option<(Value, Value, String)> {
                    if !present {
                        return Some((Value::Null, Value::Null, String::new()));
                    }
                    match parse_to_tree(path, content, &config, &wasm_dir) {
                        Ok(parsed) => {
                            let mut parsed: Value = serde_json::from_str(&parsed).ok()?;
                            let language = parsed["language"].as_str().unwrap_or("").to_owned();
                            Some((parsed["tree"].take(), Value::Null, language))
                        }
                        // Files without a parser take no part in symbol matching.
                        Err(error) if error.contains("no bundled parser") => None,
                        Err(error) => Some((Value::Null, json!(error), String::new())),
                    }
                };
                let (old_tree, old_error, old_language) =
                    side(change.status != 'A', &change.old_path, old)?;
                let (new_tree, new_error, new_language) =
                    side(change.status != 'D', &change.new_path, new)?;
                let language = if new_language.is_empty() { old_language } else { new_language };
                // Like the retired Python implementation, only code takes part in symbol
                // matching: documents, data and tests repeat names without moving them.
                let path = if change.status == 'D' { &change.old_path } else { &change.new_path };
                if NON_CODE_LANGUAGES.contains(&language.to_lowercase().as_str())
                    || crate::vocab::is_test_path(&path.to_lowercase())
                {
                    return None;
                }
                Some(json!({
                    "old_filename": if change.status == 'A' { Value::Null } else { json!(change.old_path) },
                    "new_filename": if change.status == 'D' { Value::Null } else { json!(change.new_path) },
                    "language": language,
                    "old_tree": old_tree,
                    "new_tree": new_tree,
                    "old_error": old_error,
                    "new_error": new_error,
                }))
            })
            .collect();
        if files.is_empty() {
            return;
        }
        let envelope: Value = match std::panic::catch_unwind(|| {
            dispatch(
                "commit_cross_file_index",
                &[json!(Value::Array(files).to_string())],
            )
        }) {
            Ok(raw) => serde_json::from_str(&raw).unwrap_or(Value::Null),
            Err(_) => {
                review.cross_file_error =
                    Some("IntentumDiff engine panicked in cross-file analysis".into());
                return;
            }
        };
        if envelope["ok"] != json!(true) {
            let message = envelope["error"]
                .as_str()
                .unwrap_or("cross-file analysis failed");
            review.cross_file_error = Some(truncate_chars(message, MAX_REASON_CHARS));
            return;
        }
        let result = &envelope["result"];
        review.cross_file = result["cross_file_changes"]
            .as_array()
            .map(|changes| {
                changes
                    .iter()
                    .filter_map(crate::semantic::digest_cross_file_change)
                    .take(crate::semantic::MAX_CROSS_FILE)
                    .collect()
            })
            .unwrap_or_default();
        let excluded: Vec<String> = result["index_errors"]
            .as_array()
            .map(|errors| {
                errors
                    .iter()
                    .map(|error| {
                        let file = ["new_filename", "old_filename", "file"]
                            .iter()
                            .find_map(|key| error[*key].as_str().filter(|s| !s.is_empty()))
                            .unwrap_or("?");
                        match error["code"].as_str().or(error["kind"].as_str()) {
                            Some(code) => format!("{file} ({code})"),
                            None => file.to_owned(),
                        }
                    })
                    .collect()
            })
            .unwrap_or_default();
        if !excluded.is_empty() {
            review.cross_file_error = Some(truncate_chars(
                &format!("excluded from cross-file index: {}", excluded.join(", ")),
                MAX_REASON_CHARS,
            ));
        }
    }
}

/// How an engine error is recorded for an unreviewed file: a file over the engine's size
/// limits is `too_large` (raise `semantic_max_nodes`), not a missing parser.
#[cfg(feature = "intentumdiff")]
fn unreviewed_kind(error: &intentumdiff_rust_core::api::ReviewError) -> &'static str {
    use intentumdiff_rust_core::api::ReviewError;
    let text = error.to_string();
    if text.contains("too large") || text.contains("limit is") {
        return "too_large";
    }
    match error {
        ReviewError::Unsupported(_) => "no_parser",
        _ => "pipeline_error",
    }
}

/// Worker threads for uncached commit reviews (`VERLATO_SEMANTIC_PARALLELISM`, default min(4, cpus)).
pub fn resolve_parallelism(configured: Option<usize>) -> usize {
    let env = std::env::var("VERLATO_SEMANTIC_PARALLELISM")
        .ok()
        .and_then(|v| v.trim().parse::<usize>().ok());
    let value = env.or(configured).filter(|v| *v > 0).unwrap_or_else(|| {
        std::thread::available_parallelism()
            .map(|n| n.get())
            .unwrap_or(1)
            .min(4)
    });
    value.clamp(1, 16)
}

/// `verlato doctor engine` payload.
pub fn engine_info(
    diff_engine: &str,
    cache_path: &str,
    options: EngineOptions,
) -> serde_json::Value {
    let resolved = resolve_wasm_dir();
    let available = diff_engine == "git" || (engine_linked() && resolved.is_ok());
    serde_json::json!({
        "engine": diff_engine,
        "available": available,
        "version": if diff_engine == "git" { serde_json::Value::Null } else { serde_json::json!(engine_version()) },
        "error": if diff_engine == "git" || available {
            serde_json::Value::Null
        } else if !engine_linked() {
            serde_json::json!("This verlato build does not include the IntentumDiff engine.")
        } else {
            serde_json::json!(resolved.as_ref().err().map(|e| e.to_string()))
        },
        "intentumdiff_version": if engine_linked() { serde_json::json!(engine_version()) } else { serde_json::Value::Null },
        "runtime": "rust",
        "parser_bundle": resolved.ok().map(|p| p.display().to_string()),
        "configured": diff_engine,
        "semantic_cache_path": std::env::var("VERLATO_SEMANTIC_CACHE_PATH").unwrap_or_else(|_| cache_path.to_owned()),
        "semantic_cross_file": options.cross_file,
    })
}
