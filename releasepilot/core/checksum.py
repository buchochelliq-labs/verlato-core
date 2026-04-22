from __future__ import annotations

import hashlib

CHECKSUM_VERSION = "v2"


def build_checksum(*, pr_number: int | None, commit_shas: list[str], title: str, area: str) -> str:
    """
    Versioned checksum that includes classification area for improved dedupe stability
    when PR split logic emits multiple user-facing items.
    """
    identity = f"{pr_number}:{','.join(commit_shas)}:{title}:{area}"
    return hashlib.sha256(identity.encode()).hexdigest()
