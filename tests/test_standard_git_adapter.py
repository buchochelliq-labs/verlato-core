from __future__ import annotations

from subprocess import CompletedProcess
from unittest.mock import patch

from verlato.core.adapters import StandardGitAdapter


def test_standard_git_adapter_extracts_pr_and_files():
    outputs = [
        CompletedProcess(args=[], returncode=0, stdout="feat: add search (#123)\n", stderr=""),
        CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "M\tsrc/search.py\nA\tdocs/search.md\n"
                "diff --git a/src/search.py b/src/search.py\n"
                "@@ -1 +1 @@\n-old\n+new\n"
                "diff --git a/docs/search.md b/docs/search.md\n"
                "@@ -0,0 +1 @@\n+Search docs\n"
            ),
            stderr="",
        ),
    ]

    with patch("subprocess.run", side_effect=outputs) as run:
        pr = StandardGitAdapter().find_pr_for_commit("abc")

    assert pr is not None
    assert pr.number == 123
    assert pr.title == "feat: add search (#123)"
    assert [f.path for f in pr.files] == ["src/search.py", "docs/search.md"]
    assert pr.files[0].patch
    assert "+new" in pr.files[0].patch
    assert "--first-parent" in run.call_args_list[1].args[0]


def test_standard_git_adapter_returns_none_without_pr_number():
    outputs = [CompletedProcess(args=[], returncode=0, stdout="chore: update deps\n", stderr="")]

    with patch("subprocess.run", side_effect=outputs):
        pr = StandardGitAdapter().find_pr_for_commit("abc")

    assert pr is None
