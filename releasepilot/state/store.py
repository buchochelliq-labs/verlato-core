from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from releasepilot.core.models import ChangeItem


@dataclass
class StateData:
    items: list[ChangeItem] = field(default_factory=list)
    schema_version: str = "v2"
    area_release_lineage: dict[str, list[str]] = field(default_factory=dict)


class StateStore:
    def __init__(self, path: str = ".releasepilot_state.json") -> None:
        self.path = Path(path)

    def load(self) -> StateData:
        if not self.path.exists():
            return StateData()
        raw = json.loads(self.path.read_text())
        items = [ChangeItem.from_dict(i) for i in raw.get("items", [])]
        lineage = raw.get("area_release_lineage") or self._derive_lineage(items)
        return StateData(
            items=items,
            schema_version=raw.get("schema_version", "v1"),
            area_release_lineage=lineage,
        )

    def save(self, data: StateData) -> None:
        lineage = self._derive_lineage(data.items)
        payload = {
            "schema_version": "v2",
            "area_release_lineage": lineage,
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
