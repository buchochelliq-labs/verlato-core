import json
from pathlib import Path

from verlato.cli.main import _maybe_infer_commit_intents
from verlato.core.commit_intent import (
    commit_intent_payload,
    conventional_commit_draft_evidence,
    conventional_commit_message,
    derive_conventional_commits,
    draft_conventional_commit,
    effective_conventional_commit,
    infer_commit_intents,
    is_low_signal_commit,
    parse_commit_intent_response,
    parse_conventional_commit_draft_response,
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


def test_parse_conventional_commit_draft_accepts_body():
    draft = parse_conventional_commit_draft_response(
        json.dumps(
            {
                "type": "refactor",
                "scope": "vscode",
                "description": "revise suggestion strategy settings",
                "body": [
                    "Added configurable suggestion strategy modes.",
                    "- Removed instant provisional suggestion behavior.",
                ],
                "breaking": False,
                "confidence": 0.86,
                "rationale": "Touches VS Code suggestion settings.",
            }
        ),
        model="test-model",
    )

    assert draft is not None
    assert draft.intent.type == "refactor"
    assert draft.intent.scope == "vscode"
    assert draft.body == [
        "Added configurable suggestion strategy modes.",
        "Removed instant provisional suggestion behavior.",
    ]
    assert draft.to_dict()["header"] == "refactor(vscode): revise suggestion strategy settings"
    assert draft.to_dict()["message"] == (
        "refactor(vscode): revise suggestion strategy settings\n\n"
        "- Added configurable suggestion strategy modes.\n"
        "- Removed instant provisional suggestion behavior."
    )


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


def test_derived_conventional_commit_uses_diff_only_evidence():
    provider = _CapturingIntentProvider()
    commit = Commit(
        sha="a1",
        message="feat(auth): already authored message must not leak",
        parent_sha="p0",
        files=[ChangedFile(path="api/tokens.py", status="modified", patch="@@ -1 +1 @@\n-old\n+NEW_TOKEN")],
    )

    derived = derive_conventional_commits(
        [commit],
        provider,
        privacy_mode="metadata-only",
        provider_mode="litellm",
    )
    request = provider.calls[0]

    assert derived == 1
    assert commit.derived_intent is not None
    assert commit.derived_intent.source == "model-derived-diff"
    assert conventional_commit_message(commit.derived_intent) == "feat(api): add token endpoint"
    assert request.task == "derive-conventional"
    assert "already authored message" not in request.prompt
    assert "NEW_TOKEN" not in request.prompt
    assert "previous_sha" in request.prompt
    assert "message" not in request.evidence
    assert request.metadata["request"]["raw_patch_chars_sent"] == 0


def test_derived_conventional_commit_can_include_local_patch_evidence():
    provider = _CapturingIntentProvider()
    commit = Commit(
        sha="a1",
        message="whatever",
        files=[ChangedFile(path="api/tokens.py", status="added", patch="@@ -0,0 +1 @@\n+LOCAL_DIFF")],
    )

    derived = derive_conventional_commits(
        [commit],
        provider,
        privacy_mode="local-summary-then-ai",
        provider_mode="ollama",
    )
    request = provider.calls[0]

    assert derived == 1
    assert "LOCAL_DIFF" in request.prompt
    assert request.evidence["initial_commit"] is True
    assert request.metadata["request"]["raw_patch_chars_sent"] > 0


def test_conventional_commit_draft_uses_subject_and_structured_diff_facts():
    provider = _CapturingDraftProvider()
    commit = Commit(
        sha="a1",
        message="Refactor Verlato extension settings and suggestion handling",
        parent_sha="p0",
        files=[
            ChangedFile(
                path="vscode-extension/src/webview/releaseControlMarkup.ts",
                status="modified",
                patch='@@\n-suggestionBatchSize: 2\n+suggestionBatchSize: 5\n+"full-diff-json"\n-instantProvisionalSuggestion\n',
            ),
            ChangedFile(path="vscode-extension/test/releaseControlMarkup.test.js", status="modified", patch="+strategy"),
        ],
    )

    draft = draft_conventional_commit(
        commit,
        provider,
        privacy_mode="metadata-only",
        provider_mode="github-copilot",
        evidence_mode="subject-hint",
    )
    request = provider.calls[0]

    assert draft is not None
    assert draft.to_dict()["header"] == "refactor(vscode): revise suggestion strategy settings"
    assert draft.body[0] == "Updated suggestion strategy settings."
    assert request.task == "draft-conventional"
    assert request.evidence["authored_subject"] == "Refactor Verlato extension settings and suggestion handling"
    assert "updates the VS Code release control UI" in request.evidence["diff_facts"]["themes"]
    assert "patch_excerpt" not in request.prompt
    assert request.metadata["request"]["raw_patch_chars_sent"] == 0
    assert request.metadata["request"]["evidence_mode"] == "subject-hint"


def test_conventional_commit_draft_diff_only_omits_authored_message():
    provider = _CapturingDraftProvider()
    commit = Commit(
        sha="a1",
        message="Refactor Verlato extension settings and suggestion handling",
        parent_sha="p0",
        files=[
            ChangedFile(
                path="vscode-extension/src/webview/releaseControlMarkup.ts",
                status="modified",
                patch='@@\n+"trusted-provider-patch"\n+modelPrivacy\n+allowRemotePatches\n',
            )
        ],
    )

    draft = draft_conventional_commit(
        commit,
        provider,
        privacy_mode="metadata-only",
        provider_mode="github-copilot",
        evidence_mode="diff-only",
    )
    request = provider.calls[0]

    assert draft is not None
    assert draft.to_dict()["evidence_mode"] == "diff-only"
    assert draft.to_dict()["authored_subject"] == ""
    assert request.evidence["authored_message_omitted"] is True
    assert "authored_subject" not in request.evidence
    assert "authored_message" not in request.evidence
    assert "Refactor Verlato extension settings" not in request.prompt
    assert request.metadata["request"]["evidence_mode"] == "diff-only"
    assert request.metadata["request"]["authored_message_omitted"] is True


def test_conventional_commit_draft_evidence_includes_subtle_privacy_facts():
    commit = Commit(
        sha="a1",
        message="hidden from diff-only mode",
        files=[
            ChangedFile(
                path="vscode-extension/src/commands/registerCommands.ts",
                status="modified",
                patch=(
                    "@@\n"
                    "+const modelPrivacy = await vscode.window.showQuickPick([\"metadata-only\", \"trusted-provider-patch\"])\n"
                    "+await vscode.window.showWarningMessage(\"Send Patch Evidence\")\n"
                ),
            ),
            ChangedFile(
                path="tests/test_config_and_drafts.py",
                status="modified",
                patch=(
                    "+def test_draft_suggest_preflight_builds_sanitized_envelope_without_provider_call():\n"
                    "+    assert raw_patch_chars_sent == 0\n"
                ),
            ),
        ],
    )

    evidence = conventional_commit_draft_evidence(commit, evidence_mode="diff-only")
    facts = evidence["diff_facts"]

    assert evidence["authored_message_omitted"] is True
    assert "authored_subject" not in evidence
    assert "modelPrivacy" in facts["config_keys"]
    assert "test_draft_suggest_preflight_builds_sanitized_envelope_without_provider_call" in facts["test_names"]
    assert "adds model privacy settings for AI evidence" in facts["themes"]
    assert "gates raw patch evidence by privacy and provider mode" in facts["themes"]
    assert "adds confirmation for sending raw patch evidence" in facts["themes"]


def test_conventional_commit_draft_can_include_bounded_patch_evidence():
    provider = _CapturingDraftProvider()
    commit = Commit(
        sha="a1",
        message="update suggestions",
        files=[ChangedFile(path="vscode-extension/src/extension.ts", status="modified", patch="@@\n+LOCAL_DRAFT_SENTINEL")],
    )

    draft = draft_conventional_commit(
        commit,
        provider,
        privacy_mode="trusted-provider-patch",
        provider_mode="github-copilot",
    )
    request = provider.calls[0]

    assert draft is not None
    assert "LOCAL_DRAFT_SENTINEL" in request.prompt
    assert request.metadata["request"]["raw_patch_chars_sent"] > 0


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
    assert commits[0].derived_intent is not None
    assert commits[1].derived_intent is not None


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
    assert [commit.derived_intent for commit in commits] == [None, None]


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


class _CapturingDraftProvider(VerlatoProvider):
    def __init__(self):
        self.calls: list[ProviderRequest] = []

    def summarize(self, request: ProviderRequest) -> ProviderResponse:
        self.calls.append(request)
        return ProviderResponse(
            content=json.dumps(
                {
                    "type": "refactor",
                    "scope": "vscode",
                    "description": "revise suggestion strategy settings",
                    "body": [
                        "Updated suggestion strategy settings.",
                        "Removed instant provisional suggestion behavior.",
                    ],
                    "breaking": False,
                    "confidence": 0.88,
                    "rationale": "Touches VS Code suggestion settings and tests.",
                }
            ),
            confidence=0.88,
            model="test-model",
        )

    def classify(self, request: ProviderRequest) -> ProviderResponse:
        raise AssertionError("classify should not be called")
