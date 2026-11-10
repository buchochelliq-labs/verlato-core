//! Verlato: semantic release notes powered by IntentumDiff.
//!
//! The Rust implementation links the IntentumDiff engine in-process and owns the
//! deterministic release-note pipeline (collection, grouping, semantic evidence,
//! classification, reconciliation, drafts, review state and the editor service), every model
//! provider, suggestions in every strategy, commit intents, the maintenance commands and the
//! MCP server. The `verlato` executable and the `verlato` Python package (PyO3 bindings in
//! `crates/verlato-python`) both run [`cli::run`]; see `docs/RUST_MIGRATION.md`.

pub mod adaptive;
pub mod assist;
pub mod classifier;
pub mod cli;
pub mod config;
pub mod drafts;
pub mod engine;
pub mod evidence;
pub mod git;
pub mod grouper;
pub mod intents;
pub mod maintenance;
pub mod mcp;
pub mod models;
pub mod pipeline;
pub mod providers;
pub mod pyjson;
pub mod semantic;
pub mod service;
pub mod store;
pub mod suggest;
pub mod suggestion_cache;
pub mod suggestion_tools;
pub mod vocab;
