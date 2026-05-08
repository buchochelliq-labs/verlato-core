from verlato.core.grouper import Grouper
from verlato.core.models import ChangedFile, Commit


def test_group_non_pr_conventional_commits_by_type_and_scope():
    commits = [
        Commit(sha="a1", message="feat(ui): add toolbar", author="dev1"),
        Commit(sha="a2", message="feat(ui): add keyboard shortcut", author="dev1"),
        Commit(sha="a3", message="fix(ui): patch focus bug", author="dev1"),
    ]

    groups = Grouper().group(commits, prs_by_commit={})

    assert len(groups) == 2
    assert [c.sha for c in groups[0].commits] == ["a1", "a2"]
    assert [c.sha for c in groups[1].commits] == ["a3"]
    assert groups[0].grouping_reason == "Grouped because these non-PR commits share conventional type `feat` and scope `ui`."


def test_do_not_group_unscoped_conventional_commits_by_author():
    commits = [
        Commit(sha="f1", message="feat: add toolbar", author="dev1"),
        Commit(sha="f2", message="feat: add import flow", author="dev1"),
    ]

    groups = Grouper().group(commits, prs_by_commit={})

    assert len(groups) == 2
    assert [c.sha for c in groups[0].commits] == ["f1"]
    assert [c.sha for c in groups[1].commits] == ["f2"]
    assert groups[0].grouping_reason == "Kept separate because no PR or explicit conventional scope was found."


def test_do_not_group_unscoped_conventional_commits_by_changed_area():
    commits = [
        Commit(sha="f1", message="feat: add toolbar", author="dev1"),
        Commit(sha="f2", message="feat: add keyboard shortcut", author="dev2"),
    ]

    groups = Grouper().group(commits, prs_by_commit={})

    assert len(groups) == 2
    assert [c.sha for c in groups[0].commits] == ["f1"]
    assert [c.sha for c in groups[1].commits] == ["f2"]
    assert groups[0].grouping_reason == "Kept separate because no PR or explicit conventional scope was found."


def test_group_adjacent_non_pr_commits_with_overlapping_substantive_files():
    commits = [
        Commit(
            sha="f1",
            message="feat: add toolbar",
            author="dev1",
            files=[ChangedFile(path="src/toolbar.ts", patch="+ toolbar")],
        ),
        Commit(
            sha="f2",
            message="fix: tune toolbar",
            author="dev2",
            files=[ChangedFile(path="src/toolbar.ts", patch="+ tune")],
        ),
        Commit(
            sha="f3",
            message="docs: update changelog",
            author="dev2",
            files=[ChangedFile(path="CHANGELOG.md", patch="+ generated")],
        ),
    ]

    groups = Grouper().group(commits, prs_by_commit={})

    assert len(groups) == 2
    assert [c.sha for c in groups[0].commits] == ["f1", "f2"]
    assert groups[0].files == [ChangedFile(path="src/toolbar.ts", patch="+ toolbar\n+ tune")]
    assert groups[0].grouping_reason.startswith("Grouped because adjacent non-PR commits touch overlapping substantive files")
    assert [c.sha for c in groups[1].commits] == ["f3"]


def test_do_not_group_misc_non_pr_commits():
    commits = [
        Commit(sha="m1", message="update things", author="dev1"),
        Commit(sha="m2", message="small tweak", author="dev1"),
    ]

    groups = Grouper().group(commits, prs_by_commit={})

    assert len(groups) == 2
    assert [c.sha for c in groups[0].commits] == ["m1"]
    assert [c.sha for c in groups[1].commits] == ["m2"]
    assert groups[0].grouping_reason == "Kept separate because no PR or explicit conventional scope was found."


def test_do_not_group_unscoped_docs_commits_by_author():
    commits = [
        Commit(sha="d1", message="docs: update README", author="dev1"),
        Commit(sha="d2", message="docs: add troubleshooting", author="dev1"),
    ]

    groups = Grouper().group(commits, prs_by_commit={})

    assert len(groups) == 2
    assert [c.sha for c in groups[0].commits] == ["d1"]
    assert [c.sha for c in groups[1].commits] == ["d2"]
    assert groups[0].grouping_reason == "Kept separate because no PR or explicit conventional scope was found."
