"""IntentumDiff-powered semantic evidence for Verlato.

Verlato's release-note pipeline is powered by the IntentumDiff semantic diff engine
(https://github.com/buchochelliq-labs/intentumdiff-python). For every collected commit
Verlato asks IntentumDiff to review ``parent..sha`` and keeps a compact, privacy-safe
digest of the result:

* review-level change groups (meaningful / refactoring / moved / ignored style / noise)
* definition-level symbol changes (added, removed, renamed, signature changed)
* cross-file changes (symbols moved or renamed across modules)
* protected-config guardrail violations
* files the engine could not review, with the engine's reason

Only structural metadata and definition names are kept. Source bodies, literal values and
IntentumDiff's raw change descriptions (which may embed literals) are never stored, so the
digest is safe to show in the editor and to place in model prompts under every Verlato
privacy mode.

The digest of each commit is cached by ``(parent, sha, engine version)`` because commits are
immutable: re-scanning a release range only reviews commits that have not been seen before.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from verlato.core.areas import is_test_path

DIFF_ENGINE_CHOICES = ("intentumdiff", "git")
DEFAULT_DIFF_ENGINE = "intentumdiff"
SEMANTIC_DIGEST_VERSION = "v1"
DEFAULT_SEMANTIC_CACHE_PATH = ".verlato_semantic_cache.json"
INTENTUMDIFF_INSTALL_HINT = "pip install --pre intentumdiff-python"

GROUP_KINDS = ("MEANINGFUL_CHANGE", "REFACTORING", "MOVED_CODE", "IGNORED_STYLE", "NOISE_SUPPRESSED")
MAX_SYMBOLS_PER_FILE = 24
MAX_EVIDENCE_SYMBOLS = 12
MAX_CROSS_FILE = 12
MAX_REASON_CHARS = 240
# Cross-file symbol indexing only makes sense for code; indexing large docs/data files
# costs parser fuel without producing symbol moves.
NON_CODE_LANGUAGES = {
    "markdown", "mdx", "asciidoc", "latex", "json", "yaml", "toml", "ini", "xml", "html", "css", "scss",
    "text", "plaintext", "po", "gitignore", "csv", "generic",
}


class SemanticEngineUnavailable(RuntimeError):
    """Raised when the configured semantic engine cannot be loaded."""


def normalize_diff_engine(value: object, *, default: str = DEFAULT_DIFF_ENGINE) -> str:
    normalized = str(value or "").strip().lower()
    aliases = {"intentum": "intentumdiff", "intentum-diff": "intentumdiff", "semantic": "intentumdiff",
               "legacy": "git", "text": "git", "patch": "git"}
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in DIFF_ENGINE_CHOICES else default


def resolve_diff_engine(configured: object = None, *, requested: object = None) -> str:
    env_value = os.getenv("VERLATO_DIFF_ENGINE")
    return normalize_diff_engine(requested or env_value or configured or DEFAULT_DIFF_ENGINE)


# ---------------------------------------------------------------------------
# Digest data model (JSON-serializable, cached on disk)
# ---------------------------------------------------------------------------


@dataclass
class SymbolChange:
    name: str
    change: str  # added | removed | renamed | signature
    file: str
    node_type: str = ""
    old_name: str | None = None
    refactoring_kind: str | None = None


@dataclass
class FileSemantics:
    path: str
    old_path: str
    language: str = ""
    lifecycle: str = "modified"
    group_counts: dict[str, int] = field(default_factory=dict)
    meaningful_change_types: dict[str, int] = field(default_factory=dict)
    style_only: bool = False
    has_semantic_changes: bool = False
    is_fallback: bool = False
    refactorings: list[str] = field(default_factory=list)
    symbols: list[SymbolChange] = field(default_factory=list)
    guardrails: list[dict[str, str]] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> FileSemantics:
        payload = dict(data)
        payload["symbols"] = [SymbolChange(**symbol) for symbol in payload.get("symbols", [])]
        return FileSemantics(**payload)


@dataclass
class UnreviewedFile:
    path: str
    reason: str
    kind: str = "unreviewed"  # no_parser | pipeline_error | root-commit | engine-error


@dataclass
class CommitSemantics:
    sha: str
    parent_sha: str | None
    engine: str = "intentumdiff"
    engine_version: str = ""
    digest_version: str = SEMANTIC_DIGEST_VERSION
    files: list[FileSemantics] = field(default_factory=list)
    unreviewed: list[UnreviewedFile] = field(default_factory=list)
    cross_file: list[dict[str, Any]] = field(default_factory=list)
    cross_file_error: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> CommitSemantics:
        payload = dict(data)
        payload["files"] = [FileSemantics.from_dict(item) for item in payload.get("files", [])]
        payload["unreviewed"] = [UnreviewedFile(**item) for item in payload.get("unreviewed", [])]
        return CommitSemantics(**payload)


# ---------------------------------------------------------------------------
# Group-level evidence (what the classifier, prompts and editor consume)
# ---------------------------------------------------------------------------


@dataclass
class SemanticEvidence:
    engine: str
    engine_version: str
    verdict: str  # style-only | refactor-only | additive | behavioral | unreviewed
    complete: bool
    files_reviewed: int
    files_unreviewed: int
    group_counts: dict[str, int]
    languages: list[str]
    style_only_files: list[str]
    refactor_only_files: list[str]
    meaningful_files: list[str]
    added_symbols: list[str]
    removed_symbols: list[str]
    renamed_symbols: list[str]
    signature_changes: list[str]
    refactorings: list[str]
    cross_file: list[dict[str, Any]]
    guardrails: list[dict[str, str]]
    unreviewed: list[dict[str, str]]
    errors: list[str]
    notes: list[str]
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def has_guardrails(self) -> bool:
        return bool(self.guardrails)

    @property
    def api_break_candidates(self) -> list[str]:
        """Public-looking symbols removed, renamed or re-signatured outside tests."""
        candidates: list[str] = []
        for entry in [*self.removed_symbols, *self.renamed_symbols, *self.signature_changes]:
            symbol, _, path = entry.partition(" in ")
            bare = symbol.split(" -> ")[-1].split(".")[-1].strip()
            if not bare or bare.startswith("_"):
                continue
            if path and is_test_path(path.lower()):
                continue
            candidates.append(entry)
        return candidates


def semantic_prompt_payload(evidence: SemanticEvidence | None) -> dict[str, Any] | None:
    """Compact, privacy-safe semantic block for model-facing evidence payloads."""

    if evidence is None:
        return None
    return {
        "engine": evidence.engine,
        "engine_version": evidence.engine_version,
        "verdict": evidence.verdict,
        "complete": evidence.complete,
        "group_counts": dict(evidence.group_counts),
        "added_symbols": [entry.split(" in ")[0] for entry in evidence.added_symbols],
        "removed_symbols": [entry.split(" in ")[0] for entry in evidence.removed_symbols],
        "renamed_symbols": [entry.split(" in ")[0] for entry in evidence.renamed_symbols],
        "signature_changes": [entry.split(" in ")[0] for entry in evidence.signature_changes],
        "refactorings": list(evidence.refactorings),
        "style_only_files": list(evidence.style_only_files),
        "guardrail_rules": sorted({str(item.get("rule_id")) for item in evidence.guardrails}),
        "summary": evidence.summary,
    }


def commit_semantic_payload(commit: Any) -> dict[str, Any] | None:
    """Semantic block for a single commit, or ``None`` when it was not reviewed."""

    if not isinstance(getattr(commit, "semantics", None), CommitSemantics):
        return None
    return semantic_prompt_payload(build_semantic_evidence([commit]))


def build_semantic_evidence(
    commits: Iterable[Any],
    *,
    paths: Iterable[str] | None = None,
) -> SemanticEvidence | None:
    """Aggregate per-commit IntentumDiff digests into one group-level evidence block.

    ``paths`` restricts aggregation to the files the change group owns (PR area/scope
    splits share commits but not files). Returns ``None`` when no commit carries a digest,
    i.e. the semantic engine was not used for this scan.
    """

    digests: list[CommitSemantics] = [
        digest
        for digest in (getattr(commit, "semantics", None) for commit in commits)
        if isinstance(digest, CommitSemantics)
    ]
    if not digests:
        return None
    path_filter = {path for path in paths} if paths is not None else None

    def owned(path: str, old_path: str = "") -> bool:
        return path_filter is None or path in path_filter or bool(old_path and old_path in path_filter)

    group_counts = {kind: 0 for kind in GROUP_KINDS}
    meaningful_types: dict[str, int] = {}
    reviewed: dict[str, FileSemantics] = {}
    unreviewed: dict[str, UnreviewedFile] = {}
    added: list[str] = []
    removed: list[str] = []
    renamed: list[str] = []
    signatures: list[str] = []
    refactorings: list[str] = []
    cross_file: list[dict[str, Any]] = []
    guardrails: list[dict[str, str]] = []
    errors: list[str] = []
    notes: list[str] = []
    file_meaningful: dict[str, int] = {}
    file_structural: dict[str, int] = {}
    file_style: dict[str, bool] = {}

    for digest in digests:
        if digest.error:
            errors.append(f"{digest.sha[:12]}: {digest.error}")
        if digest.cross_file_error:
            notes.append(f"{digest.sha[:12]}: cross-file analysis: {digest.cross_file_error}")
        for file in digest.files:
            if not owned(file.path, file.old_path):
                continue
            reviewed[file.path] = file
            unreviewed.pop(file.path, None)
            for kind, count in file.group_counts.items():
                group_counts[kind] = group_counts.get(kind, 0) + count
            for change_type, count in file.meaningful_change_types.items():
                meaningful_types[change_type] = meaningful_types.get(change_type, 0) + count
            file_meaningful[file.path] = file_meaningful.get(file.path, 0) + file.group_counts.get(
                "MEANINGFUL_CHANGE", 0
            )
            file_structural[file.path] = file_structural.get(file.path, 0) + file.group_counts.get(
                "REFACTORING", 0
            ) + file.group_counts.get("MOVED_CODE", 0)
            file_style[file.path] = file_style.get(file.path, True) and (
                file.style_only or not file.has_semantic_changes
            )
            for symbol in file.symbols:
                label = f"{symbol.name} in {symbol.file}"
                if symbol.change == "added":
                    _append_unique(added, label)
                elif symbol.change == "removed":
                    _append_unique(removed, label)
                elif symbol.change == "renamed":
                    _append_unique(renamed, f"{symbol.old_name or '?'} -> {symbol.name} in {symbol.file}")
                elif symbol.change == "signature":
                    _append_unique(signatures, label)
            for kind in file.refactorings:
                _append_unique(refactorings, kind)
            for violation in file.guardrails:
                guardrails.append({**violation, "file": violation.get("file") or file.path})
        for item in digest.unreviewed:
            if owned(item.path) and item.path not in reviewed:
                unreviewed[item.path] = item
        for change in digest.cross_file:
            if owned(str(change.get("old_file", ""))) or owned(str(change.get("new_file", ""))):
                if len(cross_file) < MAX_CROSS_FILE:
                    cross_file.append(change)

    # A symbol added then removed (or vice versa) inside the same group is churn, not a change.
    churn = set(added) & set(removed)
    added = [entry for entry in added if entry not in churn]
    removed = [entry for entry in removed if entry not in churn]

    meaningful_files = sorted(path for path, count in file_meaningful.items() if count > 0)
    refactor_only_files = sorted(
        path for path in reviewed if file_meaningful.get(path, 0) == 0 and file_structural.get(path, 0) > 0
    )
    style_only_files = sorted(
        path
        for path in reviewed
        if file_meaningful.get(path, 0) == 0 and file_structural.get(path, 0) == 0 and file_style.get(path, False)
    )
    verdict = _verdict(
        reviewed=len(reviewed),
        meaningful=group_counts.get("MEANINGFUL_CHANGE", 0),
        structural=group_counts.get("REFACTORING", 0) + group_counts.get("MOVED_CODE", 0) + len(cross_file),
        meaningful_types=meaningful_types,
        removed=removed,
        signatures=signatures,
    )
    languages = sorted({file.language for file in reviewed.values() if file.language})
    evidence = SemanticEvidence(
        engine="intentumdiff",
        engine_version=next((digest.engine_version for digest in digests if digest.engine_version), ""),
        verdict=verdict,
        complete=not unreviewed and not any(digest.error for digest in digests),
        files_reviewed=len(reviewed),
        files_unreviewed=len(unreviewed),
        group_counts={kind: count for kind, count in group_counts.items() if count},
        languages=languages,
        style_only_files=style_only_files,
        refactor_only_files=refactor_only_files,
        meaningful_files=meaningful_files,
        added_symbols=added[:MAX_EVIDENCE_SYMBOLS],
        removed_symbols=removed[:MAX_EVIDENCE_SYMBOLS],
        renamed_symbols=renamed[:MAX_EVIDENCE_SYMBOLS],
        signature_changes=signatures[:MAX_EVIDENCE_SYMBOLS],
        refactorings=refactorings,
        cross_file=cross_file,
        guardrails=guardrails,
        unreviewed=[{"path": item.path, "kind": item.kind, "reason": item.reason} for item in unreviewed.values()],
        errors=errors,
        notes=notes,
        summary="",
    )
    evidence.summary = render_semantic_summary(evidence)
    return evidence


def _verdict(
    *,
    reviewed: int,
    meaningful: int,
    structural: int,
    meaningful_types: dict[str, int],
    removed: list[str],
    signatures: list[str],
) -> str:
    if reviewed == 0:
        return "unreviewed"
    if meaningful == 0 and structural == 0:
        return "style-only"
    if meaningful == 0:
        return "refactor-only"
    non_additive = {kind: count for kind, count in meaningful_types.items() if kind != "ADDITION" and count}
    if not non_additive and not removed and not signatures:
        return "additive"
    return "behavioral"


VERDICT_TEXT = {
    "style-only": "formatting/style-only (no semantic change)",
    "refactor-only": "behavior-preserving refactoring/moves only",
    "additive": "additive changes only",
    "behavioral": "behavioral changes",
    "unreviewed": "not semantically reviewed",
}


def render_semantic_summary(evidence: SemanticEvidence) -> str:
    """Deterministic, privacy-safe one-paragraph rendering of the evidence."""

    parts = [
        f"IntentumDiff semantic review: {VERDICT_TEXT.get(evidence.verdict, evidence.verdict)}",
        f"{evidence.files_reviewed} file(s) reviewed" + (
            f", {evidence.files_unreviewed} unreviewed" if evidence.files_unreviewed else ""
        ),
    ]
    counts = evidence.group_counts
    group_text = ", ".join(
        f"{_GROUP_LABELS[kind]}:{counts[kind]}" for kind in GROUP_KINDS if counts.get(kind)
    )
    if group_text:
        parts.append(f"groups: {group_text}")
    if evidence.added_symbols:
        parts.append(f"added: {_symbol_list(evidence.added_symbols)}")
    if evidence.removed_symbols:
        parts.append(f"removed: {_symbol_list(evidence.removed_symbols)}")
    if evidence.renamed_symbols:
        parts.append(f"renamed: {_symbol_list(evidence.renamed_symbols)}")
    if evidence.signature_changes:
        parts.append(f"signature changed: {_symbol_list(evidence.signature_changes)}")
    if evidence.cross_file:
        moved = [
            f"{change.get('symbol_name')} ({change.get('change_type', '').lower().replace('_', ' ')})"
            for change in evidence.cross_file[:4]
        ]
        parts.append(f"cross-file: {', '.join(moved)}")
    if evidence.style_only_files:
        parts.append(f"style-only files: {', '.join(evidence.style_only_files[:3])}")
    if evidence.guardrails:
        rules = sorted({f"{item.get('rule_id')} ({item.get('severity')})" for item in evidence.guardrails})
        parts.append(f"guardrails: {', '.join(rules[:3])}")
    return "; ".join(parts)


_GROUP_LABELS = {
    "MEANINGFUL_CHANGE": "meaningful",
    "REFACTORING": "refactoring",
    "MOVED_CODE": "moved",
    "IGNORED_STYLE": "style",
    "NOISE_SUPPRESSED": "noise",
}


def _symbol_list(entries: list[str], limit: int = 5) -> str:
    names = [entry.split(" in ")[0] for entry in entries[:limit]]
    text = ", ".join(names)
    if len(entries) > limit:
        text += f" (+{len(entries) - limit} more)"
    return text


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


# ---------------------------------------------------------------------------
# Digest extraction from IntentumDiff results
# ---------------------------------------------------------------------------


def _enum_value(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw) if raw is not None else ""


def _node_name(node: Any) -> str:
    return str(getattr(node, "label", "") or "").strip()


_IDENTIFIER = re.compile(r"^[A-Za-z_$@][\w$@.:\-]{0,119}$")


def _is_definition(node: Any) -> bool:
    # IntentumDiff attaches privacy-safe NodeFacts only to definition nodes
    # (functions, methods, classes, keyed resources). Data-format nodes whose label is
    # just their node type or an index (``pair``, ``[5]``) are not named symbols.
    if node is None or getattr(node, "facts", None) is None:
        return False
    name = _node_name(node)
    return bool(_IDENTIFIER.match(name)) and name != str(getattr(node, "node_type", ""))


def _looks_like_parameter(node: Any) -> bool:
    node_type = str(getattr(node, "node_type", "") or "").lower()
    return "parameter" in node_type


_CHANGE_TYPE_GROUP = {
    "MOVE": "MOVED_CODE",
    "MOVE_TO_MODULE": "MOVED_CODE",
    "REFACTORING": "REFACTORING",
    "CROSS_FILE_RENAME": "REFACTORING",
    "SPLIT_MODULE": "REFACTORING",
    "STYLE_ONLY": "IGNORED_STYLE",
    "REORDER": "NOISE_SUPPRESSED",
}


def digest_file_diff(file_diff: Any, *, path: str, old_path: str) -> FileSemantics:
    """Reduce an IntentumDiff ``SemanticDiff`` to Verlato's privacy-safe digest."""

    changes = list(getattr(file_diff, "changes", []) or [])
    groups = list(getattr(file_diff, "change_groups", []) or [])
    group_counts: dict[str, int] = {}
    meaningful_types: dict[str, int] = {}
    refactorings: list[str] = []
    symbols: list[SymbolChange] = []
    seen_symbols: set[tuple[str, str]] = set()

    def add_symbol(symbol: SymbolChange) -> None:
        key = (symbol.change, symbol.name if symbol.change != "renamed" else f"{symbol.old_name}->{symbol.name}")
        if key in seen_symbols or len(symbols) >= MAX_SYMBOLS_PER_FILE:
            return
        seen_symbols.add(key)
        symbols.append(symbol)

    for group in groups:
        kind = _enum_value(getattr(group, "kind", ""))
        group_counts[kind] = group_counts.get(kind, 0) + 1
        group_refactoring = _enum_value(getattr(group, "refactoring_kind", None))
        if group_refactoring:
            _append_unique(refactorings, group_refactoring)
        if kind != "MEANINGFUL_CHANGE":
            continue
        signature_symbol: str | None = None
        for index in getattr(group, "raw_change_indices", []) or []:
            if not isinstance(index, int) or not 0 <= index < len(changes):
                continue
            change = changes[index]
            change_type = _enum_value(getattr(change, "change_type", ""))
            meaningful_types[change_type] = meaningful_types.get(change_type, 0) + 1
            node = getattr(change, "new_node", None) or getattr(change, "old_node", None)
            if _looks_like_parameter(node):
                labels = list(getattr(group, "new_labels", []) or getattr(group, "old_labels", []) or [])
                if labels and labels[0]:
                    signature_symbol = str(labels[0])
        if signature_symbol:
            add_symbol(SymbolChange(name=signature_symbol, change="signature", file=path))

    if not groups and changes:
        # Whole-file additions/deletions (and some fallback diffs) carry raw changes without
        # review-level groups; derive the group counts from the change types instead.
        for change in changes:
            change_type = _enum_value(getattr(change, "change_type", ""))
            kind = _CHANGE_TYPE_GROUP.get(change_type, "MEANINGFUL_CHANGE")
            group_counts[kind] = group_counts.get(kind, 0) + 1
            if kind == "MEANINGFUL_CHANGE":
                meaningful_types[change_type] = meaningful_types.get(change_type, 0) + 1

    for change in changes:
        change_type = _enum_value(getattr(change, "change_type", ""))
        old_node: Any = getattr(change, "old_node", None)
        new_node: Any = getattr(change, "new_node", None)
        refactoring_kind: str | None = _enum_value(getattr(change, "refactoring_kind", None)) or None
        if change_type == "ADDITION" and _is_definition(new_node):
            add_symbol(SymbolChange(name=_node_name(new_node), change="added", file=path,
                                    node_type=str(new_node.node_type)))
        elif change_type == "DELETION" and _is_definition(old_node):
            add_symbol(SymbolChange(name=_node_name(old_node), change="removed", file=path,
                                    node_type=str(old_node.node_type)))
        elif (
            refactoring_kind
            and refactoring_kind.startswith("RENAME")
            and old_node is not None
            and new_node is not None
        ):
            if _node_name(old_node) != _node_name(new_node):
                add_symbol(SymbolChange(name=_node_name(new_node), old_name=_node_name(old_node), change="renamed",
                                        file=path, node_type=str(new_node.node_type),
                                        refactoring_kind=refactoring_kind))
        elif refactoring_kind == "CHANGE_SIGNATURE" and _is_definition(new_node):
            add_symbol(SymbolChange(name=_node_name(new_node), change="signature", file=path,
                                    node_type=str(new_node.node_type), refactoring_kind=refactoring_kind))
        elif (
            change_type == "MODIFICATION"
            and _is_definition(old_node)
            and _is_definition(new_node)
            and old_node.facts.param_count is not None
            and new_node.facts.param_count is not None
            and old_node.facts.param_count != new_node.facts.param_count
        ):
            add_symbol(SymbolChange(name=_node_name(new_node), change="signature", file=path,
                                    node_type=str(new_node.node_type)))

    guardrails = [
        {
            "rule_id": str(getattr(violation, "rule_id", "")),
            "severity": _enum_value(getattr(violation, "severity", "")),
            "file": str(getattr(violation, "file", "") or path),
            "semantic_path": str(getattr(violation, "semantic_path", "")),
            "message": str(getattr(violation, "message", ""))[:MAX_REASON_CHARS],
        }
        for violation in getattr(file_diff, "guardrail_violations", []) or []
    ]
    metadata = dict(getattr(file_diff, "metadata", {}) or {})
    return FileSemantics(
        path=path,
        old_path=old_path,
        language=str(getattr(file_diff, "language", "") or ""),
        lifecycle=str(metadata.get("file_lifecycle") or "modified"),
        group_counts=group_counts,
        meaningful_change_types=meaningful_types,
        style_only=bool(getattr(file_diff, "is_style_only", False)),
        has_semantic_changes=bool(getattr(file_diff, "has_semantic_changes", False)),
        is_fallback=bool(getattr(file_diff, "is_fallback", False)),
        refactorings=refactorings,
        symbols=symbols,
        guardrails=guardrails,
        parse_errors=[str(error)[:MAX_REASON_CHARS] for error in getattr(file_diff, "parse_errors", []) or []][:5],
    )


