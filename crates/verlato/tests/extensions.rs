//! An out-of-crate extension, built only from verlato's public API, registering every kind of
//! extension point. This is how Verlato Enterprise plugs into the open-source runtime.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use serde_json::{json, Map, Value};
use verlato::extensions::{
    self, CommandExtension, Event, EventSink, Extensions, ForgeAdapter, ForgeContext,
    ModelCallInfo, Policy, PolicyDenied, PublishInfo, ServiceExtension,
};
use verlato::git::{map_commits_to_prs, PrAdapter};
use verlato::models::{Commit, PullRequest};
use verlato::pipeline::Context;
use verlato::providers::{
    build_provider, Provider, ProviderConfig, ProviderRequest, ProviderResponse,
};

/// The process-wide extension set and env vars are shared, so these tests run one at a time.
static SERIAL: Mutex<()> = Mutex::new(());

struct ForgeOfRecord;

impl ForgeAdapter for ForgeOfRecord {
    fn name(&self) -> &str {
        "forge-of-record"
    }
    fn map_commits(&self, commits: &[Commit]) -> anyhow::Result<HashMap<String, PullRequest>> {
        Ok(commits
            .iter()
            .enumerate()
            .map(|(i, c)| {
                (
                    c.sha.clone(),
                    PullRequest {
                        number: 100 + i as i64,
                        title: format!("PR for {}", c.message),
                        ..PullRequest::default()
                    },
                )
            })
            .collect())
    }
}

struct EchoProvider;

impl Provider for EchoProvider {
    fn summarize(&self, request: &ProviderRequest) -> ProviderResponse {
        ProviderResponse {
            content: format!("echo: {}", request.prompt),
            confidence: 1.0,
            model: "echo-1".into(),
            metadata: Map::new(),
            reasoning: String::new(),
        }
    }
    fn classify(&self, request: &ProviderRequest) -> ProviderResponse {
        self.summarize(request)
    }
    fn model(&self) -> Option<String> {
        Some("echo-1".into())
    }
    fn class_name(&self) -> &'static str {
        "EchoProvider"
    }
}

/// Refuses remote providers, remote forges and publishing unreviewed items.
struct StrictPolicy;

impl Policy for StrictPolicy {
    fn check_model_call(&self, call: &ModelCallInfo) -> Result<(), PolicyDenied> {
        if call.provider == "echo" {
            Ok(())
        } else {
            Err(PolicyDenied(format!(
                "provider {} is not approved",
                call.provider
            )))
        }
    }
    fn check_forge(&self, name: &str, remote: bool) -> Result<(), PolicyDenied> {
        if remote && name == "github" {
            Err(PolicyDenied("no remote forges".into()))
        } else {
            Ok(())
        }
    }
    fn check_publish(&self, publish: &PublishInfo) -> Result<(), PolicyDenied> {
        if publish.version == Some("blocked") {
            return Err(PolicyDenied("release frozen".into()));
        }
        Ok(())
    }
}

#[derive(Clone, Default)]
struct Recorder(Arc<Mutex<Vec<Event>>>);

impl EventSink for Recorder {
    fn emit(&self, event: &Event) {
        self.0.lock().unwrap().push(event.clone());
    }
}

impl Recorder {
    fn kinds(&self) -> Vec<&'static str> {
        self.0.lock().unwrap().iter().map(|e| e.kind).collect()
    }
}

struct Hello(Arc<Mutex<Vec<String>>>);

impl CommandExtension for Hello {
    fn command(&self) -> clap::Command {
        clap::Command::new("hello").arg(clap::Arg::new("name").long("name").required(true))
    }
    fn run(&self, _cwd: &std::path::Path, matches: &clap::ArgMatches) -> anyhow::Result<i32> {
        let name: &String = matches.get_one("name").expect("required");
        self.0.lock().unwrap().push(name.clone());
        Ok(7)
    }
}

struct Ping;

impl ServiceExtension for Ping {
    fn methods(&self) -> Vec<String> {
        vec!["ext.ping".into()]
    }
    fn call(
        &self,
        _cwd: &std::path::Path,
        _method: &str,
        params: &Map<String, Value>,
    ) -> anyhow::Result<Value> {
        Ok(json!({"pong": params.get("n").cloned().unwrap_or(Value::Null)}))
    }
}

fn enterprise(recorder: &Recorder, greeted: &Arc<Mutex<Vec<String>>>) -> Extensions {
    Extensions::new()
        .forge("forge-of-record", |_ctx: &ForgeContext| {
            Ok(Some(Arc::new(ForgeOfRecord) as Arc<dyn ForgeAdapter>))
        })
        .provider("echo", |_config: &ProviderConfig| {
            Box::new(EchoProvider) as Box<dyn Provider>
        })
        .policy(StrictPolicy)
        .sink(recorder.clone())
        .command(Hello(greeted.clone()))
        .service(Ping)
}

