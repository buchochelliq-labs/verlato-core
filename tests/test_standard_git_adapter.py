from __future__ import annotations

from subprocess import CompletedProcess
from unittest.mock import patch

from releasepilot.core.adapters import StandardGitAdapter


def test_standard_git_adapter_extracts_pr_and_files():
    outputs = [
        CompletedProcess(args=[], returncode=0, stdout="feat: add search (#123)\n", stderr=""),
        CompletedProcess(args=[], returncode=0, stdout="M\tsrc/search.py\nA\tdocs/search.md\n", stderr=""),
    ]

    with patch("subprocess.run", side_effect=outputs):
        pr = StandardGitAdapter().find_pr_for_commit("abc")

    assert pr is not None
    assert pr.number == 123
    assert pr.title == "feat: add search (#123)"
    assert [f.path for f in pr.files] == ["src/search.py", "docs/search.md"]


def test_standard_git_adapter_returns_none_without_pr_number():
    outputs = [CompletedProcess(args=[], returncode=0, stdout="chore: update deps\n", stderr="")]

    with patch("subprocess.run", side_effect=outputs):
        pr = StandardGitAdapter().find_pr_for_commit("abc")

    assert pr is None
