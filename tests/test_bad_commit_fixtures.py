import json
from pathlib import Path

from verlato.core.classifier import Classifier
from verlato.core.models import ChangedFile, ChangeGroup, Commit, PullRequest


def test_bad_commit_fixture_behaviors():
    fixture_path = Path("tests/fixtures/commits/bad_messages.json")
    cases = json.loads(fixture_path.read_text())
    classifier = Classifier()

    for idx, case in enumerate(cases):
        group = ChangeGroup(
            pr=PullRequest(
                number=100 + idx,
                title=case["title"],
                files=[ChangedFile(path=p) for p in case["files"]],
            ),
            commits=[Commit(sha=f"sha-{idx}", message=case["message"])],
        )
        item = classifier.classify(group, base_ref="v1", head_ref="main")[0]
        assert item.classification.kind == case["expected_kind"]
        assert case["expected_signal"] in item.evidence.commit_signals