def digest_cross_file_change(change: Any) -> dict[str, Any]:
    return {
        "change_type": _enum_value(getattr(change, "change_type", "")),
        "symbol_name": str(getattr(change, "symbol_name", "")),
        "symbol_kind": str(getattr(change, "symbol_kind", "") or getattr(change, "node_type", "")),
        "old_file": str(getattr(change, "old_file", "")),
        "new_file": str(getattr(change, "new_file", "")),
        "confidence": float(getattr(change, "confidence", 1.0) or 0.0),
    }


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


class SemanticCache:
    """Small JSON cache of commit digests keyed by ``parent..sha@engine-version``."""

    def __init__(self, path: str | os.PathLike[str] | None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] | None = None
        self._dirty = False

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._entries is not None:
            return self._entries
        entries: dict[str, dict[str, Any]] = {}
        if self.path and self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and raw.get("digest_version") == SEMANTIC_DIGEST_VERSION:
                    stored = raw.get("entries")
                    if isinstance(stored, dict):
                        entries = {str(key): value for key, value in stored.items() if isinstance(value, dict)}
            except (OSError, json.JSONDecodeError):
                entries = {}
        self._entries = entries
        return entries

    def get(self, key: str) -> CommitSemantics | None:
        with self._lock:
            payload = self._load().get(key)
        if payload is None:
            return None
        try:
            return CommitSemantics.from_dict(payload)
        except (TypeError, KeyError, ValueError):
            return None

    def put(self, key: str, digest: CommitSemantics) -> None:
        with self._lock:
            self._load()[key] = digest.to_dict()
            self._dirty = True

    def flush(self) -> None:
        with self._lock:
            if not self.path or not self._dirty or self._entries is None:
                return
            payload = {"digest_version": SEMANTIC_DIGEST_VERSION, "entries": self._entries}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=".verlato-semantic-", dir=str(self.path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, sort_keys=True)
                os.chmod(tmp_name, 0o644)
                os.replace(tmp_name, self.path)
            except OSError:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                return
            self._dirty = False


