//! Release-note operations shared by the CLI and the stdio service: scan, reconcile, draft
//! documents, review updates and state maintenance. Ports the deterministic flows of
//! `verlato/cli/main.py` and `verlato/mcp/service.py`.

use anyhow::{anyhow, bail, Result};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};

use crate::assist::{resolve_model_privacy, ModelAssist, Style};
use crate::classifier::classify;
use crate::config::{self, Config};
use crate::drafts::{self, today, ApplyResult, DraftDocument};
use crate::engine::Engine;
use crate::git::{map_commits_to_prs, Git, PrAdapter};
use crate::grouper;
use crate::models::{ChangeGroup, ChangeItem};
use crate::providers::{build_provider, ProviderConfig};
use crate::semantic::resolve_diff_engine;
use crate::store::{StateData, StateStore};

pub fn trace_progress(stage: &str, label: &str, extra: Value) {
    if std::env::var("VERLATO_TRACE_PROGRESS").as_deref() != Ok("1") {
        return;
    }
    let mut event = Map::new();
    event.insert("event".into(), json!("progress"));
    event.insert("stage".into(), json!(stage));
    event.insert("label".into(), json!(label));
    if let Value::Object(extra) = extra {
        event.extend(extra.into_iter().filter(|(_, v)| !v.is_null() && v != ""));
    }
    // serde_json maps are key-sorted, matching Python's sort_keys=True.
    eprintln!("[verlato:progress] json {}", Value::Object(event));
}

pub struct Context {
    pub cwd: PathBuf,
    pub config: Config,
}

/// Model options of a scan (`--model-provider`, `--privacy`, `--allow-remote-patches`).
#[derive(Debug, Clone, Default)]
pub struct ModelRequest {
    pub provider: Option<String>,
    pub privacy: Option<String>,
    pub allow_remote_patches: bool,
    /// `--infer-commit-intents` / `--no-infer-commit-intents` (None: env, then config).
    pub infer_commit_intents: Option<bool>,
}

impl Context {
    pub fn load(cwd: &Path) -> Result<Context> {
        Ok(Context {
            cwd: cwd.to_path_buf(),
            config: config::load(cwd)?,
        })
    }

    pub fn store(&self) -> StateStore {
        let path = PathBuf::from(self.config.state_path());
        StateStore::new(if path.is_absolute() {
            path
        } else {
            self.cwd.join(path)
        })
    }

    fn git(&self) -> Git {
        Git::new(&self.cwd)
    }

