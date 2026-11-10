//! Extension points: how a separately built binary (Verlato Enterprise, or anything else)
//! adds behaviour to the open-source runtime without patching it.
//!
//! An embedding binary builds an [`Extensions`] value and runs the command line with
//! [`crate::cli::run_with`]:
//!
//! ```no_run
//! use std::sync::Arc;
//! use verlato::extensions::{Extensions, ForgeAdapter, ForgeContext};
//!
//! # struct MyForge;
//! # impl ForgeAdapter for MyForge {
//! #     fn name(&self) -> &str { "my-forge" }
//! #     fn map_commits(&self, _: &[verlato::models::Commit])
//! #         -> anyhow::Result<std::collections::HashMap<String, verlato::models::PullRequest>> {
//! #         Ok(Default::default())
//! #     }
//! # }
//! let extensions = Extensions::new().forge("my-forge", |_ctx: &ForgeContext| {
//!     Ok(Some(Arc::new(MyForge) as Arc<dyn ForgeAdapter>))
//! });
//! let code = verlato::cli::run_with(std::env::args().skip(1).collect(), &extensions);
//! std::process::exit(code);
//! ```
//!
//! What can be extended:
//!
//! | Point | Trait / hook | Consulted |
//! |---|---|---|
//! | Forge (PR) adapters | [`ForgeAdapter`] via [`Extensions::forge`] | `VERLATO_PR_ADAPTER=<name>` names other than the built-ins |
//! | Model providers | [`ProviderFactory`] via [`Extensions::provider`] | provider names/modes other than the built-ins |
//! | Policy | [`Policy`] via [`Extensions::policy`] | before every model call, forge selection and publish |
//! | Events | [`EventSink`] via [`Extensions::sink`] | reviews, feedback, model calls, draft applies, publishes |
//! | CLI subcommands | [`CommandExtension`] via [`Extensions::command`] | `verlato <name> ...` for names the core CLI lacks |
//! | Service methods | [`ServiceExtension`] via [`Extensions::service`] | stdio service / `call` methods the core lacks |
//!
//! Built-ins always win: an extension cannot shadow a core forge, provider, command or
//! service method name. With no extensions installed (the open-source binary), every hook is a
//! no-op and output is byte-identical to a build without this module.

use anyhow::Result;
use serde_json::{json, Map, Value};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, RwLock};

use crate::models::{Commit, PullRequest};
use crate::providers::{Provider, ProviderConfig};

/// The forge adapters core itself provides (`VERLATO_PR_ADAPTER`): `standard-git` (PR numbers
/// from merge-commit subjects), `none`, and `github` (read-only PR lookup on the GitHub REST
/// API).
pub const BUILTIN_FORGES: &[&str] = &["standard-git", "none", "github"];

/// Maps commits to the pull/merge requests that introduced them.
pub trait ForgeAdapter: Send + Sync {
    /// The adapter's registered name, as used in `VERLATO_PR_ADAPTER`.
    fn name(&self) -> &str;
    /// PRs keyed by commit SHA. Commits without a PR are simply absent.
    fn map_commits(&self, commits: &[Commit]) -> Result<HashMap<String, PullRequest>>;
    /// Whether this adapter makes network calls (air-gapped policies refuse those).
    fn is_remote(&self) -> bool {
        true
    }
}

/// What a forge factory sees when an adapter is selected.
pub struct ForgeContext<'a> {
    pub cwd: &'a Path,
    /// The repository's `verlato.json` (raw), for adapter-specific settings.
    pub config: &'a Value,
}

/// Builds a forge adapter; `Ok(None)` means "not configured here" and falls back to
/// `standard-git`, as the built-in GitHub adapter does without a repository and token.
pub type ForgeFactory =
    dyn Fn(&ForgeContext) -> Result<Option<Arc<dyn ForgeAdapter>>> + Send + Sync;

/// Builds a model provider for a provider name/mode core does not know.
pub type ProviderFactory = dyn Fn(&ProviderConfig) -> Box<dyn Provider> + Send + Sync;

