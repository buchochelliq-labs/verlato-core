from verlato.core.classifier import Classifier
from verlato.core.generator import MarkdownGenerator
from verlato.core.grouper import Grouper
from verlato.core.models import ChangedFile, ChangeGroup, Commit, PullRequest
from verlato.core.reconciler import Reconciler


def test_group_and_reconcile_and_render():
    commits = [Commit(sha="a1", message="feat: add login"), Commit(sha="b2", message="did some stuff")]
    pr = PullRequest(number=12, title="Add login", labels=["feature"], files=[ChangedFile(path="src/auth.py")])
    prs_by_commit = {"a1": pr, "b2": pr}

    groups = Grouper().group(commits, prs_by_commit)
    assert len(groups) == 1

    items = Classifier().classify(groups[0], base_ref="v1.0.0", head_ref="main")
    assert items[0].classification.kind == "added"

    unseen = Reconciler().unpublished(existing=[], detected=items)
    assert len(unseen) == 1

    md = MarkdownGenerator().render_github_release(unseen, version="v1.1.0")
    assert "### Added" in md
    assert "PR #12" in md


def test_grouper_splits_pr_with_unrelated_user_facing_areas():
    commits = [Commit(sha="a1", message="feat: multi-surface change")]
    pr = PullRequest(
        number=55,
        title="Cross-cutting refactor",
        files=[
            ChangedFile(path="api/users.py"),
            ChangedFile(path="frontend/src/users.ts"),
        ],
    )
    groups = Grouper().group(commits, {"a1": pr})
    assert len(groups) == 2
    titles = sorted(group.pr.title for group in groups if group.pr)
    assert titles == ["Cross-cutting refactor [api]", "Cross-cutting refactor [frontend]"]


def test_grouper_splits_integration_pr_by_explicit_commit_scope():
    commits = [
        Commit(sha="a1", message="feat(ui): add toolbar", files=[ChangedFile(path="frontend/src/toolbar.ts")]),
        Commit(sha="b2", message="feat(api): add users endpoint", files=[ChangedFile(path="api/users.py")]),
        Commit(sha="c3", message="fix(api): validate users endpoint", files=[ChangedFile(path="api/users.py")]),
    ]
    pr = PullRequest(number=77, title="Integrate feature branches", files=[
        ChangedFile(path="frontend/src/toolbar.ts"),
        ChangedFile(path="api/users.py"),
    ])

    groups = Grouper().group(commits, {"a1": pr, "b2": pr, "c3": pr})

    assert len(groups) == 2
    assert [[commit.sha for commit in group.commits] for group in groups] == [["a1"], ["b2", "c3"]]
    assert [group.pr.title for group in groups if group.pr] == [
        "Integrate feature branches [ui]",
        "Integrate feature branches [api]",
    ]
    assert groups[0].grouping_reason == (
        "Grouped by PR #77, then split because the PR contains multiple explicit conventional scopes; "
        "this changeset uses scope `ui`."
    )


def test_grouper_keeps_same_scope_pr_as_one_changeset():
    commits = [
        Commit(sha="a1", message="feat(ui): add toolbar"),
        Commit(sha="b2", message="fix(ui): patch toolbar focus"),
    ]
    pr = PullRequest(number=78, title="Improve toolbar", files=[ChangedFile(path="frontend/src/toolbar.ts")])

    groups = Grouper().group(commits, {"a1": pr, "b2": pr})

    assert len(groups) == 1
    assert [commit.sha for commit in groups[0].commits] == ["a1", "b2"]
    assert groups[0].grouping_reason == "Grouped by PR #78."


def test_pr_group_without_pr_files_keeps_commit_file_evidence():
    commits = [
        Commit(
            sha="a1",
            message="Merge pull request #12 from feature/release-control",
            files=[ChangedFile(path="src/release_control.py", patch="+render actual diff evidence")],
        )
    ]
    pr = PullRequest(number=12, title="Improve release control", files=[])

    groups = Grouper().group(commits, {"a1": pr})
    items = Classifier().classify(groups[0], base_ref="v1.0.0", head_ref="main")

    assert groups[0].files[0].path == "src/release_control.py"
    assert "src/release_control.py" in items[0].content.summary
    assert items[0].evidence.files == ["src/release_control.py"]


def test_render_with_confidence_and_provenance():
    commits = [Commit(sha="a1", message="fix: typo")]
    pr = PullRequest(number=7, title="Fix typo", files=[ChangedFile(path="docs/readme.md")])
    items = Classifier().classify(ChangeGroup(pr=pr, commits=commits), base_ref="v1.0.0", head_ref="main")
    md = MarkdownGenerator().render_github_release(
        items,
        version="v1.0.1",
        include_confidence=True,
        include_provenance=True,
    )
    assert "confidence" in md
    assert "source v1.0.0..main" in md