fn provider_named(name: &str) -> Box<dyn Provider> {
    build_provider(
        &ProviderConfig {
            name: name.into(),
            ..ProviderConfig::default()
        },
        None,
    )
}

fn request(task: &str) -> ProviderRequest {
    ProviderRequest {
        task: task.into(),
        prompt: "hi".into(),
        ..ProviderRequest::default()
    }
}

#[test]
fn an_extension_registers_every_extension_point() {
    let _serial = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
    std::env::remove_var("VERLATO_MODEL_PROVIDER");
    let dir = tempfile::tempdir().unwrap();
    let recorder = Recorder::default();
    let greeted = Arc::new(Mutex::new(Vec::new()));
    let extensions = enterprise(&recorder, &greeted);
    assert_eq!(
        extensions.forge_names(),
        ["standard-git", "none", "github", "forge-of-record"]
    );

    // Subcommands and service methods run only through run_with / while installed.
    let code = verlato::cli::run_with(
        vec!["hello".into(), "--name".into(), "ada".into()],
        &extensions,
    );
    assert_eq!(code, 7);
    assert_eq!(*greeted.lock().unwrap(), ["ada"]);
    assert_ne!(verlato::cli::run(vec!["hello".into()]), 7);

    let _installed = extensions::install(&extensions);

    // Forge adapter.
    std::env::set_var("VERLATO_PR_ADAPTER", "forge-of-record");
    let ctx = Context::load(dir.path()).unwrap();
    let adapter = ctx.pr_adapter().unwrap();
    assert_eq!(adapter.name(), "forge-of-record");
    let commits = vec![Commit {
        sha: "abc".into(),
        message: "tweak".into(),
        ..Commit::default()
    }];
    let prs = map_commits_to_prs(&adapter, &commits).unwrap();
    assert_eq!(prs["abc"].number, 100);
    assert_eq!(prs["abc"].title, "PR for tweak");

    // The policy sees forge selection: the built-in GitHub adapter is refused when configured.
    std::env::set_var("VERLATO_PR_ADAPTER", "github");
    std::env::set_var("VERLATO_GITHUB_REPO", "o/r");
    std::env::set_var("GITHUB_TOKEN", "t");
    let refused = ctx.pr_adapter().unwrap_err();
    assert!(
        refused.to_string().contains("no remote forges"),
        "{refused}"
    );
    for var in ["VERLATO_PR_ADAPTER", "VERLATO_GITHUB_REPO", "GITHUB_TOKEN"] {
        std::env::remove_var(var);
    }
    assert!(matches!(ctx.pr_adapter().unwrap(), PrAdapter::StandardGit));

    // Provider registry, with the policy consulted and the sink told about each call.
    let echo = provider_named("echo");
    assert_eq!(echo.summarize(&request("summarize")).content, "echo: hi");
    let denied = provider_named("mock").summarize(&request("summarize"));
    assert_eq!(denied.metadata["policy_denied"], json!(true));
    assert!(denied.metadata["error"]
        .as_str()
        .unwrap()
        .contains("provider mock is not approved"));

    // Service method.
    let answer: Value = serde_json::from_str(&verlato::service::call(
        dir.path(),
        "ext.ping",
        r#"{"n": 3}"#,
    ))
    .unwrap();
    assert_eq!(answer["result"], json!({"pong": 3}));

    // Publishing: the policy can refuse; allowed publishes and feedback reach the sink.
    let blocked = ctx.mark_published("blocked", &[], true).unwrap_err();
    assert!(blocked.to_string().contains("release frozen"), "{blocked}");
    ctx.mark_published("1.0.0", &[], true).unwrap();
    ctx.record_feedback("useful", "item-1", json!({})).unwrap();

    assert_eq!(
        recorder.kinds(),
        ["model.call", "release.published", "feedback.recorded"]
    );
    let events = recorder.0.lock().unwrap();
    assert_eq!(events[0].payload["provider"], json!("echo"));
    assert_eq!(events[0].payload["ok"], json!(true));
    assert_eq!(events[1].payload["version"], json!("1.0.0"));
}

#[test]
fn without_extensions_nothing_is_hooked() {
    let _serial = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
    std::env::remove_var("VERLATO_MODEL_PROVIDER");
    assert!(extensions::current().is_empty());
    // Unknown provider names still fall back to the stub, and unknown forges to standard-git.
    let stub = provider_named("echo");
    assert_eq!(stub.class_name(), "StubProvider");
    std::env::set_var("VERLATO_PR_ADAPTER", "forge-of-record");
    let dir = tempfile::tempdir().unwrap();
    let adapter = Context::load(dir.path()).unwrap().pr_adapter().unwrap();
    std::env::remove_var("VERLATO_PR_ADAPTER");
    assert!(matches!(adapter, PrAdapter::StandardGit));
    let unknown: Value =
        serde_json::from_str(&verlato::service::call(dir.path(), "ext.ping", "{}")).unwrap();
    assert_eq!(unknown["error"]["code"], json!("invalid_request"));
}
