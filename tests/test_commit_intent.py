import json
from pathlib import Path

from verlato.cli.main import _maybe_infer_commit_intents
from verlato.core.commit_intent import (
    commit_intent_payload,
    effective_conventional_commit,
    infer_commit_intents,
    is_low_signal_commit,
    parse_commit_intent_response,
)
from verlato.core.models import ChangedFile, Commit
from verlato.providers.base import ProviderRequest, ProviderResponse, VerlatoProvider
from verlato.providers.mock import MockProvider


def test_parse_commit_intent_accepts_valid_response():
    intent = parse_commit_intent_response(
        json.dumps(
            {
                "type": "feat",
                "scope": "api",
                "description": "add token rotation",
                "breaking": False,
                "confidence": 0.86,
                "rationale": "Touches API token rotation files.",
            }
        ),
        model="test-model",
    )

    assert intent is not None
    assert intent.type == "feat"
    assert intent.scope == "api"
    assert intent.description == "add token rotation"
    assert intent.confidence == 0.86
    assert intent.model == "test-model"
    assert commit_intent_payload(intent)["source"] == "model-inferred"


def test_parse_commit_intent_rejects_invalid_or_low_confidence_responses():
    valid_base = {
        "type": "feat",
        "scope": "api",
        "description": "add token rotation",
        "breaking": False,
        "confidence": 0.86,
        "rationale": "Touches API token rotation files.",
    }

    cases = [
        {**valid_base, "type": "feature"},
        {**valid_base, "description": ""},
        {**valid_base, "scope": "API Tokens!"},
        {**valid_base, "confidence": 0.64},
    ]

    for payload in cases:
        assert parse_commit_intent_response(json.dumps(payload), model="test-model") is None


def test_low_signal_detection_and_effective_conventional_metadata():
    scoped = Commit(sha="a1", message="feat(api): add token rotation")
    unscoped = Commit(sha="a2", message="feat: add token rotation")
    misc = Commit(sha="a3", message="add token rotation")

    assert is_low_signal_commit(scoped) is False
    assert is_low_signal_commit(unscoped) is True
    assert is_low_signal_commit(misc) is True

    unscoped.inferred_intent = parse_commit_intent_response(
        json.dumps(
            {
                "type": "fix",
                "scope": "api",
                "description": "patch token rotation",
                "breaking": True,
                "confidence": 0.9,
                "rationale": "Patch touches API files.",
            }
        ),
        model="test-model",
    )
    effective, used_inferred = effective_conventional_commit(unscoped)

    assert used_inferred is True
    assert effective is not None
    assert effective.type == "feat"
    assert effective.scope == "api"
    assert effective.description == "add token rotation"
    assert effective.breaking is True


def test_inference_provider_errors_are_ignored():
    commits = [
        Commit(
            sha="a1",
            message="add token rotation",
            files=[ChangedFile(path="api/tokens.py", patch="+ rotate tokens")],
        )
    ]

    inferred = infer_commit_intents(commits, _ErroringProvider())

    assert inferred == 0
    assert commits[0].inferred_intent is None


def test_commit_intent_inference_uses_patch_free_evidence_by_default():
    provider = _CapturingIntentProvider()
    commit = Commit(
        sha="a1",
        message="add token endpoint",
        files=[ChangedFile(path="api/tokens.py", patch="@@ -1 +1 @@\n-old\n+REMOTE_INTENT_SENTINEL")],
    )

    inferred = infer_commit_intents(
        [commit],
        provider,
        privacy_mode="metadata-only",
        provider_mode="litellm",
    )
    request = provider.calls[0]

    assert inferred == 1
    assert commit.inferred_intent is not None
    assert "REMOTE_INTENT_SENTINEL" not in request.prompt
    assert "@@" not in request.prompt
    assert "patch_excerpt" not in request.prompt
    assert request.evidence["raw_patch_chars_sent"] == 0
    assert request.metadata["request"]["raw_patch_chars_sent"] == 0
    assert request.metadata["request"]["contains_raw_patch_markers"] is False


def test_commit_intent_inference_allows_bounded_patch_evidence_for_local_mode():
    provider = _CapturingIntentProvider()
    commit = Commit(
        sha="a1",
        message="add token endpoint",
        files=[ChangedFile(path="api/tokens.py", patch="@@ -1 +1 @@\n-old\n+LOCAL_INTENT_SENTINEL")],
    )

    inferred = infer_commit_intents(
        [commit],
        provider,
        privacy_mode="local-summary-then-ai",
        provider_mode="ollama",
    )
    request = provider.calls[0]

    assert inferred == 1
    assert "LOCAL_INTENT_SENTINEL" in request.prompt
    assert request.evidence["files"][0]["patch_excerpt"].endswith("LOCAL_INTENT_SENTINEL")
    assert request.evidence["raw_patch_chars_sent"] > 0
    assert request.metadata["request"]["raw_patch_chars_sent"] > 0
    assert request.metadata["request"]["contains_raw_patch_markers"] is True


def test_cli_commit_intent_inference_enriches_eligible_commits(monkeypatch, tmp_path: Path):
    (tmp_path / "verlato.json").write_text(json.dumps({"commit_intent_inference": "low-signal"}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: MockProvider())
    commits = [
        Commit(
            sha="a1",
            message="add token endpoint",
            files=[ChangedFile(path="api/tokens.py", patch="+token endpoint")],
        ),
        Commit(
            sha="a2",
            message="feat(ui): keep authored scope",
            files=[ChangedFile(path="web/ui.ts", patch="+toolbar")],
        ),
    ]

    _maybe_infer_commit_intents(commits, model_provider=None, commit_intent_inference=None)

    assert commits[0].inferred_intent is not None
    assert commits[0].inferred_intent.type == "feat"
    assert commits[0].inferred_intent.scope == "api"
    assert commits[1].inferred_intent is None


def test_cli_commit_intent_inference_respects_fanout_guard(monkeypatch, tmp_path: Path):
    (tmp_path / "verlato.json").write_text(
        json.dumps({"commit_intent_inference": "low-signal", "provider": {"max_items_per_scan": 1}})
    )
    monkeypatch.chdir(tmp_path)

    def fail_build_provider(*args, **kwargs):
        raise AssertionError("fanout guard should skip provider construction")

    monkeypatch.setattr("verlato.cli.main.build_provider", fail_build_provider)
    commits = [
        Commit(sha="a1", message="add first endpoint"),
        Commit(sha="a2", message="add second endpoint"),
    ]

    _maybe_infer_commit_intents(commits, model_provider=None, commit_intent_inference=None)

    assert [commit.inferred_intent for commit in commits] == [None, None]


class _ErroringProvider(VerlatoProvider):
    def summarize(self, request: ProviderRequest) -> ProviderResponse:
        raise RuntimeError("provider unavailable")

    def classify(self, request: ProviderRequest) -> ProviderResponse:
        raise AssertionError("classify should not be called")


class _CapturingIntentProvider(VerlatoProvider):
    def __init__(self):
        self.calls: list[ProviderRequest] = []

    def summarize(self, request: ProviderRequest) -> ProviderResponse:
        self.calls.append(request)
        return ProviderResponse(
            content=json.dumps(
                {
                    "type": "feat",
                    "scope": "api",
                    "description": "add token endpoint",
                    "breaking": False,
                    "confidence": 0.9,
                    "rationale": "Touches API token endpoint metadata.",
                }
            ),
            confidence=0.9,
            model="test-model",
        )

    def classify(self, request: ProviderRequest) -> ProviderResponse:
        raise AssertionError("classify should not be called")
