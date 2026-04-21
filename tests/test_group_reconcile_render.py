from releasepilot.core.classifier import Classifier
from releasepilot.core.generator import MarkdownGenerator
from releasepilot.core.grouper import Grouper
from releasepilot.core.models import ChangedFile, Commit, PullRequest
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
