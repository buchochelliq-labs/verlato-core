//! `verlato` command line. Native (Rust) commands cover the deterministic release-note
//! pipeline; anything else is delegated to the Python implementation unchanged, so the command
//! surface stays identical during the migration.

use anyhow::Result;
use clap::{Args, Parser, Subcommand};
use serde_json::{json, Map, Value};
use std::process::ExitCode;

use verlato::{delegate, engine, maintenance, pipeline::Context, service};

/// Command paths implemented natively. Everything else goes to Python.
const NATIVE: &[&[&str]] = &[
    &["scan"],
    &["generate"],
    &["reconcile"],
    &["render"],
    &["service"],
    &["serve"],
    &["draft", "json"],
    &["draft", "preview"],
    &["draft", "apply"],
    &["draft", "review"],
    &["draft", "prompt"],
    &["draft", "suggest"],
    &["state", "show"],
    &["state", "mark-published"],
    &["state", "clear-working"],
    &["state", "clear-suggestion-cache"],
    &["feedback"],
    &["doctor"],
    &["model-logs"],
    &["commit", "conventional"],
];

#[derive(Parser)]
#[command(
    name = "verlato",
    version,
    about = "Semantic release notes powered by IntentumDiff"
)]
struct Cli {
    /// Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches).
    #[arg(long, global = true, value_parser = ["intentumdiff", "git"])]
    diff_engine: Option<String>,
    #[command(subcommand)]
    command: Command,
}

#[derive(Args, Clone)]
struct RangeArgs {
    #[arg(long)]
    base: Option<String>,
    #[arg(long, default_value = "HEAD")]
    head: String,
    #[arg(long)]
    from_latest_tag: bool,
    #[arg(long)]
    from_latest_published: bool,
}

#[derive(Args, Clone)]
struct ModelArgs {
    #[arg(long)]
    model_provider: Option<String>,
    #[arg(long, overrides_with = "no_infer_commit_intents")]
    infer_commit_intents: bool,
    #[arg(long)]
    no_infer_commit_intents: bool,
    /// Privacy mode for model evidence: metadata-only, local-summary-then-ai or trusted-provider-patch.
    #[arg(long = "privacy")]
    privacy: Option<String>,
    #[arg(long)]
    allow_remote_patches: bool,
}

impl ModelArgs {
    fn inference(&self) -> Option<bool> {
        if self.infer_commit_intents {
            Some(true)
        } else if self.no_infer_commit_intents {
            Some(false)
        } else {
            None
        }
    }
}

#[derive(Args, Clone)]
struct DraftArgs {
    #[command(flatten)]
    range: RangeArgs,
    #[arg(long, default_value = "Unreleased")]
    version: String,
    #[arg(long)]
    include_confidence: bool,
    #[arg(long)]
    include_provenance: bool,
    #[command(flatten)]
    model: ModelArgs,
}

#[derive(Subcommand)]
enum Command {
    Scan(ScanArgs),
    Generate(ScanArgs),
    Reconcile(ScanArgs),
    Render {
        #[arg(long, value_parser = ["github-release", "changelog"])]
        format: String,
        #[arg(long, default_value = "Unreleased")]
        version: String,
        #[arg(long)]
        include_confidence: bool,
        #[arg(long)]
        include_provenance: bool,
    },
    Service,
    /// MCP server on stdio.
    Serve,
    #[command(subcommand)]
    Draft(DraftCommand),
    #[command(subcommand)]
    State(StateCommand),
    #[command(subcommand)]
    Feedback(FeedbackCommand),
    #[command(subcommand)]
    Doctor(DoctorCommand),
    #[command(subcommand)]
    Commit(CommitCommand),
    #[command(subcommand)]
    ModelLogs(ModelLogsCommand),
}

#[derive(Subcommand)]
enum ModelLogsCommand {
    Export {
        /// JSONL model debug log produced by VERLATO_MODEL_LOG_PATH.
        #[arg(long)]
        file: String,
        #[arg(long, value_parser = ["json", "jsonl"], default_value = "json")]
        format: String,
        /// Optional output path. Defaults to stdout.
        #[arg(long)]
        output: Option<String>,
        /// Filter to one event name. Can be provided more than once.
        #[arg(long)]
        event: Vec<String>,
    },
}

