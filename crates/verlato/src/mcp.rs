//! Native MCP server for `verlato serve` (ports `verlato/mcp/server.py` and
//! `verlato/mcp/service.py`). Speaks newline-delimited JSON-RPC 2.0 on stdio and answers like
//! the FastMCP server it replaces: the same handshake, tool/resource/prompt catalog (embedded
//! from FastMCP's own output in `mcp_catalog.json`), argument validation messages, result
//! shapes and error codes.

use anyhow::Result;
use serde_json::{json, Map, Value};
use std::io::{BufRead, Write};
use std::path::Path;
use std::sync::OnceLock;

use crate::pipeline::{Context, ConventionalDraftRequest, ModelRequest, ServiceError};
use crate::pyjson;

const VERSION: &str = env!("CARGO_PKG_VERSION");
const CATALOG_JSON: &str = include_str!("mcp_catalog.json");
/// Protocol versions of the initialize handshake (mcp's `HANDSHAKE_PROTOCOL_VERSIONS`).
const PROTOCOL_VERSIONS: &[&str] = &["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"];
const LATEST_PROTOCOL_VERSION: &str = "2025-11-25";
/// Raw-patch tools exist only for the legacy `git` diff engine, which has no semantic diff.
const LEGACY_PATCH_TOOLS: &[&str] = &["verlato_get_file_patch", "verlato_search_item_diff"];
/// Provider label for MCP clients: an external agent, never a local model.
const MCP_AGENT_PROVIDER: &str = "mcp-agent";
const PYDANTIC_DOCS: &str = "https://errors.pydantic.dev/2.14/v";

const RESOURCE_URIS: &[&str] = &[
    "verlato://overview",
    "verlato://guidance/{name}",
    "verlato://sample-config",
];
const PROMPT_NAMES: &[&str] = &["verlato_review_request"];
const GUIDANCE_NAMES: &[&str] = &["readme", "agent", "architecture", "copilot", "skill"];
const SAMPLE_CONFIG: &str = include_str!("../../../verlato.sample.json");

fn guidance_document(name: &str) -> Option<&'static str> {
    Some(match name {
        "readme" => include_str!("../../../README.md"),
        "agent" => include_str!("../../../AGENT.md"),
        "architecture" => include_str!("../../../docs/technical/architecture.md"),
        "copilot" => include_str!("../../../.github/copilot-instructions.md"),
        "skill" => include_str!("../../../.codex/skills/verlato/SKILL.md"),
        _ => return None,
    })
}

fn catalog() -> &'static Value {
    static CATALOG: OnceLock<Value> = OnceLock::new();
    CATALOG.get_or_init(|| {
        serde_json::from_str(&CATALOG_JSON.replace("@VERSION@", VERSION))
            .expect("embedded MCP catalog is valid JSON")
    })
}

struct RpcError {
    code: i64,
    message: String,
    data: Option<Value>,
}

impl RpcError {
    fn invalid_params() -> RpcError {
        RpcError {
            code: -32602,
            message: "Invalid request parameters".into(),
            data: Some(json!("")),
        }
    }
}

struct Server {
    cwd: std::path::PathBuf,
    legacy_patch_tools: bool,
}

pub fn run(cwd: &Path) -> Result<i32> {
    let legacy_patch_tools = Context::load(cwd)?.diff_engine() == "git";
    let server = Server {
        cwd: cwd.to_path_buf(),
        legacy_patch_tools,
    };
    let stdin = std::io::stdin();
    let mut stdout = std::io::stdout();
    for line in stdin.lock().lines() {
        let line = line?;
        if let Some(response) = server.handle_line(&line) {
            writeln!(stdout, "{response}")?;
            stdout.flush()?;
        }
    }
    Ok(0)
}