    pub fn diff_engine(&self) -> &'static str {
        resolve_diff_engine(Some(&self.config.diff_engine), None)
    }

    pub fn semantic_engine(&self) -> Result<Option<Engine>> {
        if self.diff_engine() == "git" {
            return Ok(None);
        }
        Engine::new(
            &self.cwd,
            &self.config.semantic_cache_path,
            self.config.semantic_parallelism,
        )
        .map(Some)
    }

    /// The forge adapter `VERLATO_PR_ADAPTER` selects: a built-in (`standard-git`, `none`,
    /// `github`) or one an extension registered; unknown names use `standard-git`.
    pub fn pr_adapter(&self) -> Result<PrAdapter> {
        let name = std::env::var("VERLATO_PR_ADAPTER")
            .unwrap_or_else(|_| "standard-git".into())
            .to_lowercase();
        let adapter = match name.as_str() {
            "none" => PrAdapter::None,
            // Without a repository and token Python falls back to local git as well.
            "github" => crate::git::GitHubApi::from_env()
                .map(PrAdapter::GitHub)
                .unwrap_or(PrAdapter::StandardGit),
            "standard-git" => PrAdapter::StandardGit,
            other => match crate::extensions::current().forge_factory(other) {
                Some(factory) => factory(&crate::extensions::ForgeContext {
                    cwd: &self.cwd,
                    config: &self.config.raw,
                })?
                .map(PrAdapter::Extension)
                .unwrap_or(PrAdapter::StandardGit),
                None => PrAdapter::StandardGit,
            },
        };
        crate::extensions::check_forge(adapter.name(), adapter.is_remote())?;
        Ok(adapter)
    }

    // -- detection ---------------------------------------------------------

    /// Commit intent inference for collected commits (ports `_maybe_infer_commit_intents`):
    /// the model derives a conventional commit from each diff, and low-signal commits use it
    /// for grouping.
    fn infer_commit_intents(&self, commits: &mut [crate::models::Commit], request: &ModelRequest) {
        if self
            .config
            .commit_intent_inference(request.infer_commit_intents)
            == "off"
        {
            return;
        }
        let raw = &self.config.raw;
        let privacy = resolve_model_privacy(
            raw.get("model_privacy").and_then(Value::as_str),
            request.privacy.as_deref(),
            request.allow_remote_patches,
        );
        let eligible: Vec<usize> = (0..commits.len())
            .filter(|&i| crate::intents::is_low_signal_commit(&commits[i]))
            .collect();
        let total = commits.len();
        const STAGE: &str = "inferring_commit_intents";
        trace_progress(
            STAGE,
            "Deriving conventional commit intents from diffs",
            json!({"detail": format!("{total} commit(s), {} eligible for grouping inference", eligible.len()), "current": 0, "total": total, "unit": "commit"}),
        );
        if commits.is_empty() {
            return;
        }
        let max_items = self.config.max_model_items();
        if max_items >= 0 && total as i64 > max_items {
            trace_progress(
                STAGE,
                "Skipped commit intent inference",
                json!({"detail": format!("{total} commit(s) exceeds fanout guard {max_items}"), "current": 0, "total": total, "unit": "commit"}),
            );
            return;
        }
        let provider_config = ProviderConfig::from_raw(raw);
        let provider = build_provider(&provider_config, request.provider.as_deref());
        let provider_mode = request
            .provider
            .clone()
            .filter(|p| !p.is_empty())
            .unwrap_or_else(|| provider_config.name.clone());
        let dispatch_privacy = if crate::assist::can_send_raw_patches_for_config(
            &provider_config,
            Some(&provider_mode),
            &privacy,
        ) {
            privacy
        } else {
            "metadata-only".to_owned()
        };
        let mut derived = 0;
        for commit in commits.iter_mut() {
            if let Some(intent) = crate::intents::derive_conventional_commit(
                commit,
                provider.as_ref(),
                &dispatch_privacy,
                &provider_mode,
            ) {
                commit.derived_intent = Some(intent);
                derived += 1;
            }
        }
        let mut inferred = 0;
        for &index in &eligible {
            if let Some(intent) = commits[index].derived_intent.clone() {
                commits[index].inferred_intent = Some(intent);
                inferred += 1;
            }
        }
        trace_progress(
            STAGE,
            "Derived conventional commit intents",
            json!({"detail": format!("{derived}/{total} derived; {inferred}/{} used for grouping inference", eligible.len()), "current": total, "total": total, "unit": "commit"}),
        );
    }

    pub fn collect_groups(
        &self,
        base: &str,
        head: &str,
        request: &ModelRequest,
    ) -> Result<Vec<ChangeGroup>> {
        trace_progress(
            "collecting_commits",
            "Collecting commits",
            json!({"detail": format!("{base}..{head}")}),
        );
        let mut commits = self.git().collect_commits(base, head)?;
        if let Some(engine) = self.semantic_engine()? {
            let progress = |current: usize, total: usize, sha: &str| {
                trace_progress(
                    "semantic_review",
                    "IntentumDiff semantic review",
                    json!({"detail": sha.chars().take(12).collect::<String>(), "current": current, "total": total, "unit": "commit"}),
                );
            };
            engine.annotate(&mut commits, &progress);
        }
        self.infer_commit_intents(&mut commits, request);
        let count = commits.len();
        trace_progress(
            "mapping_pull_requests",
            "Mapping commits to PRs",
            json!({"current": count, "total": count, "unit": "commit"}),
        );
        let prs = map_commits_to_prs(&self.pr_adapter()?, &commits)?;
        trace_progress(
            "grouping_changesets",
            "Grouping changesets",
            json!({"detail": format!("{count} commit(s)"), "current": 0, "total": count, "unit": "commit"}),
        );
        let groups = grouper::group(&commits, &prs);
        let total = groups.len();
        trace_progress(
            "grouping_changesets",
            "Grouped changesets",
            json!({"current": total, "total": total, "unit": "changeset"}),
        );
        Ok(groups)
    }

    /// The classifier's model assists for a scan of `groups` changesets (ports the provider
    /// set-up in `detect_change_items`): none when the fan-out guard is exceeded.
    fn model_assist(&self, request: &ModelRequest, groups: usize) -> Option<ModelAssist> {
        let max_items = self.config.max_model_items();
        if max_items >= 0 && groups as i64 > max_items {
            return None;
        }
        let raw = &self.config.raw;
        let provider_config = ProviderConfig::from_raw(raw);
        let provider_mode = request
            .provider
            .clone()
            .filter(|p| !p.is_empty())
            .or_else(|| Some(provider_config.name.clone()));
        Some(ModelAssist {
            provider: build_provider(&provider_config, request.provider.as_deref()),
            provider_mode,
            privacy: resolve_model_privacy(
                raw.get("model_privacy").and_then(Value::as_str),
                request.privacy.as_deref(),
                request.allow_remote_patches,
            ),
            style: Style::from_config(raw),
            summary_only: false,
        })
    }

    pub fn detect_items(
        &self,
        base: &str,
        head: &str,
        stage: &str,
        label: &str,
        request: &ModelRequest,
        with_model: bool,
    ) -> Result<Vec<ChangeItem>> {
        let groups = self.collect_groups(base, head, request)?;
        let total = groups.len();
        let assist = with_model
            .then(|| self.model_assist(request, total))
            .flatten();
        if total == 0 {
            trace_progress(
                stage,
                label,
                json!({"current": 0, "total": 0, "unit": "changeset"}),
            );
        }
        let mut items = Vec::with_capacity(total);
        for (index, group) in groups.iter().enumerate() {
            trace_progress(
                stage,
                label,
                json!({"detail": group_detail(group), "current": index, "total": total, "unit": "changeset"}),
            );
            let item = classify(group, base, head, assist.as_ref());
            trace_progress(
                stage,
                "Classified changeset",
                json!({"detail": item.id, "current": index + 1, "total": total, "unit": "changeset"}),
            );
            items.push(item);
        }
        Ok(items)
    }

    /// Draft and review candidates (ports `_detect_review_candidates`): with a requested model
    /// provider they are classified with model assists, as a scan would.
    fn review_candidates(
        &self,
        base: &str,
        head: &str,
        model: Option<&ModelRequest>,
    ) -> Result<Vec<ChangeItem>> {
        let default = ModelRequest::default();
        let request = model.unwrap_or(&default);
        if request
            .provider
            .as_deref()
            .is_some_and(|p| !p.trim().is_empty())
        {
            self.detect_items(
                base,
                head,
                "classifying_changes",
                "Classifying changesets",
                request,
                true,
            )
        } else {
            self.detect_items(
                base,
                head,
                "detecting_review_candidates",
                "Detecting review candidates",
                request,
                false,
            )
        }
    }

    /// Unreconciled items for a range; `persist` appends them to state (`generate`).
    pub fn scan(
        &self,
        base: &str,
        head: &str,
        persist: bool,
        model: &ModelRequest,
    ) -> Result<Vec<ChangeItem>> {
        let detected = self.detect_items(
            base,
            head,
            "classifying_changes",
            "Classifying changesets",
            model,
            true,
        )?;
        let total = detected.len();
        trace_progress(
            "reconciling_unpublished",
            "Reconciling unpublished changes",
            json!({"current": 0, "total": total, "unit": "item"}),
        );
        let store = self.store();
        let existing = store.load()?.items;
        let unpublished: Vec<ChangeItem> = detected
            .into_iter()
            .filter(|item| {
                !existing
                    .iter()
                    .any(|e| e.checksum_key() == item.checksum_key())
            })
            .collect();
        trace_progress(
            "reconciling_unpublished",
            "Reconciled unpublished changes",
            json!({"current": total, "total": total, "unit": "item", "unpublished": unpublished.len()}),
        );
        if persist && !unpublished.is_empty() {
            let count = unpublished.len();
            trace_progress(
                "persisting_working_state",
                "Persisting working state",
                json!({"current": count, "total": count, "unit": "item"}),
            );
            store.append_new(unpublished.clone())?;
        }
        Ok(unpublished)
    }

    pub fn merge_review_state(&self, items: Vec<ChangeItem>) -> Result<Vec<ChangeItem>> {
        let existing = self.store().load()?.items;
        let total = items.len();
        if total == 0 {
            trace_progress(
                "merging_review_state",
                "Merging review state",
                json!({"current": 0, "total": 0, "unit": "item"}),
            );
        }
        let mut merged = Vec::with_capacity(total);
        for (index, mut item) in items.into_iter().enumerate() {
            trace_progress(
                "merging_review_state",
                "Merging review state",
                json!({"detail": item.id, "current": index, "total": total, "unit": "item"}),
            );
            let persisted = existing
                .iter()
                .find(|e| e.checksum_key() == item.checksum_key())
                .or_else(|| existing.iter().find(|e| e.id == item.id));
            if let Some(persisted) = persisted {
                if persisted.published {
                    trace_progress(
                        "merging_review_state",
                        "Skipped published item",
                        json!({"detail": item.id, "current": index + 1, "total": total, "unit": "item"}),
                    );
                    continue;
                }
                item.status = persisted.status.clone();
                item.reviewed_by = persisted.reviewed_by.clone();
                item.reviewed_at = persisted.reviewed_at.clone();
                item.edited_summary = persisted.edited_summary.clone();
                item.summary_provenance = persisted.summary_provenance.clone();
                item.hidden = persisted.hidden;
                for signal in &persisted.evidence.commit_signals {
                    if signal.starts_with("provider-summary:")
                        && !item.evidence.commit_signals.contains(signal)
                    {
                        item.evidence.commit_signals.push(signal.clone());
                    }
                }
            }
            trace_progress(
                "merging_review_state",
                "Merged review state",
                json!({"detail": item.id, "current": index + 1, "total": total, "unit": "item"}),
            );
            merged.push(item);
        }
        Ok(merged)
    }

    pub fn build_draft(
        &self,
        base: &str,
        head: &str,
        version: &str,
        include_confidence: bool,
        include_provenance: bool,
        model: Option<&ModelRequest>,
    ) -> Result<DraftDocument> {
        trace_progress(
            "draft_detecting_changes",
            "Detecting draft changes",
            json!({"detail": format!("{base}..{head}")}),
        );
        let detected = self.review_candidates(base, head, model)?;
        let items = self.merge_review_state(detected)?;
        trace_progress(
            "draft_rendering",
            "Rendering release notes",
            json!({"current": 0, "total": items.len(), "unit": "item"}),
        );
        let document = drafts::build_document(
            &self.config,
            &items,
            base,
            head,
            version,
            include_confidence,
            include_provenance,
            &self.cwd,
        );
        trace_progress(
            "draft_ready",
            "Release notes ready",
            json!({"current": document.item_count, "total": document.item_count, "unit": "item"}),
        );
        Ok(document)
    }

    pub fn apply_draft(
        &self,
        document: &DraftDocument,
        target: &str,
        file: Option<&str>,
    ) -> Result<ApplyResult> {
        trace_progress(
            "draft_apply_writing",
            "Writing release notes",
            json!({"detail": target, "current": 0, "total": 1, "unit": "target"}),
        );
        crate::extensions::check_publish(&crate::extensions::PublishInfo {
            action: "draft.apply",
            version: Some(&document.version),
            target: Some(target),
            items: document
                .sections
                .iter()
                .flat_map(|section| section.items.iter())
                .map(|item| (item.id.clone(), item.status.clone()))
                .collect(),
        })?;
        let result = drafts::apply_document(&self.config, document, target, file, &self.cwd)?;
        crate::extensions::emit(
            "draft.applied",
            json!({"version": document.version, "target": result.target, "path": result.path, "items": document.item_count}),
        );
        trace_progress(
            "draft_apply_writing",
            "Wrote release notes",
            json!({"detail": result.target, "current": 1, "total": 1, "unit": "target"}),
        );
        Ok(result)
    }

    // -- review / state ----------------------------------------------------

    #[allow(clippy::too_many_arguments)]
    pub fn update_review(
        &self,
        base: &str,
        head: &str,
        item_id: &str,
        status: Option<&str>,
        summary: Option<&str>,
        reviewer: Option<&str>,
        summary_provenance: Option<Map<String, Value>>,
        model: Option<&ModelRequest>,
    ) -> Result<ChangeItem> {
        if status.is_none() && summary.is_none() && reviewer.is_none() {
            bail!("Provide at least one review update: --status, --summary, or --reviewer");
        }
        if let Some(status) = status {
            if !crate::models::REVIEW_STATUS_VALUES.contains(&status) {
                bail!("Unsupported review status: {status}");
            }
        }
        let candidates = self.review_candidates(base, head, model)?;
        let candidate = candidates
            .into_iter()
            .find(|item| item.id == item_id)
            .ok_or_else(|| {
                anyhow!("Could not find draft item {item_id} for range {base}..{head}")
            })?;
        self.persist_review(candidate, status, summary, reviewer, summary_provenance)
    }

    /// Apply a review update to `candidate` in state (ports `_persist_review_update`): adds the
    /// item if it is new, records summary provenance and the matching feedback event.
    pub fn persist_review(
        &self,
        candidate: ChangeItem,
        status: Option<&str>,
        summary: Option<&str>,
        reviewer: Option<&str>,
        summary_provenance: Option<Map<String, Value>>,
    ) -> Result<ChangeItem> {
        let store = self.store();
        let mut state = store.load()?;
        let index = match state
            .items
            .iter()
            .position(|i| i.checksum_key() == candidate.checksum_key() || i.id == candidate.id)
        {
            Some(index) => index,
            None => {
                let mut candidate = candidate;
                candidate.lineage_releases = state
                    .area_release_lineage
                    .get(&candidate.classification.area)
                    .cloned()
                    .unwrap_or_default();
                state.items.push(candidate);
                state.items.len() - 1
            }
        };
        let include_text = self.config.feedback_include_text();
        let mut feedback: Option<(String, Value)> = None;
        {
            let item = &mut state.items[index];
            if let Some(summary) = summary {
                let normalized = summary.trim().to_owned();
                let prior = item
                    .edited_summary
                    .clone()
                    .filter(|s| !s.is_empty())
                    .unwrap_or_else(|| item.content.summary.clone());
                item.edited_summary = (!normalized.is_empty()).then(|| normalized.clone());
                if !normalized.is_empty() {
                    let event = if let Some(provenance) = summary_provenance {
                        item.summary_provenance = Some(normalize_summary_provenance(provenance));
                        "suggestion_applied"
                    } else if let Some(existing) = item.summary_provenance.clone() {
                        let mut updated = existing.clone();
                        let model_sourced = existing
                            .get("source")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .starts_with("model");
                        updated.insert(
                            "source".into(),
                            json!(if model_sourced {
                                "human-edited-model"
                            } else {
                                "human-edited"
                            }),
                        );
                        updated.insert("edited_at".into(), json!(today()));
                        item.summary_provenance = Some(updated);
                        "summary_edited"
                    } else {
                        item.summary_provenance = Some(Map::from_iter([
                            ("source".to_owned(), json!("human-edited")),
                            ("applied_at".to_owned(), json!(today())),
                        ]));
                        "summary_edited"
                    };
                    let provenance = item.summary_provenance.clone().unwrap_or_default();
                    let field = |key: &str| provenance.get(key).cloned().unwrap_or(Value::Null);
                    let mut metadata = summary_metadata(&normalized, include_text);
                    metadata.insert("previous_summary_sha".into(), json!(short_sha(&prior)));
                    for key in [
                        "source",
                        "provider",
                        "model",
                        "prompt_sha",
                        "prompt_profile",
                        "cache_hit",
                        "tone",
                        "style_profile",
                        "similarity_to_current",
                        "specificity_score",
                        "source_evidence_count",
                    ] {
                        metadata.insert(key.into(), field(key));
                    }
                    metadata.retain(|_, value| !value.is_null());
                    feedback = Some((event.into(), Value::Object(metadata)));
                    if status.is_none() && item.status != "hidden" {
                        item.status = "edited".into();
                    }
                } else {
                    item.summary_provenance = None;
                }
            }
            if let Some(status) = status {
                item.status = status.into();
                item.hidden = status == "hidden";
            }
            if let Some(reviewer) = reviewer {
                let reviewer = reviewer.trim();
                item.reviewed_by = (!reviewer.is_empty()).then(|| reviewer.to_owned());
            }
            item.reviewed_at = Some(today());
        }
        if let Some((event, metadata)) = feedback {
            let id = state.items[index].id.clone();
            push_feedback(&mut state, &event, &id, metadata);
        }
        let updated = state.items[index].clone();
        store.save(&state)?;
        crate::extensions::emit(
            "review.updated",
            json!({"item_id": updated.id, "status": updated.status, "reviewer": updated.reviewed_by, "summary_edited": summary.is_some()}),
        );
        Ok(updated)
    }

    pub fn record_feedback(&self, event: &str, item_id: &str, metadata: Value) -> Result<Value> {
        let store = self.store();
        let mut state = store.load()?;
        push_feedback(&mut state, event, item_id, metadata);
        store.save(&state)?;
        if let Some(recorded) = state.feedback_events.last() {
            crate::extensions::emit("feedback.recorded", recorded.clone());
        }
        Ok(json!({"recorded": true, "event": event, "item_id": item_id}))
    }

    pub fn state_show(&self) -> Result<Value> {
        Ok(json!({"items": self.store().load()?.items}))
    }

    pub fn clear_working_state(&self) -> Result<Value> {
        let store = self.store();
        let mut state = store.load()?;
        let removed_ids: Vec<String> = state
            .items
            .iter()
            .filter(|i| !i.published)
            .map(|i| i.id.clone())
            .collect();
        let before = state.feedback_events.len();
        if !removed_ids.is_empty() {
            state.feedback_events.retain(|event| {
                !event
                    .get("item_id")
                    .and_then(Value::as_str)
                    .is_some_and(|id| removed_ids.iter().any(|r| r == id))
            });
        }
        let removed_feedback = before - state.feedback_events.len();
        let removed_items = removed_ids.len();
        state.items.retain(|i| i.published);
        store.save(&state)?;
        Ok(json!({
            "removed_working_items": removed_items,
            "removed_feedback_events": removed_feedback,
            "preserved_published_items": state.items.len(),
        }))
    }

    pub fn mark_published(
        &self,
        version: &str,
        ids: &[String],
        all_unpublished: bool,
    ) -> Result<Value> {
        let store = self.store();
        let mut state = store.load()?;
        crate::extensions::check_publish(&crate::extensions::PublishInfo {
            action: "state.mark_published",
            version: Some(version),
            target: None,
            items: state
                .items
                .iter()
                .filter(|item| (all_unpublished || ids.contains(&item.id)) && !item.published)
                .map(|item| (item.id.clone(), item.status.clone()))
                .collect(),
        })?;
        let mut updated = 0;
        let mut published_ids = Vec::new();
        for item in &mut state.items {
            if (all_unpublished || ids.contains(&item.id)) && !item.published {
                item.published = true;
                item.published_in = Some(version.into());
                updated += 1;
                published_ids.push(item.id.clone());
            }
        }
        store.save(&state)?;
        crate::extensions::emit(
            "release.published",
            json!({"version": version, "item_ids": published_ids}),
        );
        Ok(json!({"updated": updated, "version": version}))
    }

    pub fn latest_published_version(&self) -> Result<Option<String>> {
        Ok(self
            .store()
            .load()?
            .items
            .iter()
            .rev()
            .filter(|i| i.published)
            .find_map(|i| i.published_in.clone()))
    }

    pub fn resolve_range(
        &self,
        base: Option<&str>,
        head: &str,
        from_latest_tag: bool,
        from_latest_published: bool,
    ) -> Result<(String, String)> {
        if let Some(base) = base {
            return Ok((base.into(), head.into()));
        }
        match (from_latest_tag, from_latest_published) {
            (true, true) => {
                bail!("Choose only one marker mode: --from-latest-tag or --from-latest-published")
            }
            (false, false) => bail!(
                "Provide --base or one marker mode: --from-latest-tag / --from-latest-published"
            ),
            (true, false) => {
                let tag = self
                    .git()
                    .latest_tag(head)
                    .ok_or_else(|| anyhow!("No reachable tag found from {head}"))?;
                Ok((tag, head.into()))
            }
            (false, true) => {
                let marker = self
                    .latest_published_version()?
                    .ok_or_else(|| anyhow!("No published release marker found in state"))?;
                Ok((marker, head.into()))
            }
        }
    }
}