#[derive(Subcommand)]
enum CommitCommand {
    /// Draft a full Conventional Commits message for one existing commit.
    Conventional {
        #[arg(long)]
        base: String,
        #[arg(long, default_value = "HEAD")]
        head: String,
        #[arg(long)]
        sha: String,
        #[arg(long)]
        model_provider: Option<String>,
        #[arg(long, value_parser = ["diff-only", "subject-hint"], default_value = "diff-only")]
        evidence_mode: String,
        #[arg(long = "privacy")]
        privacy: Option<String>,
        #[arg(long)]
        allow_remote_patches: bool,
    },
}

#[derive(Args, Clone)]
struct ScanArgs {
    #[command(flatten)]
    range: RangeArgs,
    #[command(flatten)]
    model: ModelArgs,
}

#[derive(Subcommand)]
enum DraftCommand {
    Json(DraftArgs),
    Preview(DraftArgs),
    Apply {
        #[command(flatten)]
        draft: DraftArgs,
        #[arg(long, value_parser = ["changelog", "github-release"], default_value = "changelog")]
        target: String,
        #[arg(long)]
        file: Option<String>,
    },
    Review {
        #[command(flatten)]
        range: RangeArgs,
        #[arg(long)]
        id: String,
        #[arg(long, value_parser = ["needs-review", "ready", "accepted", "edited", "hidden"])]
        status: Option<String>,
        #[arg(long)]
        summary: Option<String>,
        #[arg(long, hide = true)]
        summary_provenance_json: Option<String>,
        #[arg(long)]
        reviewer: Option<String>,
        #[command(flatten)]
        model: ModelArgs,
    },
    /// Model suggestions for draft items (native for the single strategy with `--preview`).
    Suggest {
        #[command(flatten)]
        range: RangeArgs,
        #[arg(long)]
        id: Option<String>,
        #[arg(long)]
        all_visible: bool,
        #[arg(long)]
        preview: bool,
        /// Build planned AI request envelopes without calling the provider.
        #[arg(long)]
        preflight: bool,
        #[arg(long)]
        count: Option<i64>,
        #[arg(long)]
        parallelism: Option<i64>,
        #[arg(long)]
        batch_size: Option<i64>,
        #[arg(long, value_parser = ["auto", "single", "paired", "full-diff-json", "tool-retrieval"])]
        strategy: Option<String>,
        #[arg(long, value_parser = ["conservative", "lean", "minimal"])]
        prompt_profile: Option<String>,
        #[arg(long)]
        adaptive_recommendations: bool,
        #[arg(long)]
        suggest_model: Option<String>,
        #[arg(long)]
        no_cache: bool,
        #[arg(long)]
        refresh_cache: bool,
        #[arg(long, value_parser = ["release-note", "concise", "technical", "customer-facing"])]
        tone: Option<String>,
        #[arg(long)]
        reviewer: Option<String>,
        /// Emit compact suggestion lifecycle trace logs for this invocation.
        #[arg(long)]
        trace_suggestions: bool,
        #[command(flatten)]
        model: ModelArgs,
    },
    /// The exact prompt, evidence and audit record a suggestion for one item would send.
    Prompt {
        #[command(flatten)]
        range: RangeArgs,
        #[arg(long)]
        id: String,
        #[arg(long, value_parser = ["release-note", "concise", "technical", "customer-facing"])]
        tone: Option<String>,
        #[command(flatten)]
        model: ModelArgs,
    },
}

#[derive(Subcommand)]
enum StateCommand {
    Show,
    MarkPublished {
        #[arg(long)]
        version: String,
        #[arg(long, num_args = 1..)]
        ids: Vec<String>,
        #[arg(long)]
        all_unpublished: bool,
    },
    ClearWorking,
    ClearSuggestionCache,
}