impl Server {
    /// One JSON-RPC message; `None` for notifications, responses and messages the MCP SDK drops
    /// (invalid JSON, batches, a missing `jsonrpc`, ids that are neither strings nor integers).
    fn handle_line(&self, line: &str) -> Option<String> {
        let message: Value = serde_json::from_str(line.trim()).ok()?;
        let object = message.as_object()?;
        if object.get("jsonrpc").and_then(Value::as_str) != Some("2.0") {
            return None;
        }
        let method = object.get("method")?.as_str()?;
        let id = object.get("id")?;
        if !(id.is_string() || id.is_i64() || id.is_u64()) {
            return None;
        }
        let params = match object.get("params") {
            None | Some(Value::Null) => Map::new(),
            Some(Value::Object(map)) => map.clone(),
            Some(_) => {
                return Some(error_response(id, RpcError::invalid_params()));
            }
        };
        Some(match self.handle_request(method, &params) {
            Ok(result) => json!({"jsonrpc": "2.0", "id": id, "result": result}).to_string(),
            Err(error) => error_response(id, error),
        })
    }

    fn handle_request(&self, method: &str, params: &Map<String, Value>) -> Result<Value, RpcError> {
        let catalog = catalog();
        match method {
            "initialize" => initialize(params),
            "ping" | "logging/setLevel" => Ok(json!({})),
            "tools/list" => Ok(json!({"tools": self.tools()})),
            "resources/list" => Ok(json!({"resources": catalog["resources"]})),
            "resources/templates/list" => {
                Ok(json!({"resourceTemplates": catalog["resourceTemplates"]}))
            }
            "prompts/list" => Ok(json!({"prompts": catalog["prompts"]})),
            "tools/call" => {
                let name = params
                    .get("name")
                    .and_then(Value::as_str)
                    .ok_or_else(RpcError::invalid_params)?;
                let arguments = match params.get("arguments") {
                    None | Some(Value::Null) => Map::new(),
                    Some(Value::Object(map)) => map.clone(),
                    Some(_) => return Err(RpcError::invalid_params()),
                };
                Ok(self.call_tool(name, &arguments))
            }
            "resources/read" => {
                let uri = params
                    .get("uri")
                    .and_then(Value::as_str)
                    .ok_or_else(RpcError::invalid_params)?;
                self.read_resource(uri)
            }
            "prompts/get" => {
                let name = params
                    .get("name")
                    .and_then(Value::as_str)
                    .ok_or_else(RpcError::invalid_params)?;
                let arguments = match params.get("arguments") {
                    None | Some(Value::Null) => Map::new(),
                    Some(Value::Object(map)) => map.clone(),
                    Some(_) => return Err(RpcError::invalid_params()),
                };
                get_prompt(name, &arguments)
            }
            other => Err(RpcError {
                code: -32601,
                message: "Method not found".into(),
                data: Some(json!(other)),
            }),
        }
    }

    fn tool_enabled(&self, name: &str) -> bool {
        self.legacy_patch_tools || !LEGACY_PATCH_TOOLS.contains(&name)
    }

    fn tools(&self) -> Vec<Value> {
        catalog()["tools"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|tool| self.tool_enabled(tool["name"].as_str().unwrap_or("")))
            .cloned()
            .collect()
    }

    fn tool_names(&self) -> Vec<String> {
        self.tools()
            .iter()
            .filter_map(|t| t["name"].as_str().map(str::to_owned))
            .collect()
    }

    fn call_tool(&self, name: &str, arguments: &Map<String, Value>) -> Value {
        let Some(tool) = self.tools().into_iter().find(|t| t["name"] == name) else {
            return tool_error(format!("Unknown tool: {}", py_repr_str(name)));
        };
        let args = match validate_arguments(name, &tool["inputSchema"], arguments) {
            Ok(args) => args,
            Err(message) => return tool_error(message),
        };
        match self.run_tool(name, &args) {
            Ok(value) => {
                let wrap = tool["outputSchema"]["x-fastmcp-wrap-result"] == json!(true);
                let text = match &value {
                    Value::String(text) => text.clone(),
                    other => pyjson::dumps(other, PYDANTIC_JSON),
                };
                if wrap {
                    json!({
                        "_meta": {"fastmcp": {"wrap_result": true}},
                        "content": [{"type": "text", "text": text}],
                        "isError": false,
                        "structuredContent": {"result": value},
                    })
                } else {
                    json!({
                        "content": [{"type": "text", "text": text}],
                        "isError": false,
                        "structuredContent": value,
                    })
                }
            }
            Err(error) => tool_error(format!(
                "Error calling tool {}: {}",
                py_repr_str(name),
                error_text(&error)
            )),
        }
    }

