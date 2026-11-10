# Open core: verlato-core and Verlato Enterprise

Verlato is open core. This repository, **verlato-core**, is public and licensed under
Apache-2.0. It holds everything one developer needs to turn their history into release notes.
Paid team and organisation features live in the private **verlato-enterprise** repository. They
are built on core's public extension points and shipped as a separate binary.

## The boundary

The rule: anything that is valuable to **an individual developer working on their own
repository** is free. Automation across a team or organisation, write-back to other systems,
coverage of more forges, governance, analytics and multi-user operation are paid.

| verlato-core (public, Apache-2.0) | Verlato Enterprise (private, separate binary) |
|---|---|
| CLI, VS Code extension, MCP server, IntentumDiff semantic evidence | **GitHub App / CI bot:** draft notes on PRs and tags automatically, publish to GitHub Releases, Slack and Jira |
| All model providers: Ollama, LiteLLM, GitHub Models / Copilot, Claude Code and Codex CLIs, the plugin protocol | **Write-back and automation:** PR comments, Releases publishing, runs across many repos |
| Privacy modes, request audit hashes, OTEL model-log export | **Forge breadth:** GitHub Enterprise Server and GitHub App authentication, GitLab, Bitbucket and Jira adapters |
| Read-only commit-to-PR lookup on github.com (`VERLATO_PR_ADAPTER=github`), `standard-git` and `none` | **Org policy and governance:** central config, enforced privacy level, approved providers, house style, required review before publish |
| Local feedback, adaptive tuning and the `doctor` commands for one user's repository | **Team analytics:** cross-repo history, feedback, provider cost and latency (DuckDB/Delta or Postgres) |
| The extension points below | **Multi-user service:** team service with SSO/SAML, RBAC and review workflows |

### Decisions behind the line

- **GitHub Models stays in core.** It is just another model endpoint, like Ollama or LiteLLM.
  Bring-your-own-model is a core value, and charging for model choice would be arbitrary.
- **Read-only PR lookup stays free.** PR titles and labels are the strongest release-note
  signal, and most users are on GitHub. Removing them would hollow out the free product and
  break existing configurations.
  - Core keeps a read-only lookup against the GitHub REST API using the user's own token.
  - Enterprise adds GitHub Enterprise Server support, App authentication, write-back and
    organisation-wide runs.
  - Core's `VERLATO_GITHUB_API_URL` override remains for proxies and tests. Supported GHES
    deployments, with App authentication and certified API coverage, are an enterprise
    feature.
- **Compliance basics stay in core.** Privacy modes, audit hashes and OTEL export protect a
  single developer's data as much as an organisation's. Enterprise *enforces* them centrally
  through policy.
- **No paid code in this repository.** Enterprise never needs a patch to core. If it needs
  a hook, the hook is added here as a general extension point, open to everyone.

## Distribution

- **Core:**
  - the `verlato` binary and per-platform bundles on GitHub Releases;
  - the `verlato` wheel on PyPI;
  - the VS Code extension.

  See [RELEASING.md](RELEASING.md).
- **Enterprise:** a separate `verlato` binary, built in the private repository.
  - It depends on core's crates through Cargo (a git dependency pinned to a core commit).
  - It calls [`verlato::cli::run_with`](../crates/verlato/src/cli.rs) with its extensions
    installed.
  - Its commands, output formats and editor protocol are a superset of core's, so the VS Code
    extension and MCP clients work with either binary.
- **Deployment targets for the enterprise services:**
  - **Self-hosted** in the customer's infrastructure.
  - **Hosted SaaS** run by Buchochelliq Labs.
  - **Air-gapped:** no outbound network calls at all. Only local model providers, local git
    and file-based storage. Licence checks and updates must work offline too.

## Extension points

These are defined in [`crates/verlato/src/extensions.rs`](../crates/verlato/src/extensions.rs).
`tests/extensions.rs` exercises all of them from outside the crate.

| Point | Registered with | Used for |
|---|---|---|
| Forge adapters (`ForgeAdapter`) | `Extensions::forge(name, factory)` | `VERLATO_PR_ADAPTER=<name>`. Core's built-ins are `standard-git`, `none` and `github`. |
| Model providers (`ProviderFactory`) | `Extensions::provider(name, factory)` | provider names and modes core does not know |
| Policy (`Policy`) | `Extensions::policy(policy)` | consulted before every model call, on forge selection, and before `draft apply` / `state mark-published` |
| Event sinks (`EventSink`) | `Extensions::sink(sink)` | `review.updated`, `feedback.recorded`, `model.call`, `draft.applied`, `release.published` |
| CLI subcommands (`CommandExtension`) | `Extensions::command(command)` | `verlato <name> ...` for names core lacks |
| Service methods (`ServiceExtension`) | `Extensions::service(service)` | stdio service and `verlato._native.call` methods core lacks |

Built-ins always win, so an extension cannot shadow a core name. With no extensions installed,
every hook is a no-op and output is byte-identical; the golden parity suite checks this.
Anyone can use these points, so they are not an enterprise privilege.

## Changing the boundary

Moving a feature between core and enterprise is a product decision. Record it in this file, in
the same pull request as the change. Features never move from core to enterprise without a
deprecation note in the changelog.
