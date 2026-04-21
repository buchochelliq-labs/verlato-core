from releasepilot.core.models import (
    ChangeItem,
    Classification,
    Content,
    Evidence,
    SourceRef,
)
from releasepilot.state.store import StateStore


def test_state_store_roundtrip(tmp_path):
    store = StateStore(str(tmp_path / "state.json"))
    item = ChangeItem(
        id="ci_1",
        source=SourceRef(pr_number=1, commit_shas=["abc"], base_ref="v1", head_ref="v2"),
        classification=Classification(kind="added", area="api", audience="external", semver="minor"),
        content=Content(title="Add API", summary="Adds endpoint"),
        evidence=Evidence(labels=["feature"], files=["api/x.py"], commit_signals=["conventional:feat"]),
        confidence=0.9,
        checksum="sum1",
    )
    store.append_new([item])
    loaded = store.load()
    assert len(loaded.items) == 1
    assert loaded.items[0].id == "ci_1"
