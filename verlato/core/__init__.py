from __future__ import annotations

from verlato.core.models import ChangeItem


class Reconciler:
    def unpublished(self, existing: list[ChangeItem], detected: list[ChangeItem]) -> list[ChangeItem]:
        known_checksums = {(item.checksum_version, item.checksum) for item in existing}
        return [item for item in detected if (item.checksum_version, item.checksum) not in known_checksums]