fn group_detail(group: &ChangeGroup) -> String {
    match &group.pr {
        Some(pr) => format!("PR #{}", pr.number),
        None => group
            .commits
            .first()
            .map(|c| c.sha.chars().take(12).collect())
            .unwrap_or_else(|| "changeset".into()),
    }
}

fn short_sha(text: &str) -> String {
    hex::encode(Sha256::digest(text.as_bytes()))[..12].to_owned()
}

fn summary_metadata(summary: &str, include_text: bool) -> Map<String, Value> {
    let mut metadata = Map::new();
    metadata.insert("summary_chars".into(), json!(summary.chars().count()));
    metadata.insert("summary_sha".into(), json!(short_sha(summary)));
    if include_text {
        metadata.insert("summary".into(), json!(summary));
    }
    metadata
}

pub fn normalize_summary_provenance(mut provenance: Map<String, Value>) -> Map<String, Value> {
    let source = provenance
        .get("source")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .unwrap_or("model-suggestion")
        .to_owned();
    provenance.insert("source".into(), json!(source));
    provenance
        .entry("applied_at")
        .or_insert_with(|| json!(today()));
    provenance
}

fn push_feedback(state: &mut StateData, event: &str, item_id: &str, metadata: Value) {
    state.feedback_events.push(json!({
        "event": event,
        "item_id": item_id,
        "recorded_at": today(),
        "metadata": if metadata.is_object() { metadata } else { json!({}) },
    }));
}

