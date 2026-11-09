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
pub const ENGINE_REVISION: &str = "7ae58e9237e0be07ddf919b6d7a7cbc8a1333983";

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
}

pub type Progress<'a> = &'a (dyn Fn(usize, usize, &str) + Sync);

impl Engine {
    /// Build the engine for a repository, failing with an actionable message when it is not
    /// linked into this build or its parser bundle is missing.
    pub fn new(repo: &Path, cache_path: &str, parallelism: Option<usize>) -> Result<Engine> {
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
        })
    }

    fn cache_key(parent: Option<&str>, sha: &str) -> String {
        format!("{}..{sha}@{}", parent.unwrap_or("root"), engine_version())
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
            let key = Self::cache_key(commit.parent_sha.as_deref(), &commit.sha);
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
            Ok((files, unreviewed)) => {
                digest.files = files;
                digest.unreviewed = unreviewed;
            }
            Err(error) => {
                digest.error = Some(error.to_string().chars().take(MAX_REASON_CHARS).collect())
            }
        }
        digest
    }

    #[cfg(not(feature = "intentumdiff"))]
    fn review_range(
        &self,
        _parent: &str,
        _sha: &str,
    ) -> Result<(Vec<crate::semantic::FileSemantics>, Vec<UnreviewedFile>)> {
        bail!("IntentumDiff engine not linked")
    }

    #[cfg(feature = "intentumdiff")]
    fn review_range(
        &self,
        parent: &str,
        sha: &str,
    ) -> Result<(Vec<crate::semantic::FileSemantics>, Vec<UnreviewedFile>)> {
        use intentumdiff_rust_core::api::{review_sources, ReviewError, ReviewOptions};
        use rayon::prelude::*;

        let changes: Vec<RawChange> = self.git.raw_changes(parent, sha)?;
        let mut specs = Vec::with_capacity(changes.len() * 2);
        for change in &changes {
            specs.push(format!("{parent}:{}", change.old_path));
            specs.push(format!("{sha}:{}", change.new_path));
        }
        let blobs = self.git.read_blobs(&specs)?;
        let options = ReviewOptions::default();

        enum Outcome {
            Reviewed(crate::semantic::FileSemantics),
            Unreviewed(UnreviewedFile),
            Skipped,
        }
        let outcomes: Vec<Outcome> = changes
            .par_iter()
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
                // Binary blobs and gitlinks are not text sources; the engine skips them too.
                let (Some(old), Some(new)) = (
                    text(&blobs[index * 2], old_present),
                    text(&blobs[index * 2 + 1], new_present),
                ) else {
                    return Outcome::Skipped;
                };
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
                        &old,
                        &new,
                        &options,
                    )
                }));
                match reviewed {
                    Ok(Ok(review)) => match serde_json::to_value(&review) {
                        Ok(value) => Outcome::Reviewed(digest_file_diff(
                            &value,
                            filename,
                            &change.old_path,
                            Some(lifecycle),
                        )),
                        Err(error) => Outcome::Unreviewed(UnreviewedFile {
                            path: filename.clone(),
                            kind: "pipeline_error".into(),
                            reason: truncate_chars(&error.to_string(), MAX_REASON_CHARS),
                        }),
                    },
                    Ok(Err(error)) => Outcome::Unreviewed(UnreviewedFile {
                        path: filename.clone(),
                        kind: match error {
                            ReviewError::Unsupported(_) => "no_parser",
                            _ => "pipeline_error",
                        }
                        .into(),
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
        Ok((files, unreviewed))
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
pub fn engine_info(diff_engine: &str, cache_path: &str) -> serde_json::Value {
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
        "semantic_cross_file": false,
    })
}