    fn run_tool(&self, name: &str, args: &Args) -> Result<Value> {
        let ctx = Context::load(&self.cwd)?;
        let base = || args.text("base");
        let head = || args.text("head");
        match name {
            "verlato_service_health" => Ok(json!({"status": "ok", "version": VERSION})),
            "verlato_service_info" => {
                let state = ctx.store().load()?;
                let mut engine =
                    crate::engine::engine_info(ctx.diff_engine(), &ctx.config.semantic_cache_path);
                // `configured` is the configuration's engine, before VERLATO_DIFF_ENGINE.
                engine["configured"] = json!(ctx.config.diff_engine);
                Ok(json!({
                    "status": "ok",
                    "version": VERSION,
                    "diff_engine": engine,
                    "state_path": ctx.config.state_path,
                    "schema_version": state.schema_version,
                    "tracked_items": state.items.len(),
                    "targets": ctx.config.targets.keys().collect::<Vec<_>>(),
                    "mcp_tools": self.tool_names(),
                    "mcp_resources": RESOURCE_URIS,
                    "mcp_prompts": PROMPT_NAMES,
                }))
            }
            "verlato_scan_unpublished_changes" | "verlato_reconcile_preview" => {
                let items = ctx.scan(&base(), &head(), false, &ModelRequest::default())?;
                if name == "verlato_reconcile_preview" {
                    return Ok(json!({"count": items.len(), "unpublished": items}));
                }
                Ok(serde_json::to_value(items)?)
            }
            "verlato_get_draft_document"
            | "verlato_preview_release_notes"
            | "verlato_apply_draft" => {
                let model = ModelRequest {
                    provider: args.optional("model_provider"),
                    ..Default::default()
                };
                let document = ctx.build_draft(
                    &base(),
                    &head(),
                    &args.text("version"),
                    args.flag("include_confidence"),
                    args.flag("include_provenance"),
                    Some(&model),
                )?;
                match name {
                    "verlato_get_draft_document" => Ok(serde_json::to_value(&document)?),
                    "verlato_preview_release_notes" => {
                        Ok(json!(document.rendered["github-release"]))
                    }
                    _ => {
                        let target = args.text("target");
                        let file = args.optional("file");
                        Ok(serde_json::to_value(ctx.apply_draft(
                            &document,
                            &target,
                            file.as_deref(),
                        )?)?)
                    }
                }
            }
            "verlato_update_review_state" | "verlato_request_model_input" => {
                let item_id = args.text("item_id");
                if name == "verlato_update_review_state" {
                    ctx.update_review(
                        &base(),
                        &head(),
                        &item_id,
                        args.optional("status").as_deref(),
                        args.optional("summary").as_deref(),
                        args.optional("reviewer").as_deref(),
                        None,
                        None,
                    )?;
                } else {
                    let options = crate::suggest::SuggestOptions {
                        model_provider: args.optional("model_provider"),
                        ..Default::default()
                    };
                    crate::suggest::apply_model_suggestion(
                        &ctx,
                        &base(),
                        &head(),
                        &item_id,
                        args.optional("reviewer").as_deref(),
                        &options,
                    )?;
                }
                let document =
                    ctx.build_draft(&base(), &head(), "Unreleased", false, false, None)?;
                let document = serde_json::to_value(&document)?;
                document["sections"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .flat_map(|section| section["items"].as_array().into_iter().flatten())
                    .find(|item| item["id"] == json!(item_id))
                    .cloned()
                    .ok_or_else(|| {
                        ServiceError {
                            code: "KeyError",
                            message: py_repr_str(&format!(
                                "Updated review item {item_id} not found in draft document"
                            )),
                        }
                        .into()
                    })
            }
            "verlato_list_suggestion_items"
            | "verlato_get_suggestion_item"
            | "verlato_get_file_semantic_diff"
            | "verlato_search_semantic_diff"
            | "verlato_get_file_patch"
            | "verlato_search_item_diff" => {
                let tools = suggestion_tools(&ctx, &base(), &head())?;
                let item_id = || args.text("item_id");
                match name {
                    "verlato_list_suggestion_items" => {
                        Ok(tools.list_suggestion_items(args.flag("include_files")))
                    }
                    "verlato_get_suggestion_item" => tools.get_suggestion_item(&item_id()),
                    "verlato_get_file_semantic_diff" => {
                        tools.get_file_semantic_diff(&item_id(), &args.text("path"))
                    }
                    "verlato_search_semantic_diff" => tools.search_semantic_diff(
                        &item_id(),
                        &args.text("query"),
                        args.int("limit"),
                    ),
                    "verlato_get_file_patch" => tools.get_file_patch(
                        &item_id(),
                        &args.text("path"),
                        args.int("offset"),
                        args.int("limit"),
                    ),
                    _ => tools.search_item_diff(&item_id(), &args.text("query"), args.int("limit")),
                }
            }
            "verlato_draft_conventional_commit" | "verlato_draft_conventional_commits" => {
                let request = ConventionalDraftRequest {
                    model_provider: args.optional("model_provider"),
                    privacy: args.optional("model_privacy"),
                    allow_remote_patches: args.flag("allow_remote_patches"),
                    evidence_mode: args.text("evidence_mode"),
                };
                if name == "verlato_draft_conventional_commit" {
                    return ctx.draft_conventional_commit(
                        &base(),
                        &head(),
                        &args.text("sha"),
                        &request,
                    );
                }
                let shas: Option<Vec<String>> =
                    args.0.get("shas").and_then(Value::as_array).map(|items| {
                        items
                            .iter()
                            .filter_map(|v| v.as_str().map(str::to_owned))
                            .collect()
                    });
                ctx.draft_conventional_commits(&base(), &head(), shas.as_deref(), &request)
            }
            other => anyhow::bail!("Unknown tool: {other}"),
        }
    }

    fn read_resource(&self, uri: &str) -> Result<Value, RpcError> {
        let content = |mime: &str, text: String| {
            Ok(json!({"contents": [{"uri": uri, "mimeType": mime, "text": text}]}))
        };
        match uri {
            "verlato://overview" => content("application/json", self.overview()),
            "verlato://sample-config" => content("application/json", SAMPLE_CONFIG.to_owned()),
            _ => {
                if let Some(name) = uri
                    .strip_prefix("verlato://guidance/")
                    .filter(|n| !n.is_empty() && !n.contains('/'))
                {
                    return match guidance_document(name) {
                        Some(text) => content("text/markdown", text.to_owned()),
                        None => Err(RpcError {
                            code: -32603,
                            message: format!(
                                "Error reading resource {}: {}",
                                py_repr_str(uri),
                                validation_message(
                                    "_register_server_components.<locals>.verlato_guidance",
                                    &[ValidationIssue {
                                        loc: "name".into(),
                                        message: literal_message(GUIDANCE_NAMES),
                                        kind: "literal_error",
                                        input: json!(name),
                                    }],
                                )
                            ),
                            data: None,
                        }),
                    };
                }
                Err(RpcError {
                    code: -32602,
                    message: format!("Resource not found: {}", py_repr_str(uri)),
                    data: Some(json!({"uri": uri})),
                })
            }
        }
    }

    /// `json.dumps(_overview_payload(), indent=2)`, keeping Python's key order.
    fn overview(&self) -> String {
        let change_evidence = if self.legacy_patch_tools {
            "git-patch"
        } else {
            "intentumdiff-semantic-diff"
        };
        let fields: Vec<(&str, Value)> = vec![
            ("name", json!("verlato")),
            ("version", json!(VERSION)),
            ("instructions", catalog()["instructions"].clone()),
            ("tools", json!(self.tool_names())),
            ("change_evidence", json!(change_evidence)),
            ("resources", json!(RESOURCE_URIS)),
            ("prompts", json!(PROMPT_NAMES)),
        ];
        let body: Vec<String> = fields
            .iter()
            .map(|(key, value)| {
                format!(
                    "  {}: {}",
                    pyjson::dumps(&json!(key), pyjson::DEFAULT),
                    pyjson::dumps_indented(value, 2).replace('\n', "\n  ")
                )
            })
            .collect();
        format!("{{\n{}\n}}", body.join(",\n"))
    }
}

/// pydantic's `to_json`: compact separators, non-ASCII kept, insertion order.
const PYDANTIC_JSON: pyjson::Options = pyjson::Options {
    sort_keys: false,
    ensure_ascii: false,
    compact: true,
};

fn initialize(params: &Map<String, Value>) -> Result<Value, RpcError> {
    let version = params.get("protocolVersion").and_then(Value::as_str);
    let client = params.get("clientInfo").and_then(Value::as_object);
    let valid_client = client.is_some_and(|c| {
        c.get("name").is_some_and(Value::is_string)
            && c.get("version").is_some_and(Value::is_string)
    });
    if version.is_none()
        || !params.get("capabilities").is_some_and(Value::is_object)
        || !valid_client
    {
        return Err(RpcError::invalid_params());
    }
    let version = version.unwrap();
    let negotiated = if PROTOCOL_VERSIONS.contains(&version) {
        version
    } else {
        LATEST_PROTOCOL_VERSION
    };
    let catalog = catalog();
    Ok(json!({
        "protocolVersion": negotiated,
        "capabilities": catalog["capabilities"],
        "serverInfo": catalog["serverInfo"],
        "instructions": catalog["instructions"],
    }))
}

fn get_prompt(name: &str, arguments: &Map<String, Value>) -> Result<Value, RpcError> {
    if name != "verlato_review_request" {
        return Err(RpcError {
            code: -32602,
            message: format!("Unknown prompt: {}", py_repr_str(name)),
            data: None,
        });
    }
    let Some(base) = arguments.get("base") else {
        return Err(RpcError {
            code: -32602,
            message: "Missing required arguments: {'base'}".into(),
            data: None,
        });
    };
    let text_of = |value: Option<&Value>, default: &str| match value {
        None => default.to_owned(),
        Some(Value::String(text)) => text.clone(),
        Some(other) => other.to_string(),
    };
    let text = format!(
        "Use Verlato to review changes and generate release notes.\n\
         - Base ref: {}\n\
         - Head ref: {}\n\
         - Release version: {}\n\
         Recommended flow:\n\
         1. Call verlato_scan_unpublished_changes to inspect the candidate changes.\n\
         2. Call verlato_get_draft_document or verlato_preview_release_notes to build the draft.\n\
         3. To understand or rewrite an item, call verlato_get_suggestion_item and \
         verlato_get_file_semantic_diff. Base summaries on the IntentumDiff semantic diff: describe meaningful \
         changes and never present moved code, refactorings or formatting as new behavior.\n\
         4. If needed, call verlato_update_review_state to refine item status or summary.\n\
         5. Call verlato_apply_draft only when you are ready to write the chosen target file.",
        text_of(Some(base), ""),
        text_of(arguments.get("head"), "HEAD"),
        text_of(arguments.get("version"), "Unreleased"),
    );
    let description = catalog()["prompts"][0]["description"].clone();
    Ok(json!({
        "description": description,
        "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
    }))
}

/// `MCPService._suggestion_tool_provider`: groups classified deterministically (no review state
/// merge). Agents are remote models, so only `trusted-provider-patch` unlocks semantic detail.
pub(crate) fn suggestion_tools(
    ctx: &Context,
    base: &str,
    head: &str,
) -> Result<crate::suggestion_tools::SuggestionTools> {
    let groups = ctx.collect_groups(base, head, &ModelRequest::default())?;
    let items = groups
        .iter()
        .map(|group| crate::classifier::classify(group, base, head, None))
        .collect();
    let privacy = crate::assist::resolve_model_privacy(
        ctx.config.raw.get("model_privacy").and_then(Value::as_str),
        None,
        false,
    );
    let detail = crate::assist::can_send_raw_patches(MCP_AGENT_PROVIDER, &privacy);
    Ok(crate::suggestion_tools::SuggestionTools::from_groups(
        groups, items, detail,
    ))
}

fn error_text(error: &anyhow::Error) -> String {
    match error.downcast_ref::<ServiceError>() {
        Some(typed) => typed.message.clone(),
        None => error.to_string(),
    }
}

fn tool_error(text: String) -> Value {
    json!({"content": [{"type": "text", "text": text}], "isError": true})
}

fn error_response(id: &Value, error: RpcError) -> String {
    let mut body = json!({"code": error.code, "message": error.message});
    if let Some(data) = error.data {
        body["data"] = data;
    }
    json!({"jsonrpc": "2.0", "id": id, "error": body}).to_string()
}

// -- argument validation (pydantic's lax mode, as FastMCP applies it) ----------------------------

/// Validated tool arguments, with schema defaults filled in.
struct Args(Map<String, Value>);

impl Args {
    fn text(&self, key: &str) -> String {
        self.0
            .get(key)
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned()
    }

