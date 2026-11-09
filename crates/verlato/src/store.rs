//! JSON state persistence (`.verlato_state.json`). Ports `verlato/state/store.py`; the file
//! format is shared with the Python implementation (schema v3, keys sorted, 2-space indent).

use anyhow::{bail, Context, Result};
use indexmap::IndexMap;
use serde_json::{json, Value};
use std::path::PathBuf;

use crate::models::ChangeItem;

pub const CURRENT_SCHEMA_VERSION: &str = "v3";

#[derive(Debug, Clone, Default)]
pub struct StateData {
    pub items: Vec<ChangeItem>,
    pub schema_version: String,
    pub area_release_lineage: IndexMap<String, Vec<String>>,
    pub feedback_events: Vec<Value>,
}

pub struct StateStore {
    pub path: PathBuf,
}

impl StateStore {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self { path: path.into() }
    }

    pub fn load(&self) -> Result<StateData> {
        if !self.path.exists() {
            return Ok(StateData {
                schema_version: CURRENT_SCHEMA_VERSION.into(),
                ..StateData::default()
            });
        }
        let text = std::fs::read_to_string(&self.path)
            .with_context(|| format!("Unable to load state from {}", self.path.display()))?;
        let raw: Value = serde_json::from_str(&text)
            .with_context(|| format!("Unable to load state from {}", self.path.display()))?;
        let schema_version = raw
            .get("schema_version")
            .and_then(Value::as_str)
            .unwrap_or("v1")
            .to_owned();
        if !["v1", "v2", CURRENT_SCHEMA_VERSION].contains(&schema_version.as_str()) {
            bail!("Unsupported state schema version: {schema_version}");
        }
        let items = raw
            .get("items")
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .map(ChangeItem::from_value)
                    .collect::<Result<Vec<_>>>()
            })
            .transpose()?
            .unwrap_or_default();
        let stored_lineage: Option<IndexMap<String, Vec<String>>> = if schema_version == "v1" {
            None
        } else {
            raw.get("area_release_lineage")
                .filter(|v| v.as_object().is_some_and(|o| !o.is_empty()))
                .and_then(|v| serde_json::from_value(v.clone()).ok())
        };
        let lineage = stored_lineage.unwrap_or_else(|| derive_lineage(&items));
        let feedback_events = if schema_version == "v1" {
            Vec::new()
        } else {
            raw.get("feedback_events")
                .and_then(Value::as_array)
                .map(|events| events.iter().filter(|e| e.is_object()).cloned().collect())
                .unwrap_or_default()
        };
        Ok(StateData {
            items,
            schema_version,
            area_release_lineage: lineage,
            feedback_events,
        })
    }

    pub fn save(&self, data: &StateData) -> Result<()> {
        let payload = json!({
            "schema_version": CURRENT_SCHEMA_VERSION,
            "area_release_lineage": derive_lineage(&data.items),
            "feedback_events": data.feedback_events,
            "items": data.items,
        });
        // serde_json's default map is ordered by key, matching Python's sort_keys=True.
        let text = serde_json::to_string_pretty(&payload)?;
        std::fs::write(&self.path, text)
            .with_context(|| format!("Unable to write state to {}", self.path.display()))
    }

    pub fn append_new(&self, items: Vec<ChangeItem>) -> Result<()> {
        let mut current = self.load()?;
        let lineage = derive_lineage(&current.items);
        for mut item in items {
            item.lineage_releases = lineage
                .get(&item.classification.area)
                .cloned()
                .unwrap_or_default();
            current.items.push(item);
        }
        self.save(&current)
    }
}

pub fn derive_lineage(items: &[ChangeItem]) -> IndexMap<String, Vec<String>> {
    let mut lineage: IndexMap<String, Vec<String>> = IndexMap::new();
    for item in items {
        let (true, Some(release)) = (item.published, item.published_in.as_ref()) else {
            continue;
        };
        let releases = lineage.entry(item.classification.area.clone()).or_default();
        if !releases.contains(release) {
            releases.push(release.clone());
        }
    }
    lineage
}
