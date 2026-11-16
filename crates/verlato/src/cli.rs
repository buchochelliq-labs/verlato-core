//! The `verlato` command line, shared by the native executable and the Python bindings.

use anyhow::Result;
use clap::{Args, Parser, Subcommand};
use serde_json::{json, Map, Value};
use std::io::Write;

use crate::{engine, maintenance, pipeline::Context, service};

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
    /// Start of the range (exclusive): a tag, branch or commit.
    #[arg(long)]
    base: Option<String>,
    /// End of the range (inclusive).
    #[arg(long, default_value = "HEAD")]
    head: String,
    /// Use the latest tag reachable from --head as the base.
    #[arg(long)]
    from_latest_tag: bool,
    /// Use the most recent release recorded with `state mark-published` as the base.
    #[arg(long)]
    from_latest_published: bool,
}

#[derive(Args, Clone)]
struct ModelArgs {
    /// Model provider for this run: stub, mock, ollama, litellm, plugin, github-copilot, codex-cli or claude-code (overrides verlato.json and VERLATO_MODEL_PROVIDER).
    #[arg(long)]
    model_provider: Option<String>,
    /// Infer Conventional Commit type and scope for low-signal commits before grouping.
    #[arg(long, overrides_with = "no_infer_commit_intents")]
    infer_commit_intents: bool,
    /// Turn commit intent inference off for this run.
    #[arg(long)]
    no_infer_commit_intents: bool,
    /// Privacy mode for model evidence: metadata-only, local-summary-then-ai or trusted-provider-patch.
    #[arg(long = "privacy")]
    privacy: Option<String>,
    /// Allow trusted-provider-patch evidence for remote providers.
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
    /// Release heading used in the rendered notes.
    #[arg(long, default_value = "Unreleased")]
    version: String,
    /// Show each item's confidence score in the rendered notes.
    #[arg(long)]
    include_confidence: bool,
    /// Show each item's PR and commit provenance in the rendered notes.
    #[arg(long)]
    include_provenance: bool,
    #[command(flatten)]
    model: ModelArgs,
}

#[derive(Subcommand)]
enum Command {
    /// List the change items in a range that are not in state yet (read-only).
    Scan(ScanArgs),
    /// Detect change items in a range and save the new ones to state.
    Generate(ScanArgs),
    /// List the unpublished change items in a range, with a count (read-only).
    Reconcile(ScanArgs),
    /// Render release notes from the items saved in state.
    Render {
        /// Output format.
        #[arg(long, value_parser = ["github-release", "changelog"])]
        format: String,
        /// Release heading used in the rendered notes.
        #[arg(long, default_value = "Unreleased")]
        version: String,
        /// Show each item's confidence score.
        #[arg(long)]
        include_confidence: bool,
        /// Show each item's PR and commit provenance.
        #[arg(long)]
        include_provenance: bool,
    },
    /// JSON-lines editor service on stdio (used by the VS Code extension).
    Service,
    /// MCP server on stdio.
    Serve,
    /// Build, preview, review, apply and improve release-note drafts.
    #[command(subcommand)]
    Draft(DraftCommand),
    /// Inspect and maintain Verlato's local state.
    #[command(subcommand)]
    State(StateCommand),
    /// Record, export and learn from reviewer feedback.
    #[command(subcommand)]
    Feedback(FeedbackCommand),
    /// Health checks for the semantic engine and model providers.
    #[command(subcommand)]
    Doctor(DoctorCommand),
    /// Draft Conventional Commit messages from commit evidence.
    #[command(subcommand)]
    Commit(CommitCommand),
    /// Export model telemetry debug logs.
    #[command(subcommand)]
    ModelLogs(ModelLogsCommand),
}

