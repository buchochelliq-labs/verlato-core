from __future__ import annotations

from subprocess import CompletedProcess
from unittest.mock import patch

from verlato.core.adapters import StandardGitAdapter, parse_git_show_changed_files


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
    assert "--patch-with-raw" in run.call_args_list[1].args[0]


def test_standard_git_adapter_returns_none_without_pr_number():
    outputs = [CompletedProcess(args=[], returncode=0, stdout="chore: update deps\n", stderr="")]

    with patch("subprocess.run", side_effect=outputs):
        pr = StandardGitAdapter().find_pr_for_commit("abc")

    assert pr is None


def test_parse_git_show_changed_files_uses_raw_records_with_patch_hunks():
    raw = (
        ":100644 100644 a695b5e d81deb6 M\tBACKLOG.md\n"
        ":100644 100644 a3bed85 1d09e50 M\treleasepilot/core/grouper.py\n"
        ":000000 100644 0000000 aa34052 A\ttests/test_grouper_non_pr.py\n"
        "\n"
        "diff --git a/BACKLOG.md b/BACKLOG.md\n"
        "index a695b5e..d81deb6 100644\n"
        "--- a/BACKLOG.md\n"
        "+++ b/BACKLOG.md\n"
        "@@ -1 +1 @@\n"
        "-old backlog\n"
        "+new backlog\n"
        "diff --git a/releasepilot/core/grouper.py b/releasepilot/core/grouper.py\n"
        "index a3bed85..1d09e50 100644\n"
        "--- a/releasepilot/core/grouper.py\n"
        "+++ b/releasepilot/core/grouper.py\n"
        "@@ -2 +2 @@\n"
        "-old grouper\n"
        "+new grouper\n"
        "diff --git a/tests/test_grouper_non_pr.py b/tests/test_grouper_non_pr.py\n"
        "new file mode 100644\n"
        "index 0000000..aa34052\n"
        "--- /dev/null\n"
        "+++ b/tests/test_grouper_non_pr.py\n"
        "@@ -0,0 +1 @@\n"
        "+test content\n"
    )

    files = parse_git_show_changed_files(raw)

    assert [(file.path, file.status) for file in files] == [
        ("BACKLOG.md", "modified"),
        ("releasepilot/core/grouper.py", "modified"),
        ("tests/test_grouper_non_pr.py", "added"),
    ]
    assert "+new backlog" in files[0].patch
    assert "+new grouper" in files[1].patch
    assert "+test content" in files[2].patch