/// An error the stdio service reports with Python's exception class as its code (Python's
/// service loop answers unexpected exceptions with `exc.__class__.__name__` and `str(exc)`).
#[derive(Debug)]
pub struct ServiceError {
    pub code: &'static str,
    pub message: String,
}

impl std::fmt::Display for ServiceError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for ServiceError {}

/// Options of `commit conventional` and the `commit.conventional*` service methods.
#[derive(Debug, Clone, Default)]
pub struct ConventionalDraftRequest {
    pub model_provider: Option<String>,
    pub privacy: Option<String>,
    pub allow_remote_patches: bool,
    pub evidence_mode: String,
}

fn sha_matches(candidate: &str, wanted: &str) -> bool {
    let (candidate, wanted) = (candidate.to_lowercase(), wanted.trim().to_lowercase());
    candidate == wanted || candidate.starts_with(&wanted) || wanted.starts_with(&candidate)
}

impl Context {
    fn annotate_selected(&self, commits: &mut [crate::models::Commit]) -> Result<()> {
        if commits.is_empty() {
            return Ok(());
        }
        if let Some(engine) = self.semantic_engine()? {
            let progress = |current: usize, total: usize, sha: &str| {
                trace_progress(
                    "semantic_review",
                    "IntentumDiff semantic review",
                    json!({"detail": sha.chars().take(12).collect::<String>(), "current": current, "total": total, "unit": "commit"}),
                );
            };
            engine.annotate(commits, &progress);
        }
        Ok(())
    }

