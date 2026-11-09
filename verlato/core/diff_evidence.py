from __future__ import annotations

from collections import defaultdict

from verlato.core.areas import infer_area, is_docs_path, is_test_path
from verlato.core.models import ChangedFile, DiffEvidence
from verlato.core.semantic import build_semantic_evidence


class DiffEvidenceBuilder:
    def build(self, files: list[ChangedFile], commits: list | None = None) -> DiffEvidence:
        """Build change evidence for a group of changed files.

        When ``commits`` carry IntentumDiff digests (see ``verlato.core.semantic``) the
        evidence is led by the semantic review, restricted to these files; path/status
        signals remain as supporting context.
        """
        files_by_area: dict[str, list[str]] = defaultdict(list)
        file_statuses: dict[str, str] = {}
        api_changes: list[str] = []
        migration_changes: list[str] = []
        config_changes: list[str] = []
        representative_hunks: dict[str, list[str]] = {}

        for file in files:
            area = self.infer_area(file.path)
            files_by_area[area].append(file.path)
            file_statuses[file.path] = file.status

            path_lower = file.path.lower()
            if any(k in path_lower for k in ["public", "api", "interface", "sdk"]):
                api_changes.append(file.path)
            if any(k in path_lower for k in ["migration", "migrations", "alembic"]):
                migration_changes.append(file.path)
            if any(k in path_lower for k in ["config", ".env", "settings", "yaml", "yml", "toml"]):
                config_changes.append(file.path)

            hunks = self._extract_representative_hunks(file.patch)
            if hunks:
                representative_hunks[file.path] = hunks

        all_paths = [f.path.lower() for f in files]
        docs_only = bool(all_paths) and all(is_docs_path(p) for p in all_paths)
        tests_only = bool(all_paths) and all(is_test_path(p) for p in all_paths)
        summary = self._build_summary(
            files_by_area,
            file_statuses,
            api_changes,
            migration_changes,
            config_changes,
            representative_hunks,
        )
        semantic = build_semantic_evidence(commits or [], paths=[f.path for f in files]) if commits else None
        if semantic is not None:
            summary = f"{semantic.summary}; {summary}"

        return DiffEvidence(
            files_by_area=dict(files_by_area),
            file_statuses=file_statuses,
            api_changes=api_changes,
            migration_changes=migration_changes,
            config_changes=config_changes,
            docs_only=docs_only,
            tests_only=tests_only,
            representative_hunks=representative_hunks,
            summary=summary,
            semantic=semantic,
        )

    @staticmethod
    def infer_area(path: str) -> str:
        return infer_area(path)

    def _extract_representative_hunks(self, patch: str, max_hunks: int = 2) -> list[str]:
        if not patch:
            return []
        hunks: list[str] = []
        current: list[str] = []
        for line in patch.splitlines():
            if line.startswith("@@"):
                if current:
                    hunks.append("\n".join(current).strip())
                current = [line]
                continue
            if not current:
                continue
            if line.startswith(("+++", "---")):
                continue
            if line.startswith(("+", "-")) and line.strip() not in {"+", "-"}:
                current.append(line[:160])

        if current:
            hunks.append("\n".join(current).strip())
        return [hunk[:500] for hunk in hunks if hunk][:max_hunks]

    def _build_summary(
        self,
        files_by_area: dict[str, list[str]],
        file_statuses: dict[str, str],
        api_changes: list[str],
        migration_changes: list[str],
        config_changes: list[str],
        representative_hunks: dict[str, list[str]],
    ) -> str:
        if not file_statuses:
            return "No changed-file evidence was available from PR or commit metadata"

        status_counts: dict[str, int] = defaultdict(int)
        for status in file_statuses.values():
            status_counts[status] += 1

        changed_files = ", ".join(
            f"{status} {path}" for path, status in sorted(file_statuses.items())[:5]
        )
        if len(file_statuses) > 5:
            changed_files += f", and {len(file_statuses) - 5} more"

        status_text = ", ".join(f"{status}:{count}" for status, count in sorted(status_counts.items()))
        area_text = ", ".join(
            f"{area}({len(paths)})" for area, paths in sorted(files_by_area.items())
        )
        parts = [f"changed files: {changed_files}", f"statuses: {status_text}", f"areas: {area_text}"]
        if api_changes:
            parts.append(f"public-api: {', '.join(api_changes[:3])}")
        if migration_changes:
            parts.append(f"migrations: {', '.join(migration_changes[:3])}")
        if config_changes:
            parts.append(f"config: {', '.join(config_changes[:3])}")
        hunk_paths = list(representative_hunks)[:3]
        if hunk_paths:
            parts.append(f"diff excerpts: {', '.join(hunk_paths)}")
        return "; ".join(parts)