#[derive(Subcommand)]
enum FeedbackCommand {
    Record {
        #[arg(long)]
        event: String,
        #[arg(long)]
        item_id: String,
        #[arg(long)]
        metadata_json: Option<String>,
    },
    Export {
        #[arg(long, value_parser = ["json", "jsonl"], default_value = "json")]
        format: String,
        #[arg(long)]
        output: Option<String>,
        /// Filter to one feedback event type. Can be provided more than once.
        #[arg(long)]
        event: Vec<String>,
    },
    Recommend,
}

#[derive(Subcommand)]
enum DoctorCommand {
    /// Report the IntentumDiff semantic engine status.
    Engine,
    Ollama {
        /// Run two tiny Ollama suggestion probes concurrently to detect server queueing.
        #[arg(long)]
        parallel_probe: bool,
        /// Run a tiny suggestion-style warmup request before measuring Ollama health.
        #[arg(long)]
        warmup: bool,
        /// Include opt-in adaptive suggestion tuning recommendations in the JSON output.
        #[arg(long)]
        adaptive_recommendations: bool,
        /// Use a specific Ollama model for the suggestion-style health probe.
        #[arg(long)]
        suggest_model: Option<String>,
    },
    Model {
        /// Override the configured model provider.
        #[arg(long, value_parser = ["stub", "mock", "ollama", "litellm", "plugin", "github-copilot", "codex-cli", "claude-code"])]
        model_provider: Option<String>,
        /// Use a specific model for the suggestion-style health probe.
        #[arg(long)]
        suggest_model: Option<String>,
        /// Advisory timeout budget used to label slow probes.
        #[arg(long)]
        timeout_seconds: Option<f64>,
    },
}

/// The positional command path of `args` (skipping the global `--diff-engine` option).
fn command_path(args: &[String]) -> Vec<String> {
    let mut path = Vec::new();
    let mut skip_next = false;
    for arg in args {
        if skip_next {
            skip_next = false;
            continue;
        }
        if arg == "--diff-engine" {
            skip_next = true;
            continue;
        }
        if arg.starts_with('-') {
            if path.is_empty() {
                continue;
            }
            break;
        }
        path.push(arg.clone());
        if path.len() == 2 {
            break;
        }
    }
    path
}

fn is_native(path: &[String]) -> bool {
    NATIVE
        .iter()
        .any(|native| native.len() <= path.len() && native.iter().zip(path).all(|(a, b)| a == b))
}

fn json_out(value: &impl serde::Serialize) -> Result<()> {
    println!("{}", serde_json::to_string_pretty(value)?);
    Ok(())
}

/// Why a native command must still run in Python, if it must. Everything the detection commands
/// use (providers, commit intents, PR adapters) is native; this only reports a PR adapter that
/// fails to configure.
fn python_reason(ctx: &Context, _model: Option<&ModelArgs>) -> Option<String> {
    ctx.pr_adapter()
        .err()
        .map(|e| format!("the PR adapter: {e}"))
}

impl ModelArgs {
    fn request(&self) -> verlato::pipeline::ModelRequest {
        verlato::pipeline::ModelRequest {
            provider: self.model_provider.clone(),
            privacy: self.privacy.clone(),
            allow_remote_patches: self.allow_remote_patches,
            infer_commit_intents: self.inference(),
        }
    }
}