ProgressCallback = Callable[[int, int, str], None]


class IntentumDiffEngine:
    """Runs IntentumDiff commit reviews and produces cached ``CommitSemantics`` digests."""

    name = "intentumdiff"

    def __init__(
        self,
        repo_path: str | os.PathLike[str] = ".",
        *,
        cache: SemanticCache | None = None,
        cross_file: bool = False,
        parallelism: int = 1,
        differ_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.repo_path = str(repo_path)
        self.cache = cache
        self.cross_file = cross_file
        self.parallelism = max(1, min(int(parallelism), 16))
        self._differ_factory = differ_factory
        self._differ: Any = None
        self._version: str | None = None

    # -- availability -----------------------------------------------------

    @staticmethod
    def installed_version() -> str | None:
        try:
            from importlib import metadata

            return metadata.version("intentumdiff-python")
        except Exception:  # noqa: BLE001 - metadata lookup is best-effort
            return None

    @property
    def version(self) -> str:
        if self._version is None:
            self._version = self.installed_version() or ("custom" if self._differ_factory else "")
        return self._version

    def _commit_differ(self) -> Any:
        if self._differ is not None:
            return self._differ
        if self._differ_factory is not None:
            self._differ = self._differ_factory()
            return self._differ
        try:
            from intentumdiff import CommitDiffer
        except Exception as exc:  # noqa: BLE001 - surface any import failure the same way
            raise SemanticEngineUnavailable(
                "Verlato is powered by the IntentumDiff semantic engine, but it could not be loaded "
                f"({exc}). Install it with `{INTENTUMDIFF_INSTALL_HINT}` (Python 3.12+), or set "
                "diff_engine to \"git\" (VERLATO_DIFF_ENGINE=git) to use legacy text-patch evidence."
            ) from exc
        self._differ = CommitDiffer()
        return self._differ

    def ensure_available(self) -> None:
        self._commit_differ()

    def info(self) -> dict[str, Any]:
        try:
            self.ensure_available()
            return {"engine": self.name, "available": True, "version": self.version, "error": None}
        except SemanticEngineUnavailable as exc:
            return {"engine": self.name, "available": False, "version": self.installed_version(), "error": str(exc)}

    # -- review -------------------------------------------------------------

    def _cache_key(self, parent_sha: str | None, sha: str) -> str:
        scope = "+xf" if self.cross_file else ""
        return f"{parent_sha or 'root'}..{sha}@{self.version}{scope}"

    def commit_semantics(self, sha: str, parent_sha: str | None, *, files: Iterable[Any] = ()) -> CommitSemantics:
        key = self._cache_key(parent_sha, sha)
        if self.cache is not None:
            cached = self.cache.get(key)
            if cached is not None:
                return cached
        digest = self._review(sha, parent_sha, files=list(files))
        if self.cache is not None and digest.error is None:
            self.cache.put(key, digest)
        return digest

    def _review(self, sha: str, parent_sha: str | None, *, files: list[Any]) -> CommitSemantics:
        file_paths = [str(getattr(file, "path", file)) for file in files]
        differ = self._commit_differ() if parent_sha else None
        return review_commit(
            differ,
            self.repo_path,
            sha,
            parent_sha,
            file_paths=file_paths,
            cross_file=self.cross_file,
            engine_version=self.version,
        )

    def annotate(self, commits: list[Any], *, progress: ProgressCallback | None = None) -> None:
        """Attach ``commit.semantics`` digests to collected commits.

        Cached commits are resolved immediately. Uncached commits are reviewed in a pool of
        worker processes when ``parallelism > 1`` (IntentumDiff's Wasm parsers scale far
        better across processes than threads), otherwise in-process.
        """

        self.ensure_available()
        total = len(commits)
        pending: list[tuple[int, Any]] = []
        done = 0
        try:
            for index, commit in enumerate(commits):
                cached = self.cache.get(self._cache_key(commit.parent_sha, commit.sha)) if self.cache else None
                if cached is not None:
                    commit.semantics = cached
                    done += 1
                    if progress is not None:
                        progress(done, total, commit.sha)
                else:
                    pending.append((index, commit))

            workers = min(self.parallelism, len([c for _, c in pending if c.parent_sha]))
            if workers > 1 and self._differ_factory is None:
                done = self._annotate_parallel(pending, workers=workers, done=done, total=total, progress=progress)
            else:
                for _, commit in pending:
                    commit.semantics = self.commit_semantics(
                        commit.sha, commit.parent_sha, files=getattr(commit, "files", [])
                    )
                    done += 1
                    if progress is not None:
                        progress(done, total, commit.sha)
        finally:
            if self.cache is not None:
                self.cache.flush()

    def _annotate_parallel(
        self,
        pending: list[tuple[int, Any]],
        *,
        workers: int,
        done: int,
        total: int,
        progress: ProgressCallback | None,
    ) -> int:
        import multiprocessing
        from concurrent.futures import ProcessPoolExecutor, as_completed

        context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
            futures = {
                pool.submit(
                    _review_commit_in_worker,
                    self.repo_path,
                    commit.sha,
                    commit.parent_sha,
                    [str(getattr(file, "path", file)) for file in getattr(commit, "files", [])],
                    self.cross_file,
                    self.version,
                ): commit
                for _, commit in pending
            }
            for future in as_completed(futures):
                commit = futures[future]
                try:
                    digest = CommitSemantics.from_dict(future.result())
                except Exception as exc:  # noqa: BLE001 - a crashed worker degrades one commit only
                    digest = CommitSemantics(
                        sha=commit.sha,
                        parent_sha=commit.parent_sha,
                        engine_version=self.version,
                        error=f"{type(exc).__name__}: {exc}"[:MAX_REASON_CHARS],
                    )
                commit.semantics = digest
                if self.cache is not None and digest.error is None:
                    self.cache.put(self._cache_key(commit.parent_sha, commit.sha), digest)
                done += 1
                if progress is not None:
                    progress(done, total, commit.sha)
        return done


def review_commit(
    differ: Any,
    repo_path: str,
    sha: str,
    parent_sha: str | None,
    *,
    file_paths: list[str],
    cross_file: bool,
    engine_version: str,
) -> CommitSemantics:
    """Review one commit (``parent_sha..sha``) with an IntentumDiff ``CommitDiffer``."""

    digest = CommitSemantics(sha=sha, parent_sha=parent_sha, engine="intentumdiff", engine_version=engine_version)
    if not parent_sha:
        # The root commit has no parent revision for IntentumDiff to compare against;
        # its files are reported as unreviewed additions rather than guessed at.
        digest.unreviewed = [
            UnreviewedFile(path=path, kind="root-commit", reason="Root commit: no parent revision to diff against.")
            for path in file_paths
        ]
        return digest

    results: list[Any] = []
    try:
        for result in differ.iter_file_diffs(repo_path, parent_sha, sha):
            file_diff = getattr(result, "file_diff", None)
            if file_diff is None:
                digest.unreviewed.append(
                    UnreviewedFile(
                        path=str(getattr(result, "new_path", "") or getattr(result, "old_path", "")),
                        kind=str(getattr(result, "kind", "pipeline_error")),
                        reason=str(getattr(result, "reason", ""))[:MAX_REASON_CHARS],
                    )
                )
                continue
            new_path = str(getattr(result, "new_path", "") or getattr(file_diff, "new_filename", ""))
            old_path = str(getattr(result, "old_path", "") or getattr(file_diff, "old_filename", new_path))
            digest.files.append(digest_file_diff(file_diff, path=new_path, old_path=old_path))
            if (
                str(getattr(file_diff, "language", "")).lower() not in NON_CODE_LANGUAGES
                and not is_test_path(new_path.lower())
            ):
                results.append(result)
    except Exception as exc:  # noqa: BLE001 - one bad commit must not abort the scan
        digest.error = f"{type(exc).__name__}: {exc}"[:MAX_REASON_CHARS]
        return digest

    if cross_file:
        _attach_cross_file(differ, digest, results, parent_sha=parent_sha, sha=sha)
    return digest


_INDEX_FAILURE = re.compile(r"index incomplete for '([^']+)'")
MAX_CROSS_FILE_EXCLUSIONS = 3


def _attach_cross_file(differ: Any, digest: CommitSemantics, results: list[Any], *, parent_sha: str, sha: str) -> None:
    """Best-effort cross-file analysis.

    IntentumDiff builds a symbol index over every result; a single file that exceeds the
    parser fuel budget fails the whole index. Such files are dropped (and reported) so the
    rest of the commit still gets cross-file move/rename detection.
    """

    excluded: list[str] = []
    candidates = list(results)
    while len(candidates) > 1:
        try:
            commit_diff = differ.finalize_commit_diff(candidates, [], old_ref=parent_sha, new_ref=sha)
        except Exception as exc:  # noqa: BLE001 - cross-file analysis is best-effort
            match = _INDEX_FAILURE.search(str(exc))
            failed = match.group(1) if match else None
            remaining = [item for item in candidates if failed and getattr(item, "new_path", None) != failed
                         and getattr(item, "old_path", None) != failed]
            if failed and len(remaining) < len(candidates) and len(excluded) < MAX_CROSS_FILE_EXCLUSIONS:
                excluded.append(failed)
                candidates = remaining
                continue
            digest.cross_file_error = str(exc)[:MAX_REASON_CHARS]
            break
        digest.cross_file = [
            digest_cross_file_change(change)
            for change in list(getattr(commit_diff, "cross_file_changes", []) or [])[:MAX_CROSS_FILE]
        ]
        break
    if excluded and digest.cross_file_error is None:
        digest.cross_file_error = (
            "excluded from cross-file index (parser fuel budget exceeded): " + ", ".join(excluded)
        )[:MAX_REASON_CHARS]


_WORKER_DIFFER: Any = None


def _review_commit_in_worker(
    repo_path: str,
    sha: str,
    parent_sha: str | None,
    file_paths: list[str],
    cross_file: bool,
    engine_version: str,
) -> dict[str, Any]:
    global _WORKER_DIFFER
    differ = None
    if parent_sha:
        if _WORKER_DIFFER is None:
            from intentumdiff import CommitDiffer

            _WORKER_DIFFER = CommitDiffer()
        differ = _WORKER_DIFFER
    return review_commit(
        differ,
        repo_path,
        sha,
        parent_sha,
        file_paths=file_paths,
        cross_file=cross_file,
        engine_version=engine_version,
    ).to_dict()


def build_semantic_engine(
    engine: str,
    *,
    repo_path: str | os.PathLike[str] = ".",
    cache_path: str | None = DEFAULT_SEMANTIC_CACHE_PATH,
    parallelism: int | None = None,
    cross_file: bool | None = None,
) -> IntentumDiffEngine | None:
    """Return the configured semantic engine, or ``None`` for the legacy ``git`` engine."""

    if normalize_diff_engine(engine) == "git":
        return None
    resolved_cache = os.getenv("VERLATO_SEMANTIC_CACHE_PATH", cache_path or "")
    cache = SemanticCache(resolved_cache) if resolved_cache and resolved_cache.lower() != "off" else None
    env_cross_file = os.getenv("VERLATO_SEMANTIC_CROSS_FILE")
    if env_cross_file is not None and env_cross_file.strip():
        cross_file = env_cross_file.strip().lower() in {"1", "true", "yes", "on"}
    return IntentumDiffEngine(
        repo_path,
        cache=cache,
        parallelism=resolve_semantic_parallelism(parallelism),
        cross_file=bool(cross_file),
    )


def resolve_semantic_parallelism(configured: int | None = None) -> int:
    """Worker processes for uncached commit reviews (env ``VERLATO_SEMANTIC_PARALLELISM``)."""

    raw = os.getenv("VERLATO_SEMANTIC_PARALLELISM")
    value: int | None = None
    if raw and raw.strip():
        try:
            value = int(raw)
        except ValueError:
            value = None
    if value is None:
        value = configured
    if value is None or value <= 0:
        value = min(4, os.cpu_count() or 1)
    return max(1, min(value, 16))


def semantic_engine_info(engine: str) -> dict[str, Any]:
    resolved = normalize_diff_engine(engine)
    if resolved == "git":
        return {
            "engine": "git",
            "available": True,
            "version": None,
            "error": None,
            "intentumdiff_version": IntentumDiffEngine.installed_version(),
        }
    info = IntentumDiffEngine(".").info()
    info["intentumdiff_version"] = info.get("version")
    return info
