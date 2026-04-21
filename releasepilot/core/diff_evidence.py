from __future__ import annotations

from collections import defaultdict

from releasepilot.core.models import ChangedFile, DiffEvidence


class DiffEvidenceBuilder:
    AREA_RULES = {
        "api": ["api/", "openapi", "schema", "graphql"],
        "frontend": ["web/", "ui/", "frontend/"],
        "backend": ["server/", "backend/", "service/", "src/"],
        "docs": ["docs/", "README", ".md"],
        "tests": ["test/", "tests/", "spec/"],
        "infra": ["terraform", "k8s", "helm", ".github/"],
    }

    def build(self, files: list[ChangedFile]) -> DiffEvidence:
        files_by_area: dict[str, list[str]] = defaultdict(list)
        api_changes: list[str] = []
        migration_changes: list[str] = []
        config_changes: list[str] = []
        representative_hunks: dict[str, list[str]] = {}

        for file in files:
            area = self._infer_area(file.path)
            files_by_area[area].append(file.path)

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
        docs_only = bool(all_paths) and all(self._is_docs_path(p) for p in all_paths)
        tests_only = bool(all_paths) and all(self._is_test_path(p) for p in all_paths)
        summary = self._build_summary(files_by_area, api_changes, migration_changes, config_changes)

        return DiffEvidence(
            files_by_area=dict(files_by_area),
            api_changes=api_changes,
            migration_changes=migration_changes,
            config_changes=config_changes,
            docs_only=docs_only,
            tests_only=tests_only,
            representative_hunks=representative_hunks,
            summary=summary,
        )

    def _infer_area(self, path: str) -> str:
        lower = path.lower()
        for area, signals in self.AREA_RULES.items():
            if any(signal in lower for signal in signals):
                return area
        return "general"

    def _is_docs_path(self, path: str) -> bool:
        return path.endswith(".md") or "docs/" in path

    def _is_test_path(self, path: str) -> bool:
        return "test" in path or "spec" in path

    def _extract_representative_hunks(self, patch: str, max_hunks: int = 2) -> list[str]:
        if not patch:
            return []
        blocks = [b.strip() for b in patch.split("@@") if b.strip()]
        return [block[:240] for block in blocks[:max_hunks]]

    def _build_summary(
        self,
        files_by_area: dict[str, list[str]],
        api_changes: list[str],
        migration_changes: list[str],
        config_changes: list[str],
    ) -> str:
        parts = [f"areas: {', '.join(sorted(files_by_area))}" if files_by_area else "areas: none"]
        if api_changes:
            parts.append(f"public-api: {len(api_changes)} file(s)")
        if migration_changes:
            parts.append(f"migrations: {len(migration_changes)}")
        if config_changes:
            parts.append(f"config: {len(config_changes)}")
        return "; ".join(parts)