fn run(raw_args: Vec<String>) -> Result<i32> {
    let path = command_path(&raw_args);
    if !path.is_empty() && !is_native(&path) {
        return delegate::run(
            &raw_args,
            &format!(
                "`{}` is served by the Python implementation",
                path.join(" ")
            ),
        );
    }
    let cli =
        Cli::parse_from(std::iter::once("verlato".to_owned()).chain(raw_args.iter().cloned()));
    if let Some(engine) = &cli.diff_engine {
        std::env::set_var("VERLATO_DIFF_ENGINE", engine);
    }
    let cwd = std::env::current_dir()?;
    let ctx = Context::load(&cwd)?;
    let delegate_if = |reason: Option<String>| -> Option<Result<i32>> {
        reason.map(|r| delegate::run(&raw_args, &r))
    };

    match cli.command {
        Command::Scan(args) | Command::Generate(args) | Command::Reconcile(args) => {
            if let Some(code) = delegate_if(python_reason(&ctx, Some(&args.model))) {
                return code;
            }
            let (base, head) = ctx.resolve_range(
                args.range.base.as_deref(),
                &args.range.head,
                args.range.from_latest_tag,
                args.range.from_latest_published,
            )?;
            let persist = path[0] == "generate";
            let items = ctx.scan(&base, &head, persist, &args.model.request())?;
            if path[0] == "reconcile" {
                json_out(&json!({"unpublished": items, "count": items.len()}))?;
            } else {
                json_out(&items)?;
            }
        }
        Command::Render {
            format,
            version,
            include_confidence,
            include_provenance,
        } => {
            let items = ctx.store().load()?.items;
            let document = verlato::drafts::build_document(
                &ctx.config,
                &items,
                "state",
                "state",
                &version,
                include_confidence,
                include_provenance,
                &cwd,
            );
            println!("{}", document.rendered[&format]);
        }
        Command::Service => return service::run(&cwd),
        Command::Serve => return verlato::mcp::run(&cwd),
        Command::Draft(command) => match command {
            DraftCommand::Json(args)
            | DraftCommand::Preview(args)
            | DraftCommand::Apply { draft: args, .. }
                if python_reason(&ctx, Some(&args.model)).is_some() =>
            {
                return delegate::run(&raw_args, &python_reason(&ctx, Some(&args.model)).unwrap());
            }
            DraftCommand::Json(args) => {
                let document = draft(&ctx, &args)?;
                verlato::pipeline::trace_progress(
                    "draft_json_serializing",
                    "Serializing draft document",
                    json!({"current": document.item_count, "total": document.item_count, "unit": "item"}),
                );
                json_out(&document)?;
            }
            DraftCommand::Preview(args) => {
                let document = draft(&ctx, &args)?;
                verlato::pipeline::trace_progress(
                    "draft_preview_rendering",
                    "Rendering GitHub release notes",
                    json!({"current": document.item_count, "total": document.item_count, "unit": "item"}),
                );
                println!("{}", document.rendered["github-release"]);
            }
            DraftCommand::Apply {
                draft: args,
                target,
                file,
            } => {
                let document = draft(&ctx, &args)?;
                json_out(&ctx.apply_draft(&document, &target, file.as_deref())?)?;
            }
            DraftCommand::Suggest {
                range,
                id,
                all_visible,
                preview,
                preflight,
                count,
                parallelism,
                batch_size,
                strategy,
                prompt_profile,
                adaptive_recommendations,
                suggest_model,
                no_cache,
                refresh_cache,
                tone,
                reviewer,
                trace_suggestions,
                model,
            } => {
                if let Some(code) = delegate_if(python_reason(&ctx, Some(&model))) {
                    return code;
                }
                let args = verlato::suggest::SuggestArgs {
                    item_id: id,
                    all_visible,
                    count,
                    parallelism,
                    batch_size,
                    strategy,
                    prompt_profile,
                    suggest_model,
                    no_cache,
                    refresh_cache,
                    adaptive_recommendations,
                    preflight,
                    options: verlato::suggest::SuggestOptions {
                        model_provider: model.model_provider.clone(),
                        tone,
                        privacy: model.privacy.clone(),
                        allow_remote_patches: model.allow_remote_patches,
                        infer_commit_intents: model.inference(),
                    },
                };
                if trace_suggestions {
                    std::env::set_var("VERLATO_TRACE_MODEL_CALLS", "1");
                }
                let (base, head) = ctx.resolve_range(
                    range.base.as_deref(),
                    &range.head,
                    range.from_latest_tag,
                    range.from_latest_published,
                )?;
                if preview || args.preflight {
                    json_out(&verlato::suggest::preview_suggestions(
                        &ctx, &base, &head, &args,
                    )?)?;
                } else {
                    if args.all_visible {
                        anyhow::bail!("--all-visible requires --preview");
                    }
                    let Some(item_id) = args.item_id.as_deref() else {
                        anyhow::bail!("Provide --id for draft suggest");
                    };
                    json_out(&verlato::suggest::apply_model_suggestion(
                        &ctx,
                        &base,
                        &head,
                        item_id,
                        reviewer.as_deref(),
                        &args.options,
                    )?)?;
                }
            }
            DraftCommand::Prompt {
                range,
                id,
                tone,
                model,
            } => {
                if let Some(code) = delegate_if(python_reason(&ctx, Some(&model))) {
                    return code;
                }
                let (base, head) = ctx.resolve_range(
                    range.base.as_deref(),
                    &range.head,
                    range.from_latest_tag,
                    range.from_latest_published,
                )?;
                let options = verlato::suggest::SuggestOptions {
                    model_provider: model.model_provider.clone(),
                    tone,
                    privacy: model.privacy.clone(),
                    allow_remote_patches: model.allow_remote_patches,
                    infer_commit_intents: model.inference(),
                };
                json_out(&verlato::suggest::draft_prompt(
                    &ctx, &base, &head, &id, &options,
                )?)?;
            }
            DraftCommand::Review {
                range,
                id,
                status,
                summary,
                summary_provenance_json,
                reviewer,
                model,
            } => {
                if let Some(code) = delegate_if(python_reason(&ctx, Some(&model))) {
                    return code;
                }
                let provenance = match summary_provenance_json {
                    None => None,
                    Some(raw) => {
                        let parsed: Value = serde_json::from_str(&raw)
                            .map_err(|e| anyhow::anyhow!("Invalid summary provenance JSON: {e}"))?;
                        let Value::Object(map) = parsed else {
                            anyhow::bail!("Invalid summary provenance JSON: expected an object")
                        };
                        Some(map)
                    }
                };
                let (base, head) = ctx.resolve_range(
                    range.base.as_deref(),
                    &range.head,
                    range.from_latest_tag,
                    range.from_latest_published,
                )?;
                let updated = ctx.update_review(
                    &base,
                    &head,
                    &id,
                    status.as_deref(),
                    summary.as_deref(),
                    reviewer.as_deref(),
                    provenance,
                    // Python passes only the provider and inference choice to review detection.
                    Some(&verlato::pipeline::ModelRequest {
                        provider: model.model_provider.clone(),
                        infer_commit_intents: model.inference(),
                        ..Default::default()
                    }),
                )?;
                json_out(&updated)?;
            }
        },
        Command::State(command) => match command {
            StateCommand::Show => json_out(&ctx.state_show()?)?,
            StateCommand::ClearWorking => json_out(&ctx.clear_working_state()?)?,
            StateCommand::ClearSuggestionCache => {
                maintenance::print_unsorted(&maintenance::clear_suggestion_cache(&ctx)?)
            }
            StateCommand::MarkPublished {
                version,
                ids,
                all_unpublished,
            } => {
                if !all_unpublished && ids.is_empty() {
                    anyhow::bail!("Provide --all-unpublished or one/more --ids values");
                }
                json_out(&ctx.mark_published(&version, &ids, all_unpublished)?)?;
            }
        },
        Command::Feedback(FeedbackCommand::Record {
            event,
            item_id,
            metadata_json,
        }) => {
            let metadata = match metadata_json {
                None => Value::Object(Map::new()),
                Some(raw) => {
                    let parsed: Value = serde_json::from_str(&raw)
                        .map_err(|e| anyhow::anyhow!("Invalid feedback metadata JSON: {e}"))?;
                    anyhow::ensure!(
                        parsed.is_object(),
                        "Invalid feedback metadata JSON: expected an object"
                    );
                    parsed
                }
            };
            json_out(&ctx.record_feedback(&event, &item_id, metadata)?)?;
        }
        Command::Feedback(FeedbackCommand::Export {
            format,
            output,
            event,
        }) => maintenance::feedback_export(&ctx, &format, output.as_deref(), &event)?,
        Command::Feedback(FeedbackCommand::Recommend) => maintenance::feedback_recommend(&ctx)?,
        Command::ModelLogs(ModelLogsCommand::Export {
            file,
            format,
            output,
            event,
        }) => maintenance::model_logs_export(&file, &format, output.as_deref(), &event)?,
        Command::Doctor(DoctorCommand::Ollama {
            parallel_probe,
            warmup,
            adaptive_recommendations,
            suggest_model,
        }) => {
            maintenance::print_unsorted(&maintenance::doctor_ollama(
                &ctx,
                parallel_probe,
                warmup,
                suggest_model.as_deref(),
                adaptive_recommendations,
            )?);
        }
        Command::Doctor(DoctorCommand::Model {
            model_provider,
            suggest_model,
            timeout_seconds,
        }) => maintenance::print_unsorted(&maintenance::doctor_model(
            &ctx,
            model_provider.as_deref(),
            suggest_model.as_deref(),
            timeout_seconds,
        )?),
        Command::Commit(CommitCommand::Conventional {
            base,
            head,
            sha,
            model_provider,
            evidence_mode,
            privacy,
            allow_remote_patches,
        }) => {
            let request = verlato::pipeline::ConventionalDraftRequest {
                model_provider,
                privacy,
                allow_remote_patches,
                evidence_mode,
            };
            json_out(&ctx.draft_conventional_commit(&base, &head, &sha, &request)?)?;
        }
        Command::Doctor(DoctorCommand::Engine) => {
            json_out(&engine::engine_info(
                ctx.diff_engine(),
                &ctx.config.semantic_cache_path,
            ))?;
        }
    }
    Ok(0)
}