/// A request the policy is asked about before a model call.
pub struct ModelCallInfo<'a> {
    /// The provider name/mode (`ollama`, `litellm`, `mock`, a plugin, an extension provider).
    pub provider: &'a str,
    pub task: &'a str,
    pub model: Option<String>,
    /// The privacy mode recorded in the request metadata, when the caller set one.
    pub privacy_mode: Option<&'a str>,
}

/// What is about to be published.
pub struct PublishInfo<'a> {
    /// `draft.apply` (writing release notes to a target) or `state.mark_published`.
    pub action: &'a str,
    pub version: Option<&'a str>,
    pub target: Option<&'a str>,
    /// `(item id, review status)` of every item being published.
    pub items: Vec<(String, String)>,
}

/// A policy refusal; its message is shown to the user.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PolicyDenied(pub String);

impl std::fmt::Display for PolicyDenied {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Blocked by policy: {}", self.0)
    }
}

impl std::error::Error for PolicyDenied {}

/// Governance hook. Every method defaults to allowing the action.
pub trait Policy: Send + Sync {
    fn check_model_call(&self, _call: &ModelCallInfo) -> Result<(), PolicyDenied> {
        Ok(())
    }
    /// Called with the selected forge adapter's name and whether it makes network calls.
    fn check_forge(&self, _name: &str, _remote: bool) -> Result<(), PolicyDenied> {
        Ok(())
    }
    fn check_publish(&self, _publish: &PublishInfo) -> Result<(), PolicyDenied> {
        Ok(())
    }
}

/// An event core reports to sinks.
#[derive(Debug, Clone, PartialEq)]
pub struct Event {
    /// `review.updated`, `feedback.recorded`, `model.call`, `draft.applied`, `release.published`.
    pub kind: &'static str,
    pub payload: Value,
}

/// Receives events (analytics, audit trails). Sinks must not fail the operation: errors are
/// theirs to handle.
pub trait EventSink: Send + Sync {
    fn emit(&self, event: &Event);
}

/// An extra `verlato <name>` subcommand.
pub trait CommandExtension: Send + Sync {
    /// The subcommand, named as it is invoked (`clap::Command::new("org")`).
    fn command(&self) -> clap::Command;
    /// Run it; `matches` were parsed from the subcommand's own arguments.
    fn run(&self, cwd: &Path, matches: &clap::ArgMatches) -> Result<i32>;
}

/// Extra stdio service (`verlato service`) methods.
pub trait ServiceExtension: Send + Sync {
    fn methods(&self) -> Vec<String>;
    /// Answer `method`; `Err` becomes an error response with code `ExtensionError`.
    fn call(&self, cwd: &Path, method: &str, params: &Map<String, Value>) -> Result<Value>;
}

/// The set of extensions a binary runs with. `Extensions::default()` is the open-source build.
#[derive(Default, Clone)]
pub struct Extensions {
    forges: Vec<(String, Arc<ForgeFactory>)>,
    providers: Vec<(String, Arc<ProviderFactory>)>,
    policy: Option<Arc<dyn Policy>>,
    sinks: Vec<Arc<dyn EventSink>>,
    commands: Vec<Arc<dyn CommandExtension>>,
    services: Vec<Arc<dyn ServiceExtension>>,
}

impl Extensions {
    pub fn new() -> Self {
        Self::default()
    }

    /// Register a forge adapter under `name` (lowercase, as matched against
    /// `VERLATO_PR_ADAPTER`).
    pub fn forge(
        mut self,
        name: &str,
        factory: impl Fn(&ForgeContext) -> Result<Option<Arc<dyn ForgeAdapter>>> + Send + Sync + 'static,
    ) -> Self {
        self.forges.push((name.to_lowercase(), Arc::new(factory)));
        self
    }

