from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from releasepilot.core.models import ChangeItem


@dataclass
class StateData:
    items: list[ChangeItem] = field(default_factory=list)


class StateStore:
    def __init__(self, path: str = ".releasepilot_state.json") -> None:
        self.path = Path(path)

    def load(self) -> StateData:
        if not self.path.exists():
            return StateData()
        raw = json.loads(self.path.read_text())
        return StateData(items=[ChangeItem.from_dict(i) for i in raw.get("items", [])])

    def save(self, data: StateData) -> None:
        payload = {"items": [i.to_dict() for i in data.items]}
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True))

    def append_new(self, items: list[ChangeItem]) -> None:
        current = self.load()
        current.items.extend(items)
        self.save(current)
