from __future__ import annotations

import json
from pathlib import Path

from releasepilot.cli.main import main
from releasepilot.config import load_config
from releasepilot.core.classifier import Classifier
from releasepilot.core.drafts import normalize_remote_url
from releasepilot.core.models import (
    ChangeGroup,
    ChangedFile,
    ChangeItem,
    Classification,
    Commit,
    Content,
    Evidence,
    PullRequest,
    SourceRef,
)
from releasepilot.providers.base import ProviderRequest, ProviderResponse
from releasepilot.providers.stub import StubProvider
from releasepilot.state.store import StateData, StateStore


def _mk_item(
    item_id: str,
    *,
    kind: str = "changed",
    title: str = "Improve login",
    summary: str = "Improves login flows",
    base_ref: str = "v1.0.0",
    head_ref: str = "HEAD",
) -> ChangeItem:
    return ChangeItem(
        id=item_id,
        source=SourceRef(pr_number=12, commit_shas=["a1"], base_ref=base_ref, head_ref=head_ref),
        classification=Classification(kind=kind, area="general", audience="external", semver="patch"),
        content=Content(title=title, summary=summary),
        evidence=Evidence(files=["src/auth.py"], commit_signals=["diff-fallback"]),
        confidence=0.72,
        checksum=f"sum-{item_id}",
    )


def _mk_group(
    *,
    number: int,
    title: str,
    commit_sha: str,
    commit_message: str,
    path: str = "src/auth.py",
    patch: str = "+ change",
) -> ChangeGroup:
    return ChangeGroup(
        pr=PullRequest(
            number=number,
            title=title,
            files=[ChangedFile(path=path, patch=patch)],
        ),
        commits=[Commit(sha=commit_sha, message=commit_message)],
    )


