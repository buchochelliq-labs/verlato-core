from __future__ import annotations

import json
from unittest.mock import patch

from verlato.github.adapter import GitHubApiAdapter


class _FakeResp:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def test_find_pr_for_commit_enriches_metadata_and_files():
    responses = [
        [{"number": 42}],
        {
            "title": "Improve auth",
            "body": "Details",
            "labels": [{"name": "feature"}],
            "base": {"ref": "main"},
            "head": {"ref": "feature/auth"},
        },
        [{"filename": "api/auth.py", "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b"}],
    ]

    def fake_urlopen(_request):
        return _FakeResp(responses.pop(0))

    adapter = GitHubApiAdapter(repo="org/repo", token="t")
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        pr = adapter.find_pr_for_commit("abc123")

    assert pr is not None
    assert pr.number == 42
    assert pr.title == "Improve auth"
    assert pr.labels == ["feature"]
    assert pr.base_ref == "main"
    assert pr.head_ref == "feature/auth"
    assert len(pr.files) == 1
    assert pr.files[0].path == "api/auth.py"


def test_github_adapter_paginates_file_results():
    first_page = [{"filename": f"src/file_{idx}.py", "status": "modified", "patch": ""} for idx in range(100)]
    responses = [
        [{"number": 7}],
        {"title": "Big PR", "labels": [], "base": {"ref": "main"}, "head": {"ref": "feat/huge"}},
        first_page,
        [{"filename": "src/b.py", "status": "modified", "patch": ""}],
        [],
    ]

    def fake_urlopen(_request):
        return _FakeResp(responses.pop(0))

    adapter = GitHubApiAdapter(repo="org/repo", token="t")
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        pr = adapter.find_pr_for_commit("abc123")

    assert pr is not None
    assert len(pr.files) == 101
    assert pr.files[-1].path == "src/b.py"


def test_github_adapter_caches_pr_details_across_commits():
    responses = [
        [{"number": 42}],
        {"title": "Shared PR", "labels": [], "base": {"ref": "main"}, "head": {"ref": "feat/shared"}},
        [{"filename": "src/a.py", "status": "modified", "patch": ""}],
        [{"number": 42}],
    ]
    requests: list[object] = []

    def fake_urlopen(request):
        requests.append(request)
        return _FakeResp(responses.pop(0))

    adapter = GitHubApiAdapter(repo="org/repo", token="t")
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        first = adapter.find_pr_for_commit("abc123")
        second = adapter.find_pr_for_commit("def456")

    assert first is not None
    assert second is not None
    assert first is second
    assert len(requests) == 4
