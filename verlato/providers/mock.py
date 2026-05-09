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
        if request.task == "draft-conventional":
            return ProviderResponse(
                content=json.dumps(_draft_conventional_from_evidence(request.evidence), sort_keys=True),
                confidence=0.84,
                model="mock-local",
            )
        if request.task in {"infer-conventional", "derive-conventional"}:
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
    change_summary = evidence.get("change_summary")
    if isinstance(change_summary, dict):
        files = change_summary.get("files")
        if isinstance(files, list) and files:
            statuses: dict[str, int] = {}
            paths: list[str] = []
            for file in files:
                if not isinstance(file, dict):
                    continue
                status = str(file.get("status") or "modified")
                statuses[status] = statuses.get(status, 0) + 1
                path = str(file.get("path") or "").strip()
                if path:
                    paths.append(path)
            if paths:
                status_text = ", ".join(f"{status} {count}" for status, count in sorted(statuses.items()))
                shown_paths = sorted(paths)[:6]
                suffix = f", plus {len(paths) - len(shown_paths)} more" if len(paths) > len(shown_paths) else ""
                return f"Updates {len(paths)} changed file(s) ({status_text}): {', '.join(shown_paths)}{suffix}."
        commits = change_summary.get("commits")
        if isinstance(commits, list) and commits:
            first = commits[0]
            if isinstance(first, dict):
                subject = str(first.get("subject") or first.get("message") or "").splitlines()[0]
            else:
                subject = str(first).splitlines()[0]
            if subject:
                return f"Updates release scope from commit evidence: {subject}."

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
    file_payloads = [file for file in files if isinstance(file, dict)] if isinstance(files, list) else []
    file_paths = [str(file.get("path") or "") for file in file_payloads]
    statuses = {str(file.get("status") or "").lower() for file in file_payloads}
    joined_paths = " ".join(file_paths).lower()
    if "remove" in subject or "delete" in subject:
        commit_type = "remove"
    elif "fix" in subject or "bug" in subject:
        commit_type = "fix"
    elif "doc" in subject or "readme" in joined_paths:
        commit_type = "docs"
    elif "test" in subject or "/test" in joined_paths:
        commit_type = "test"
    elif statuses <= {"added"} and statuses:
        commit_type = "feat"
    elif statuses & {"removed", "deleted"}:
        commit_type = "remove"
    elif "api" in joined_paths:
        commit_type = "feat"
    else:
        commit_type = "feat" if ("add" in subject or "new" in subject) else "chore"
    if "api" in joined_paths:
        scope = "api"
    elif "vscode" in joined_paths or "extension" in joined_paths:
        scope = "vscode"
    elif "/ui" in joined_paths or "web/" in joined_paths or "frontend" in joined_paths:
        scope = "ui"
    elif "mcp" in joined_paths:
        scope = "mcp"
    elif "doc" in joined_paths or "readme" in joined_paths:
        scope = "docs"
    elif "test" in joined_paths:
        scope = "tests"
    else:
        scope = "general"
    description = subject or _description_from_diff(files if isinstance(files, list) else [])
    return {
        "type": commit_type,
        "scope": scope,
        "description": description,
        "breaking": "breaking" in subject,
        "confidence": 0.82,
        "rationale": "Deterministic mock intent derived from changed paths.",
    }


def _draft_conventional_from_evidence(evidence: dict | None) -> dict[str, object]:
    evidence = evidence or {}
    subject = str(evidence.get("authored_subject") or evidence.get("subject") or "").strip()
    lowered_subject = subject.lower()
    files = evidence.get("files")
    file_payloads = [file for file in files if isinstance(file, dict)] if isinstance(files, list) else []
    file_paths = [str(file.get("path") or "") for file in file_payloads]
    joined_paths = " ".join(file_paths).lower()
    facts = evidence.get("diff_facts") if isinstance(evidence.get("diff_facts"), dict) else {}
    themes = [str(theme) for theme in facts.get("themes", [])] if isinstance(facts.get("themes"), list) else []
    raw_added_terms = facts.get("added_terms")
    raw_removed_terms = facts.get("removed_terms")
    added_terms = " ".join(str(term).lower() for term in raw_added_terms) if isinstance(raw_added_terms, list) else ""
    removed_terms = " ".join(str(term).lower() for term in raw_removed_terms) if isinstance(raw_removed_terms, list) else ""

    if "fix" in lowered_subject or "repair" in lowered_subject:
        commit_type = "fix"
    elif "refactor" in lowered_subject or "remove" in removed_terms or "removes instant provisional" in " ".join(themes):
        commit_type = "refactor"
    elif "doc" in lowered_subject and not ("vscode-extension/src" in joined_paths or "/src/" in joined_paths):
        commit_type = "docs"
    elif "test" in lowered_subject and "src" not in joined_paths:
        commit_type = "test"
    elif "add" in lowered_subject or "adds configurable" in " ".join(themes):
        commit_type = "feat"
    else:
        commit_type = "chore"

    if "vscode-extension" in joined_paths:
        scope = "vscode"
    elif "mcp" in joined_paths:
        scope = "mcp"
    elif "api" in joined_paths:
        scope = "api"
    elif "test" in joined_paths:
        scope = "tests"
    elif "readme" in joined_paths or ".md" in joined_paths:
        scope = "docs"
    else:
        scope = "general"

    description = _draft_description(subject, themes)
    body = _draft_body(themes=themes, added_terms=added_terms, removed_terms=removed_terms, file_paths=file_paths)
    return {
        "type": commit_type,
        "scope": scope,
        "description": description,
        "body": body,
        "breaking": "breaking" in lowered_subject,
        "confidence": 0.84,
        "rationale": "Deterministic mock draft derived from commit-scoped evidence.",
    }


def _draft_description(subject: str, themes: list[str]) -> str:
    lowered = subject.lower()
    if "suggestion strategy" in " ".join(themes).lower() or "suggestion" in lowered:
        return "revise suggestion strategy settings"
    if lowered.startswith("refactor "):
        return lowered.removeprefix("refactor ").replace("verlato ", "")[:120]
    return lowered or "update commit diff"


def _draft_body(*, themes: list[str], added_terms: str, removed_terms: str, file_paths: list[str]) -> list[str]:
    body: list[str] = []
    theme_text = " ".join(themes).lower()
    if "batch-size" in theme_text or "suggestionbatchsize" in added_terms:
        body.append("Updated suggestion batch-size defaults and configuration.")
    if "strategy" in theme_text or "full-diff-json" in added_terms:
        body.append("Added configurable suggestion strategy modes for auto, single, paired, and full-diff-json.")
    if "instant" in removed_terms or "provisional" in removed_terms:
        body.append("Removed instant provisional suggestion behavior and related settings.")
    if any("releaseControlView" in path or "releaseControlMarkup" in path for path in file_paths):
        body.append("Updated the release control view and markup to match the new suggestion flow.")
    if any("test" in path.lower() for path in file_paths):
        body.append("Updated tests to cover the revised suggestion handling.")
    return body[:6] or ["Updated files according to the commit-scoped diff evidence."]


def _description_from_diff(files: list[object]) -> str:
    file_payloads = [file for file in files if isinstance(file, dict)]
    if not file_payloads:
        return "update commit diff"
    first = file_payloads[0]
    status = str(first.get("status") or "modify").lower()
    path = str(first.get("path") or "files")
    verb = {
        "added": "add",
        "modified": "update",
        "removed": "remove",
        "deleted": "remove",
        "renamed": "rename",
    }.get(status, "update")
    return f"{verb} {path}"
