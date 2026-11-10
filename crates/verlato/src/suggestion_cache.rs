//! JSON suggestion cache (ports `JsonSuggestionCacheStore` from
//! `verlato/state/suggestion_cache.py`): same file format, TTL, file lock and atomic save, so
//! both runtimes can share one cache file.

use anyhow::{bail, Result};
use chrono::{DateTime, NaiveDateTime, Utc};
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

const CURRENT_SCHEMA_VERSION: &str = "v1";

pub struct CachedSuggestion {
    pub item: Map<String, Value>,
    pub age_seconds: i64,
}

pub struct JsonSuggestionCache {
    pub path: PathBuf,
    ttl_days: i64,
    created_at: String,
    entries: BTreeMap<String, Value>,
    pub dirty: bool,
}

/// Backends a CLI process can open (`build_suggestion_cache_backend`'s registry). Python
/// integrations can register more with `register_suggestion_cache_backend`, but only inside their
/// own interpreter, so the Python CLI and service only ever see `json`.
pub const BACKENDS: &[&str] = &["json"];

/// `build_suggestion_cache_backend`: open the (already normalized) `backend` at `path`.
pub fn open_backend(backend: &str, path: &str, ttl_days: i64) -> Result<JsonSuggestionCache> {
    match backend {
        "json" => JsonSuggestionCache::load(path, ttl_days),
        other => bail!(
            "Unknown suggestion cache backend {}; available: {}",
            crate::mcp::py_repr_str(other),
            BACKENDS.join(", ")
        ),
    }
}

/// `_utc_now`: second precision with a `Z` suffix.
pub fn utc_now() -> String {
    Utc::now().format("%Y-%m-%dT%H:%M:%SZ").to_string()
}

/// `_parse_utc`: ISO 8601 timestamps; naive ones are treated as UTC.
fn parse_utc(value: &str) -> Option<DateTime<Utc>> {
    if value.is_empty() {
        return None;
    }
    let normalized = value.replace('Z', "+00:00");
    if let Ok(parsed) = DateTime::parse_from_rfc3339(&normalized) {
        return Some(parsed.with_timezone(&Utc));
    }
    for format in [
        "%Y-%m-%dT%H:%M:%S%.f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S%.f",
    ] {
        if let Ok(parsed) = NaiveDateTime::parse_from_str(&normalized, format) {
            return Some(parsed.and_utc());
        }
    }
    None
}

fn text(entry: &Value, key: &str) -> Option<String> {
    match entry.get(key) {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) if s.is_empty() => None,
        Some(Value::String(s)) => Some(s.clone()),
        Some(other) => Some(other.to_string()),
    }
}

/// An exclusive interprocess lock on `<cache>.lock`, held until dropped. `File::lock` is
/// `flock(LOCK_EX)` on Unix (as Python's `fcntl.flock`) and `LockFileEx` over the whole file
/// on Windows, which overlaps the one-byte `msvcrt.locking` region Python takes, so native and
/// Python writers exclude each other on both platforms.
struct FileLock {
    _file: std::fs::File,
}

fn lock_path(path: &Path) -> std::path::PathBuf {
    path.with_file_name(format!(
        "{}.lock",
        path.file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default()
    ))
}

fn lock_file(path: &Path) -> Result<FileLock> {
    let lock_path = lock_path(path);
    if let Some(parent) = lock_path.parent().filter(|p| !p.as_os_str().is_empty()) {
        std::fs::create_dir_all(parent)?;
    }
    let file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .read(true)
        .open(&lock_path)?;
    file.lock()?;
    Ok(FileLock { _file: file })
}

