//! Locally authenticated CLI agents as providers (ports `verlato/providers/cli_agents.py`):
//! Codex CLI (`codex exec`) and Claude Code (`claude --print`).

use serde_json::{json, Map, Value};
use std::time::{Duration, Instant};

use super::github::agent_summarize_task;
use super::model_log;
use super::plugin::format_g;
use super::process::{run_command, which, Env, Output, RunError};
use super::{Provider, ProviderRequest, ProviderResponse};
use crate::pyjson::{self, py_len};

const DEFAULT_CLI_AGENT_TIMEOUT_SECONDS: f64 = 120.0;

#[derive(Clone, Copy, PartialEq)]
pub enum Agent {
    Codex,
    ClaudeCode,
}

impl Agent {
    fn provider(self) -> &'static str {
        match self {
            Agent::Codex => "codex-cli",
            Agent::ClaudeCode => "claude-code",
        }
    }

    fn display(self) -> &'static str {
        match self {
            Agent::Codex => "Codex CLI",
            Agent::ClaudeCode => "Claude Code",
        }
    }

    fn event(self) -> &'static str {
        match self {
            Agent::Codex => "verlato.model.codex_cli",
            Agent::ClaudeCode => "verlato.model.claude_code",
        }
    }
}

pub struct CliAgentProvider {
    agent: Agent,
    command: String,
    model: String,
    timeout_seconds: f64,
}

fn optional_env(name: &str, configured: Option<String>) -> Option<String> {
    std::env::var(name)
        .ok()
        .or(configured)
        .map(|v| v.trim().to_owned())
        .filter(|v| !v.is_empty())
}

impl CliAgentProvider {
    pub fn new(agent: Agent, model: Option<String>, timeout_seconds: Option<f64>) -> Self {
        let (command_env, model_env, timeout_env, default_command) = match agent {
            Agent::Codex => (
                "VERLATO_CODEX_COMMAND",
                "VERLATO_CODEX_MODEL",
                "VERLATO_CODEX_TIMEOUT_SECONDS",
                "codex",
            ),
            Agent::ClaudeCode => (
                "VERLATO_CLAUDE_COMMAND",
                "VERLATO_CLAUDE_MODEL",
                "VERLATO_CLAUDE_TIMEOUT_SECONDS",
                "claude",
            ),
        };
        let timeout_seconds = match std::env::var(timeout_env) {
            Ok(raw) if !raw.trim().is_empty() => raw.trim().parse().ok(),
            _ => None,
        }
        .or(timeout_seconds)
        .unwrap_or(DEFAULT_CLI_AGENT_TIMEOUT_SECONDS);
        CliAgentProvider {
            agent,
            command: std::env::var(command_env)
                .ok()
                .filter(|c| !c.is_empty())
                .unwrap_or_else(|| default_command.into()),
            model: optional_env(model_env, model).unwrap_or_else(|| "default".into()),
            timeout_seconds,
        }
    }

    fn trace(&self, message: &str) {
        model_log::emit(
            message,
            self.agent.event(),
            Map::from_iter([
                ("provider".to_owned(), json!(self.agent.provider())),
                ("model".to_owned(), json!(self.model)),
            ]),
            false,
        );
    }

    fn prompt(&self, task: &str, request: &ProviderRequest) -> String {
        let output = if !request.response_format.is_null() {
            "Return only JSON matching the requested schema. Do not include Markdown fences or commentary."
        } else if task == "classify" {
            "Return exactly one line in the form kind/area/audience/semver. Do not include prose."
        } else {
            "Return only the release-note suggestion text. Do not include Markdown fences or commentary."
        };
        format!(
            "You are Verlato running through {}. Use only the supplied prompt and evidence. \
             Do not edit files or run tools.\n{output}\n\n\
             Task: {task}\n\
             Prompt:\n{}\n\n\
             Evidence JSON:\n{}",
            self.agent.display(),
            request.prompt,
            pyjson::dumps(&request.evidence, pyjson::DEFAULT_SORTED),
        )
    }

    fn run(
        &self,
        args: &[String],
        input: Option<&str>,
        prompt_chars: Option<usize>,
    ) -> Result<(Output, i64), RunError> {
        let prompt_detail = prompt_chars
            .map(|chars| format!(" prompt_chars={chars}"))
            .unwrap_or_default();
        self.trace(&format!(
            "post provider={} task=agent command={} model={}{prompt_detail} timeout_seconds={}",
            self.agent.provider(),
            self.command,
            self.model,
            format_g(self.timeout_seconds),
        ));
        let start = Instant::now();
        let cwd = std::env::current_dir().unwrap_or_else(|_| std::env::temp_dir());
        let output = run_command(
            args,
            input,
            &cwd,
            Env::Inherit,
            Duration::from_secs_f64(self.timeout_seconds),
        )?;
        let elapsed_ms = start.elapsed().as_millis() as i64;
        self.trace(&format!(
            "response provider={} returncode={} stdout_chars={} stderr_chars={} elapsed_ms={elapsed_ms}",
            self.agent.provider(),
            output.code,
            py_len(&output.stdout),
            py_len(&output.stderr),
        ));
        Ok((output, elapsed_ms))
    }

