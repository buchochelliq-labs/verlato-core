# Extension points

Verlato can be embedded in another binary that adds behaviour without patching core: extra
forge adapters, model providers, a policy, event sinks, subcommands and service methods. This
is how [Verlato Enterprise](../open-core.md) is built, but the points are public and open to
anyone. They are defined in
[`crates/verlato/src/extensions.rs`](https://github.com/buchochelliq-labs/verlato-core/blob/main/crates/verlato/src/extensions.rs).

## Embedding Verlato

Depend on the `verlato` crate (a git dependency; it is not published to crates.io) and call
`verlato::cli::run_with` instead of `verlato::cli::run`:

```rust
use verlato::extensions::Extensions;

fn main() {
    let extensions = Extensions::default()
        .forge("my-forge", |ctx| Ok(Some(std::sync::Arc::new(MyForge::new(ctx)?))))
        .provider("my-model", |config| Box::new(MyProvider::new(config)))
        .policy(MyPolicy::load())
        .sink(MyAuditLog::new())
        .command(MyCommand)
        .service(MyServiceMethods);
    let args = std::env::args().skip(1).collect();
    std::process::exit(verlato::cli::run_with(args, &extensions));
}
```

`run_with` installs the extensions for the duration of the command, runs it like the normal
CLI, and returns the exit code. Because the binary's commands, output formats and editor
protocol are a superset of core's, the VS Code extension and MCP clients work with it
unchanged (point `verlato.cliPath` at it).

## The points

| Point | Trait | Registered with | Used when |
|---|---|---|---|
| Forge adapter | `ForgeAdapter` (`name`, `map_commits`, `is_remote`) | `Extensions::forge(name, factory)` | `VERLATO_PR_ADAPTER=<name>` maps commits to pull requests |
| Model provider | `verlato::providers::Provider` | `Extensions::provider(name, factory)` | a provider name core does not know is selected |
| Policy | `Policy` (`check_model_call`, `check_forge`, `check_publish`) | `Extensions::policy(policy)` | before every model call, on forge selection, and before `draft apply` / `state mark-published`; a `PolicyDenied` stops the operation with its message |
| Event sink | `EventSink` (`emit`) | `Extensions::sink(sink)` | `review.updated`, `feedback.recorded`, `model.call`, `draft.applied`, `release.published` |
| Subcommand | `CommandExtension` (`command` returns a `clap::Command`, `run`) | `Extensions::command(command)` | `verlato <name> ...` for a name core does not have |
| Service method | `ServiceExtension` (`methods`, `call`) | `Extensions::service(service)` | editor-service and `verlato.call` methods core does not have |

Rules:

- **Built-ins win.** An extension cannot replace a core command, method, provider or forge
  name.
- **No extensions, no change.** With nothing installed every hook is a no-op, and output is
  byte for byte what it was; the golden parity suite checks this.
- **Public API only.** Extensions use `verlato::cli`, `verlato::extensions`,
  `verlato::providers`, `verlato::pipeline`, `verlato::git` and `verlato::models`. If an
  extension needs a hook that does not exist, it is added to core as a general extension
  point.

A complete, compiling example that registers one of each is
[`crates/verlato/tests/extensions.rs`](https://github.com/buchochelliq-labs/verlato-core/blob/main/crates/verlato/tests/extensions.rs).

## Patched crates

The IntentumDiff engine depends on two patched crates (`quote` and `zmij`, from
`intentumdiff-core`). Cargo only applies `[patch.crates-io]` at the workspace root, so an
embedding workspace must repeat the `[patch.crates-io]` section of Verlato's root
`Cargo.toml`, pinned to the same revision.