impl JsonSuggestionCache {
    /// Open and load the cache (`build_suggestion_cache_backend(...).load()`).
    pub fn load(path: &str, ttl_days: i64) -> Result<JsonSuggestionCache> {
        let now = utc_now();
        let mut cache = JsonSuggestionCache {
            path: PathBuf::from(path),
            ttl_days: ttl_days.max(0),
            created_at: now,
            entries: BTreeMap::new(),
            dirty: false,
        };
        if !cache.path.exists() {
            return Ok(cache);
        }
        let raw: Value = match std::fs::read_to_string(&cache.path)
            .map_err(|e| e.to_string())
            .and_then(|t| serde_json::from_str(&t).map_err(|e| e.to_string()))
        {
            Ok(raw) => raw,
            Err(error) => bail!(
                "Unable to load suggestion cache from {}: {error}",
                cache.path.display()
            ),
        };
        let Some(raw) = raw.as_object() else {
            cache.dirty = true;
            return Ok(cache);
        };
        let schema =
            text(&Value::Object(raw.clone()), "schema_version").unwrap_or_else(|| "v1".into());
        if schema != CURRENT_SCHEMA_VERSION {
            cache.dirty = true;
            return Ok(cache);
        }
        let raw_value = Value::Object(raw.clone());
        cache.created_at = text(&raw_value, "created_at").unwrap_or(cache.created_at);
        let updated_at = text(&raw_value, "updated_at").unwrap_or_else(|| cache.created_at.clone());
        let Some(entries) = raw.get("entries").and_then(Value::as_object) else {
            cache.dirty = true;
            return Ok(cache);
        };
        for (key, entry) in entries {
            if !entry.is_object() || !entry.get("item").is_some_and(Value::is_object) {
                cache.dirty = true;
                continue;
            }
            if cache.expired(entry) {
                cache.dirty = true;
                continue;
            }
            let normalized = json!({
                "key": text(entry, "key").unwrap_or_else(|| key.clone()),
                "created_at": text(entry, "created_at").unwrap_or_else(|| cache.created_at.clone()),
                "updated_at": text(entry, "updated_at")
                    .or_else(|| text(entry, "created_at"))
                    .unwrap_or_else(|| updated_at.clone()),
                "item": entry["item"],
            });
            cache.entries.insert(key.clone(), normalized);
        }
        Ok(cache)
    }

    fn expired(&self, entry: &Value) -> bool {
        if self.ttl_days <= 0 {
            return true;
        }
        let stamp = text(entry, "updated_at")
            .or_else(|| text(entry, "created_at"))
            .unwrap_or_default();
        match parse_utc(&stamp) {
            None => true,
            Some(timestamp) => Utc::now() - timestamp > chrono::Duration::days(self.ttl_days),
        }
    }

    pub fn get(&mut self, key: &str) -> Option<CachedSuggestion> {
        let entry = self.entries.get(key)?.clone();
        if self.expired(&entry) {
            self.entries.remove(key);
            self.dirty = true;
            return None;
        }
        let updated_at = text(&entry, "updated_at")
            .or_else(|| text(&entry, "created_at"))
            .unwrap_or_default();
        let age_seconds = parse_utc(&updated_at)
            .map(|t| (Utc::now() - t).num_seconds().max(0))
            .unwrap_or(0);
        Some(CachedSuggestion {
            item: entry["item"].as_object().cloned().unwrap_or_default(),
            age_seconds,
        })
    }

    pub fn put(&mut self, key: &str, item: Map<String, Value>) {
        let now = utc_now();
        let created_at = self
            .entries
            .get(key)
            .and_then(|e| text(e, "created_at"))
            .unwrap_or_else(|| now.clone());
        self.entries.insert(
            key.to_owned(),
            json!({"key": key, "created_at": created_at, "updated_at": now, "item": item}),
        );
        self.dirty = true;
    }