    /// Provider, provider mode and dispatch privacy for drafts (ports
    /// `MCPService._conventional_draft_context`).
    fn conventional_draft_context(
        &self,
        request: &ConventionalDraftRequest,
    ) -> (Box<dyn crate::providers::Provider>, String, String) {
        let raw = &self.config.raw;
        let allow = request.allow_remote_patches
            || std::env::var("VERLATO_ALLOW_REMOTE_PATCHES").as_deref() == Ok("1");
        let privacy = resolve_model_privacy(
            raw.get("model_privacy").and_then(Value::as_str),
            request.privacy.as_deref(),
            allow,
        );
        let selected = request.model_provider.clone().filter(|p| p != "configured");
        let config = ProviderConfig::from_raw(raw);
        let provider_mode = selected
            .clone()
            .filter(|p| !p.is_empty())
            .or_else(|| {
                std::env::var("VERLATO_MODEL_PROVIDER")
                    .ok()
                    .filter(|p| !p.is_empty())
            })
            .unwrap_or_else(|| config.name.clone());
        let dispatch = if crate::assist::can_send_raw_patches_for_config(
            &config,
            Some(&provider_mode),
            &privacy,
        ) {
            privacy
        } else {
            "metadata-only".to_owned()
        };
        (
            build_provider(&config, selected.as_deref()),
            provider_mode,
            dispatch,
        )
    }

