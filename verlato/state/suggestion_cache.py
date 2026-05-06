from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol


class SuggestionCacheError(RuntimeError):
    pass


@dataclass
class CachedSuggestion:
    key: str
    item: dict[str, Any]
    created_at: str
    updated_at: str
    age_seconds: int


class SuggestionCacheBackend(Protocol):
    path: Path
    dirty: bool

    def load(self) -> "SuggestionCacheBackend":
        ...

    def get(self, key: str) -> CachedSuggestion | None:
        ...

    def put(self, key: str, item: dict[str, Any]) -> None:
        ...

    def save(self) -> None:
        ...

    def clear(self) -> int:
        ...


SuggestionCacheBackendFactory = Callable[[str, int], SuggestionCacheBackend]
_CACHE_BACKENDS: dict[str, SuggestionCacheBackendFactory] = {}


def register_suggestion_cache_backend(name: str, factory: SuggestionCacheBackendFactory) -> None:
    normalized = _normalize_backend_name(name)
    if not normalized:
        raise SuggestionCacheError("Suggestion cache backend name cannot be empty")
    _CACHE_BACKENDS[normalized] = factory


def build_suggestion_cache_backend(
    *,
    backend: str = "json",
    path: str = ".verlato_suggestion_cache.json",
    ttl_days: int = 30,
) -> SuggestionCacheBackend:
    normalized = _normalize_backend_name(backend)
    factory = _CACHE_BACKENDS.get(normalized)
    if not factory:
        available = ", ".join(sorted(_CACHE_BACKENDS)) or "none"
        raise SuggestionCacheError(f"Unknown suggestion cache backend {backend!r}; available: {available}")
    return factory(path, ttl_days).load()


def _normalize_backend_name(name: str) -> str:
    return str(name or "json").strip().lower()


class JsonSuggestionCacheStore:
    CURRENT_SCHEMA_VERSION = "v1"

    def __init__(self, path: str = ".verlato_suggestion_cache.json", *, ttl_days: int = 30) -> None:
        self.path = Path(path)
        self.ttl_days = max(0, int(ttl_days))
        self.created_at = _utc_now()
        self.updated_at = self.created_at
        self.entries: dict[str, dict[str, Any]] = {}
        self.dirty = False

    def load(self) -> "JsonSuggestionCacheStore":
        if not self.path.exists():
            return self
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise SuggestionCacheError(f"Unable to load suggestion cache from {self.path}: {exc}") from exc
        if not isinstance(raw, dict):
            self.dirty = True
            return self
        schema_version = str(raw.get("schema_version") or "v1")
        if schema_version != self.CURRENT_SCHEMA_VERSION:
            self.dirty = True
            return self
        self.created_at = str(raw.get("created_at") or self.created_at)
        self.updated_at = str(raw.get("updated_at") or self.updated_at)
        raw_entries = raw.get("entries")
        if not isinstance(raw_entries, dict):
            self.dirty = True
            return self
        for key, entry in raw_entries.items():
            if not isinstance(key, str) or not isinstance(entry, dict):
                self.dirty = True
                continue
            item = entry.get("item")
            if not isinstance(item, dict):
                self.dirty = True
                continue
            if self._expired(entry):
                self.dirty = True
                continue
            normalized = {
                "key": str(entry.get("key") or key),
                "created_at": str(entry.get("created_at") or self.created_at),
                "updated_at": str(entry.get("updated_at") or entry.get("created_at") or self.updated_at),
                "item": item,
            }
            self.entries[key] = normalized
        return self

    def get(self, key: str) -> CachedSuggestion | None:
        entry = self.entries.get(key)
        if not entry:
            return None
        if self._expired(entry):
            self.entries.pop(key, None)
            self.dirty = True
            return None
        updated_at = str(entry.get("updated_at") or entry.get("created_at") or self.updated_at)
        return CachedSuggestion(
            key=key,
            item=dict(entry["item"]),
            created_at=str(entry.get("created_at") or updated_at),
            updated_at=updated_at,
            age_seconds=_age_seconds(updated_at),
        )

    def put(self, key: str, item: dict[str, Any]) -> None:
        now = _utc_now()
        previous = self.entries.get(key)
        self.entries[key] = {
            "key": key,
            "created_at": str(previous.get("created_at") if previous else now),
            "updated_at": now,
            "item": item,
        }
        self.updated_at = now
        self.dirty = True

    def save(self) -> None:
        now = _utc_now()
        compacted: dict[str, dict[str, Any]] = {}
        for key, entry in self.entries.items():
            if self._expired(entry):
                self.dirty = True
                continue
            compacted[key] = entry
        self.entries = compacted
        payload = {
            "schema_version": self.CURRENT_SCHEMA_VERSION,
            "created_at": self.created_at,
            "updated_at": now,
            "entries": self.entries,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(f"{self.path.name}.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp_path.replace(self.path)
        self.updated_at = now
        self.dirty = False

    def clear(self) -> int:
        count = 0
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text())
            except (OSError, json.JSONDecodeError):
                raw = {}
            entries = raw.get("entries") if isinstance(raw, dict) else None
            count = len(entries) if isinstance(entries, dict) else 0
            self.path.unlink()
        self.entries = {}
        self.dirty = False
        return count

    def _expired(self, entry: dict[str, Any]) -> bool:
        if self.ttl_days <= 0:
            return True
        timestamp = _parse_utc(str(entry.get("updated_at") or entry.get("created_at") or ""))
        if timestamp is None:
            return True
        return datetime.now(timezone.utc) - timestamp > timedelta(days=self.ttl_days)


SuggestionCacheStore = JsonSuggestionCacheStore


register_suggestion_cache_backend("json", lambda path, ttl_days: JsonSuggestionCacheStore(path=path, ttl_days=ttl_days))


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_seconds(value: str) -> int:
    timestamp = _parse_utc(value)
    if timestamp is None:
        return 0
    return max(0, int((datetime.now(timezone.utc) - timestamp).total_seconds()))