    fn optional(&self, key: &str) -> Option<String> {
        self.0.get(key).and_then(Value::as_str).map(str::to_owned)
    }

    fn flag(&self, key: &str) -> bool {
        self.0.get(key).and_then(Value::as_bool).unwrap_or(false)
    }

    fn int(&self, key: &str) -> i64 {
        self.0.get(key).and_then(Value::as_i64).unwrap_or(0)
    }
}

struct ValidationIssue {
    loc: String,
    message: String,
    kind: &'static str,
    input: Value,
}

/// Each tool's parameter names in signature order (serde_json maps are sorted; pydantic reports
/// errors in signature order).
fn parameter_order(tool: &str) -> Vec<String> {
    #[derive(serde::Deserialize)]
    struct Schema {
        #[serde(default)]
        properties: indexmap::IndexMap<String, serde::de::IgnoredAny>,
    }
    #[derive(serde::Deserialize)]
    struct Tool {
        name: String,
        #[serde(rename = "inputSchema")]
        input_schema: Schema,
    }
    #[derive(serde::Deserialize)]
    struct Catalog {
        tools: Vec<Tool>,
    }
    static ORDER: OnceLock<Vec<(String, Vec<String>)>> = OnceLock::new();
    ORDER
        .get_or_init(|| {
            let parsed: Catalog =
                serde_json::from_str(CATALOG_JSON).expect("embedded MCP catalog is valid JSON");
            parsed
                .tools
                .into_iter()
                .map(|t| (t.name, t.input_schema.properties.into_keys().collect()))
                .collect()
        })
        .iter()
        .find(|(name, _)| name == tool)
        .map(|(_, order)| order.clone())
        .unwrap_or_default()
}

fn validate_arguments(
    tool: &str,
    schema: &Value,
    arguments: &Map<String, Value>,
) -> Result<Args, String> {
    let empty = Map::new();
    let properties = schema["properties"].as_object().unwrap_or(&empty);
    let order = parameter_order(tool);
    let properties: Vec<(&String, &Value)> = order
        .iter()
        .filter_map(|name| properties.get_key_value(name))
        .collect();
    let required: Vec<&str> = schema["required"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .collect();
    let mut issues = Vec::new();
    let mut args = Map::new();
    for &(name, property) in &properties {
        match arguments.get(name) {
            None => {
                if required.contains(&name.as_str()) {
                    issues.push(ValidationIssue {
                        loc: name.clone(),
                        message: "Missing required argument".into(),
                        kind: "missing_argument",
                        input: Value::Object(arguments.clone()),
                    });
                } else {
                    args.insert(
                        name.clone(),
                        property.get("default").cloned().unwrap_or(Value::Null),
                    );
                }
            }
            Some(value) => match coerce(property, value, name) {
                Ok(value) => {
                    args.insert(name.clone(), value);
                }
                Err(mut found) => issues.append(&mut found),
            },
        }
    }
    for (name, value) in arguments {
        if !properties.iter().any(|(known, _)| *known == name) {
            issues.push(ValidationIssue {
                loc: name.clone(),
                message: "Unexpected keyword argument".into(),
                kind: "unexpected_keyword_argument",
                input: value.clone(),
            });
        }
    }
    if issues.is_empty() {
        Ok(Args(args))
    } else {
        Err(validation_message(&format!("call[{tool}]"), &issues))
    }
}

fn issue(loc: &str, kind: &'static str, message: &str, input: &Value) -> Vec<ValidationIssue> {
    vec![ValidationIssue {
        loc: loc.to_owned(),
        message: message.to_owned(),
        kind,
        input: input.clone(),
    }]
}

fn coerce(schema: &Value, value: &Value, loc: &str) -> Result<Value, Vec<ValidationIssue>> {
    if let Some(options) = schema.get("anyOf").and_then(Value::as_array) {
        if value.is_null() && options.iter().any(|o| o["type"] == "null") {
            return Ok(Value::Null);
        }
        let inner = options
            .iter()
            .find(|o| o["type"] != "null")
            .cloned()
            .unwrap_or(Value::Null);
        return coerce(&inner, value, loc);
    }
    if let Some(choices) = schema.get("enum").and_then(Value::as_array) {
        if choices.contains(value) {
            return Ok(value.clone());
        }
        let names: Vec<&str> = choices.iter().filter_map(Value::as_str).collect();
        return Err(issue(loc, "literal_error", &literal_message(&names), value));
    }
    match schema["type"].as_str().unwrap_or("") {
        "string" => match value {
            Value::String(_) => Ok(value.clone()),
            _ => Err(issue(
                loc,
                "string_type",
                "Input should be a valid string",
                value,
            )),
        },
        "boolean" => coerce_bool(value).ok_or_else(|| {
            if value.is_string() || value.is_number() {
                issue(
                    loc,
                    "bool_parsing",
                    "Input should be a valid boolean, unable to interpret input",
                    value,
                )
            } else {
                issue(loc, "bool_type", "Input should be a valid boolean", value)
            }
        }),
        "integer" => coerce_int(value, loc),
        "array" => {
            let Some(items) = value.as_array() else {
                return Err(issue(
                    loc,
                    "list_type",
                    "Input should be a valid list",
                    value,
                ));
            };
            let mut out = Vec::new();
            let mut issues = Vec::new();
            for (index, item) in items.iter().enumerate() {
                match coerce(&schema["items"], item, &format!("{loc}.{index}")) {
                    Ok(item) => out.push(item),
                    Err(mut found) => issues.append(&mut found),
                }
            }
            if issues.is_empty() {
                Ok(Value::Array(out))
            } else {
                Err(issues)
            }
        }
        _ => Ok(value.clone()),
    }
}

fn coerce_bool(value: &Value) -> Option<Value> {
    match value {
        Value::Bool(_) => Some(value.clone()),
        Value::Number(n) => match n.as_f64() {
            Some(0.0) => Some(json!(false)),
            Some(1.0) => Some(json!(true)),
            _ => None,
        },
        Value::String(text) => match text.trim().to_lowercase().as_str() {
            "0" | "off" | "f" | "false" | "n" | "no" => Some(json!(false)),
            "1" | "on" | "t" | "true" | "y" | "yes" => Some(json!(true)),
            _ => None,
        },
        _ => None,
    }
}

fn coerce_int(value: &Value, loc: &str) -> Result<Value, Vec<ValidationIssue>> {
    match value {
        Value::Bool(b) => Ok(json!(i64::from(*b))),
        Value::Number(n) if n.is_i64() || n.is_u64() => Ok(value.clone()),
        Value::Number(n) => {
            let x = n.as_f64().unwrap_or(f64::NAN);
            if x.fract() == 0.0 && x.is_finite() {
                Ok(json!(x as i64))
            } else {
                Err(issue(
                    loc,
                    "int_from_float",
                    "Input should be a valid integer, got a number with a fractional part",
                    value,
                ))
            }
        }
        Value::String(text) => text.trim().parse::<i64>().map(|n| json!(n)).map_err(|_| {
            issue(
                loc,
                "int_parsing",
                "Input should be a valid integer, unable to parse string as an integer",
                value,
            )
        }),
        _ => Err(issue(
            loc,
            "int_type",
            "Input should be a valid integer",
            value,
        )),
    }
}

fn literal_message(choices: &[&str]) -> String {
    let quoted: Vec<String> = choices.iter().map(|c| py_repr_str(c)).collect();
    let listed = match quoted.split_last() {
        Some((last, rest)) if !rest.is_empty() => format!("{} or {last}", rest.join(", ")),
        _ => quoted.join(""),
    };
    format!("Input should be {listed}")
}

/// pydantic's `ValidationError.__str__`.
fn validation_message(title: &str, issues: &[ValidationIssue]) -> String {
    let count = issues.len();
    let mut text = format!(
        "{count} validation error{} for {title}",
        if count == 1 { "" } else { "s" }
    );
    for issue in issues {
        text.push_str(&format!(
            "\n{}\n  {} [type={}, input_value={}, input_type={}]\n    For further information visit {PYDANTIC_DOCS}/{}",
            issue.loc,
            issue.message,
            issue.kind,
            truncate_repr(&py_repr(&issue.input)),
            py_type_name(&issue.input),
            issue.kind
        ));
    }
    text
}

fn truncate_repr(text: &str) -> String {
    let chars: Vec<char> = text.chars().collect();
    if chars.len() <= 50 {
        return text.to_owned();
    }
    let head: String = chars[..25].iter().collect();
    let tail: String = chars[chars.len() - 24..].iter().collect();
    format!("{head}...{tail}")
}

fn py_type_name(value: &Value) -> &'static str {
    match value {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(n) if n.is_f64() => "float",
        Value::Number(_) => "int",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

/// Python's `repr()` of a JSON value.
fn py_repr(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Number(n) => match n.as_f64() {
            Some(x) if n.is_f64() => pyjson::float_repr(x),
            _ => n.to_string(),
        },
        Value::String(text) => py_repr_str(text),
        Value::Array(items) => format!(
            "[{}]",
            items.iter().map(py_repr).collect::<Vec<_>>().join(", ")
        ),
        Value::Object(map) => format!(
            "{{{}}}",
            map.iter()
                .map(|(k, v)| format!("{}: {}", py_repr_str(k), py_repr(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

/// Python's `repr()` of a `str` (also `str(KeyError(message))`).
pub fn py_repr_str(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(text.len() + 2);
    out.push(quote);
    for ch in text.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_reprs() {
        assert_eq!(py_repr_str("a'b"), "\"a'b\"");
        assert_eq!(py_repr_str("plain"), "'plain'");
        assert_eq!(
            py_repr(&json!({"a": [1, 2.5, null, true]})),
            "{'a': [1, 2.5, None, True]}"
        );
        assert_eq!(
            truncate_repr(&py_repr_str(&"x".repeat(120))),
            format!("'{}...{}'", "x".repeat(24), "x".repeat(23))
        );
    }

    #[test]
    fn validation_follows_pydantic() {
        let schema = &catalog()["tools"]
            .as_array()
            .unwrap()
            .iter()
            .find(|t| t["name"] == "verlato_get_draft_document")
            .unwrap()["inputSchema"];
        let args = validate_arguments(
            "verlato_get_draft_document",
            schema,
            json!({"base": "v1", "include_confidence": "yes"})
                .as_object()
                .unwrap(),
        )
        .ok()
        .unwrap();
        assert_eq!(args.0["include_confidence"], json!(true));
        assert_eq!(args.0["head"], json!("HEAD"));
        let message = validate_arguments(
            "verlato_get_draft_document",
            schema,
            json!({"base": 5, "model_provider": "bad"})
                .as_object()
                .unwrap(),
        )
        .err()
        .unwrap();
        assert!(message.starts_with("2 validation errors for call[verlato_get_draft_document]\nbase\n  Input should be a valid string [type=string_type, input_value=5, input_type=int]"));
        assert!(message.contains("Input should be 'stub', 'mock', 'ollama', 'litellm', 'plugin', 'github-copilot', 'codex-cli' or 'claude-code' [type=literal_error, input_value='bad', input_type=str]"));
    }

    #[test]
    fn handshake_negotiates_known_versions() {
        let params = |v: &str| {
            json!({"protocolVersion": v, "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
                .as_object()
                .unwrap()
                .clone()
        };
        assert_eq!(
            initialize(&params("2024-11-05")).ok().unwrap()["protocolVersion"],
            "2024-11-05"
        );
        assert_eq!(
            initialize(&params("1999-01-01")).ok().unwrap()["protocolVersion"],
            LATEST_PROTOCOL_VERSION
        );
        assert!(initialize(&Map::new()).is_err());
    }
}