    /// `commit conventional` / `commit.conventional`: a drafted Conventional Commits message
    /// for one commit of the range.
    pub fn draft_conventional_commit(
        &self,
        base: &str,
        head: &str,
        sha: &str,
        request: &ConventionalDraftRequest,
    ) -> Result<Value> {
        let commits = self.git().collect_commits(base, head)?;
        let Some(commit) = commits.into_iter().find(|c| sha_matches(&c.sha, sha)) else {
            return Err(ServiceError {
                code: "KeyError",
                message: format!("'Commit {sha} was not found in range {base}..{head}'"),
            }
            .into());
        };
        let mut selected = vec![commit];
        self.annotate_selected(&mut selected)?;
        let (provider, provider_mode, privacy) = self.conventional_draft_context(request);
        crate::intents::draft_conventional_commit(
            &selected[0],
            provider.as_ref(),
            &privacy,
            &provider_mode,
            &request.evidence_mode,
        )
        .ok_or_else(|| {
            ServiceError {
                code: "RuntimeError",
                message: format!("No conventional commit draft could be generated for {sha}"),
            }
            .into()
        })
    }

    /// `commit.conventional_many`: drafts for several commits (all of the range without `shas`),
    /// with per-commit errors.
    pub fn draft_conventional_commits(
        &self,
        base: &str,
        head: &str,
        shas: Option<&[String]>,
        request: &ConventionalDraftRequest,
    ) -> Result<Value> {
        let commits = self.git().collect_commits(base, head)?;
        let mut errors: Vec<Value> = Vec::new();
        let mut selected = Vec::new();
        match shas.filter(|s| !s.is_empty()) {
            None => selected = commits,
            Some(shas) => {
                for sha in shas {
                    match commits.iter().find(|c| sha_matches(&c.sha, sha)) {
                        Some(commit) => selected.push(commit.clone()),
                        None => errors.push(
                            json!({"sha": sha, "error": format!("Commit {sha} was not found in range")}),
                        ),
                    }
                }
            }
        }
        let lookup_errors = errors.len();
        self.annotate_selected(&mut selected)?;
        let (provider, provider_mode, privacy) = self.conventional_draft_context(request);
        let mut results = Vec::new();
        for commit in &selected {
            match crate::intents::draft_conventional_commit(
                commit,
                provider.as_ref(),
                &privacy,
                &provider_mode,
                &request.evidence_mode,
            ) {
                Some(draft) => results.push(draft),
                None => errors.push(json!({
                    "sha": commit.sha,
                    "error": "No conventional commit draft could be generated",
                })),
            }
        }
        Ok(json!({
            "base_ref": base,
            "head_ref": head,
            "count": selected.len() + lookup_errors,
            "completed": results.len(),
            "failed": errors.len(),
            "results": results,
            "errors": errors,
        }))
    }
}
