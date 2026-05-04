from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from releasepilot.core.models import ChangeItem


class StateStoreError(RuntimeError):
    pass


@dataclass
class StateData:
    items: list[ChangeItem] = field(default_factory=list)
    schema_version: str = "v3"
    area_release_lineage: dict[str, list[str]] = field(default_factory=dict)
    feedback_events: list[dict] = field(default_factory=list)


class StateStore:
    CURRENT_SCHEMA_VERSION = "v3"

    def __init__(self, path: str = ".releasepilot_state.json") -> None:
        self.path = Path(path)

    def load(self) -> StateData:
        if not self.path.exists():
            return StateData()
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise StateStoreError(f"Unable to load state from {self.path}: {exc}") from exc
        schema_version = raw.get("schema_version", "v1")
        if schema_version == "v1":
            items = [ChangeItem.from_dict(i) for i in raw.get("items", [])]
            lineage = self._derive_lineage(items)
        elif schema_version in {"v2", self.CURRENT_SCHEMA_VERSION}:
            items = [ChangeItem.from_dict(i) for i in raw.get("items", [])]
            lineage = raw.get("area_release_lineage") or self._derive_lineage(items)
            feedback_events = [event for event in raw.get("feedback_events", []) if isinstance(event, dict)]
        else:
            raise StateStoreError(f"Unsupported state schema version: {schema_version}")
        return StateData(
            items=items,
            schema_version=schema_version,
            area_release_lineage=lineage,
            feedback_events=feedback_events if schema_version in {"v2", self.CURRENT_SCHEMA_VERSION} else [],
        )

    def save(self, data: StateData) -> None:
        lineage = self._derive_lineage(data.items)
        payload = {
            "schema_version": self.CURRENT_SCHEMA_VERSION,
            "area_release_lineage": lineage,
            "feedback_events": data.feedback_events,
            "items": [i.to_dict() for i in data.items],
        }
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True))

    def append_new(self, items: list[ChangeItem]) -> None:
        current = self.load()
        lineage = self._derive_lineage(current.items)
        for item in items:
            item.lineage_releases = list(lineage.get(item.classification.area, []))
        current.items.extend(items)
        self.save(current)

    @staticmethod
    def _derive_lineage(items: list[ChangeItem]) -> dict[str, list[str]]:
        lineage: dict[str, list[str]] = {}
        for item in items:
            if not item.published or not item.published_in:
                continue
            area = item.classification.area
            releases = lineage.setdefault(area, [])
            if item.published_in not in releases:
                releases.append(item.published_in)
        return lineage