fn draft(ctx: &Context, args: &DraftArgs) -> Result<verlato::drafts::DraftDocument> {
    let (base, head) = ctx.resolve_range(
        args.range.base.as_deref(),
        &args.range.head,
        args.range.from_latest_tag,
        args.range.from_latest_published,
    )?;
    ctx.build_draft(
        &base,
        &head,
        &args.version,
        args.include_confidence,
        args.include_provenance,
        Some(&args.model.request()),
    )
}

fn main() -> ExitCode {
    match run(std::env::args().skip(1).collect()) {
        Ok(code) => ExitCode::from(code.clamp(0, 255) as u8),
        Err(error) => {
            eprintln!("{error:#}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(list: &[&str]) -> Vec<String> {
        list.iter().map(|s| (*s).to_owned()).collect()
    }

    #[test]
    fn accepts_python_compatible_privacy_flags() {
        let parsed = Cli::try_parse_from([
            "verlato",
            "draft",
            "json",
            "--base",
            "v1",
            "--privacy",
            "local-summary-then-ai",
            "--allow-remote-patches",
        ]);
        assert!(parsed.is_ok(), "{:?}", parsed.err());
    }

    #[test]
    fn routes_native_and_delegated_commands() {
        assert_eq!(
            command_path(&args(&[
                "--diff-engine",
                "git",
                "draft",
                "json",
                "--base",
                "v1"
            ])),
            args(&["draft", "json"])
        );
        assert!(is_native(&command_path(&args(&[
            "draft", "json", "--base", "v1"
        ]))));
        assert!(is_native(&command_path(&args(&["scan", "--base", "v1"]))));
        // `draft suggest` is parsed natively; unported strategies/flags delegate at run time.
        assert!(is_native(&command_path(&args(&[
            "draft", "suggest", "--base", "v1"
        ]))));
        assert!(is_native(&command_path(&args(&[
            "state",
            "clear-suggestion-cache"
        ]))));
        assert!(is_native(&command_path(&args(&["commit", "conventional"]))));
        assert!(is_native(&command_path(&args(&["doctor", "ollama"]))));
        assert!(is_native(&command_path(&args(&["model-logs", "export"]))));
        assert!(is_native(&command_path(&args(&["feedback", "recommend"]))));
        assert!(is_native(&command_path(&args(&["serve"]))));
    }
}
