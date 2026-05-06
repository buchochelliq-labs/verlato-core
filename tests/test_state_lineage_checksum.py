from verlato.core.checksum import CHECKSUM_VERSION
from verlato.core.classifier import Classifier
from verlato.core.models import ChangedFile, ChangeGroup, Commit, PullRequest
from verlato.core.reconciler import Reconciler
from verlato.state.store import StateData, StateStore


def test_append_new_populates_lineage_from_prior_published(tmp_path):
    store = StateStore(str(tmp_path / "state.json"))
    published = Classifier().classify(
        ChangeGroup(
            pr=PullRequest(number=1, title="Old API", files=[ChangedFile(path="api/users.py")]),
            commits=[Commit(sha="a", message="feat: add old api")],
        ),
        base_ref="v1.0.0",
        head_ref="v1.1.0",
    )[0]
    published.published = True
    published.published_in = "v1.1.0"
    store.save(StateData(items=[published]))

    new_item = Classifier().classify(
        ChangeGroup(
            pr=PullRequest(number=2, title="New API", files=[ChangedFile(path="api/orders.py")]),
            commits=[Commit(sha="b", message="feat: add new api")],
        ),
        base_ref="v1.1.0",
        head_ref="main",
    )[0]
    store.append_new([new_item])

    loaded = store.load().items
    assert loaded[-1].lineage_releases == ["v1.1.0"]


def test_reconciler_uses_checksum_version_boundary():
    item = Classifier().classify(
        ChangeGroup(
            pr=PullRequest(number=3, title="Tweak", files=[ChangedFile(path="src/app.py")]),
            commits=[Commit(sha="c", message="fix: tweak")],
        ),
        base_ref="v1",
        head_ref="main",
    )[0]
    assert item.checksum_version == CHECKSUM_VERSION

    cloned = Classifier().classify(
        ChangeGroup(
            pr=PullRequest(number=3, title="Tweak", files=[ChangedFile(path="src/app.py")]),
            commits=[Commit(sha="c", message="fix: tweak")],
        ),
        base_ref="v1",
        head_ref="main",
    )[0]
    cloned.checksum_version = "v1"

    unpublished = Reconciler().unpublished(existing=[cloned], detected=[item])
    assert len(unpublished) == 1