    fn invoke(&self, task: &str, request: &ProviderRequest) -> ProviderResponse {
        let provider = self.agent.provider();
        let label = format!("{provider}:{}", self.model);
        let Some(executable) = which(&self.command) else {
            return ProviderResponse::error(
                format!(
                    "[{provider}-unavailable] command not found: {}",
                    self.command
                ),
                label,
                Map::new(),
            );
        };
        let executable = executable.to_string_lossy().into_owned();
        let prompt = self.prompt(task, request);
        let model_args = |args: &mut Vec<String>| {
            if self.model != "default" {
                args.extend(["--model".to_owned(), self.model.clone()]);
            }
        };
        let (result, output_file) = match self.agent {
            Agent::Codex => {
                let dir = match tempfile_dir() {
                    Ok(dir) => dir,
                    Err(error) => {
                        return ProviderResponse::error(
                            format!("[{provider}-error] command failed (-1): {error}"),
                            label,
                            Map::new(),
                        )
                    }
                };
                let output_path = dir.join("last-message.txt");
                let mut args: Vec<String> = [
                    executable.as_str(),
                    "exec",
                    "--sandbox",
                    "read-only",
                    "--skip-git-repo-check",
                    "--ephemeral",
                    "--output-last-message",
                ]
                .iter()
                .map(|s| (*s).to_owned())
                .collect();
                args.push(output_path.to_string_lossy().into_owned());
                model_args(&mut args);
                args.push("-".into());
                let result = self.run(&args, Some(&prompt), Some(py_len(&prompt)));
                let content = std::fs::read_to_string(&output_path).ok();
                let _ = std::fs::remove_dir_all(&dir);
                (result, content)
            }
            Agent::ClaudeCode => {
                let mut args: Vec<String> = [
                    executable.as_str(),
                    "--print",
                    "--output-format",
                    "json",
                    "--max-turns",
                    "1",
                    "--permission-mode",
                    "plan",
                ]
                .iter()
                .map(|s| (*s).to_owned())
                .collect();
                model_args(&mut args);
                args.push(prompt.clone());
                (self.run(&args, None, None), None)
            }
        };
        let (output, elapsed_ms) = match result {
            Ok(result) => result,
            Err(RunError::Timeout(_)) => {
                return ProviderResponse::error(
                    format!(
                        "[{provider}-timeout] command exceeded {}s",
                        format_g(self.timeout_seconds)
                    ),
                    label,
                    Map::from_iter([
                        (
                            "elapsed_ms".to_owned(),
                            json!((self.timeout_seconds * 1000.0) as i64),
                        ),
                        ("task".to_owned(), json!(task)),
                        ("timeout".to_owned(), json!(true)),
                    ]),
                )
            }
            Err(RunError::Start(error)) => {
                return ProviderResponse::error(
                    format!("[{provider}-error] command failed (-1): {error}"),
                    label,
                    Map::new(),
                )
            }
        };
        if output.code != 0 {
            let raw = if output.stderr.is_empty() {
                &output.stdout
            } else {
                &output.stderr
            };
            let compact = raw.split_whitespace().collect::<Vec<_>>().join(" ");
            let compact = if compact.is_empty() {
                "no error output".to_owned()
            } else {
                compact.chars().take(500).collect()
            };
            return ProviderResponse::error(
                format!(
                    "[{provider}-error] command failed ({}): {compact}",
                    output.code
                ),
                label,
                Map::from_iter([
                    ("elapsed_ms".to_owned(), json!(elapsed_ms)),
                    ("task".to_owned(), json!(task)),
                    ("returncode".to_owned(), json!(output.code)),
                ]),
            );
        }
        let content = match self.agent {
            Agent::Codex => output_file.unwrap_or_else(|| output.stdout.clone()),
            Agent::ClaudeCode => Some(parse_claude_json_result(&output.stdout))
                .filter(|c| !c.is_empty())
                .unwrap_or_else(|| output.stdout.trim().to_owned()),
        };
        let content = content.trim().to_owned();
        let metadata = Map::from_iter([
            ("elapsed_ms".to_owned(), json!(elapsed_ms)),
            ("task".to_owned(), json!(task)),
        ]);
        if content.is_empty() {
            return ProviderResponse::error(
                format!("[{provider}-empty] no content"),
                label,
                metadata,
            );
        }
        ProviderResponse {
            content,
            confidence: 0.75,
            model: label,
            metadata,
            reasoning: String::new(),
        }
    }
}

fn tempfile_dir() -> std::io::Result<std::path::PathBuf> {
    let base = std::env::temp_dir();
    for attempt in 0..100u32 {
        let dir = base.join(format!(
            "verlato-codex-{}-{}-{attempt}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.subsec_nanos())
                .unwrap_or(0)
        ));
        match std::fs::create_dir(&dir) {
            Ok(()) => return Ok(dir),
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error),
        }
    }
    Err(std::io::Error::other(
        "unable to create a temporary directory",
    ))
}

fn parse_claude_json_result(stdout: &str) -> String {
    let Ok(Value::Object(parsed)) = serde_json::from_str::<Value>(stdout) else {
        return String::new();
    };
    ["result", "response", "content", "message"]
        .iter()
        .find_map(|key| {
            parsed
                .get(*key)
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .map(str::to_owned)
        })
        .unwrap_or_default()
}

impl Provider for CliAgentProvider {
    fn summarize(&self, request: &ProviderRequest) -> ProviderResponse {
        self.invoke(agent_summarize_task(&request.task), request)
    }

    fn classify(&self, request: &ProviderRequest) -> ProviderResponse {
        self.invoke("classify", request)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_claude_json_results() {
        assert_eq!(
            parse_claude_json_result(r#"{"result": " Fixes paging. "}"#),
            "Fixes paging."
        );
        assert_eq!(parse_claude_json_result("not json"), "");
    }
}