#[derive(Subcommand)]
enum ModelLogsCommand {
    /// Normalize a model debug log into a verlato.model_logs.v1 document (redacted by default).
    Export {
        /// JSONL model debug log produced by VERLATO_MODEL_LOG_PATH.
        #[arg(long)]
        file: String,
        /// Output format: one JSON document, or one record per line.
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
        /// Start of the range that contains the commit (exclusive).
        #[arg(long)]
        base: String,
        /// End of the range that contains the commit (inclusive).
        #[arg(long, default_value = "HEAD")]
        head: String,
        /// The commit to draft a message for.
        #[arg(long)]
        sha: String,
        /// Model provider for this run (see `verlato scan --help`).
        #[arg(long)]
        model_provider: Option<String>,
        /// diff-only hides the authored commit message from the model; subject-hint includes it.
        #[arg(long, value_parser = ["diff-only", "subject-hint"], default_value = "diff-only")]
        evidence_mode: String,
        /// Privacy mode for model evidence: metadata-only, local-summary-then-ai or trusted-provider-patch.
        #[arg(long = "privacy")]
        privacy: Option<String>,
        /// Allow trusted-provider-patch evidence for remote providers.
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
    /// The draft for a range as a JSON document (items, sections, rendered Markdown).
    Json(DraftArgs),
    /// The draft for a range rendered as GitHub release notes (Markdown).
    Preview(DraftArgs),
    /// Write the draft into a release-note file (a target from verlato.json, or --file).
    Apply {
        #[command(flatten)]
        draft: DraftArgs,
        /// Target from verlato.json `targets`.
        #[arg(long, value_parser = ["changelog", "github-release"], default_value = "changelog")]
        target: String,
        /// Write to this file instead of the target's configured path.
        #[arg(long)]
        file: Option<String>,
    },
    /// Save a reviewer decision for one draft item: status, edited summary, reviewer.
    Review {
        #[command(flatten)]
        range: RangeArgs,
        /// The draft item ID (`ci_...`).
        #[arg(long)]
        id: String,
        /// New review status. `hidden` leaves the item out of rendered notes.
        #[arg(long, value_parser = ["needs-review", "ready", "accepted", "edited", "hidden"])]
        status: Option<String>,
        /// Replacement summary text (marks the item edited).
        #[arg(long)]
        summary: Option<String>,
        #[arg(long, hide = true)]
        summary_provenance_json: Option<String>,
        /// Who made the decision (recorded with the item).
        #[arg(long)]
        reviewer: Option<String>,
        #[command(flatten)]
        model: ModelArgs,
    },
    /// Model suggestions for draft items.
    Suggest {
        #[command(flatten)]
        range: RangeArgs,
        /// The draft item to suggest a summary for.
        #[arg(long)]
        id: Option<String>,
        /// Suggest for every item that is not hidden (needs --preview).
        #[arg(long)]
        all_visible: bool,
        /// Return suggestions as JSON without changing state. Without it, the suggestion for --id is applied.
        #[arg(long)]
        preview: bool,
        /// Build planned AI request envelopes without calling the provider.
        #[arg(long)]
        preflight: bool,
        /// Suggestions per item (1-5).
        #[arg(long)]
        count: Option<i64>,
        /// Item suggestions to run at once (1-8).
        #[arg(long)]
        parallelism: Option<i64>,
        /// Uncached items that may share one structured model request (1-5).
        #[arg(long)]
        batch_size: Option<i64>,
        /// How items are sent to the model.
        #[arg(long, value_parser = ["auto", "single", "paired", "full-diff-json", "tool-retrieval"])]
        strategy: Option<String>,
        /// Prompt budget for single-item prompts.
        #[arg(long, value_parser = ["conservative", "lean", "minimal"])]
        prompt_profile: Option<String>,
        /// Include recommendation-only tuning advice in the JSON output.
        #[arg(long)]
        adaptive_recommendations: bool,
        /// A different (smaller or faster) model for suggestions only.
        #[arg(long)]
        suggest_model: Option<String>,
        /// Neither read nor write the suggestion cache.
        #[arg(long)]
        no_cache: bool,
        /// Ignore cached suggestions and overwrite them.
        #[arg(long)]
        refresh_cache: bool,
        /// Wording preset.
        #[arg(long, value_parser = ["release-note", "concise", "technical", "customer-facing"])]
        tone: Option<String>,
        /// Who applied the suggestion (recorded with the item).
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
        /// The draft item ID (`ci_...`).
        #[arg(long)]
        id: String,
        /// Wording preset.
        #[arg(long, value_parser = ["release-note", "concise", "technical", "customer-facing"])]
        tone: Option<String>,
        #[command(flatten)]
        model: ModelArgs,
    },
}

#[derive(Subcommand)]
enum StateCommand {
    /// Print the saved state as JSON.
    Show,
    /// Record that items shipped in a release (the base for --from-latest-published).
    MarkPublished {
        /// The release or tag the items shipped in.
        #[arg(long)]
        version: String,
        /// Item IDs to mark.
        #[arg(long, num_args = 1..)]
        ids: Vec<String>,
        /// Mark every unpublished item.
        #[arg(long)]
        all_unpublished: bool,
    },
    /// Remove unpublished items (and their feedback) from state; published history is kept.
    ClearWorking,
    /// Delete the suggestion cache.
    ClearSuggestionCache,
}

#[derive(Subcommand)]
enum FeedbackCommand {
    /// Record one feedback event for a draft item.
    Record {
        /// Event name, e.g. skip, regenerate, useful or not-useful.
        #[arg(long)]
        event: String,
        /// The draft item ID.
        #[arg(long)]
        item_id: String,
        /// Extra metadata as a JSON object.
        #[arg(long)]
        metadata_json: Option<String>,
    },
    /// Export the recorded feedback events.
    Export {
        /// One JSON document, or one event per line.
        #[arg(long, value_parser = ["json", "jsonl"], default_value = "json")]
        format: String,
        /// Optional output path. Defaults to stdout.
        #[arg(long)]
        output: Option<String>,
        /// Filter to one feedback event type. Can be provided more than once.
        #[arg(long)]
        event: Vec<String>,
    },
    /// Prompt and style recommendations from aggregate feedback.
    Recommend,
}

#[derive(Subcommand)]
enum DoctorCommand {
    /// Report the IntentumDiff semantic engine status.
    Engine,
    /// Probe a local Ollama server: load, queue, prompt and generation timing.
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
    /// Probe the configured suggestion model with a tiny structured request.
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

fn json_out(value: &impl serde::Serialize) -> Result<()> {
    println!("{}", serde_json::to_string_pretty(value)?);
    Ok(())
}

impl ModelArgs {
    fn request(&self) -> crate::pipeline::ModelRequest {
        crate::pipeline::ModelRequest {
            provider: self.model_provider.clone(),
            privacy: self.privacy.clone(),
            allow_remote_patches: self.allow_remote_patches,
            infer_commit_intents: self.inference(),
        }
    }
}

/// Run the command line with `args` (without the program name) and return the exit code.
///
/// Never exits the process: usage errors and `--help`/`--version` print as clap does and
/// return its exit code, and command errors print to stderr and return 1. Both stdout and
/// stderr are flushed before returning, so embedding hosts (the Python bindings) see all output.
pub fn run(args: Vec<String>) -> i32 {
    run_with(args, &crate::extensions::Extensions::default())
}

/// [`run`] with `extensions` installed for the duration of the command: extra subcommands,
/// service methods, forge adapters, providers, policy and event sinks (see
/// [`crate::extensions`]). This is the entry point of binaries that embed Verlato.
pub fn run_with(args: Vec<String>, extensions: &crate::extensions::Extensions) -> i32 {
    let _installed = crate::extensions::install(extensions);
    let code = match extension_command(&args, extensions) {
        Some(code) => code,
        None => run_core(args),
    };
    let _ = std::io::stdout().flush();
    let _ = std::io::stderr().flush();
    code
}

/// Run `args` as an extension subcommand when its first word names one core lacks.
fn extension_command(args: &[String], extensions: &crate::extensions::Extensions) -> Option<i32> {
    use clap::CommandFactory;
    let name = args.first()?;
    if Cli::command().find_subcommand(name).is_some() {
        return None;
    }
    let extension = extensions.command_named(name)?;
    let parsed = extension
        .command()
        .bin_name(format!("verlato {name}"))
        .try_get_matches_from(args);
    Some(match parsed {
        Err(error) => {
            let _ = error.print();
            error.exit_code()
        }
        Ok(matches) => {
            let result = std::env::current_dir()
                .map_err(anyhow::Error::from)
                .and_then(|cwd| extension.run(&cwd, &matches));
            match result {
                Ok(code) => code,
                Err(error) => {
                    let _ = std::io::stdout().flush();
                    eprintln!("{error:#}");
                    1
                }
            }
        }
    })
}

fn run_core(args: Vec<String>) -> i32 {
    let code = match Cli::try_parse_from(std::iter::once("verlato".to_owned()).chain(args)) {
        Err(error) => {
            let _ = error.print();
            error.exit_code()
        }
        Ok(cli) => match execute(cli) {
            Ok(code) => code,
            Err(error) => {
                let _ = std::io::stdout().flush();
                eprintln!("{error:#}");
                1
            }
        },
    };
    let _ = std::io::stdout().flush();
    let _ = std::io::stderr().flush();
    code
}

fn execute(cli: Cli) -> Result<i32> {
    if let Some(engine) = &cli.diff_engine {
        std::env::set_var("VERLATO_DIFF_ENGINE", engine);
    }
    let cwd = std::env::current_dir()?;
    let ctx = Context::load(&cwd)?;

    match cli.command {
        command @ (Command::Scan(_) | Command::Generate(_) | Command::Reconcile(_)) => {
            let persist = matches!(command, Command::Generate(_));
            let reconcile = matches!(command, Command::Reconcile(_));
            let (Command::Scan(args) | Command::Generate(args) | Command::Reconcile(args)) =
                command
            else {
                unreachable!()
            };
            let (base, head) = ctx.resolve_range(
                args.range.base.as_deref(),
                &args.range.head,
                args.range.from_latest_tag,
                args.range.from_latest_published,
            )?;
            let items = ctx.scan(&base, &head, persist, &args.model.request())?;
            if reconcile {
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
            let document = crate::drafts::build_document(
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
        Command::Serve => return crate::mcp::run(&cwd),
        Command::Draft(command) => match command {
            DraftCommand::Json(args) => {
                let document = draft(&ctx, &args)?;
                crate::pipeline::trace_progress(
                    "draft_json_serializing",
                    "Serializing draft document",
                    json!({"current": document.item_count, "total": document.item_count, "unit": "item"}),
                );
                json_out(&document)?;
            }
            DraftCommand::Preview(args) => {
                let document = draft(&ctx, &args)?;
                crate::pipeline::trace_progress(
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
                let args = crate::suggest::SuggestArgs {
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
                    options: crate::suggest::SuggestOptions {
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
                    json_out(&crate::suggest::preview_suggestions(
                        &ctx, &base, &head, &args,
                    )?)?;
                } else {
                    if args.all_visible {
                        anyhow::bail!("--all-visible requires --preview");
                    }
                    let Some(item_id) = args.item_id.as_deref() else {
                        anyhow::bail!("Provide --id for draft suggest");
                    };
                    json_out(&crate::suggest::apply_model_suggestion(
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
                let (base, head) = ctx.resolve_range(
                    range.base.as_deref(),
                    &range.head,
                    range.from_latest_tag,
                    range.from_latest_published,
                )?;
                let options = crate::suggest::SuggestOptions {
                    model_provider: model.model_provider.clone(),
                    tone,
                    privacy: model.privacy.clone(),
                    allow_remote_patches: model.allow_remote_patches,
                    infer_commit_intents: model.inference(),
                };
                json_out(&crate::suggest::draft_prompt(
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
                    Some(&crate::pipeline::ModelRequest {
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
            let request = crate::pipeline::ConventionalDraftRequest {
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

fn draft(ctx: &Context, args: &DraftArgs) -> Result<crate::drafts::DraftDocument> {
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
    fn unknown_commands_are_usage_errors() {
        assert_eq!(run(args(&["no-such-command"])), 2);
        assert_eq!(run(args(&["--version"])), 0);
        assert_eq!(run(args(&["draft", "suggest", "--strategy", "bogus"])), 2);
    }
}
