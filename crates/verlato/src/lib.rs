//! Verlato: semantic release notes powered by IntentumDiff.
//!
//! The Rust implementation links the IntentumDiff engine in-process and owns the
//! deterministic release-note pipeline (collection, grouping, semantic evidence,
//! classification, reconciliation, drafts, review state and the editor service), and the
//! stub, plugin and LiteLLM model providers with the classifier's model assists. Other
//! model-backed features are delegated to the Python implementation until they are ported;
//! see `docs/RUST_MIGRATION.md`.

pub mod assist;
pub mod classifier;
pub mod config;
pub mod delegate;
pub mod drafts;
pub mod engine;
pub mod evidence;
pub mod git;
pub mod grouper;
pub mod intents;
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
