from releasepilot.core.classifier import Classifier
from releasepilot.core.models import ChangedFile, ChangeGroup, Commit, PullRequest


def test_classifier_prefers_label_based_area():
    group = ChangeGroup(
        pr=PullRequest(
            number=5,
            title="Backend fix",
            labels=["backend"],
            files=[ChangedFile(path="frontend/app.tsx")],
        ),
        commits=[Commit(sha="abc", message="fix: correct query")],
    )
    item = Classifier().classify(group, base_ref="v1.0.0", head_ref="main")[0]
    assert item.classification.area == "backend"


def test_classifier_marks_internal_by_label():
    group = ChangeGroup(
        pr=PullRequest(
            number=6,
            title="Maintenance",
            labels=["chore"],
            files=[ChangedFile(path="src/app.py")],
        ),
        commits=[Commit(sha="def", message="chore: clean comments")],
    )
    item = Classifier().classify(group, base_ref="v1.0.0", head_ref="main")[0]
    assert item.classification.audience == "internal"
