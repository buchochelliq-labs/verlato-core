import pytest


@pytest.fixture(autouse=True)
def _isolated_semantic_cache(monkeypatch, tmp_path):
    """Keep IntentumDiff commit digests out of the working tree during tests."""
    monkeypatch.setenv("VERLATO_SEMANTIC_CACHE_PATH", str(tmp_path / "semantic-cache.json"))
    monkeypatch.delenv("VERLATO_DIFF_ENGINE", raising=False)
    monkeypatch.setenv("VERLATO_SEMANTIC_PARALLELISM", "1")
