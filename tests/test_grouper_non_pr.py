from releasepilot.core.grouper import Grouper
from releasepilot.core.models import Commit


def test_group_non_pr_conventional_commits_by_type_scope_and_author():
    commits = [
        Commit(sha="a1", message="feat(ui): add toolbar", author="dev1"),
        Commit(sha="a2", message="feat(ui): add keyboard shortcut", author="dev1"),
        Commit(sha="a3", message="fix(ui): patch focus bug", author="dev1"),
    ]

    groups = Grouper().group(commits, prs_by_commit={})

    assert len(groups) == 2
    assert [c.sha for c in groups[0].commits] == ["a1", "a2"]
    assert [c.sha for c in groups[1].commits] == ["a3"]


def test_do_not_group_misc_non_pr_commits():
    commits = [
        Commit(sha="m1", message="update things", author="dev1"),
        Commit(sha="m2", message="small tweak", author="dev1"),
    ]

    groups = Grouper().group(commits, prs_by_commit={})

    assert len(groups) == 2
    assert [c.sha for c in groups[0].commits] == ["m1"]
    assert [c.sha for c in groups[1].commits] == ["m2"]


def test_group_non_pr_docs_commits_by_author():
    commits = [
        Commit(sha="d1", message="docs: update README", author="dev1"),
        Commit(sha="d2", message="docs: add troubleshooting", author="dev1"),
        Commit(sha="d3", message="docs: wording fix", author="dev2"),
    ]

    groups = Grouper().group(commits, prs_by_commit={})

    assert len(groups) == 2
    assert [c.sha for c in groups[0].commits] == ["d1", "d2"]
    assert [c.sha for c in groups[1].commits] == ["d3"]