    fn read_disk_entries(&self) -> (Option<String>, BTreeMap<String, Value>) {
        let mut entries = BTreeMap::new();
        let Some(raw) = std::fs::read_to_string(&self.path)
            .ok()
            .and_then(|t| serde_json::from_str::<Value>(&t).ok())
            .filter(Value::is_object)
        else {
            return (None, entries);
        };
        if text(&raw, "schema_version").unwrap_or_else(|| "v1".into()) != CURRENT_SCHEMA_VERSION {
            return (None, entries);
        }
        let created_at = text(&raw, "created_at").unwrap_or_else(|| self.created_at.clone());
        let updated_at = text(&raw, "updated_at").unwrap_or_else(|| created_at.clone());
        if let Some(raw_entries) = raw.get("entries").and_then(Value::as_object) {
            for (key, entry) in raw_entries {
                if !entry.is_object() || !entry.get("item").is_some_and(Value::is_object) {
                    continue;
                }
                let normalized = json!({
                    "key": text(entry, "key").unwrap_or_else(|| key.clone()),
                    "created_at": text(entry, "created_at").unwrap_or_else(|| created_at.clone()),
                    "updated_at": text(entry, "updated_at")
                        .or_else(|| text(entry, "created_at"))
                        .unwrap_or_else(|| updated_at.clone()),
                    "item": entry["item"],
                });
                if !self.expired(&normalized) {
                    entries.insert(key.clone(), normalized);
                }
            }
        }
        (Some(created_at), entries)
    }

    /// Merge with the file on disk under the lock, drop expired entries and write atomically.
    pub fn save(&mut self) -> Result<()> {
        if let Some(parent) = self.path.parent().filter(|p| !p.as_os_str().is_empty()) {
            std::fs::create_dir_all(parent)?;
        }
        let _lock = lock_file(&self.path)?;
        let (disk_created_at, disk_entries) = self.read_disk_entries();
        let mut merged = disk_entries;
        merged.extend(std::mem::take(&mut self.entries));
        self.entries = merged
            .into_iter()
            .filter(|(_, entry)| !self.expired(entry))
            .collect();
        if let Some(created_at) = disk_created_at {
            self.created_at = created_at;
        }
        let payload = json!({
            "schema_version": CURRENT_SCHEMA_VERSION,
            "created_at": self.created_at,
            "updated_at": utc_now(),
            "entries": self.entries,
        });
        let name = self
            .path
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let tmp = self
            .path
            .with_file_name(format!("{name}.{}.{nonce:x}.tmp", std::process::id()));
        let written = std::fs::write(&tmp, serde_json::to_string_pretty(&payload)?)
            .and_then(|()| std::fs::rename(&tmp, &self.path));
        let _ = std::fs::remove_file(&tmp);
        written?;
        self.dirty = false;
        Ok(())
    }

    /// `JsonSuggestionCacheStore.clear`: delete the cache file under the lock and return how
    /// many entries it held (0 for an unreadable file).
    pub fn clear(&mut self) -> Result<usize> {
        if let Some(parent) = self.path.parent().filter(|p| !p.as_os_str().is_empty()) {
            std::fs::create_dir_all(parent)?;
        }
        let mut count = 0;
        {
            let _lock = lock_file(&self.path)?;
            if self.path.exists() {
                let raw: Value = std::fs::read_to_string(&self.path)
                    .ok()
                    .and_then(|text| serde_json::from_str(&text).ok())
                    .unwrap_or(Value::Null);
                count = raw
                    .get("entries")
                    .and_then(Value::as_object)
                    .map_or(0, Map::len);
                std::fs::remove_file(&self.path)?;
            }
        }
        self.entries.clear();
        self.dirty = false;
        Ok(count)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn save_lock_excludes_other_holders_until_released() {
        let dir = tempfile::tempdir().unwrap();
        let cache = dir.path().join("cache.json");
        let held = lock_file(&cache).unwrap();
        let other = std::fs::OpenOptions::new()
            .read(true)
            .append(true)
            .open(lock_path(&cache))
            .unwrap();
        assert!(matches!(
            other.try_lock(),
            Err(std::fs::TryLockError::WouldBlock)
        ));
        drop(held);
        other.try_lock().unwrap();
    }
}
