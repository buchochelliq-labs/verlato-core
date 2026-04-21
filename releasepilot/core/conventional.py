from __future__ import annotations

import re
from dataclasses import dataclass

PATTERN = re.compile(r"^(?P<type>\w+)(?:\((?P<scope>[\w\-/]+)\))?(?P<breaking>!)?:\s(?P<description>.+)$")


@dataclass
class ConventionalCommit:
    type: str
    scope: str | None
    description: str
    breaking: bool


def parse_conventional_commit(message: str) -> ConventionalCommit | None:
    first = message.strip().splitlines()[0] if message.strip() else ""
    match = PATTERN.match(first)
    if not match:
        return None
    breaking_text = "BREAKING CHANGE" in message
    return ConventionalCommit(
        type=match.group("type"),
        scope=match.group("scope"),
        description=match.group("description"),
        breaking=bool(match.group("breaking")) or breaking_text,
    )
