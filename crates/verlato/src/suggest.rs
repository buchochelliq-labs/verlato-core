//! Model suggestions for draft items (ports the `draft prompt` / `draft suggest` flows of
//! `verlato/cli/main.py`).

use anyhow::{bail, Result};
use serde_json::{json, Value};

use crate::assist::{
    build_summary_prompt, can_send_raw_patches_for_config, change_summary_payload,
    model_request_record, privacy_summary, prompt_profile, raw_patch_chars_available,
    resolve_model_privacy, PromptInput, Style, OMITTED_PATCH_EVIDENCE,
};
use crate::classifier::classify;
use crate::evidence;
use crate::models::{changed_files_for_group, ChangeGroup, ChangeItem};
use crate::pipeline::Context;
use crate::providers::ProviderConfig;

/// Options shared by `draft prompt` and `draft suggest`.
#[derive(Debug, Clone, Default)]
pub struct SuggestOptions {
    pub model_provider: Option<String>,
    pub tone: Option<String>,
    pub privacy: Option<String>,
    pub allow_remote_patches: bool,
}

/// `_resolve_suggest_model_provider`: suggestions always use a real model; `stub` (or nothing
/// configured) means the local Ollama default.
pub fn resolve_suggest_model_provider(ctx: &Context, requested: Option<&str>) -> String {
    if let Some(requested) = requested.filter(|p| !p.is_empty()) {
        return requested.to_owned();
    }
    let configured = std::env::var("VERLATO_MODEL_PROVIDER")
        .unwrap_or_else(|_| ProviderConfig::from_raw(&ctx.config.raw).name)
        .trim()
        .to_lowercase();
    if !configured.is_empty() && configured != "stub" {
        configured
    } else {
        "ollama".into()
    }
}

/// `_find_group_for_item_id`: the change group whose deterministic item has `item_id`.
pub fn find_group(
    ctx: &Context,
    base: &str,
    head: &str,
    item_id: &str,
) -> Result<(ChangeGroup, ChangeItem)> {
    for group in ctx.collect_groups(base, head)? {
        let item = classify(&group, base, head, None);
        if item.id == item_id {
            return Ok((group, item));
        }
    }
    bail!("Could not find draft item {item_id} for range {base}..{head}")
}

/// `draft prompt`: the exact prompt, evidence and audit record a suggestion would send.
pub fn draft_prompt(
    ctx: &Context,
    base: &str,
    head: &str,
    item_id: &str,
    options: &SuggestOptions,
) -> Result<Value> {
    let provider = resolve_suggest_model_provider(ctx, options.model_provider.as_deref());
    let raw = &ctx.config.raw;
    let privacy = resolve_model_privacy(
        raw.get("model_privacy").and_then(Value::as_str),
        options.privacy.as_deref(),
        options.allow_remote_patches,
    );
    let provider_config = ProviderConfig::from_raw(raw);
    let raw_patches_allowed =
        can_send_raw_patches_for_config(&provider_config, Some(&provider), &privacy);
    let style = Style::resolve(raw, options.tone.as_deref());
    let (group, item) = find_group(ctx, base, head, item_id)?;
    let files = changed_files_for_group(&group);
    let diff = evidence::build(&files, Some(&group.commits));
    let labels: Vec<String> = group
        .pr
        .as_ref()
        .map(|pr| pr.labels.iter().map(|l| l.to_lowercase()).collect())
        .unwrap_or_default();
    let prompt = build_summary_prompt(&PromptInput {
        title: &item.content.title,
        labels: &labels,
        pr_body: group.pr.as_ref().map(|p| p.body.as_str()).unwrap_or(""),
        diff: &diff,
        commits: &group.commits,
        suggestion_count: 1,
        kind: None,
        audience: None,
        style: &style,
        profile: prompt_profile("suggestion"),
        include_hunks: raw_patches_allowed,
    });
    let raw_chars = raw_patch_chars_available(&files);
    let raw_sent = prompt
        .budget
        .get("raw_patch_chars_sent")
        .and_then(Value::as_u64)
        .unwrap_or(0) as usize;
    let mut budget = prompt.budget.clone();
    budget.insert("privacy_mode".into(), json!(privacy));
    budget.insert("raw_patch_chars_available".into(), json!(raw_chars));
    let evidence = json!({
        "title": item.content.title,
        "item_id": item.id,
        "change_summary": change_summary_payload(&group, Some(&item), &diff, &files),
        "privacy_mode": privacy,
    });
    let request = model_request_record(
        "suggest-release-note",
        Some(&provider),
        &privacy,
        &prompt.prompt,
        &evidence,
        raw_chars,
        raw_sent,
        if raw_patches_allowed {
            &[]
        } else {
            OMITTED_PATCH_EVIDENCE
        },
        if raw_patches_allowed {
            &[]
        } else {
            &["metadata-only mode sends ChangeSummary instead of raw diff text"]
        },
    );
    Ok(json!({
        "item_id": item.id,
        "title": item.content.title,
        "provider": provider,
        "privacy": privacy_summary(&provider, &privacy, raw_chars, raw_sent),
        "tone": style.tone,
        "style_profile": style.profile(),
        "prompt": prompt.prompt,
        "evidence": evidence,
        "ai_request": request,
        "prompt_budget": budget,
    }))
}
