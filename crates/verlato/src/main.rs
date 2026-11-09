//! `verlato` command line. Native (Rust) commands cover the deterministic release-note
//! pipeline; anything else is delegated to the Python implementation unchanged, so the command
//! surface stays identical during the migration.

use anyhow::Result;
use clap::{Args, Parser, Subcommand};
use serde_json::{json, Map, Value};
use std::process::ExitCode;

use verlato::{delegate, engine, pipeline::Context, service};

/// Command paths implemented natively. Everything else goes to Python.
const NATIVE: &[&[&str]] = &[
    &["scan"],
    &["generate"],
    &["reconcile"],
    &["render"],
    &["service"],
    &["draft", "json"],
    &["draft", "preview"],
    &["draft", "apply"],
    &["draft", "review"],
    &["state", "show"],
    &["state", "mark-published"],
    &["state", "clear-working"],
    &["feedback", "record"],
    &["doctor", "engine"],
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
    #[command(subcommand)]
    Draft(DraftCommand),
    #[command(subcommand)]
    State(StateCommand),
    #[command(subcommand)]
    Feedback(FeedbackCommand),
    #[command(subcommand)]
    Doctor(DoctorCommand),
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
}

#[derive(Subcommand)]
enum DoctorCommand {
    Engine,
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

/// Why a native command must still run in Python, if it must: every model provider is
/// native, but commit intent inference and the GitHub PR adapter are not ported yet.
fn python_reason(ctx: &Context, model: Option<&ModelArgs>) -> Option<String> {
    if let Some(model) = model {
        if ctx.config.commit_intent_inference(model.inference()) != "off" {
            return Some("commit intent inference uses a model provider".into());
        }
    }
    if ctx.pr_adapter().is_err() {
        return Some("the github PR adapter".into());
    }
    None
}

impl ModelArgs {
    fn request(&self) -> verlato::pipeline::ModelRequest {
        verlato::pipeline::ModelRequest {
            provider: self.model_provider.clone(),
            privacy: self.privacy.clone(),
            allow_remote_patches: self.allow_remote_patches,
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
                    // Python passes only the provider to review detection.
                    Some(&verlato::pipeline::ModelRequest {
                        provider: model.model_provider.clone(),
                        ..Default::default()
                    }),
                )?;
                json_out(&updated)?;
            }
        },
        Command::State(command) => match command {
            StateCommand::Show => json_out(&ctx.state_show()?)?,
            StateCommand::ClearWorking => json_out(&ctx.clear_working_state()?)?,
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
        assert!(!is_native(&command_path(&args(&[
            "draft", "suggest", "--base", "v1"
        ]))));
        assert!(!is_native(&command_path(&args(&[
            "commit",
            "conventional"
        ]))));
        assert!(!is_native(&command_path(&args(&["doctor", "ollama"]))));
        assert!(!is_native(&command_path(&args(&["serve"]))));
    }
}
