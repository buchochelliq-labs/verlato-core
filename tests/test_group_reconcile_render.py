from releasepilot.core.classifier import Classifier
from releasepilot.core.generator import MarkdownGenerator
from releasepilot.core.grouper import Grouper
from releasepilot.core.models import ChangedFile, ChangeGroup, Commit, PullRequest
from releasepilot.core.reconciler import Reconciler


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
