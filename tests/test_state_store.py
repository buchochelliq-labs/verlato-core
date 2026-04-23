import json

from releasepilot.core.models import (
    ChangeItem,
    Classification,
    Content,
    Evidence,
    SourceRef,
)
from releasepilot.state.store import StateStore, StateStoreError


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


def test_state_store_raises_for_invalid_json(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not valid json")

    try:
        StateStore(str(path)).load()
    except StateStoreError as exc:
        assert "Unable to load state" in str(exc)
    else:
        raise AssertionError("Expected StateStoreError")


def test_state_store_rejects_unknown_schema_version(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"schema_version": "v99", "items": []}))

    try:
        StateStore(str(path)).load()
    except StateStoreError as exc:
        assert "Unsupported state schema version" in str(exc)
    else:
        raise AssertionError("Expected StateStoreError")


def test_state_store_migrates_v1_lineage(tmp_path):
    path = tmp_path / "state.json"
    payload = {
        "schema_version": "v1",
        "items": [
            {
                "id": "ci_legacy",
                "source": {"pr_number": 1, "commit_shas": ["abc"], "base_ref": "v1.0.0", "head_ref": "main"},
                "classification": {"kind": "added", "area": "api", "audience": "external", "semver": "minor"},
                "content": {"title": "Legacy API", "summary": "Adds endpoint"},
                "evidence": {"labels": [], "files": ["api/x.py"], "commit_signals": [], "diff_summary": {}},
                "confidence": 0.9,
                "published": True,
                "published_in": "v1.1.0",
                "checksum": "legacy-sum",
                "checksum_version": "v1",
            }
        ],
    }
    path.write_text(json.dumps(payload))

    loaded = StateStore(str(path)).load()

    assert loaded.schema_version == "v1"
    assert loaded.area_release_lineage == {"api": ["v1.1.0"]}