    /// Register a model provider under `name` (matched against the provider name/mode).
    pub fn provider(
        mut self,
        name: &str,
        factory: impl Fn(&ProviderConfig) -> Box<dyn Provider> + Send + Sync + 'static,
    ) -> Self {
        self.providers
            .push((name.to_lowercase(), Arc::new(factory)));
        self
    }

    /// Install the policy (one per binary; a later call replaces it).
    pub fn policy(mut self, policy: impl Policy + 'static) -> Self {
        self.policy = Some(Arc::new(policy));
        self
    }

    pub fn sink(mut self, sink: impl EventSink + 'static) -> Self {
        self.sinks.push(Arc::new(sink));
        self
    }

    pub fn command(mut self, command: impl CommandExtension + 'static) -> Self {
        self.commands.push(Arc::new(command));
        self
    }

    pub fn service(mut self, service: impl ServiceExtension + 'static) -> Self {
        self.services.push(Arc::new(service));
        self
    }

    /// Whether anything is installed (the open-source binary has nothing).
    pub fn is_empty(&self) -> bool {
        self.forges.is_empty()
            && self.providers.is_empty()
            && self.policy.is_none()
            && self.sinks.is_empty()
            && self.commands.is_empty()
            && self.services.is_empty()
    }

    /// Forge names this binary accepts: the built-ins, then registered ones.
    pub fn forge_names(&self) -> Vec<String> {
        BUILTIN_FORGES
            .iter()
            .map(|s| (*s).to_owned())
            .chain(self.forges.iter().map(|(name, _)| name.clone()))
            .collect()
    }

    pub(crate) fn forge_factory(&self, name: &str) -> Option<Arc<ForgeFactory>> {
        self.forges
            .iter()
            .find(|(n, _)| n == name)
            .map(|(_, f)| f.clone())
    }

    pub(crate) fn provider_factory(&self, name: &str) -> Option<Arc<ProviderFactory>> {
        self.providers
            .iter()
            .find(|(n, _)| n == name)
            .map(|(_, f)| f.clone())
    }

    pub(crate) fn command_named(&self, name: &str) -> Option<Arc<dyn CommandExtension>> {
        self.commands
            .iter()
            .find(|c| c.command().get_name() == name)
            .cloned()
    }

    pub(crate) fn service_for(&self, method: &str) -> Option<Arc<dyn ServiceExtension>> {
        self.services
            .iter()
            .find(|s| s.methods().iter().any(|m| m == method))
            .cloned()
    }

    pub(crate) fn has_model_hooks(&self) -> bool {
        self.policy.is_some() || !self.sinks.is_empty()
    }
}

static CURRENT: RwLock<Option<Arc<Extensions>>> = RwLock::new(None);

/// The extensions of the running binary (empty unless [`install`] / `cli::run_with` set them).
pub fn current() -> Arc<Extensions> {
    CURRENT
        .read()
        .ok()
        .and_then(|guard| guard.clone())
        .unwrap_or_default()
}

/// Install `extensions` process-wide until the returned guard drops (then the previous set is
/// restored). `cli::run_with` does this for the duration of a command.
pub fn install(extensions: &Extensions) -> InstallGuard {
    let mut guard = CURRENT.write().unwrap_or_else(|e| e.into_inner());
    let previous = guard.replace(Arc::new(extensions.clone()));
    InstallGuard { previous }
}

pub struct InstallGuard {
    previous: Option<Arc<Extensions>>,
}

impl Drop for InstallGuard {
    fn drop(&mut self) {
        let mut guard = CURRENT.write().unwrap_or_else(|e| e.into_inner());
        *guard = self.previous.take();
    }
}

// -- hooks core calls -----------------------------------------------------------

/// Report an event to every installed sink.
pub fn emit(kind: &'static str, payload: Value) {
    let extensions = current();
    if extensions.sinks.is_empty() {
        return;
    }
    let event = Event { kind, payload };
    for sink in &extensions.sinks {
        sink.emit(&event);
    }
}