def test_load_config_defaults_without_file(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.state_path == ".releasepilot_state.json"
    assert config.targets["changelog"].path == "CHANGELOG.md"
    assert config.targets["github-release"].path == ".github/release.md"


def test_load_config_reads_custom_targets(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "releasepilot.json"
    config_path.write_text(
        json.dumps(
            {
                "state_path": ".custom-state.json",
                "targets": {
                    "changelog": {"path": "docs/CHANGELOG.md", "format": "changelog"},
                    "github-release": {"path": ".github/release-notes.md", "format": "github-release"},
                },
            }
        )
    )
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.state_path == ".custom-state.json"
    assert config.targets["changelog"].path == "docs/CHANGELOG.md"
    assert config.targets["github-release"].path == ".github/release-notes.md"


def test_load_config_reads_provider_defaults(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "releasepilot.json"
    config_path.write_text(
        json.dumps(
            {
                "provider": {
                    "name": "ollama",
                    "model": "llama3.2:3b",
                    "endpoint": "http://127.0.0.1:11434/api/chat",
                }
            }
        )
    )
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.provider.name == "ollama"
    assert config.provider.model == "llama3.2:3b"
    assert config.provider.endpoint == "http://127.0.0.1:11434/api/chat"


def test_load_config_reads_provider_fanout_limit(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "releasepilot.json"
    config_path.write_text(json.dumps({"provider": {"max_items_per_scan": 7}}))
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.provider.max_items_per_scan == 7


def test_load_config_reads_provider_timeout(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "releasepilot.json"
    config_path.write_text(json.dumps({"provider": {"timeout_seconds": 45}}))
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.provider.timeout_seconds == 45


def test_load_config_reads_suggestion_count(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "releasepilot.json"
    config_path.write_text(json.dumps({"suggestion_count": 4}))
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.suggestion_count == 4


def test_load_config_reads_suggestion_tone(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "releasepilot.json"
    config_path.write_text(json.dumps({"suggestion_tone": "technical"}))
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.suggestion_tone == "technical"


def test_load_config_reads_suggestion_style(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "releasepilot.json"
    config_path.write_text(
        json.dumps(
            {
                "suggestion_style": {
                    "guidance": "Prefer product language.",
                    "project_vocabulary": ["Release Control", "draft review"],
                    "banned_phrases": ["stuff", "things"],
                    "specificity_floor": 0.4,
                }
            }
        )
    )
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.suggestion_style.guidance == "Prefer product language."
    assert config.suggestion_style.project_vocabulary == ["Release Control", "draft review"]
    assert config.suggestion_style.banned_phrases == ["stuff", "things"]
    assert config.suggestion_style.specificity_floor == 0.4


def test_load_config_reads_feedback_text_opt_in(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "releasepilot.json"
    config_path.write_text(json.dumps({"feedback_include_text": True}))
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.feedback_include_text is True


def test_load_config_defaults_to_one_suggestion(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.suggestion_count == 1


def test_draft_json_command_outputs_sections_and_targets(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "releasepilot.cli.main.detect_change_items",
        lambda base, head: [_mk_item("ci_1", base_ref=base, head_ref=head, kind="added")],
    )

    rc = main(["draft", "json", "--base", "v1.0.0", "--head", "HEAD", "--version", "v1.1.0"])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["version"] == "v1.1.0"
    assert "repository_url" in out
    assert out["base_ref"] == "v1.0.0"
    assert out["targets"][0]["name"] == "changelog"
    assert out["sections"][0]["kind"] == "added"
    item = out["sections"][0]["items"][0]
    assert item["commit_shas"] == ["a1"]
    assert item["summary_source"] == "Deterministic summary generated from changed-file evidence"
    assert item["classification_source"] == "Deterministic classification from changed-file evidence"
    assert item["evidence_summary"] == "Changed files: src/auth.py"


def test_draft_preview_command_renders_github_release(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "releasepilot.cli.main.detect_change_items",
        lambda base, head: [_mk_item("ci_1", base_ref=base, head_ref=head, kind="fixed")],
    )

    rc = main(["draft", "preview", "--base", "v1.0.0", "--version", "v1.0.1"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "## v1.0.1" in out
    assert "### Fixed" in out


def test_draft_apply_updates_changelog_target(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\nExisting notes\n")
    monkeypatch.setattr(
        "releasepilot.cli.main.detect_change_items",
        lambda base, head: [_mk_item("ci_1", base_ref=base, head_ref=head)],
    )

    rc = main(["draft", "apply", "--base", "v1.0.0", "--version", "v1.1.0", "--target", "changelog"])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["target"] == "changelog"
    updated = (tmp_path / "CHANGELOG.md").read_text()
    assert updated.startswith("# Changelog\n\n### v1.1.0")
    assert "Existing notes" in updated


def test_draft_apply_uses_custom_file_for_github_release(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    custom_file = tmp_path / "notes" / "release.md"
    monkeypatch.setattr(
        "releasepilot.cli.main.detect_change_items",
        lambda base, head: [_mk_item("ci_1", base_ref=base, head_ref=head)],
    )

    rc = main(
        [
            "draft",
            "apply",
            "--base",
            "v1.0.0",
            "--version",
            "v1.1.0",
            "--target",
            "github-release",
            "--file",
            str(custom_file),
        ]
    )

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["format"] == "github-release"
    assert custom_file.read_text().startswith("## v1.1.0")


def test_draft_review_persists_and_overlays_review_state(monkeypatch, tmp_path: Path, capsys):
    state_path = tmp_path / "state.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(state_path))
    monkeypatch.setattr(
        "releasepilot.cli.main.detect_change_items",
        lambda base, head: [_mk_item("ci_1", base_ref=base, head_ref=head)],
    )

    rc = main(
        [
            "draft",
            "review",
            "--base",
            "v1.0.0",
            "--id",
            "ci_1",
            "--status",
            "hidden",
            "--summary",
            "Edited hidden summary",
            "--reviewer",
            "nick",
        ]
    )

    assert rc == 0
    reviewed = json.loads(capsys.readouterr().out)
    assert reviewed["status"] == "hidden"
    assert reviewed["edited_summary"] == "Edited hidden summary"
    assert reviewed["hidden"] is True

    rc = main(["draft", "json", "--base", "v1.0.0", "--version", "v1.1.0"])
    assert rc == 0
    document = json.loads(capsys.readouterr().out)
    assert document["sections"][0]["items"][0]["status"] == "hidden"
    assert document["sections"][0]["items"][0]["summary"] == "Edited hidden summary"
    assert document["sections"][0]["items"][0]["original_summary"] == "Improves login flows"
    assert document["sections"][0]["items"][0]["summary_source"].startswith("Human edited summary")
    assert "Improve login" not in document["rendered"]["github-release"]


def test_draft_json_excludes_items_already_published_in_another_release(monkeypatch, tmp_path: Path, capsys):
    state_path = tmp_path / "state.json"
    published = _mk_item("ci_1")
    published.published = True
    published.published_in = "v1.0.0"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(state_path))
    StateStore(str(state_path)).save(StateData(items=[published]))
    monkeypatch.setattr(
        "releasepilot.cli.main.detect_change_items",
        lambda base, head: [_mk_item("ci_1", base_ref=base, head_ref=head)],
    )

    rc = main(["draft", "json", "--base", "v0.9.0", "--head", "HEAD", "--version", "v1.1.0"])

    assert rc == 0
    document = json.loads(capsys.readouterr().out)
    assert document["item_count"] == 0
    assert document["sections"] == []


def test_draft_suggest_persists_model_summary(monkeypatch, tmp_path: Path, capsys):
    state_path = tmp_path / "state.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(state_path))
    group = _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow")
    target_item = Classifier().classify(group, "v1.0.0", "HEAD")[0]

    class MockSummaryProvider:
        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="Model-written summary", confidence=0.9, model="mock-local")

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="[mock-classification] changed/general/external/patch", confidence=0.8, model="mock-local")

    monkeypatch.setattr("releasepilot.cli.main._collect_groups", lambda base, head: [group])

    def fake_build_provider(config, mode=None):
        if mode == "mock":
            return MockSummaryProvider()
        return StubProvider()

    monkeypatch.setattr("releasepilot.cli.main.build_provider", fake_build_provider)

    rc = main(["draft", "suggest", "--base", "v1.0.0", "--id", target_item.id, "--model-provider", "mock"])

    assert rc == 0
    suggested = json.loads(capsys.readouterr().out)
    assert suggested["edited_summary"] == "Model-written summary"
    assert suggested["status"] == "edited"
    assert suggested["summary_provenance"]["source"] == "model-generated"
    assert suggested["summary_provenance"]["provider"] == "mock"
    assert suggested["summary_provenance"]["model"] == "mock-local"
    assert suggested["summary_provenance"]["tone"] == "release-note"
    assert suggested["summary_provenance"]["applied_at"]

    rc = main(["draft", "json", "--base", "v1.0.0"])
    assert rc == 0
    document = json.loads(capsys.readouterr().out)
    item = document["sections"][0]["items"][0]
    assert item["summary"] == "Model-written summary"
    assert item["original_summary"] != "Model-written summary"
    assert item["summary_provenance"]["model"] == "mock-local"
    assert item["summary_source"] == "LLM summary generated from diff evidence (mock-local)"
    state = json.loads(state_path.read_text())
    assert state["feedback_events"][0]["event"] == "suggestion_applied"
    assert state["feedback_events"][0]["item_id"] == target_item.id
    assert state["feedback_events"][0]["metadata"]["summary_sha"]
    assert "summary" not in state["feedback_events"][0]["metadata"]


def test_draft_review_persists_model_suggestion_provenance(monkeypatch, tmp_path: Path, capsys):
    state_path = tmp_path / "state.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(state_path))
    group = _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow")
    target_item = Classifier().classify(group, "v1.0.0", "HEAD")[0]
    provenance = {
        "source": "model-suggestion",
        "provider": "ollama",
        "model": "gemma4:latest",
        "prompt_sha": "promptabc123",
        "suggestion_index": 0,
        "response_metadata": {"total_duration": 12},
    }
    monkeypatch.setattr("releasepilot.cli.main._collect_groups", lambda base, head: [group])

    rc = main(
        [
            "draft",
            "review",
            "--base",
            "v1.0.0",
            "--id",
            target_item.id,
            "--summary",
            "Model preview summary",
            "--summary-provenance-json",
            json.dumps(provenance),
        ]
    )

    assert rc == 0
    reviewed = json.loads(capsys.readouterr().out)
    assert reviewed["edited_summary"] == "Model preview summary"
    assert reviewed["summary_provenance"]["source"] == "model-suggestion"
    assert reviewed["summary_provenance"]["provider"] == "ollama"
    assert reviewed["summary_provenance"]["model"] == "gemma4:latest"
    assert reviewed["summary_provenance"]["prompt_sha"] == "promptabc123"
    assert reviewed["summary_provenance"]["response_metadata"] == {"total_duration": 12}
    assert reviewed["summary_provenance"]["applied_at"]

    rc = main(["draft", "json", "--base", "v1.0.0"])
    assert rc == 0
    document = json.loads(capsys.readouterr().out)
    item = document["sections"][0]["items"][0]
    assert item["summary_provenance"]["prompt_sha"] == "promptabc123"
    assert item["summary_source"] == "LLM summary generated from diff evidence (gemma4:latest)"


def test_feedback_record_and_export(monkeypatch, tmp_path: Path, capsys):
    state_path = tmp_path / "state.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(state_path))

    rc = main(
        [
            "feedback",
            "record",
            "--event",
            "suggestion_skipped",
            "--item-id",
            "ci_1",
            "--metadata-json",
            json.dumps({"scope": "item"}),
        ]
    )

    assert rc == 0
    recorded = json.loads(capsys.readouterr().out)
    assert recorded["recorded"] is True

    rc = main(["feedback", "export"])
    assert rc == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported["schema_version"] == "releasepilot.feedback.v1"
    assert exported["record_count"] == 1
    assert exported["records"][0]["event"] == "suggestion_skipped"
    assert exported["records"][0]["metadata"] == {"scope": "item"}


def test_feedback_recommend_uses_safe_aggregates(monkeypatch, tmp_path: Path, capsys):
    state_path = tmp_path / "state.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(state_path))
    events = [
        ("suggestion_regenerated", "concise", {}),
        ("suggestion_regenerated", "concise", {}),
        ("suggestion_skipped", "concise", {}),
        ("suggestion_applied", "technical", {"specificity_score": 0.3}),
        ("summary_edited", "technical", {"specificity_score": 0.4}),
    ]
    for index, (event, tone, extra) in enumerate(events):
        metadata = {"tone": tone, **extra}
        rc = main(
            [
                "feedback",
                "record",
                "--event",
                event,
                "--item-id",
                f"ci_{index}",
                "--metadata-json",
                json.dumps(metadata),
            ]
        )
        assert rc == 0
        capsys.readouterr()

    rc = main(["feedback", "recommend"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == "releasepilot.feedback_recommendations.v1"
    assert payload["event_count"] == 5
    assert payload["signals"]["average_specificity"] == 0.35
    kinds = {recommendation["kind"] for recommendation in payload["recommendations"]}
    assert "tighten-style-guidance" in kinds
    assert "generate-more-options" in kinds
    assert "raise-specificity-floor" in kinds
    assert "prefer-tone" in kinds


def test_draft_suggest_preview_returns_variants_without_persisting(monkeypatch, tmp_path: Path, capsys):
    state_path = tmp_path / "state.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(state_path))
    group = _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow")
    target_item = Classifier().classify(group, "v1.0.0", "HEAD")[0]

    class VariantProvider:
        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            assert "Return exactly 3 distinct complete release-note options as a numbered list" in request.prompt
            assert "downstream consumers understand the change" in request.prompt
            assert "Tone: precise engineering wording" in request.prompt
            return ProviderResponse(
                content=(
                    "1. Make login guidance easier to follow\n"
                    "2. Clarify the login flow\n"
                    "3. Improve login recovery wording"
                ),
                confidence=0.9,
                model="mock-local",
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="mock-local")

    monkeypatch.setattr("releasepilot.cli.main._collect_groups", lambda base, head: [group])
    monkeypatch.setattr("releasepilot.cli.main.build_provider", lambda config, mode=None: VariantProvider())

    rc = main(
        [
            "draft",
            "suggest",
            "--base",
            "v1.0.0",
            "--id",
            target_item.id,
            "--preview",
            "--count",
            "3",
            "--tone",
            "technical",
            "--model-provider",
            "mock",
        ]
    )

    assert rc == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["count"] == 3
    assert preview["tone"] == "technical"
    assert preview["items"][0]["item_id"] == target_item.id
    assert preview["items"][0]["tone"] == "technical"
    assert preview["items"][0]["suggestions"] == [
        "Make login guidance easier to follow",
        "Clarify the login flow",
        "Improve login recovery wording",
    ]
    assert preview["items"][0]["source_evidence"]
    assert preview["items"][0]["provider_reasoning"] == ""
    assert preview["items"][0]["suggestion_details"][0]["comparison"]["current"]
    assert preview["items"][0]["suggestion_details"][0]["comparison"]["suggested"] == (
        "Make login guidance easier to follow"
    )

    rc = main(["state", "show"])
    assert rc == 0
    state = json.loads(capsys.readouterr().out)
    assert state["items"] == []


def test_draft_suggest_applies_style_controls_to_prompt_and_rejections(monkeypatch, tmp_path: Path, capsys):
    config_path = tmp_path / "releasepilot.json"
    config_path.write_text(
        json.dumps(
            {
                "suggestion_style": {
                    "guidance": "Use Release Control terminology.",
                    "project_vocabulary": ["Release Control"],
                    "banned_phrases": ["nice things"],
                    "specificity_floor": 0.2,
                }
            }
        )
    )
    state_path = tmp_path / "state.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(state_path))
    group = _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow")
    target_item = Classifier().classify(group, "v1.0.0", "HEAD")[0]

    class StyledProvider:
        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            assert "Project style guidance: Use Release Control terminology." in request.prompt
            assert "Preferred project vocabulary: Release Control" in request.prompt
            assert "Avoid these phrases exactly: nice things" in request.prompt
            return ProviderResponse(
                content=(
                    "1. Nice things for users\n"
                    "2. Clarifies login flow validation for Release Control reviewers"
                ),
                confidence=0.9,
                model="mock-local",
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="mock-local")

    monkeypatch.setattr("releasepilot.cli.main._collect_groups", lambda base, head: [group])
    monkeypatch.setattr("releasepilot.cli.main.build_provider", lambda config, mode=None: StyledProvider())

    rc = main(
        [
            "draft",
            "suggest",
            "--base",
            "v1.0.0",
            "--id",
            target_item.id,
            "--preview",
            "--count",
            "2",
            "--model-provider",
            "mock",
        ]
    )

    assert rc == 0
    preview = json.loads(capsys.readouterr().out)
    item = preview["items"][0]
    assert item["style_profile"]["guidance"] == "Use Release Control terminology."
    assert item["suggestions"] == ["Clarifies login flow validation for Release Control reviewers"]
    assert item["suggestion_details"][0]["specificity_score"] >= 0.2
    assert item["rejected_candidates"][0]["reason"] == "banned-phrase:nice things"


def test_draft_suggest_preview_defaults_to_one_suggestion(monkeypatch, tmp_path: Path, capsys):
    state_path = tmp_path / "state.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(state_path))
    group = _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow")
    target_item = Classifier().classify(group, "v1.0.0", "HEAD")[0]

    class VariantProvider:
        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            assert "Return one complete release-note entry" in request.prompt
            assert "Use a short paragraph or focused bullet points" in request.prompt
            assert "Release context: kind=changed, audience=external" in request.prompt
            return ProviderResponse(
                content="1. Clarifies login guidance for users\n2. Documents login recovery behavior",
                confidence=0.9,
                model="mock-local",
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="mock-local")

    monkeypatch.setattr("releasepilot.cli.main._collect_groups", lambda base, head: [group])
    monkeypatch.setattr("releasepilot.cli.main.build_provider", lambda config, mode=None: VariantProvider())

    rc = main(["draft", "suggest", "--base", "v1.0.0", "--id", target_item.id, "--preview", "--model-provider", "mock"])

    assert rc == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["count"] == 1
    assert preview["items"][0]["suggestions"] == ["Clarifies login guidance for users"]


def test_draft_suggest_preview_rejects_too_similar_candidates(monkeypatch, tmp_path: Path, capsys):
    state_path = tmp_path / "state.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(state_path))
    group = _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow")
    target_item = Classifier().classify(group, "v1.0.0", "HEAD")[0]

    class VariantProvider:
        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(
                content=(
                    f"1. {target_item.content.summary}\n"
                    "2. Clarifies login recovery guidance for release readers"
                ),
                confidence=0.9,
                model="mock-local",
                metadata={"prompt_tokens": 12},
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="mock-local")

    monkeypatch.setattr("releasepilot.cli.main._collect_groups", lambda base, head: [group])
    monkeypatch.setattr("releasepilot.cli.main.build_provider", lambda config, mode=None: VariantProvider())

    rc = main(
        [
            "draft",
            "suggest",
            "--base",
            "v1.0.0",
            "--id",
            target_item.id,
            "--preview",
            "--model-provider",
            "mock",
        ]
    )

    assert rc == 0
    preview = json.loads(capsys.readouterr().out)
    item = preview["items"][0]
    assert item["suggestions"] == ["Clarifies login recovery guidance for release readers"]
    assert item["provider_metadata"] == {"prompt_tokens": 12}
    assert item["rejected_candidates"][0]["reason"] == "too-similar-to-current"
    assert item["rejected_candidates"][0]["similarity_to_current"] == 1.0


def test_draft_suggest_batch_preview_excludes_hidden_items(monkeypatch, tmp_path: Path, capsys):
    state_path = tmp_path / "state.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(state_path))
    groups = [
        _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow"),
        _mk_group(number=2, title="Tune dashboard", commit_sha="b2", commit_message="update dashboard"),
    ]
    first = Classifier().classify(groups[0], "v1.0.0", "HEAD")[0]
    second = Classifier().classify(groups[1], "v1.0.0", "HEAD")[0]

    class VariantProvider:
        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(
                content="1. Clarifies review wording for visible release items",
                confidence=0.9,
                model="mock-local",
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="mock-local")

    monkeypatch.setattr("releasepilot.cli.main._collect_groups", lambda base, head: groups)
    monkeypatch.setattr("releasepilot.cli.main.build_provider", lambda config, mode=None: VariantProvider())

    rc = main(["draft", "review", "--base", "v1.0.0", "--id", second.id, "--status", "hidden"])
    assert rc == 0
    capsys.readouterr()

    rc = main(
        [
            "draft",
            "suggest",
            "--base",
            "v1.0.0",
            "--all-visible",
            "--preview",
            "--count",
            "2",
            "--model-provider",
            "mock",
        ]
    )

    assert rc == 0
    preview = json.loads(capsys.readouterr().out)
    assert [item["item_id"] for item in preview["items"]] == [first.id]


def test_draft_suggest_trace_logs_filter_reasons(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("RELEASEPILOT_TRACE_MODEL_CALLS", "1")
    group = _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow")
    target_item = Classifier().classify(group, "v1.0.0", "HEAD")[0]

    class VariantProvider:
        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(
                content="1. Improve login\n2. Suggested wording\n3. Clarifies login guidance for reviewers",
                confidence=0.9,
                model="mock-local",
                reasoning="The third option uses the diff evidence without repeating the title.",
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="mock-local")

    monkeypatch.setattr("releasepilot.cli.main._collect_groups", lambda base, head: [group])
    monkeypatch.setattr("releasepilot.cli.main.build_provider", lambda config, mode=None: VariantProvider())

    rc = main(["draft", "suggest", "--base", "v1.0.0", "--id", target_item.id, "--preview", "--model-provider", "mock"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "suggest stage=target-selection" in captured.err
    assert "suggest stage=evidence-built" in captured.err
    assert "suggestion reject reason=repeats-title-or-commit" in captured.err
    assert "suggestion reject reason=too-short-or-generic" in captured.err
    assert "suggestion accept index=1" in captured.err
    assert "response-detail model=mock-local" in captured.err
    assert "reasoning_exposed=true" in captured.err
    assert "reasoning_chars=" in captured.err
    assert "decision suggestions parsed=1 requested=1 accepted=Clarifies login guidance for reviewers rejected=2" in captured.err
    preview = json.loads(captured.out)
    assert preview["items"][0]["provider_reasoning"] == ""


def test_draft_suggest_trace_suggestions_alias_enables_trace(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.delenv("RELEASEPILOT_TRACE_MODEL_CALLS", raising=False)
    group = _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow")
    target_item = Classifier().classify(group, "v1.0.0", "HEAD")[0]

    class VariantProvider:
        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(
                content="1. Clarifies login guidance for reviewers",
                confidence=0.9,
                model="mock-local",
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="mock-local")

    monkeypatch.setattr("releasepilot.cli.main._collect_groups", lambda base, head: [group])
    monkeypatch.setattr("releasepilot.cli.main.build_provider", lambda config, mode=None: VariantProvider())

    rc = main(
        [
            "draft",
            "suggest",
            "--base",
            "v1.0.0",
            "--id",
            target_item.id,
            "--preview",
            "--trace-suggestions",
            "--model-provider",
            "mock",
        ]
    )

    assert rc == 0
    captured = capsys.readouterr()
    assert "suggest stage=target-selection" in captured.err
    assert json.loads(captured.out)["items"][0]["suggestions"] == ["Clarifies login guidance for reviewers"]


def test_draft_suggest_can_include_provider_reasoning_payload(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("RELEASEPILOT_TRACE_MODEL_REASONING", "1")
    group = _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow")
    target_item = Classifier().classify(group, "v1.0.0", "HEAD")[0]

    class VariantProvider:
        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(
                content="1. Clarifies login guidance for reviewers",
                confidence=0.9,
                model="mock-local",
                reasoning="The option uses the diff evidence without repeating the title.",
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="mock-local")

    monkeypatch.setattr("releasepilot.cli.main._collect_groups", lambda base, head: [group])
    monkeypatch.setattr("releasepilot.cli.main.build_provider", lambda config, mode=None: VariantProvider())

    rc = main(["draft", "suggest", "--base", "v1.0.0", "--id", target_item.id, "--preview", "--model-provider", "mock"])

    assert rc == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["items"][0]["provider_reasoning"] == (
        "The option uses the diff evidence without repeating the title."
    )


def test_draft_suggest_trace_can_emit_prompt_payloads_and_debug_jsonl(monkeypatch, tmp_path: Path, capsys):
    log_path = tmp_path / "model-debug.jsonl"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("RELEASEPILOT_TRACE_MODEL_CALLS", "1")
    monkeypatch.setenv("RELEASEPILOT_TRACE_MODEL_PAYLOADS", "1")
    monkeypatch.setenv("RELEASEPILOT_TRACE_MODEL_REASONING", "1")
    monkeypatch.setenv("RELEASEPILOT_MODEL_LOG_PATH", str(log_path))
    group = _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow")
    target_item = Classifier().classify(group, "v1.0.0", "HEAD")[0]

    class VariantProvider:
        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(
                content="1. Clarifies login guidance for reviewers",
                confidence=0.9,
                model="mock-local",
                reasoning="Used the login diff hunk.",
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="mock-local")

    monkeypatch.setattr("releasepilot.cli.main._collect_groups", lambda base, head: [group])
    monkeypatch.setattr("releasepilot.cli.main.build_provider", lambda config, mode=None: VariantProvider())

    rc = main(["draft", "suggest", "--base", "v1.0.0", "--id", target_item.id, "--preview", "--model-provider", "mock"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "[releasepilot:model] prompt-preview" in captured.err
    assert "[releasepilot:model] json " in captured.err
    payload_events = [
        json.loads(line.removeprefix("[releasepilot:model] json "))
        for line in captured.err.splitlines()
        if line.startswith("[releasepilot:model] json ")
    ]
    assert any(event["event"] == "prompt_preview" and "Improve login" in event["payload"] for event in payload_events)
    assert any(event["event"] == "reasoning_preview" and event["payload"] == "Used the login diff hunk." for event in payload_events)
    persisted = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert any(record["event"] == "prompt_preview" and "payload" in record for record in persisted)


def test_draft_suggest_redacts_payload_secrets_by_default(monkeypatch, tmp_path: Path, capsys):
    log_path = tmp_path / "model-debug.jsonl"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("RELEASEPILOT_TRACE_MODEL_CALLS", "1")
    monkeypatch.setenv("RELEASEPILOT_TRACE_MODEL_PAYLOADS", "1")
    monkeypatch.setenv("RELEASEPILOT_MODEL_LOG_PATH", str(log_path))
    group = _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow")
    assert group.pr is not None
    group.pr.body = "Use token=ghp_abcdefghijklmnopqrstuvwxyz123456 for local testing"
    target_item = Classifier().classify(group, "v1.0.0", "HEAD")[0]

    class VariantProvider:
        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(
                content="1. Clarifies login guidance for reviewers",
                confidence=0.9,
                model="mock-local",
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="mock-local")

    monkeypatch.setattr("releasepilot.cli.main._collect_groups", lambda base, head: [group])
    monkeypatch.setattr("releasepilot.cli.main.build_provider", lambda config, mode=None: VariantProvider())

    rc = main(["draft", "suggest", "--base", "v1.0.0", "--id", target_item.id, "--preview", "--model-provider", "mock"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "ghp_abcdefghijklmnopqrstuvwxyz" not in captured.err
    assert "redacted=true" in captured.err
    persisted = [json.loads(line) for line in log_path.read_text().splitlines()]
    prompt_records = [record for record in persisted if record.get("event") == "prompt_preview"]
    assert prompt_records
    assert prompt_records[0]["attributes"]["redacted"] is True
    assert "ghp_abcdefghijklmnopqrstuvwxyz" not in prompt_records[0]["payload"]
    assert "[REDACTED]" in prompt_records[0]["payload"]


def test_draft_suggest_defaults_to_ollama_when_no_provider_configured(monkeypatch, tmp_path: Path, capsys):
    state_path = tmp_path / "state.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(state_path))
    monkeypatch.delenv("RELEASEPILOT_MODEL_PROVIDER", raising=False)
    group = _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow")
    target_item = Classifier().classify(group, "v1.0.0", "HEAD")[0]

    class LocalSummaryProvider:
        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="Local model summary", confidence=0.9, model="ollama:llama3.2:3b")

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="[ollama-error] classify should not run", confidence=0.0, model="ollama:llama3.2:3b")

    def fake_build_provider(config, mode=None):
        assert mode == "ollama"
        return LocalSummaryProvider()

    monkeypatch.setattr("releasepilot.cli.main._collect_groups", lambda base, head: [group])
    monkeypatch.setattr("releasepilot.cli.main.build_provider", fake_build_provider)

    rc = main(["draft", "suggest", "--base", "v1.0.0", "--id", target_item.id])

    assert rc == 0
    suggested = json.loads(capsys.readouterr().out)
    assert suggested["edited_summary"] == "Local model summary"


def test_draft_suggest_surfaces_provider_errors(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(tmp_path / "state.json"))
    group = _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow")
    target_item = Classifier().classify(group, "v1.0.0", "HEAD")[0]

    class ErrorProvider:
        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(
                content="[github-copilot-unavailable] missing GITHUB_TOKEN/GH_TOKEN",
                confidence=0.0,
                model="github-copilot:gpt-4o-mini",
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="[github-copilot-error] classify should not run", confidence=0.0, model="github-copilot:gpt-4o-mini")

    monkeypatch.setattr("releasepilot.cli.main._collect_groups", lambda base, head: [group])
    monkeypatch.setattr("releasepilot.cli.main.build_provider", lambda config, mode=None: ErrorProvider())

    try:
        main(["draft", "suggest", "--base", "v1.0.0", "--id", target_item.id, "--model-provider", "github-copilot"])
    except SystemExit as exc:
        assert "missing GITHUB_TOKEN/GH_TOKEN" in str(exc)
    else:
        raise AssertionError("Expected draft suggest to fail with the provider error")


def test_draft_suggest_only_requests_summary_for_selected_item(monkeypatch, tmp_path: Path, capsys):
    class CountingProvider:
        def __init__(self):
            self.summarize_calls = 0
            self.classify_calls = 0

        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            self.summarize_calls += 1
            return ProviderResponse(content="Local targeted summary", confidence=0.9, model="ollama:llama3.2:3b")

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            self.classify_calls += 1
            return ProviderResponse(content="[ollama-error] classify should not be called", confidence=0.0, model="ollama:llama3.2:3b")

    provider = CountingProvider()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(tmp_path / "state.json"))

    groups = [
        ChangeGroup(
            pr=PullRequest(
                number=1,
                title="Add login hints",
                files=[ChangedFile(path="src/auth.py", patch="+ help text")],
            ),
            commits=[Commit(sha="a1", message="update login flow")],
        ),
        ChangeGroup(
            pr=PullRequest(
                number=2,
                title="Tune dashboard spacing",
                files=[ChangedFile(path="src/ui.ts", patch="+ spacing")],
            ),
            commits=[Commit(sha="b2", message="update dashboard layout")],
        ),
    ]
    target_item = Classifier().classify(groups[0], "v1.0.0", "HEAD")[0]

    monkeypatch.setattr("releasepilot.cli.main._collect_groups", lambda base, head: groups)
    monkeypatch.setattr("releasepilot.cli.main.build_provider", lambda config, mode=None: provider)

    rc = main(["draft", "suggest", "--base", "v1.0.0", "--id", target_item.id])

    assert rc == 0
    suggested = json.loads(capsys.readouterr().out)
    assert suggested["edited_summary"] == "Local targeted summary"
    assert provider.summarize_calls == 1
    assert provider.classify_calls == 0


def test_scan_disables_provider_when_change_fanout_exceeds_limit(monkeypatch, tmp_path: Path):
    class CountingProvider:
        def __init__(self):
            self.summarize_calls = 0

        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            self.summarize_calls += 1
            return ProviderResponse(content="Model summary", confidence=0.9, model="mock-local")

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="changed/general/external/patch", confidence=0.9, model="mock-local")

    provider = CountingProvider()
    groups = [
        _mk_group(number=1, title="First change", commit_sha="a1", commit_message="first"),
        _mk_group(number=2, title="Second change", commit_sha="b2", commit_message="second"),
    ]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASEPILOT_MODEL_MAX_ITEMS", "1")
    monkeypatch.setattr("releasepilot.cli.main._collect_groups", lambda base, head: groups)
    monkeypatch.setattr("releasepilot.cli.main.build_provider", lambda config, mode=None: provider)

    from releasepilot.cli.main import detect_change_items

    items = detect_change_items("v1.0.0", "HEAD", model_provider="mock")

    assert len(items) == 2
    assert provider.summarize_calls == 0
    assert all("provider-summary" not in signal for item in items for signal in item.evidence.commit_signals)


def test_draft_suggest_all_visible_refuses_large_model_fanout(monkeypatch, tmp_path: Path):
    groups = [
        _mk_group(number=1, title="First change", commit_sha="a1", commit_message="first"),
        _mk_group(number=2, title="Second change", commit_sha="b2", commit_message="second"),
    ]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASEPILOT_MODEL_MAX_ITEMS", "1")
    monkeypatch.setattr("releasepilot.cli.main._collect_groups", lambda base, head: groups)
    monkeypatch.setattr(
        "releasepilot.cli.main.build_provider",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fanout guard should stop provider setup")),
    )

    try:
        main(["draft", "suggest", "--base", "v1.0.0", "--all-visible", "--preview", "--model-provider", "mock"])
    except SystemExit as exc:
        assert "Refusing to request model suggestions for 2 visible item(s)" in str(exc)
    else:
        raise AssertionError("Expected all-visible suggestion fanout to be refused")


def test_draft_prompt_outputs_prompt_without_calling_provider(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    group = _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow")
    target_item = Classifier().classify(group, "v1.0.0", "HEAD")[0]

    monkeypatch.setattr("releasepilot.cli.main._collect_groups", lambda base, head: [group])
    monkeypatch.setattr(
        "releasepilot.cli.main.build_provider",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("draft prompt should not build a provider")),
    )

    rc = main(["draft", "prompt", "--base", "v1.0.0", "--id", target_item.id])

    assert rc == 0
    prompt_preview = json.loads(capsys.readouterr().out)
    assert prompt_preview["item_id"] == target_item.id
    assert prompt_preview["provider"] == "ollama"
    assert "Lead with what changed and why it matters" in prompt_preview["prompt"]
    assert prompt_preview["prompt_budget"]["prompt_chars"] == len(prompt_preview["prompt"])
    assert prompt_preview["prompt_budget"]["files_included"] == 1


def test_render_uses_edited_summary_and_skips_hidden_items(monkeypatch, tmp_path: Path, capsys):
    from releasepilot.state.store import StateData, StateStore

    state_path = tmp_path / "state.json"
    hidden = _mk_item("ci_hidden", title="Hidden change")
    hidden.status = "hidden"
    hidden.hidden = True
    edited = _mk_item("ci_edited", title="Edited change", summary="Original summary")
    edited.status = "edited"
    edited.edited_summary = "Custom reviewer summary"
    StateStore(str(state_path)).save(StateData(items=[hidden, edited]))
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(state_path))

    rc = main(["render", "--format", "changelog", "--version", "v1.2.0"])

    assert rc == 0
    rendered = capsys.readouterr().out
    assert "Hidden change" not in rendered
    assert "Custom reviewer summary" in rendered


def test_normalize_remote_url_for_github_links():
    assert normalize_remote_url("git@github.com:owner/repo.git") == "https://github.com/owner/repo"
    assert normalize_remote_url("https://github.com/owner/repo.git") == "https://github.com/owner/repo"
