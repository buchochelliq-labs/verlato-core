from __future__ import annotations

AREA_RULES = {
    "api": ["api/", "openapi", "schema", "graphql"],
    "frontend": ["web/", "ui/", "frontend/"],
    "backend": ["server/", "backend/", "service/", "src/"],
    "docs": ["docs/", "readme", ".md"],
    "tests": ["test/", "tests/", "spec/"],
    "infra": ["terraform", "k8s", "helm", ".github/"],
}


def infer_area(path: str) -> str:
    lower = path.lower()
    for area, signals in AREA_RULES.items():
        if any(signal in lower for signal in signals):
            return area
    return "general"


def is_docs_path(path: str) -> bool:
    lower = path.lower()
    return lower.endswith(".md") or "docs/" in lower


def is_test_path(path: str) -> bool:
    lower = path.lower()
    return "test" in lower or "spec" in lower