pub fn check_publish(publish: &PublishInfo) -> Result<(), PolicyDenied> {
    match &current().policy {
        Some(policy) => policy.check_publish(publish),
        None => Ok(()),
    }
}

pub fn check_forge(name: &str, remote: bool) -> Result<(), PolicyDenied> {
    match &current().policy {
        Some(policy) => policy.check_forge(name, remote),
        None => Ok(()),
    }
}

/// Wraps a provider so the policy sees each call and sinks see its outcome. Only used when a
/// policy or sink is installed, so the open-source path is untouched.
pub(crate) struct HookedProvider {
    pub inner: Box<dyn Provider>,
    pub name: String,
    pub extensions: Arc<Extensions>,
}

impl HookedProvider {
    fn call(
        &self,
        request: &crate::providers::ProviderRequest,
        run: impl FnOnce(&dyn Provider) -> crate::providers::ProviderResponse,
    ) -> crate::providers::ProviderResponse {
        let model = self
            .inner
            .model_for_task(&request.task)
            .or_else(|| self.inner.model());
        let info = ModelCallInfo {
            provider: &self.name,
            task: &request.task,
            model: model.clone(),
            privacy_mode: request.metadata.get("privacy_mode").and_then(Value::as_str),
        };
        if let Some(policy) = &self.extensions.policy {
            if let Err(denied) = policy.check_model_call(&info) {
                let mut metadata = Map::new();
                metadata.insert("error".into(), json!(denied.to_string()));
                metadata.insert("policy_denied".into(), json!(true));
                return crate::providers::ProviderResponse::error(
                    String::new(),
                    model.unwrap_or_default(),
                    metadata,
                );
            }
        }
        let started = std::time::Instant::now();
        let response = run(self.inner.as_ref());
        if !self.extensions.sinks.is_empty() {
            let event = Event {
                kind: "model.call",
                payload: json!({
                    "provider": self.name,
                    "task": request.task,
                    "model": response.model,
                    "ok": !response.metadata.contains_key("error"),
                    "duration_ms": started.elapsed().as_millis() as u64,
                }),
            };
            for sink in &self.extensions.sinks {
                sink.emit(&event);
            }
        }
        response
    }
}

impl Provider for HookedProvider {
    fn summarize(
        &self,
        request: &crate::providers::ProviderRequest,
    ) -> crate::providers::ProviderResponse {
        self.call(request, |p| p.summarize(request))
    }
    fn classify(
        &self,
        request: &crate::providers::ProviderRequest,
    ) -> crate::providers::ProviderResponse {
        self.call(request, |p| p.classify(request))
    }
    fn model(&self) -> Option<String> {
        self.inner.model()
    }
    fn suggest_model(&self) -> Option<String> {
        self.inner.suggest_model()
    }
    fn model_for_task(&self, task: &str) -> Option<String> {
        self.inner.model_for_task(task)
    }
    fn class_name(&self) -> &'static str {
        self.inner.class_name()
    }
    fn ollama_attributes(
        &self,
        request: &crate::providers::ProviderRequest,
    ) -> Option<Map<String, Value>> {
        self.inner.ollama_attributes(request)
    }
}

/// Wrap `provider` with policy/sink hooks when any are installed.
pub(crate) fn hook_provider(name: &str, provider: Box<dyn Provider>) -> Box<dyn Provider> {
    let extensions = current();
    if !extensions.has_model_hooks() {
        return provider;
    }
    Box::new(HookedProvider {
        inner: provider,
        name: name.to_owned(),
        extensions,
    })
}

/// A sink that appends events as JSON lines to a file (useful for audit trails and tests).
pub struct JsonlSink {
    path: PathBuf,
}

impl JsonlSink {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        JsonlSink { path: path.into() }
    }
}

impl EventSink for JsonlSink {
    fn emit(&self, event: &Event) {
        use std::io::Write;
        let line = json!({"kind": event.kind, "payload": event.payload}).to_string();
        if let Ok(mut file) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
        {
            let _ = writeln!(file, "{line}");
        }
    }
}
