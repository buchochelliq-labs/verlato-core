from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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

    def load(self) -> SuggestionCacheBackend:
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
_PROCESS_LOCKS: dict[Path, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


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

    def load(self) -> JsonSuggestionCacheStore:
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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _cache_file_lock(self.path):
            disk_created_at, disk_entries = self._read_disk_entries_for_merge()
            merged = {**disk_entries, **self.entries}
            compacted: dict[str, dict[str, Any]] = {}
            for key, entry in merged.items():
                if self._expired(entry):
                    self.dirty = True
                    continue
                compacted[key] = entry
            now = _utc_now()
            self.entries = compacted
            self.created_at = disk_created_at or self.created_at
            payload = {
                "schema_version": self.CURRENT_SCHEMA_VERSION,
                "created_at": self.created_at,
                "updated_at": now,
                "entries": self.entries,
            }
            tmp_path = self.path.with_name(f"{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            try:
                tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
                tmp_path.replace(self.path)
            finally:
                try:
                    tmp_path.unlink()
                except FileNotFoundError:
                    pass
            self.updated_at = now
            self.dirty = False

    def clear(self) -> int:
        count = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _cache_file_lock(self.path):
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

    def _read_disk_entries_for_merge(self) -> tuple[str | None, dict[str, dict[str, Any]]]:
        if not self.path.exists():
            return None, {}
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return None, {}
        if not isinstance(raw, dict):
            return None, {}
        if str(raw.get("schema_version") or "v1") != self.CURRENT_SCHEMA_VERSION:
            return None, {}
        created_at = str(raw.get("created_at") or self.created_at)
        updated_at = str(raw.get("updated_at") or self.updated_at)
        raw_entries = raw.get("entries")
        if not isinstance(raw_entries, dict):
            return created_at, {}
        entries: dict[str, dict[str, Any]] = {}
        for key, entry in raw_entries.items():
            if not isinstance(key, str) or not isinstance(entry, dict):
                continue
            item = entry.get("item")
            if not isinstance(item, dict):
                continue
            normalized = {
                "key": str(entry.get("key") or key),
                "created_at": str(entry.get("created_at") or created_at),
                "updated_at": str(entry.get("updated_at") or entry.get("created_at") or updated_at),
                "item": item,
            }
            if not self._expired(normalized):
                entries[key] = normalized
        return created_at, entries

    def _expired(self, entry: dict[str, Any]) -> bool:
        if self.ttl_days <= 0:
            return True
        timestamp = _parse_utc(str(entry.get("updated_at") or entry.get("created_at") or ""))
        if timestamp is None:
            return True
        return datetime.now(timezone.utc) - timestamp > timedelta(days=self.ttl_days)


SuggestionCacheStore = JsonSuggestionCacheStore


register_suggestion_cache_backend("json", lambda path, ttl_days: JsonSuggestionCacheStore(path=path, ttl_days=ttl_days))


@contextmanager
def _cache_file_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    process_lock = _process_lock_for(lock_path)
    with process_lock:
        with lock_path.open("a+b") as lock_file:
            _lock_file(lock_file)
            try:
                yield
            finally:
                _unlock_file(lock_file)


def _process_lock_for(path: Path) -> threading.Lock:
    try:
        key = path.resolve()
    except OSError:
        key = path.absolute()
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PROCESS_LOCKS[key] = lock
        return lock


def _lock_file(lock_file) -> None:
    if sys.platform == "win32":
        import msvcrt

        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        return
    import fcntl

    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)


def _unlock_file(lock_file) -> None:
    if sys.platform == "win32":
        import msvcrt

        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


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
