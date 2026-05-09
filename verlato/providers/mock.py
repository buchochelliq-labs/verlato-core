from __future__ import annotations

import hashlib
import json
from random import Random

from verlato.providers.base import ProviderRequest, ProviderResponse, VerlatoProvider


class MockProvider(VerlatoProvider):
    """
    Deterministic local provider for tests. Outputs are derived from prompt hash.
    """

    def summarize(self, request: ProviderRequest) -> ProviderResponse:
        if request.task == "infer-conventional":
            return ProviderResponse(
                content=json.dumps(_infer_conventional_from_evidence(request.evidence), sort_keys=True),
                confidence=0.82,
                model="mock-local",
            )
        seed = self._seed(request)
        rng = Random(seed)
        confidence = round(0.6 + (rng.random() * 0.35), 2)
        content = _summarize_from_evidence(request.evidence)
        return ProviderResponse(
            content=content,
            confidence=confidence,
            model="mock-local",
        )

    def classify(self, request: ProviderRequest) -> ProviderResponse:
        seed = self._seed(request)
        rng = Random(seed)
        classes = [
            "added/api/external/minor",
            "fixed/backend/external/patch",
            "internal/docs/internal/none",
            "changed/general/external/patch",
        ]
        choice = classes[rng.randrange(len(classes))]
        confidence = round(0.55 + (rng.random() * 0.4), 2)
        return ProviderResponse(content=f"[mock-classification] {choice}", confidence=confidence, model="mock-local")

    @staticmethod
    def _seed(request: ProviderRequest) -> int:
        digest = hashlib.sha256(f"{request.task}:{request.prompt}".encode()).hexdigest()[:8]
        return int(digest, 16)


def _summarize_from_evidence(evidence: dict | None) -> str:
    evidence = evidence or {}
    file_statuses = evidence.get("file_statuses")
    if isinstance(file_statuses, dict) and file_statuses:
        statuses: dict[str, int] = {}
        for status in file_statuses.values():
            normalized = str(status or "modified")
            statuses[normalized] = statuses.get(normalized, 0) + 1
        status_text = ", ".join(f"{status} {count}" for status, count in sorted(statuses.items()))
        paths = sorted(str(path) for path in file_statuses)[:6]
        suffix = f", plus {len(file_statuses) - len(paths)} more" if len(file_statuses) > len(paths) else ""
        return f"Updates {len(file_statuses)} changed file(s) ({status_text}): {', '.join(paths)}{suffix}."

    commits = evidence.get("commit_subjects") or evidence.get("commits")
    if isinstance(commits, list) and commits:
        subject = str(commits[0]).splitlines()[0]
        return f"Updates release scope from commit evidence: {subject}."

    return "No changed-file evidence was available from PR or commit metadata."


def _infer_conventional_from_evidence(evidence: dict | None) -> dict[str, object]:
    evidence = evidence or {}
    subject = str(evidence.get("subject") or "").lower()
    files = evidence.get("files")
    file_paths = [str(file.get("path") or "") for file in files if isinstance(file, dict)] if isinstance(files, list) else []
    joined_paths = " ".join(file_paths).lower()
    if "remove" in subject or "delete" in subject:
        commit_type = "remove"
    elif "fix" in subject or "bug" in subject:
        commit_type = "fix"
    elif "doc" in subject or "readme" in joined_paths:
        commit_type = "docs"
    elif "test" in subject or "/test" in joined_paths:
        commit_type = "test"
    else:
        commit_type = "feat" if ("add" in subject or "new" in subject) else "chore"
    if "api" in joined_paths:
        scope = "api"
    elif "vscode" in joined_paths or "extension" in joined_paths:
        scope = "vscode"
    elif "mcp" in joined_paths:
        scope = "mcp"
    elif "doc" in joined_paths or "readme" in joined_paths:
        scope = "docs"
    elif "test" in joined_paths:
        scope = "tests"
    else:
        scope = "general"
    return {
        "type": commit_type,
        "scope": scope,
        "description": subject or "update commit intent",
        "breaking": "breaking" in subject,
        "confidence": 0.82,
        "rationale": "Deterministic mock intent derived from commit subject and changed paths.",
    }
