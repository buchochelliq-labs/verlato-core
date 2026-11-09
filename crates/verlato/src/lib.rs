//! Verlato: semantic release notes powered by IntentumDiff.
//!
//! The Rust implementation links the IntentumDiff engine in-process and owns the
//! deterministic release-note pipeline (collection, grouping, semantic evidence,
//! classification, reconciliation, drafts, review state and the editor service). Model-backed
//! features are delegated to the Python implementation until they are ported; see
//! `docs/RUST_MIGRATION.md`.

pub mod classifier;
pub mod config;
pub mod delegate;
pub mod drafts;
pub mod engine;
pub mod evidence;
pub mod git;
pub mod grouper;
pub mod models;
pub mod pipeline;
pub mod semantic;
pub mod service;
pub mod store;
pub mod vocab;
