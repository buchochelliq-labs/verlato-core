from __future__ import annotations

from releasepilot.core.models import ChangeItem


class Reconciler:
    def unpublished(self, existing: list[ChangeItem], detected: list[ChangeItem]) -> list[ChangeItem]:
        known_checksums = {item.checksum for item in existing}
        return [item for item in detected if item.checksum not in known_checksums]
