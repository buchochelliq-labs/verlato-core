from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from verlato.cli.main import (
    _build_suggestion_adaptive_recommendations,
    _normalized_provider_timings,
    _ollama_api_ps_url,
    _provider_cache_model_id,
    _resolve_commit_intent_inference,
    _resolve_suggestion_batch_size,
    _resolve_suggestion_cache_backend,
    _resolve_suggestion_cache_ttl_days,
    _resolve_suggestion_parallelism,
    _resolve_suggestion_prompt_profile,
    _resolve_suggestion_strategy,
    _run_model_doctor,
    _run_ollama_doctor,
    main,
)
from verlato.config import load_config
from verlato.core.classifier import Classifier
from verlato.core.drafts import DraftService, normalize_remote_url
from verlato.core.grouper import Grouper
from verlato.core.models import (
    ChangedFile,
    ChangeGroup,
    ChangeItem,
    Classification,
    Commit,
    Content,
    Evidence,
    PullRequest,
    SourceRef,
)
from verlato.providers.base import ProviderRequest, ProviderResponse
from verlato.providers.ollama import OllamaProvider
from verlato.providers.stub import StubProvider
from verlato.state.store import StateData, StateStore
from verlato.state.suggestion_cache import (
    CachedSuggestion,
    JsonSuggestionCacheStore,
    SuggestionCacheError,
    build_suggestion_cache_backend,
    register_suggestion_cache_backend,
)


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


def _progress_events(stderr: str) -> list[dict]:
    prefix = "[verlato:progress] json "
    return [
        json.loads(line.removeprefix(prefix))
        for line in stderr.splitlines()
        if line.startswith(prefix)
    ]


def _suggestion_events(stderr: str) -> list[dict]:
    prefix = "[verlato:suggestion] json "
    return [
        json.loads(line.removeprefix(prefix))
        for line in stderr.splitlines()
        if line.startswith(prefix)
    ]


def test_load_config_defaults_without_file(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.state_path == ".verlato_state.json"
    assert config.suggestion_cache_backend == "json"
    assert config.suggestion_cache_path == ".verlato_suggestion_cache.json"
    assert config.suggestion_cache_ttl_days == 30
    assert config.suggestion_prompt_profile == "conservative"
    assert config.suggestion_adaptive_recommendations is False
    assert config.targets["changelog"].path == "CHANGELOG.md"
    assert config.targets["github-release"].path == ".github/release.md"


def test_load_config_reads_custom_targets(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "verlato.json"
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
    config_path = tmp_path / "verlato.json"
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
    config_path = tmp_path / "verlato.json"
    config_path.write_text(json.dumps({"provider": {"max_items_per_scan": 7}}))
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.provider.max_items_per_scan == 7


def test_load_config_reads_provider_timeout(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "verlato.json"
    config_path.write_text(json.dumps({"provider": {"timeout_seconds": 45}}))
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.provider.timeout_seconds == 45


def test_load_config_reads_ollama_tuning(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "verlato.json"
    config_path.write_text(
        json.dumps(
            {
                "provider": {
                    "keep_alive": "20m",
                    "suggest_model": "gemma3:1b",
                    "num_ctx": 4096,
                    "summary_num_predict": 96,
                    "classify_num_predict": 12,
                    "suggest_num_predict": 120,
                    "suggest_think": True,
                }
            }
        )
    )
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.provider.keep_alive == "20m"
    assert config.provider.suggest_model == "gemma3:1b"
    assert config.provider.num_ctx == 4096
    assert config.provider.summary_num_predict == 96
    assert config.provider.classify_num_predict == 12
    assert config.provider.suggest_num_predict == 120
    assert config.provider.suggest_think is True


def test_load_config_reads_suggestion_count(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "verlato.json"
    config_path.write_text(json.dumps({"suggestion_count": 4}))
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.suggestion_count == 4


def test_load_config_reads_suggestion_parallelism(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "verlato.json"
    config_path.write_text(json.dumps({"suggestion_parallelism": 3}))
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.suggestion_parallelism == 3


def test_load_config_reads_suggestion_batch_size(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "verlato.json"
    config_path.write_text(json.dumps({"suggestion_batch_size": 2}))
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.suggestion_batch_size == 2


def test_load_config_reads_suggestion_strategy(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "verlato.json"
    config_path.write_text(json.dumps({"suggestion_strategy": "single"}))
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.suggestion_strategy == "single"


def test_load_config_reads_commit_intent_inference(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "verlato.json"
    config_path.write_text(json.dumps({"commit_intent_inference": "low-signal"}))
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.commit_intent_inference == "low-signal"


def test_resolve_commit_intent_inference_cli_env_and_config(monkeypatch):
    monkeypatch.setenv("VERLATO_COMMIT_INTENT_INFERENCE", "low-signal")

    assert _resolve_commit_intent_inference("off") == "low-signal"
    assert _resolve_commit_intent_inference("low-signal", requested="off") == "off"
    assert _resolve_commit_intent_inference("off", requested="low-signal") == "low-signal"

    monkeypatch.setenv("VERLATO_COMMIT_INTENT_INFERENCE", "invalid")
    assert _resolve_commit_intent_inference("low-signal") == "low-signal"


def test_load_config_reads_suggestion_prompt_profile_and_adaptive_flag(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "verlato.json"
    config_path.write_text(
        json.dumps(
            {
                "suggestion_prompt_profile": "lean",
                "suggestion_adaptive_recommendations": True,
            }
        )
    )
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.suggestion_prompt_profile == "lean"
    assert config.suggestion_adaptive_recommendations is True


def test_load_config_reads_suggestion_cache_settings(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "verlato.json"
    config_path.write_text(
        json.dumps(
            {
                "suggestion_cache_backend": "json",
                "suggestion_cache_path": ".cache/suggestions.json",
                "suggestion_cache_ttl_days": 7,
            }
        )
    )
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.suggestion_cache_backend == "json"
    assert config.suggestion_cache_path == ".cache/suggestions.json"
    assert config.suggestion_cache_ttl_days == 7


def test_resolve_suggestion_cache_backend_reads_env(monkeypatch):
    monkeypatch.setenv("VERLATO_SUGGESTION_CACHE_BACKEND", " JSON ")
    assert _resolve_suggestion_cache_backend("custom") == "json"

    monkeypatch.delenv("VERLATO_SUGGESTION_CACHE_BACKEND")
    assert _resolve_suggestion_cache_backend("custom-backend") == "custom-backend"
    assert _resolve_suggestion_cache_backend("") == "json"


def test_resolve_suggestion_cache_ttl_days_reads_env(monkeypatch):
    monkeypatch.setenv("VERLATO_SUGGESTION_CACHE_TTL_DAYS", "14")
    assert _resolve_suggestion_cache_ttl_days(30) == 14

    monkeypatch.setenv("VERLATO_SUGGESTION_CACHE_TTL_DAYS", "not-an-int")
    assert _resolve_suggestion_cache_ttl_days(30) == 30

    monkeypatch.setenv("VERLATO_SUGGESTION_CACHE_TTL_DAYS", "-5")
    assert _resolve_suggestion_cache_ttl_days(30) == 0


def test_suggestion_cache_backend_registry_rejects_unknown_backend():
    try:
        build_suggestion_cache_backend(backend="missing-test-backend")
    except SuggestionCacheError as exc:
        assert "missing-test-backend" in str(exc)
        assert "json" in str(exc)
    else:
        raise AssertionError("Expected unknown suggestion cache backend to fail")


def test_resolve_suggestion_parallelism_clamps_cli_env_and_config(monkeypatch):
    monkeypatch.setenv("VERLATO_SUGGESTION_PARALLELISM", "6")

    assert _resolve_suggestion_parallelism(1) == 6
    assert _resolve_suggestion_parallelism(1, requested=99) == 8
    assert _resolve_suggestion_parallelism(3, requested=0) == 1

    monkeypatch.setenv("VERLATO_SUGGESTION_PARALLELISM", "not-an-int")
    assert _resolve_suggestion_parallelism(4) == 4


def test_resolve_suggestion_batch_size_clamps_cli_env_and_config(monkeypatch):
    monkeypatch.setenv("VERLATO_SUGGESTION_BATCH_SIZE", "2")

    assert _resolve_suggestion_batch_size(1) == 2
    assert _resolve_suggestion_batch_size(1, requested=99) == 5
    assert _resolve_suggestion_batch_size(2, requested=0) == 1

    monkeypatch.setenv("VERLATO_SUGGESTION_BATCH_SIZE", "not-an-int")
    assert _resolve_suggestion_batch_size(2) == 2


def test_resolve_suggestion_strategy_cli_env_and_config(monkeypatch):
    monkeypatch.setenv("VERLATO_SUGGESTION_STRATEGY", "full-diff-json")

    assert _resolve_suggestion_strategy("auto") == "full-diff-json"
    try:
        _resolve_suggestion_strategy("auto", requested="instant")
    except SystemExit as exc:
        assert "instant suggestion strategy has been removed" in str(exc)
    else:
        raise AssertionError("Expected instant suggestion strategy to be rejected")
    assert _resolve_suggestion_strategy("auto", requested="paired") == "paired"
    assert _resolve_suggestion_strategy("paired", requested="invalid") == "auto"

    monkeypatch.delenv("VERLATO_SUGGESTION_STRATEGY")
    assert _resolve_suggestion_strategy("single") == "single"


def test_resolve_suggestion_prompt_profile_cli_env_and_config(monkeypatch):
    monkeypatch.setenv("VERLATO_SUGGESTION_PROMPT_PROFILE", "minimal")

    assert _resolve_suggestion_prompt_profile("conservative") == "minimal"
    assert _resolve_suggestion_prompt_profile("conservative", requested="lean") == "lean"
    assert _resolve_suggestion_prompt_profile("lean", requested="invalid") == "conservative"

    monkeypatch.delenv("VERLATO_SUGGESTION_PROMPT_PROFILE")
    assert _resolve_suggestion_prompt_profile("lean") == "lean"


def test_ollama_suggestion_cache_model_id_uses_effective_suggest_model(monkeypatch):
    monkeypatch.delenv("VERLATO_OLLAMA_SUGGEST_MODEL", raising=False)
    base_provider = OllamaProvider(model="base-model")
    suggest_provider = OllamaProvider(model="base-model", suggest_model="fast-model")

    assert _provider_cache_model_id(base_provider, "ollama") == "ollama:base-model"
    assert _provider_cache_model_id(suggest_provider, "ollama") == "ollama:fast-model"


def test_normalized_provider_timings_converts_ollama_metadata():
    timings = _normalized_provider_timings(
        {
            "total_duration": 2_000_000_000,
            "load_duration": 100_000_000,
            "prompt_eval_duration": 700_000_000,
            "eval_duration": 800_000_000,
            "prompt_eval_count": 20,
            "eval_count": 40,
        },
        model_ms=2500,
    )

    assert timings == {
        "ollama_total_ms": 2000,
        "load_ms": 100,
        "prompt_eval_ms": 700,
        "eval_ms": 800,
        "prompt_tokens": 20,
        "eval_tokens": 40,
        "queue_ms": 500,
        "tokens_per_second": 50.0,
    }


def test_adaptive_recommendations_classify_prompt_queue_generation_cache_and_feedback():
    prompt = _build_suggestion_adaptive_recommendations(
        current={
            "provider": "ollama",
            "prompt_profile": "conservative",
            "suggestion_count": 1,
            "suggestion_parallelism": 2,
            "suggestion_batch_size": 2,
            "cache_warming": None,
        },
        timing_sources=[{"timings": {"model_ms": 2000, "prompt_eval_ms": 1200}}],
        feedback_events=[],
        cache_hit_count=0,
        item_count=2,
        prompt_chars=4000,
        source="preview",
    )
    assert prompt["schema_version"] == "verlato.suggestion_adaptive.v1"
    assert any(item["kind"] == "use-lean-prompt-profile" for item in prompt["recommendations"])
    assert any(item["kind"] == "improve-cache-hit-rate" for item in prompt["recommendations"])

    queue = _build_suggestion_adaptive_recommendations(
        current={
            "provider": "ollama",
            "prompt_profile": "conservative",
            "suggestion_count": 1,
            "suggestion_parallelism": 4,
            "suggestion_batch_size": 2,
            "cache_warming": None,
        },
        timing_sources=[{"timings": {"model_ms": 1200, "queue_ms": 600}}],
        feedback_events=[],
        cache_hit_count=1,
        item_count=1,
        prompt_chars=1000,
        source="preview",
    )
    queue_rec = next(item for item in queue["recommendations"] if item["kind"] == "reduce-queueing")
    assert queue_rec["suggested_config"]["suggestion_parallelism"] == 1

    generation = _build_suggestion_adaptive_recommendations(
        current={
            "provider": "ollama",
            "prompt_profile": "lean",
            "suggestion_count": 2,
            "suggestion_parallelism": 1,
            "suggestion_batch_size": 1,
            "cache_warming": None,
        },
        timing_sources=[{"timings": {"model_ms": 2000, "eval_ms": 1100}}],
        feedback_events=[],
        cache_hit_count=1,
        item_count=1,
        prompt_chars=1000,
        source="preview",
    )
    generation_rec = next(item for item in generation["recommendations"] if item["kind"] == "reduce-generation-cost")
    assert generation_rec["suggested_config"]["suggestion_count"] == 1

    feedback = _build_suggestion_adaptive_recommendations(
        current={
            "provider": "ollama",
            "prompt_profile": "lean",
            "suggestion_count": 1,
            "suggestion_parallelism": 1,
            "suggestion_batch_size": 1,
            "cache_warming": None,
        },
        timing_sources=[{"timings": {"model_ms": 2000, "prompt_eval_ms": 1200}}],
        feedback_events=[
            {"event": "suggestion_marked_not_useful"},
            {"event": "suggestion_regenerated"},
            {"event": "suggestion_skipped"},
        ],
        cache_hit_count=1,
        item_count=1,
        prompt_chars=1000,
        source="preview",
    )
    assert any(item["kind"] == "tune-style-before-slimming" for item in feedback["recommendations"])

    source_feedback = _build_suggestion_adaptive_recommendations(
        current={
            "provider": "ollama",
            "prompt_profile": "conservative",
            "suggestion_count": 1,
            "suggestion_parallelism": 1,
            "suggestion_batch_size": 1,
            "cache_warming": None,
        },
        timing_sources=[],
        feedback_events=[
            {"event": "suggestion_applied", "metadata": {"source": "model-suggestion"}},
            {"event": "suggestion_marked_useful", "metadata": {"source": "model-suggestion"}},
            {"event": "suggestion_marked_not_useful", "metadata": {"source": "model-suggestion"}},
        ],
        cache_hit_count=1,
        item_count=1,
        prompt_chars=1000,
        source="preview",
    )
    model_feedback = source_feedback["signals"]["feedback"]["source_feedback"]["model-suggestion"]
    assert model_feedback["event_count"] == 3
    assert model_feedback["positive_rate"] == 0.667
    assert not any(item["kind"] == "use-minimal-prompt-profile" for item in feedback["recommendations"])


def test_ollama_doctor_command_emits_json(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)

    class DoctorProvider:
        endpoint = "http://127.0.0.1:11434/api/chat"
        model = "doctor-model"
        timeout_seconds = 15.0
        stream = False
        keep_alive = "10m"
        num_ctx = 4096
        num_predict = None
        suggest_num_predict = 96
        suggest_think = False

        def _options_for_task(self, task: str, *, request_data: ProviderRequest):
            return {"num_ctx": self.num_ctx, "num_predict": self.suggest_num_predict}

        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            assert request.task == "suggest"
            return ProviderResponse(
                content="Adds a model health check for suggestion performance.",
                confidence=0.9,
                model="ollama:doctor-model",
                metadata={
                    "total_duration": 100_000_000,
                    "eval_duration": 50_000_000,
                    "eval_count": 10,
                },
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="ollama:doctor-model")

    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: DoctorProvider())

    rc = main(["doctor", "ollama"])
    result = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert result["schema"] == "ollama_doctor.v1"
    assert result["config"]["model"] == "doctor-model"
    assert result["config"]["suggest_num_predict_effective"] == 96
    assert result["probes"][0]["ok"] is True
    assert result["summary"]["bottleneck"] == "healthy"
    assert "adaptive_recommendations" not in result


def test_model_doctor_validates_structured_diff_response(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)

    class DoctorProvider:
        model = "doctor-main"
        suggest_model = "doctor-fast"

        def model_for_task(self, task: str) -> str:
            return self.suggest_model if task == "suggest" else self.model

        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            assert request.task == "suggest"
            assert "docs/release-notes.md" in request.prompt
            assert request.response_format["required"] == ["items"]
            return ProviderResponse(
                content=json.dumps(
                    {"items": [{"item_id": "doctor_model", "suggestions": ["Clarifies model health probing"]}]}
                ),
                confidence=0.9,
                model="mock-doctor",
                metadata={"audit": {"request_hash": "abc"}},
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="mock-doctor")

    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None, suggest_model=None: DoctorProvider())

    result = _run_model_doctor(model_provider="mock")

    assert result["ok"] is True
    assert result["status"] == "ok"
    assert result["model"] == "mock-doctor"


def test_ollama_doctor_adaptive_recommendations_are_opt_in(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)

    class FakeOllamaProvider:
        model = "gemma4:latest"
        endpoint = "http://127.0.0.1:11434/api/chat"
        timeout_seconds = 15
        stream = False
        keep_alive = "10m"
        num_ctx = None
        num_predict = None
        suggest_num_predict = None
        suggest_think = False

        def _options_for_task(self, task: str, *, request_data: ProviderRequest):
            return {"num_predict": 96}

        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(
                content="Adds model health suggestions.",
                confidence=0.9,
                model="ollama:gemma4:latest",
                metadata={
                    "total_duration": 2_000_000_000,
                    "prompt_eval_duration": 1_200_000_000,
                    "eval_duration": 200_000_000,
                },
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="ollama:gemma4:latest")

    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: FakeOllamaProvider())
    monkeypatch.setattr(
        "verlato.cli.main._collect_ollama_runtime",
        lambda provider, snapshot: {"api_ps": {"ok": True, "models": []}, "selected_model": {"loaded": False}},
    )

    result = _run_ollama_doctor(adaptive_recommendations=True)

    assert result["adaptive_recommendations"]["schema_version"] == "verlato.suggestion_adaptive.v1"
    assert result["adaptive_recommendations"]["current"]["prompt_profile"] == "conservative"
    assert result["adaptive_recommendations"]["recommendations"]


def test_ollama_doctor_uses_effective_suggest_model_for_runtime(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("verlato.cli.main.shutil.which", lambda name: None)

    class SuggestDoctorProvider:
        endpoint = "http://127.0.0.1:11434/api/chat"
        model = "base-model"
        suggest_model = "fast-model"
        timeout_seconds = 15.0
        stream = False
        keep_alive = "10m"
        num_ctx = None
        num_predict = None
        suggest_num_predict = None
        suggest_think = False

        def model_for_task(self, task: str) -> str:
            return self.suggest_model if task == "suggest" else self.model

        def _options_for_task(self, task: str, *, request_data: ProviderRequest):
            return {"num_predict": 96}

        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(
                content="Adds model health suggestions.",
                confidence=0.9,
                model="ollama:fast-model",
                metadata={"total_duration": 80_000_000, "eval_duration": 40_000_000},
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="ollama:base-model")

    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None, suggest_model=None: SuggestDoctorProvider())
    monkeypatch.setattr(
        "verlato.cli.main._get_json",
        lambda *args, **kwargs: {"models": [{"name": "fast-model", "model": "fast-model"}]},
    )

    result = _run_ollama_doctor(suggest_model="fast-model")

    assert result["config"]["model"] == "base-model"
    assert result["config"]["suggest_model"] == "fast-model"
    assert result["config"]["effective_suggest_model"] == "fast-model"
    assert result["runtime"]["selected_model"]["model"] == "fast-model"
    assert result["runtime"]["selected_model"]["loaded"] is True


def test_ollama_doctor_runtime_resolves_api_ps_urls():
    assert _ollama_api_ps_url("http://127.0.0.1:11434/api/chat") == "http://127.0.0.1:11434/api/ps"
    assert _ollama_api_ps_url("http://127.0.0.1:11434/api/generate") == "http://127.0.0.1:11434/api/ps"
    assert _ollama_api_ps_url("http://127.0.0.1:11434") == "http://127.0.0.1:11434/api/ps"


def test_ollama_doctor_includes_api_ps_runtime_state(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("verlato.cli.main.shutil.which", lambda name: None)

    class RuntimeProvider:
        endpoint = "http://127.0.0.1:11434/api/chat"
        model = "doctor-model"
        timeout_seconds = 15.0
        stream = False
        keep_alive = "10m"
        num_ctx = None
        num_predict = None
        suggest_num_predict = None
        suggest_think = False

        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(
                content="Adds a model health check for suggestion performance.",
                confidence=0.9,
                model="ollama:doctor-model",
                metadata={"total_duration": 80_000_000, "eval_duration": 40_000_000},
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="ollama:doctor-model")

    def fake_get_json(url: str, *, timeout_seconds: float):
        assert url == "http://127.0.0.1:11434/api/ps"
        return {
            "models": [
                {
                    "name": "doctor-model",
                    "model": "doctor-model",
                    "size": 5_000_000_000,
                    "size_vram": 4_000_000_000,
                    "context_length": 4096,
                    "expires_at": "2099-01-01T00:00:00Z",
                    "details": {"parameter_size": "4B"},
                }
            ]
        }

    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: RuntimeProvider())
    monkeypatch.setattr("verlato.cli.main._get_json", fake_get_json)

    result = _run_ollama_doctor()

    assert result["runtime"]["api_ps"]["ok"] is True
    assert result["runtime"]["api_ps"]["url"] == "http://127.0.0.1:11434/api/ps"
    assert result["runtime"]["selected_model"]["loaded"] is True
    assert result["runtime"]["selected_model"]["size_vram"] == 4_000_000_000
    assert result["runtime"]["selected_model"]["context_length"] == 4096


def test_ollama_doctor_runtime_api_ps_failure_is_diagnostic(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("verlato.cli.main.shutil.which", lambda name: None)

    class RuntimeProvider:
        endpoint = "http://127.0.0.1:11434/api/chat"
        model = "doctor-model"
        timeout_seconds = 15.0
        stream = False
        keep_alive = "10m"
        num_ctx = None
        num_predict = None
        suggest_num_predict = None
        suggest_think = False

        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(
                content="Adds a model health check for suggestion performance.",
                confidence=0.9,
                model="ollama:doctor-model",
                metadata={"total_duration": 80_000_000, "eval_duration": 40_000_000},
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="ollama:doctor-model")

    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: RuntimeProvider())
    monkeypatch.setattr("verlato.cli.main._get_json", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("api down")))

    result = _run_ollama_doctor()

    assert result["probes"][0]["ok"] is True
    assert result["summary"]["bottleneck"] == "healthy"
    assert result["runtime"]["api_ps"]["ok"] is False
    assert any("/api/ps" in diagnostic and "api down" in diagnostic for diagnostic in result["summary"]["diagnostics"])


def test_ollama_doctor_runtime_uses_optional_cli_ps_processor(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)

    class RuntimeProvider:
        endpoint = "http://127.0.0.1:11434/api/chat"
        model = "doctor-model"
        timeout_seconds = 15.0
        stream = False
        keep_alive = "10m"
        num_ctx = None
        num_predict = None
        suggest_num_predict = None
        suggest_think = False

        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(
                content="Adds a model health check for suggestion performance.",
                confidence=0.9,
                model="ollama:doctor-model",
                metadata={"total_duration": 80_000_000, "eval_duration": 40_000_000},
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="ollama:doctor-model")

    class FakeCompleted:
        returncode = 0
        stdout = "NAME    ID    SIZE    PROCESSOR    UNTIL\ndoctor-model    abc123    5 GB    100% GPU    4 minutes from now\n"
        stderr = ""

    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: RuntimeProvider())
    monkeypatch.setattr(
        "verlato.cli.main._get_json",
        lambda *args, **kwargs: {"models": [{"name": "doctor-model", "model": "doctor-model", "size_vram": 4_000_000_000}]},
    )
    monkeypatch.setattr("verlato.cli.main.shutil.which", lambda name: "ollama")
    monkeypatch.setattr("verlato.cli.main.subprocess.run", lambda *args, **kwargs: FakeCompleted())

    result = _run_ollama_doctor()

    assert result["runtime"]["cli_ps"]["ok"] is True
    assert result["runtime"]["cli_ps"]["rows"][0]["processor"] == "100% GPU"
    assert result["runtime"]["selected_model"]["processor"] == "100% GPU"


def test_ollama_doctor_runtime_recommends_warmup_when_model_not_loaded(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("verlato.cli.main.shutil.which", lambda name: None)

    class RuntimeProvider:
        endpoint = "http://127.0.0.1:11434/api/chat"
        model = "doctor-model"
        timeout_seconds = 15.0
        stream = False
        keep_alive = "10m"
        num_ctx = None
        num_predict = None
        suggest_num_predict = None
        suggest_think = False

        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(
                content="Adds a model health check for suggestion performance.",
                confidence=0.9,
                model="ollama:doctor-model",
                metadata={"total_duration": 80_000_000, "eval_duration": 40_000_000},
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="ollama:doctor-model")

    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: RuntimeProvider())
    monkeypatch.setattr("verlato.cli.main._get_json", lambda *args, **kwargs: {"models": []})

    result = _run_ollama_doctor()

    assert result["runtime"]["selected_model"]["loaded"] is False
    assert any("not reported" in diagnostic for diagnostic in result["summary"]["diagnostics"])
    assert any("Warm model" in item for item in result["summary"]["recommendations"])


def test_ollama_doctor_runtime_warns_about_large_loaded_context(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VERLATO_SUGGESTION_PARALLELISM", "3")
    monkeypatch.setattr("verlato.cli.main.shutil.which", lambda name: None)

    class RuntimeProvider:
        endpoint = "http://127.0.0.1:11434/api/chat"
        model = "doctor-model"
        timeout_seconds = 15.0
        stream = False
        keep_alive = "10m"
        num_ctx = None
        num_predict = None
        suggest_num_predict = None
        suggest_think = False

        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(
                content="Adds a model health check for suggestion performance.",
                confidence=0.9,
                model="ollama:doctor-model",
                metadata={"total_duration": 80_000_000, "eval_duration": 40_000_000},
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="ollama:doctor-model")

    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: RuntimeProvider())
    monkeypatch.setattr(
        "verlato.cli.main._get_json",
        lambda *args, **kwargs: {
            "models": [
                {
                    "name": "doctor-model",
                    "model": "doctor-model",
                    "size_vram": 4_000_000_000,
                    "context_length": 32768,
                }
            ]
        },
    )

    result = _run_ollama_doctor()

    assert any("context length is 32768" in diagnostic for diagnostic in result["summary"]["diagnostics"])
    assert any("Lower suggestionParallelism" in item for item in result["summary"]["recommendations"])


def test_ollama_doctor_warmup_runs_before_measured_probe(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)

    class WarmupProvider:
        endpoint = "http://127.0.0.1:11434/api/chat"
        model = "doctor-model"
        timeout_seconds = 15.0
        stream = False
        keep_alive = "10m"
        num_ctx = None
        num_predict = None
        suggest_num_predict = None
        suggest_think = False

        def __init__(self) -> None:
            self.calls = 0

        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            self.calls += 1
            metadata = (
                {"total_duration": 2_000_000_000, "load_duration": 1_700_000_000}
                if self.calls == 1
                else {"total_duration": 80_000_000, "eval_duration": 40_000_000}
            )
            return ProviderResponse(
                content="Adds a model health check for suggestion performance.",
                confidence=0.9,
                model="ollama:doctor-model",
                metadata=metadata,
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="ollama:doctor-model")

    provider = WarmupProvider()
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: provider)

    rc = main(["doctor", "ollama", "--warmup"])
    result = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert provider.calls == 2
    assert [probe["name"] for probe in result["probes"]] == ["warmup", "single"]
    assert [probe["purpose"] for probe in result["probes"]] == ["warmup", "measure"]
    assert result["summary"]["warmed_up"] is True
    assert result["summary"]["bottleneck"] == "healthy"
    assert "absorbed 1700ms" in result["summary"]["diagnostics"][1]


def test_ollama_doctor_classifies_common_bottlenecks(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VERLATO_SUGGESTION_PARALLELISM", "3")

    class DoctorProvider:
        endpoint = "http://127.0.0.1:11434/api/chat"
        model = "doctor-model"
        timeout_seconds = 15.0
        stream = False
        keep_alive = "10m"
        num_ctx = None
        num_predict = None
        suggest_num_predict = None
        suggest_think = False

        def __init__(self, metadata: dict[str, object], model_ms_padding: float = 0.0) -> None:
            self.metadata = metadata
            self.model_ms_padding = model_ms_padding

        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            if self.model_ms_padding:
                time.sleep(self.model_ms_padding)
            return ProviderResponse(
                content="Adds a model health check for suggestion performance.",
                confidence=0.9,
                model="ollama:doctor-model",
                metadata=self.metadata,
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="ollama:doctor-model")

    cases = [
        (
            "load",
            {"total_duration": 1_000_000_000, "load_duration": 800_000_000},
            0.0,
        ),
        (
            "prompt_eval",
            {"total_duration": 1_000_000_000, "prompt_eval_duration": 800_000_000},
            0.0,
        ),
        (
            "generation",
            {"total_duration": 1_000_000_000, "eval_duration": 800_000_000},
            0.0,
        ),
        (
            "queue",
            {"total_duration": 20_000_000, "eval_duration": 10_000_000},
            0.35,
        ),
    ]
    for expected, metadata, padding in cases:
        monkeypatch.setattr(
            "verlato.cli.main.build_provider",
            lambda config, mode=None, metadata=metadata, padding=padding: DoctorProvider(metadata, padding),
        )
        result = _run_ollama_doctor()
        assert result["summary"]["bottleneck"] == expected
        assert result["summary"]["status"] == "warning"
        assert result["summary"]["recommendations"]
        if expected == "queue":
            assert result["summary"]["recommended_settings"]["suggestion_parallelism"] == 1
            assert any("parallelism" in item for item in result["summary"]["recommendations"])


def test_ollama_doctor_warns_about_large_context_when_prompt_eval_dominates(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)

    class ContextProvider:
        endpoint = "http://127.0.0.1:11434/api/chat"
        model = "doctor-model"
        timeout_seconds = 15.0
        stream = False
        keep_alive = "10m"
        num_ctx = 16384
        num_predict = None
        suggest_num_predict = None
        suggest_think = False

        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(
                content="Adds a model health check for suggestion performance.",
                confidence=0.9,
                model="ollama:doctor-model",
                metadata={"total_duration": 1_000_000_000, "prompt_eval_duration": 800_000_000},
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="ollama:doctor-model")

    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: ContextProvider())

    result = _run_ollama_doctor()

    assert result["summary"]["bottleneck"] == "prompt_eval"
    assert any("num_ctx is 16384" in diagnostic for diagnostic in result["summary"]["diagnostics"])
    assert any("lower it" in item for item in result["summary"]["recommendations"])


def test_ollama_doctor_reports_unavailable_provider(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)

    class UnavailableProvider:
        endpoint = "http://127.0.0.1:11434/api/chat"
        model = "missing-model"
        timeout_seconds = 15.0
        stream = False
        keep_alive = "10m"
        num_ctx = None
        num_predict = None
        suggest_num_predict = None
        suggest_think = False

        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(
                content="[ollama-unavailable] unable to reach local Ollama",
                confidence=0.0,
                model="ollama:missing-model",
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="ollama:missing-model")

    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: UnavailableProvider())

    result = _run_ollama_doctor()

    assert result["summary"]["status"] == "error"
    assert result["summary"]["bottleneck"] == "unavailable"
    assert "Ollama is running" in result["summary"]["recommendations"][0]


def test_ollama_doctor_parallel_probe_overlaps_calls(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)

    class BlockingProvider:
        endpoint = "http://127.0.0.1:11434/api/chat"
        model = "doctor-model"
        timeout_seconds = 15.0
        stream = False
        keep_alive = "10m"
        num_ctx = None
        num_predict = None
        suggest_num_predict = None
        suggest_think = False

        def __init__(self) -> None:
            self.calls = 0
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()
            self.barrier = threading.Barrier(2, timeout=2)

        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            with self.lock:
                self.calls += 1
                call_number = self.calls
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                if call_number > 1:
                    self.barrier.wait()
                    time.sleep(0.02)
                return ProviderResponse(
                    content="Adds a model health check for suggestion performance.",
                    confidence=0.9,
                    model="ollama:doctor-model",
                    metadata={"total_duration": 20_000_000, "eval_duration": 10_000_000},
                )
            finally:
                with self.lock:
                    self.active -= 1

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="ollama:doctor-model")

    provider = BlockingProvider()
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: provider)

    result = _run_ollama_doctor(parallel_probe=True)

    assert provider.calls == 3
    assert provider.max_active == 2
    assert [probe["name"] for probe in result["probes"]] == ["single", "parallel-a", "parallel-b"]


def test_load_config_reads_suggestion_tone(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "verlato.json"
    config_path.write_text(json.dumps({"suggestion_tone": "technical"}))
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.suggestion_tone == "technical"


def test_load_config_reads_suggestion_style(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "verlato.json"
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
    config_path = tmp_path / "verlato.json"
    config_path.write_text(json.dumps({"feedback_include_text": True}))
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.feedback_include_text is True


def test_load_config_defaults_to_one_suggestion(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.suggestion_count == 1
    assert config.suggestion_batch_size == 5
    assert config.suggestion_strategy == "auto"


def test_draft_json_command_outputs_sections_and_targets(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "verlato.cli.main._detect_change_items_with_provider",
        lambda base, head, model_provider, **_kwargs: [_mk_item("ci_1", base_ref=base, head_ref=head, kind="added")],
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


def test_draft_document_groups_extended_changelog_sections(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    document = DraftService().build_document(
        items=[
            _mk_item("ci_docs", kind="documentation", title="Document API"),
            _mk_item("ci_breaking", kind="breaking", title="Replace API"),
            _mk_item("ci_perf", kind="perf", title="Speed up API"),
        ],
        base_ref="v1.0.0",
        head_ref="HEAD",
        version="v1.1.0",
    )

    assert [(section.kind, section.title) for section in document.sections] == [
        ("breaking", "Breaking Changes"),
        ("documentation", "Documentation"),
        ("performance", "Performance"),
    ]


def test_draft_document_distinguishes_inferred_classification_source(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    item = _mk_item("ci_inferred", kind="added", title="Add API tokens")
    item.evidence.commit_signals = ["inferred-conventional:feat(api)"]

    document = DraftService().build_document(
        items=[item],
        base_ref="v1.0.0",
        head_ref="HEAD",
        version="v1.1.0",
    )

    assert document.sections[0].items[0].classification_source == (
        "Deterministic classification from inferred conventional commit evidence"
    )


def test_progress_telemetry_is_silent_by_default(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "verlato.cli.main._detect_change_items_with_provider",
        lambda base, head, model_provider, **_kwargs: [_mk_item("ci_1", base_ref=base, head_ref=head)],
    )

    rc = main(["draft", "json", "--base", "v1.0.0"])
    captured = capsys.readouterr()

    assert rc == 0
    assert json.loads(captured.out)["item_count"] == 1
    assert "[verlato:progress]" not in captured.err


def test_progress_telemetry_emits_when_enabled(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VERLATO_TRACE_PROGRESS", "1")
    monkeypatch.setattr(
        "verlato.cli.main._detect_change_items_with_provider",
        lambda base, head, model_provider, **_kwargs: [_mk_item("ci_1", base_ref=base, head_ref=head)],
    )

    rc = main(["draft", "json", "--base", "v1.0.0"])
    captured = capsys.readouterr()
    events = _progress_events(captured.err)

    assert rc == 0
    assert "[verlato:progress]" not in captured.out
    assert json.loads(captured.out)["item_count"] == 1
    assert {event["stage"] for event in events} >= {
        "draft_detecting_changes",
        "draft_rendering",
        "draft_ready",
        "draft_json_serializing",
    }
    assert any(
        event.get("current") == 1 and event.get("total") == 1 and event.get("unit") == "item"
        for event in events
    )


def test_draft_preview_command_renders_github_release(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "verlato.cli.main._detect_change_items_with_provider",
        lambda base, head, model_provider, **_kwargs: [_mk_item("ci_1", base_ref=base, head_ref=head, kind="fixed")],
    )

    rc = main(["draft", "preview", "--base", "v1.0.0", "--version", "v1.0.1"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "## v1.0.1" in out
    assert "### Fixed" in out


def test_draft_json_ignores_configured_provider_without_explicit_override(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VERLATO_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("VERLATO_MODEL_PROVIDER", "mock")
    group = _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow")
    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: [group])

    def fail_build_provider(*args, **kwargs):
        raise AssertionError("draft json should stay deterministic unless --model-provider is explicit")

    monkeypatch.setattr("verlato.cli.main.build_provider", fail_build_provider)

    rc = main(["draft", "json", "--base", "v1.0.0", "--version", "v1.1.0"])

    assert rc == 0
    document = json.loads(capsys.readouterr().out)
    item = document["sections"][0]["items"][0]
    assert item["summary_source"] == "Deterministic summary generated from changed-file evidence"


def test_draft_apply_updates_changelog_target(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\nExisting notes\n")
    monkeypatch.setattr(
        "verlato.cli.main._detect_change_items_with_provider",
        lambda base, head, model_provider, **_kwargs: [_mk_item("ci_1", base_ref=base, head_ref=head)],
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
        "verlato.cli.main._detect_change_items_with_provider",
        lambda base, head, model_provider, **_kwargs: [_mk_item("ci_1", base_ref=base, head_ref=head)],
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
    monkeypatch.setenv("VERLATO_STATE_PATH", str(state_path))
    group = _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow")
    target_item = Classifier().classify(group, "v1.0.0", "HEAD")[0]
    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: [group])

    rc = main(
        [
            "draft",
            "review",
            "--base",
            "v1.0.0",
            "--id",
            target_item.id,
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

    rc = main(["draft", "json", "--base", "v1.0.0", "--version", "v1.1.0", "--model-provider", "stub"])
    assert rc == 0
    document = json.loads(capsys.readouterr().out)
    assert document["sections"][0]["items"][0]["status"] == "hidden"
    assert document["sections"][0]["items"][0]["summary"] == "Edited hidden summary"
    assert document["sections"][0]["items"][0]["original_summary"] != "Edited hidden summary"
    assert document["sections"][0]["items"][0]["summary_source"].startswith("Human edited summary")
    assert "Improve login" not in document["rendered"]["github-release"]


def test_draft_json_excludes_items_already_published_in_another_release(monkeypatch, tmp_path: Path, capsys):
    state_path = tmp_path / "state.json"
    published = _mk_item("ci_1")
    published.published = True
    published.published_in = "v1.0.0"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VERLATO_STATE_PATH", str(state_path))
    StateStore(str(state_path)).save(StateData(items=[published]))
    monkeypatch.setattr(
        "verlato.cli.main._detect_change_items_with_provider",
        lambda base, head, model_provider, **_kwargs: [_mk_item("ci_1", base_ref=base, head_ref=head)],
    )

    rc = main(["draft", "json", "--base", "v0.9.0", "--head", "HEAD", "--version", "v1.1.0"])

    assert rc == 0
    document = json.loads(capsys.readouterr().out)
    assert document["item_count"] == 0
    assert document["sections"] == []


def test_draft_suggest_persists_model_summary(monkeypatch, tmp_path: Path, capsys):
    state_path = tmp_path / "state.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VERLATO_STATE_PATH", str(state_path))
    group = _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow")
    target_item = Classifier().classify(group, "v1.0.0", "HEAD")[0]

    class MockSummaryProvider:
        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="Model-written summary", confidence=0.9, model="mock-local")

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="[mock-classification] changed/general/external/patch", confidence=0.8, model="mock-local")

    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: [group])

    def fake_build_provider(config, mode=None):
        if mode == "mock":
            return MockSummaryProvider()
        return StubProvider()

    monkeypatch.setattr("verlato.cli.main.build_provider", fake_build_provider)

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

    rc = main(["draft", "json", "--base", "v1.0.0", "--model-provider", "stub"])
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
    monkeypatch.setenv("VERLATO_STATE_PATH", str(state_path))
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
    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: [group])

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

    rc = main(["draft", "json", "--base", "v1.0.0", "--model-provider", "stub"])
    assert rc == 0
    document = json.loads(capsys.readouterr().out)
    item = document["sections"][0]["items"][0]
    assert item["summary_provenance"]["prompt_sha"] == "promptabc123"
    assert item["summary_source"] == "LLM summary generated from diff evidence (gemma4:latest)"


def test_feedback_record_and_export(monkeypatch, tmp_path: Path, capsys):
    state_path = tmp_path / "state.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VERLATO_STATE_PATH", str(state_path))

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
    assert exported["schema_version"] == "verlato.feedback.v1"
    assert exported["record_count"] == 1
    assert exported["records"][0]["event"] == "suggestion_skipped"
    assert exported["records"][0]["metadata"] == {"scope": "item"}


def test_feedback_recommend_uses_safe_aggregates(monkeypatch, tmp_path: Path, capsys):
    state_path = tmp_path / "state.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VERLATO_STATE_PATH", str(state_path))
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
    assert payload["schema_version"] == "verlato.feedback_recommendations.v1"
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
    monkeypatch.setenv("VERLATO_STATE_PATH", str(state_path))
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

    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: [group])
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: VariantProvider())

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
    assert preview["prompt_profile"] == "conservative"
    assert "adaptive_recommendations" not in preview
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


def test_draft_suggest_prompt_profile_and_adaptive_recommendations_are_opt_in(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VERLATO_STATE_PATH", str(tmp_path / "state.json"))
    group = _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow")
    target_item = Classifier().classify(group, "v1.0.0", "HEAD")[0]
    prompts: list[str] = []

    class AdaptiveProvider:
        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            prompts.append(request.prompt)
            return ProviderResponse(
                content="Clarifies login recovery guidance for reviewers",
                confidence=0.9,
                model="mock-local",
                metadata={
                    "total_duration": 2_000_000_000,
                    "prompt_eval_duration": 1_200_000_000,
                    "eval_duration": 200_000_000,
                },
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="mock-local")

    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: [group])
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: AdaptiveProvider())

    rc = main(
        [
            "draft",
            "suggest",
            "--base",
            "v1.0.0",
            "--id",
            target_item.id,
            "--preview",
            "--prompt-profile",
            "lean",
            "--adaptive-recommendations",
            "--model-provider",
            "mock",
        ]
    )
    preview = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert preview["prompt_profile"] == "lean"
    assert preview["items"][0]["prompt_budget"]["profile"] == "suggestion-lean"
    assert preview["adaptive_recommendations"]["schema_version"] == "verlato.suggestion_adaptive.v1"
    assert preview["adaptive_recommendations"]["current"]["prompt_profile"] == "lean"
    assert any(
        recommendation["kind"] in {"use-minimal-prompt-profile", "keep-current-settings"}
        for recommendation in preview["adaptive_recommendations"]["recommendations"]
    )
    assert prompts


def test_draft_suggest_preview_uses_cli_suggest_model_context(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VERLATO_STATE_PATH", str(tmp_path / "state.json"))
    group = _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow")
    target_item = Classifier().classify(group, "v1.0.0", "HEAD")[0]
    build_calls: list[dict[str, object]] = []

    class SuggestModelProvider:
        model = "base-model"
        suggest_model = "fast-model"

        def model_for_task(self, task: str) -> str:
            return self.suggest_model if task == "suggest" else self.model

        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(
                content="Clarifies login recovery guidance for reviewers",
                confidence=0.9,
                model="ollama:fast-model",
                metadata={"effective_model": "fast-model", "base_model": "base-model", "suggest_model": "fast-model"},
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="ollama:base-model")

    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: [group])

    def fake_build_provider(config, mode=None, suggest_model=None):
        build_calls.append({"mode": mode, "suggest_model": suggest_model})
        return SuggestModelProvider()

    monkeypatch.setattr("verlato.cli.main.build_provider", fake_build_provider)

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
            "ollama",
            "--suggest-model",
            "fast-model",
        ]
    )
    preview = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert build_calls[0]["suggest_model"] == "fast-model"
    assert preview["model"] == "base-model"
    assert preview["suggest_model"] == "fast-model"
    assert preview["effective_suggest_model"] == "fast-model"
    assert preview["items"][0]["provider"] == "ollama:fast-model"


def test_draft_suggest_applies_style_controls_to_prompt_and_rejections(monkeypatch, tmp_path: Path, capsys):
    config_path = tmp_path / "verlato.json"
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
    monkeypatch.setenv("VERLATO_STATE_PATH", str(state_path))
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

    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: [group])
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: StyledProvider())

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
    monkeypatch.setenv("VERLATO_STATE_PATH", str(state_path))
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

    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: [group])
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: VariantProvider())

    rc = main(["draft", "suggest", "--base", "v1.0.0", "--id", target_item.id, "--preview", "--model-provider", "mock"])

    assert rc == 0
    captured = capsys.readouterr()
    preview = json.loads(captured.out)
    assert preview["count"] == 1
    assert preview["items"][0]["suggestions"] == ["Clarifies login guidance for users"]
    assert "[verlato:suggestion]" not in captured.err


def test_draft_suggest_cache_hit_skips_provider_call(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VERLATO_STATE_PATH", str(tmp_path / "state.json"))
    group = _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow")
    target_item = Classifier().classify(group, "v1.0.0", "HEAD")[0]
    calls = {"count": 0}

    class CacheWritingProvider:
        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            calls["count"] += 1
            return ProviderResponse(
                content="Clarifies login recovery guidance for reviewers",
                confidence=0.9,
                model="mock-local",
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="mock-local")

    class FailingProvider:
        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            raise AssertionError("cache hit should skip provider summarize")

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="mock-local")

    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: [group])
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: CacheWritingProvider())

    rc = main(["draft", "suggest", "--base", "v1.0.0", "--id", target_item.id, "--preview", "--model-provider", "mock"])
    first = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert calls["count"] == 1
    assert first["items"][0]["cache"]["hit"] is False
    assert (tmp_path / ".verlato_suggestion_cache.json").exists()

    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: FailingProvider())
    rc = main(["draft", "suggest", "--base", "v1.0.0", "--id", target_item.id, "--preview", "--model-provider", "mock"])
    second = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert second["items"][0]["suggestions"] == ["Clarifies login recovery guidance for reviewers"]
    assert second["items"][0]["cache"]["hit"] is True
    assert second["items"][0]["cache"]["age_seconds"] >= 0
    assert second["items"][0]["timings"]["model_ms"] == 0
    assert second["items"][0]["timings"]["parse_ms"] == 0


def test_draft_suggest_can_use_registered_cache_backend(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VERLATO_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("VERLATO_SUGGESTION_CACHE_BACKEND", "unit-test-hit")
    monkeypatch.setenv("VERLATO_TRACE_PROGRESS", "1")
    group = _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow")
    target_item = Classifier().classify(group, "v1.0.0", "HEAD")[0]
    backend_calls = {"load": 0, "get": 0}

    class AlwaysHitBackend:
        def __init__(self, path: str, ttl_days: int) -> None:
            self.path = Path(path)
            self.ttl_days = ttl_days
            self.dirty = False

        def load(self):
            backend_calls["load"] += 1
            return self

        def get(self, key: str) -> CachedSuggestion:
            backend_calls["get"] += 1
            return CachedSuggestion(
                key=key,
                item={
                    "item_id": target_item.id,
                    "title": target_item.content.title,
                    "current_summary": target_item.content.summary,
                    "provider": "custom-cache",
                    "suggestion_details": [],
                    "rejected_candidates": [],
                    "suggestions": ["Clarifies cached login recovery guidance for reviewers"],
                },
                created_at="2026-05-06T12:00:00Z",
                updated_at="2026-05-06T12:00:00Z",
                age_seconds=42,
            )

        def put(self, key: str, item: dict) -> None:
            raise AssertionError("cache hit should not write")

        def save(self) -> None:
            raise AssertionError("clean cache should not save")

        def clear(self) -> int:
            return 0

    class FailingProvider:
        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            raise AssertionError("registered cache backend should skip provider summarize")

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="mock-local")

    register_suggestion_cache_backend("unit-test-hit", lambda path, ttl_days: AlwaysHitBackend(path, ttl_days))
    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: [group])
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: FailingProvider())

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
    captured = capsys.readouterr()
    preview = json.loads(captured.out)
    events = _suggestion_events(captured.err)

    assert rc == 0
    assert backend_calls == {"load": 1, "get": 1}
    assert preview["items"][0]["provider"] == "custom-cache"
    assert preview["items"][0]["cache"]["hit"] is True
    assert preview["items"][0]["cache"]["age_seconds"] == 42
    assert not [event for event in events if event["event"] == "suggestion_item_provisional"]
    assert [event for event in events if event["event"] == "suggestion_item_result"]
    assert not (tmp_path / ".verlato_suggestion_cache.json").exists()


def test_draft_suggest_refresh_cache_bypasses_read_and_overwrites(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VERLATO_STATE_PATH", str(tmp_path / "state.json"))
    group = _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow")
    target_item = Classifier().classify(group, "v1.0.0", "HEAD")[0]
    responses = [
        "Clarifies login recovery guidance for reviewers",
        "Clarifies refreshed login recovery guidance for reviewers",
    ]

    class RefreshProvider:
        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content=responses.pop(0), confidence=0.9, model="mock-local")

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="mock-local")

    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: [group])
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: RefreshProvider())

    assert main(["draft", "suggest", "--base", "v1.0.0", "--id", target_item.id, "--preview", "--model-provider", "mock"]) == 0
    capsys.readouterr()
    rc = main(
        [
            "draft",
            "suggest",
            "--base",
            "v1.0.0",
            "--id",
            target_item.id,
            "--preview",
            "--refresh-cache",
            "--model-provider",
            "mock",
        ]
    )
    refreshed = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert refreshed["items"][0]["suggestions"] == ["Clarifies refreshed login recovery guidance for reviewers"]
    assert refreshed["items"][0]["cache"]["hit"] is False
    assert responses == []

    class FailingProvider:
        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            raise AssertionError("refreshed cache should satisfy the next run")

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="mock-local")

    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: FailingProvider())
    assert main(["draft", "suggest", "--base", "v1.0.0", "--id", target_item.id, "--preview", "--model-provider", "mock"]) == 0
    cached = json.loads(capsys.readouterr().out)
    assert cached["items"][0]["suggestions"] == ["Clarifies refreshed login recovery guidance for reviewers"]
    assert cached["items"][0]["cache"]["hit"] is True


def test_draft_suggest_no_cache_neither_reads_nor_writes(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VERLATO_STATE_PATH", str(tmp_path / "state.json"))
    group = _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow")
    target_item = Classifier().classify(group, "v1.0.0", "HEAD")[0]
    calls = {"count": 0}

    class NoCacheProvider:
        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            calls["count"] += 1
            return ProviderResponse(
                content="Clarifies login recovery guidance for reviewers",
                confidence=0.9,
                model="mock-local",
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="mock-local")

    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: [group])
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: NoCacheProvider())

    for _ in range(2):
        rc = main(
            [
                "draft",
                "suggest",
                "--base",
                "v1.0.0",
                "--id",
                target_item.id,
                "--preview",
                "--no-cache",
                "--model-provider",
                "mock",
            ]
        )
        preview = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert preview["items"][0]["cache"]["hit"] is False

    assert calls["count"] == 2
    assert not (tmp_path / ".verlato_suggestion_cache.json").exists()


def test_draft_suggest_cache_key_changes_with_count(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VERLATO_STATE_PATH", str(tmp_path / "state.json"))
    group = _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow")
    target_item = Classifier().classify(group, "v1.0.0", "HEAD")[0]
    calls = {"count": 0}

    class CountingProvider:
        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            calls["count"] += 1
            return ProviderResponse(
                content=(
                    "1. Clarifies login recovery guidance for reviewers\n"
                    "2. Documents login recovery guidance for support teams"
                ),
                confidence=0.9,
                model="mock-local",
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="mock-local")

    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: [group])
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: CountingProvider())

    assert main(["draft", "suggest", "--base", "v1.0.0", "--id", target_item.id, "--preview", "--count", "1", "--model-provider", "mock"]) == 0
    capsys.readouterr()
    assert main(["draft", "suggest", "--base", "v1.0.0", "--id", target_item.id, "--preview", "--count", "2", "--model-provider", "mock"]) == 0
    second = json.loads(capsys.readouterr().out)

    assert calls["count"] == 2
    assert second["items"][0]["cache"]["hit"] is False
    assert second["items"][0]["suggestions"] == [
        "Clarifies login recovery guidance for reviewers",
        "Documents login recovery guidance for support teams",
    ]


def test_draft_suggest_expired_cache_entry_is_ignored_and_compacted(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VERLATO_STATE_PATH", str(tmp_path / "state.json"))
    group = _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow")
    target_item = Classifier().classify(group, "v1.0.0", "HEAD")[0]
    responses = [
        "Clarifies login recovery guidance for reviewers",
        "Clarifies login recovery guidance after cache expiry",
    ]

    class ExpiringProvider:
        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content=responses.pop(0), confidence=0.9, model="mock-local")

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="mock-local")

    cache_path = tmp_path / ".verlato_suggestion_cache.json"
    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: [group])
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: ExpiringProvider())

    assert main(["draft", "suggest", "--base", "v1.0.0", "--id", target_item.id, "--preview", "--model-provider", "mock"]) == 0
    capsys.readouterr()
    cache_data = json.loads(cache_path.read_text())
    cache_key = next(iter(cache_data["entries"]))
    cache_data["entries"][cache_key]["updated_at"] = "2000-01-01T00:00:00Z"
    cache_path.write_text(json.dumps(cache_data))

    assert main(["draft", "suggest", "--base", "v1.0.0", "--id", target_item.id, "--preview", "--model-provider", "mock"]) == 0
    preview = json.loads(capsys.readouterr().out)
    refreshed_cache = json.loads(cache_path.read_text())

    assert responses == []
    assert preview["items"][0]["suggestions"] == ["Clarifies login recovery guidance after cache expiry"]
    assert preview["items"][0]["cache"]["hit"] is False
    assert refreshed_cache["entries"][cache_key]["updated_at"] != "2000-01-01T00:00:00Z"


def test_json_suggestion_cache_save_merges_newer_disk_entries(tmp_path: Path):
    cache_path = tmp_path / "suggestions.json"
    first = JsonSuggestionCacheStore(str(cache_path), ttl_days=30).load()
    second = JsonSuggestionCacheStore(str(cache_path), ttl_days=30).load()

    first.put("key-a", {"item_id": "ci_a", "suggestions": ["First cached suggestion"]})
    second.put("key-b", {"item_id": "ci_b", "suggestions": ["Second cached suggestion"]})

    first.save()
    second.save()

    data = json.loads(cache_path.read_text())
    assert set(data["entries"]) == {"key-a", "key-b"}
    assert data["entries"]["key-a"]["item"]["item_id"] == "ci_a"
    assert data["entries"]["key-b"]["item"]["item_id"] == "ci_b"


def test_json_suggestion_cache_concurrent_saves_do_not_drop_entries(tmp_path: Path):
    cache_path = tmp_path / "suggestions.json"
    barrier = threading.Barrier(4)

    def write_entry(index: int) -> None:
        store = JsonSuggestionCacheStore(str(cache_path), ttl_days=30).load()
        store.put(f"key-{index}", {"item_id": f"ci_{index}", "suggestions": [f"Suggestion {index}"]})
        barrier.wait(timeout=5)
        store.save()

    threads = [threading.Thread(target=write_entry, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    data = json.loads(cache_path.read_text())
    assert set(data["entries"]) == {"key-0", "key-1", "key-2", "key-3"}


def test_json_suggestion_cache_clear_uses_locked_file_count(tmp_path: Path):
    cache_path = tmp_path / "suggestions.json"
    store = JsonSuggestionCacheStore(str(cache_path), ttl_days=30).load()
    store.put("key-a", {"item_id": "ci_a", "suggestions": ["First cached suggestion"]})
    store.put("key-b", {"item_id": "ci_b", "suggestions": ["Second cached suggestion"]})
    store.save()

    removed = JsonSuggestionCacheStore(str(cache_path), ttl_days=30).load().clear()

    assert removed == 2
    assert not cache_path.exists()


def test_draft_suggest_streams_suggestion_events_when_traced(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VERLATO_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("VERLATO_TRACE_PROGRESS", "1")
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

    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: [group])
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: VariantProvider())

    rc = main(["draft", "suggest", "--base", "v1.0.0", "--id", target_item.id, "--preview", "--model-provider", "mock"])
    captured = capsys.readouterr()
    preview = json.loads(captured.out)
    events = _suggestion_events(captured.err)
    item_events = [event for event in events if event["event"] == "suggestion_item_result"]

    assert rc == 0
    assert "[verlato:suggestion]" not in captured.out
    assert not [event for event in events if event["event"] == "suggestion_item_provisional"]
    assert events[0]["event"] == "suggestion_preview_started"
    assert events[0]["preview"]["base_ref"] == "v1.0.0"
    assert events[0]["preview"]["head_ref"] == "HEAD"
    assert events[0]["preview"]["count"] == 1
    assert events[0]["preview"]["items"] == []
    assert item_events[0]["order"] == 0
    assert item_events[0]["item_id"] == target_item.id
    assert item_events[0]["item"] == preview["items"][0]


def test_draft_suggest_preview_rejects_too_similar_candidates(monkeypatch, tmp_path: Path, capsys):
    state_path = tmp_path / "state.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VERLATO_STATE_PATH", str(state_path))
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

    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: [group])
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: VariantProvider())

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
    monkeypatch.setenv("VERLATO_STATE_PATH", str(state_path))
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

    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: groups)
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: VariantProvider())

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
    monkeypatch.setenv("VERLATO_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("VERLATO_TRACE_MODEL_CALLS", "1")
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

    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: [group])
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: VariantProvider())

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
    monkeypatch.setenv("VERLATO_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.delenv("VERLATO_TRACE_MODEL_CALLS", raising=False)
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

    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: [group])
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: VariantProvider())

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
    monkeypatch.setenv("VERLATO_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("VERLATO_TRACE_MODEL_REASONING", "1")
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

    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: [group])
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: VariantProvider())

    rc = main(["draft", "suggest", "--base", "v1.0.0", "--id", target_item.id, "--preview", "--model-provider", "mock"])

    assert rc == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["items"][0]["provider_reasoning"] == (
        "The option uses the diff evidence without repeating the title."
    )


def test_draft_suggest_trace_can_emit_prompt_payloads_and_debug_jsonl(monkeypatch, tmp_path: Path, capsys):
    log_path = tmp_path / "model-debug.jsonl"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VERLATO_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("VERLATO_TRACE_MODEL_CALLS", "1")
    monkeypatch.setenv("VERLATO_TRACE_MODEL_PAYLOADS", "1")
    monkeypatch.setenv("VERLATO_TRACE_MODEL_REASONING", "1")
    monkeypatch.setenv("VERLATO_MODEL_LOG_PATH", str(log_path))
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

    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: [group])
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: VariantProvider())

    rc = main(["draft", "suggest", "--base", "v1.0.0", "--id", target_item.id, "--preview", "--model-provider", "mock"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "[verlato:model] prompt-preview" in captured.err
    assert "[verlato:model] json " in captured.err
    payload_events = [
        json.loads(line.removeprefix("[verlato:model] json "))
        for line in captured.err.splitlines()
        if line.startswith("[verlato:model] json ")
    ]
    assert any(event["event"] == "prompt_preview" and "Improve login" in event["payload"] for event in payload_events)
    assert any(event["event"] == "reasoning_preview" and event["payload"] == "Used the login diff hunk." for event in payload_events)
    persisted = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert any(record["event"] == "prompt_preview" and "payload" in record for record in persisted)


def test_draft_suggest_redacts_payload_secrets_by_default(monkeypatch, tmp_path: Path, capsys):
    log_path = tmp_path / "model-debug.jsonl"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VERLATO_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("VERLATO_TRACE_MODEL_CALLS", "1")
    monkeypatch.setenv("VERLATO_TRACE_MODEL_PAYLOADS", "1")
    monkeypatch.setenv("VERLATO_MODEL_LOG_PATH", str(log_path))
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

    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: [group])
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: VariantProvider())

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
    monkeypatch.setenv("VERLATO_STATE_PATH", str(state_path))
    monkeypatch.delenv("VERLATO_MODEL_PROVIDER", raising=False)
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

    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: [group])
    monkeypatch.setattr("verlato.cli.main.build_provider", fake_build_provider)

    rc = main(["draft", "suggest", "--base", "v1.0.0", "--id", target_item.id])

    assert rc == 0
    suggested = json.loads(capsys.readouterr().out)
    assert suggested["edited_summary"] == "Local model summary"


def test_draft_suggest_surfaces_provider_errors(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VERLATO_STATE_PATH", str(tmp_path / "state.json"))
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

    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: [group])
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: ErrorProvider())

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
    monkeypatch.setenv("VERLATO_STATE_PATH", str(tmp_path / "state.json"))

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

    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: groups)
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: provider)

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
    monkeypatch.setenv("VERLATO_MODEL_MAX_ITEMS", "1")
    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: groups)
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: provider)

    from verlato.cli.main import detect_change_items

    items = detect_change_items("v1.0.0", "HEAD", model_provider="mock")

    assert len(items) == 2
    assert provider.summarize_calls == 0
    assert all("provider-summary" not in signal for item in items for signal in item.evidence.commit_signals)


def test_draft_suggest_all_visible_batches_large_model_fanout(monkeypatch, tmp_path: Path, capsys):
    class CountingProvider:
        def __init__(self):
            self.summarize_calls = 0

        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            self.summarize_calls += 1
            return ProviderResponse(
                content="Adds a focused model-written suggestion for this release note.",
                confidence=0.9,
                model="mock-local",
                metadata={
                    "total_duration": 50_000_000,
                    "prompt_eval_duration": 20_000_000,
                    "eval_duration": 30_000_000,
                    "prompt_eval_count": 8,
                    "eval_count": 6,
                },
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="mock-local")

    provider = CountingProvider()
    groups = [
        _mk_group(number=1, title="First change", commit_sha="a1", commit_message="first"),
        _mk_group(number=2, title="Second change", commit_sha="b2", commit_message="second"),
    ]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VERLATO_MODEL_MAX_ITEMS", "1")
    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: groups)
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: provider)

    rc = main(["draft", "suggest", "--base", "v1.0.0", "--all-visible", "--preview", "--model-provider", "mock"])
    preview = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert provider.summarize_calls == 2
    assert preview["visible_item_count"] == 2
    assert preview["fanout_guard"] == 1
    assert preview["batch_size"] == 1
    assert preview["batch_count"] == 2
    assert len(preview["items"]) == 2


def test_draft_suggest_ollama_batches_two_uncached_items(monkeypatch, tmp_path: Path, capsys):
    class BatchProvider:
        def __init__(self):
            self.calls: list[ProviderRequest] = []

        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            self.calls.append(request)
            assert request.response_format is not None
            item_ids = request.evidence["item_ids"]
            return ProviderResponse(
                content=json.dumps(
                    {
                        "items": [
                            {
                                "item_id": item_id,
                                "suggestions": [
                                    f"Clarifies batched release-note behavior for {item_id} reviewers"
                                ],
                            }
                            for item_id in item_ids
                        ]
                    }
                ),
                confidence=0.9,
                model="ollama:doctor-model",
                metadata={
                    "total_duration": 100_000_000,
                    "prompt_eval_duration": 40_000_000,
                    "eval_duration": 60_000_000,
                },
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="ollama:doctor-model")

    provider = BatchProvider()
    groups = [
        _mk_group(number=1, title="First change", commit_sha="a1", commit_message="first"),
        _mk_group(number=2, title="Second change", commit_sha="b2", commit_message="second"),
    ]
    expected_ids = [Classifier().classify(group, "v1.0.0", "HEAD")[0].id for group in groups]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: groups)
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: provider)

    rc = main([
        "draft",
        "suggest",
        "--base",
        "v1.0.0",
        "--all-visible",
        "--preview",
        "--model-provider",
        "ollama",
        "--privacy",
        "local-summary-then-ai",
        "--strategy",
        "full-diff-json",
        "--batch-size",
        "2",
    ])
    preview = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert len(provider.calls) == 1
    assert provider.calls[0].evidence["batch"] is True
    assert provider.calls[0].evidence["strategy"] == "full-diff-json"
    assert provider.calls[0].evidence["item_ids"] == expected_ids
    assert '"patch": "+ change"' in provider.calls[0].prompt
    assert '"commits": [' in provider.calls[0].prompt
    assert "prepared_prompt" not in provider.calls[0].prompt
    assert [item["item_id"] for item in preview["items"]] == expected_ids
    assert preview["performance"]["strategy"] == "full-diff-json"
    assert preview["performance"]["batch_size"] == 2
    assert preview["performance"]["model_call_count"] == 1
    assert preview["performance"]["batched_model_calls"] == 1
    assert preview["performance"]["single_model_calls"] == 0
    assert preview["performance"]["bulk_model_calls"] == 1
    assert preview["performance"]["full_diff_file_count"] == 2
    assert preview["performance"]["full_diff_patch_chars"] == len("+ change") * 2
    assert preview["performance"]["model_calls_saved"] == 1
    assert preview["performance"]["model_ms"] >= 0
    assert all(item["timings"]["batch_item_count"] == 2 for item in preview["items"])
    assert all(item["timings"]["strategy"] == "full-diff-json" for item in preview["items"])
    assert all("batch_model_ms" in item["timings"] for item in preview["items"])
    assert all(item["full_diff"]["patch_chars"] == len("+ change") for item in preview["items"])


def test_draft_suggest_preflight_builds_sanitized_envelope_without_provider_call(
    monkeypatch, tmp_path: Path, capsys
):
    class NoCallProvider:
        model = "litellm:test-model"

        def __init__(self):
            self.calls = 0

        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            self.calls += 1
            raise AssertionError("preflight should not call the provider")

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            raise AssertionError("preflight should not classify with the provider")

    patch = "@@ -1 +1 @@\n-old auth\n+PREFLIGHT_SENTINEL"
    group = _mk_group(
        number=7,
        title="Improve auth privacy",
        commit_sha="a1",
        commit_message="feat: improve auth privacy",
        patch=patch,
    )
    provider = NoCallProvider()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: [group])
    monkeypatch.setattr(
        "verlato.cli.main.build_provider",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("preflight should not build a provider")),
    )

    rc = main([
        "draft",
        "suggest",
        "--base",
        "v1.0.0",
        "--all-visible",
        "--preflight",
        "--no-cache",
        "--model-provider",
        "litellm",
        "--strategy",
        "full-diff-json",
    ])
    preview = json.loads(capsys.readouterr().out)
    envelope = preview["items"][0]["request_envelope"]
    envelope_json = json.dumps(envelope, sort_keys=True)

    assert rc == 0
    assert provider.calls == 0
    assert preview["preflight"] is True
    assert preview["performance"]["model_call_count"] == 0
    assert preview["performance"]["strategy"] == "single"
    assert preview["privacy"]["mode"] == "metadata-only"
    assert preview["privacy"]["raw_patch_chars_available"] == len(patch)
    assert preview["privacy"]["raw_patch_chars_sent"] == 0
    assert preview["items"][0]["preflight"] is True
    assert preview["items"][0]["suggestions"] == []
    assert preview["items"][0]["ai_request"]["raw_patch_chars_sent"] == 0
    assert preview["items"][0]["ai_request"]["contains_raw_patch_markers"] is False
    assert "PREFLIGHT_SENTINEL" not in envelope_json
    assert "@@" not in envelope_json
    assert '"patch":' not in envelope_json


def test_draft_suggest_remote_default_never_sends_raw_patch_payload(
    monkeypatch, tmp_path: Path, capsys
):
    class CapturingProvider:
        def __init__(self):
            self.calls: list[ProviderRequest] = []

        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            self.calls.append(request)
            return ProviderResponse(
                content="Clarifies authentication release-note behavior for reviewers without exposing raw diff text.",
                confidence=0.9,
                model="litellm:test-model",
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="litellm:test-model")

    patch = "@@ -1 +1 @@\n-old auth\n+REMOTE_SENTINEL"
    group = _mk_group(
        number=7,
        title="Improve auth privacy",
        commit_sha="a1",
        commit_message="feat: improve auth privacy",
        patch=patch,
    )
    provider = CapturingProvider()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: [group])
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: provider)

    rc = main([
        "draft",
        "suggest",
        "--base",
        "v1.0.0",
        "--all-visible",
        "--preview",
        "--no-cache",
        "--model-provider",
        "litellm",
        "--strategy",
        "full-diff-json",
    ])
    preview = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert len(provider.calls) == 1
    request = provider.calls[0]
    evidence_json = json.dumps(request.evidence, sort_keys=True)
    assert preview["performance"]["strategy"] == "single"
    assert preview["performance"]["raw_patch_chars_available"] == len(patch)
    assert preview["performance"]["raw_patch_chars_sent"] == 0
    assert preview["privacy"]["mode"] == "metadata-only"
    assert preview["items"][0]["ai_request"]["raw_patch_chars_sent"] == 0
    assert preview["items"][0]["ai_request"]["contains_raw_patch_markers"] is False
    assert "REMOTE_SENTINEL" not in request.prompt
    assert "@@" not in request.prompt
    assert "REMOTE_SENTINEL" not in evidence_json
    assert "@@" not in evidence_json
    assert '"patch":' not in evidence_json


def test_draft_suggest_full_diff_json_sends_uncapped_patch_body_and_files(monkeypatch, tmp_path: Path, capsys):
    class CapturingProvider:
        def __init__(self):
            self.calls: list[ProviderRequest] = []

        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            self.calls.append(request)
            item_id = request.evidence["item_ids"][0]
            return ProviderResponse(
                content=json.dumps(
                    {
                        "items": [
                            {
                                "item_id": item_id,
                                "suggestions": [
                                    "Documents the full diff evidence path for release-note reviewers."
                                ],
                            }
                        ]
                    }
                ),
                confidence=0.9,
                model="ollama:test-model",
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="ollama:test-model")

    long_patch = "@@ -1,2 +1,3 @@\n" + "+ " + ("x" * 400) + " FULL_DIFF_SENTINEL"
    group = ChangeGroup(
        pr=PullRequest(
            number=7,
            title="Full diff change",
            body="Full pull request body that should not be capped.",
            labels=["release-note"],
            files=[
                ChangedFile(path="src/full.py", status="modified", patch=long_patch),
                ChangedFile(path="docs/full.md", status="added", patch="+ docs"),
                ChangedFile(path="assets/logo.png", status="modified", patch=""),
            ],
        ),
        commits=[
            Commit(sha="a1", message="feat: first full diff commit\n\nbody one"),
            Commit(sha="b2", message="fix: second full diff commit"),
        ],
    )
    provider = CapturingProvider()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: [group])
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: provider)

    rc = main([
        "draft",
        "suggest",
        "--base",
        "v1.0.0",
        "--all-visible",
        "--preview",
        "--model-provider",
        "ollama",
        "--privacy",
        "local-summary-then-ai",
        "--strategy",
        "full-diff-json",
    ])
    preview = json.loads(capsys.readouterr().out)

    assert rc == 0
    prompt = provider.calls[0].prompt
    assert "FULL_DIFF_SENTINEL" in prompt
    assert "Full pull request body that should not be capped." in prompt
    assert "feat: first full diff commit" in prompt
    assert "fix: second full diff commit" in prompt
    assert '"path": "src/full.py"' in prompt
    assert '"path": "docs/full.md"' in prompt
    assert '"path": "assets/logo.png"' in prompt
    assert '"patch_available": false' in prompt
    assert "representative_hunks" not in prompt
    assert preview["items"][0]["timings"]["patch_chars"] == len(long_patch) + len("+ docs")
    assert preview["items"][0]["timings"]["missing_patch_count"] == 1


def test_draft_suggest_full_diff_json_uses_commit_patch_when_pr_patch_missing(
    monkeypatch, tmp_path: Path, capsys
):
    class CapturingProvider:
        def __init__(self):
            self.calls: list[ProviderRequest] = []

        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            self.calls.append(request)
            item_id = request.evidence["item_ids"][0]
            return ProviderResponse(
                content=json.dumps(
                    {
                        "items": [
                            {
                                "item_id": item_id,
                                "suggestions": [
                                    "Uses local commit diff evidence when pull request file patches are unavailable."
                                ],
                            }
                        ]
                    }
                ),
                confidence=0.9,
                model="ollama:test-model",
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="ollama:test-model")

    patch = "@@ -1 +1 @@\n-old fallback\n+LOCAL_PATCH_SENTINEL"
    commit = Commit(
        sha="a1",
        message="Merge pull request #7 from feature/release-control",
        files=[ChangedFile(path="src/auth.py", status="modified", patch=patch)],
    )
    pr = PullRequest(
        number=7,
        title="Patchless PR metadata",
        files=[ChangedFile(path="src/auth.py", status="modified", patch="")],
    )
    groups = Grouper().group([commit], {"a1": pr})
    provider = CapturingProvider()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: groups)
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: provider)

    rc = main([
        "draft",
        "suggest",
        "--base",
        "v1.0.0",
        "--all-visible",
        "--preview",
        "--model-provider",
        "ollama",
        "--privacy",
        "local-summary-then-ai",
        "--strategy",
        "full-diff-json",
    ])
    preview = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert "LOCAL_PATCH_SENTINEL" in provider.calls[0].prompt
    assert preview["items"][0]["timings"]["patch_chars"] == len(patch)
    assert preview["items"][0]["timings"]["missing_patch_count"] == 0


def test_draft_suggest_auto_uses_ollama_tool_retrieval(monkeypatch, tmp_path: Path, capsys):
    class ToolCapturingProvider:
        def __init__(self):
            self.calls: list[ProviderRequest] = []
            self.patch_result: dict[str, object] | None = None

        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            self.calls.append(request)
            assert request.tools
            assert request.tool_executor is not None
            self.patch_result = request.tool_executor(
                "verlato_get_file_patch",
                {"item_id": request.evidence["item_id"], "path": "src/auth.py"},
            )
            return ProviderResponse(
                content="Explains the authentication diff using tool-retrieved patch evidence.",
                confidence=0.9,
                model="ollama:test-model",
                metadata={"tool_call_count": 1, "tool_names": ["verlato_get_file_patch"]},
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="ollama:test-model")

    patch = "@@ -1 +1 @@\n-old auth\n+TOOL_PATCH_SENTINEL"
    group = _mk_group(
        number=7,
        title="Tool-backed diff",
        commit_sha="a1",
        commit_message="feat: improve auth",
        path="src/auth.py",
        patch=patch,
    )
    provider = ToolCapturingProvider()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: [group])
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None, suggest_model=None: provider)

    rc = main([
        "draft",
        "suggest",
        "--base",
        "v1.0.0",
        "--all-visible",
        "--preview",
        "--model-provider",
        "ollama",
        "--privacy",
        "local-summary-then-ai",
    ])
    preview = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert preview["performance"]["strategy"] == "tool-retrieval"
    assert preview["performance"]["batch_size"] == 1
    assert provider.calls[0].evidence["strategy"] == "tool-retrieval"
    assert provider.calls[0].max_tool_rounds > 0
    assert "TOOL_PATCH_SENTINEL" not in provider.calls[0].prompt
    assert provider.patch_result is not None
    assert "TOOL_PATCH_SENTINEL" in str(provider.patch_result["patch"])
    assert preview["items"][0]["prompt_budget"]["tool_count"] >= 3
    assert preview["items"][0]["provider_metadata"]["tool_call_count"] == 1


def test_draft_suggest_batch_size_one_preserves_single_item_calls(monkeypatch, tmp_path: Path, capsys):
    class CountingProvider:
        def __init__(self):
            self.calls: list[ProviderRequest] = []

        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            self.calls.append(request)
            return ProviderResponse(
                content="Clarifies single release-note behavior for reviewers without batching.",
                confidence=0.9,
                model="ollama:test-model",
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="ollama:test-model")

    provider = CountingProvider()
    groups = [
        _mk_group(number=1, title="First change", commit_sha="a1", commit_message="first"),
        _mk_group(number=2, title="Second change", commit_sha="b2", commit_message="second"),
    ]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: groups)
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: provider)

    rc = main([
        "draft",
        "suggest",
        "--base",
        "v1.0.0",
        "--all-visible",
        "--preview",
        "--model-provider",
        "ollama",
        "--batch-size",
        "1",
        "--strategy",
        "single",
    ])
    preview = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert len(provider.calls) == 2
    assert all(call.response_format is None for call in provider.calls)
    assert preview["performance"]["batch_size"] == 1
    assert preview["performance"]["single_model_calls"] == 2
    assert preview["performance"]["model_calls_saved"] == 0


def test_draft_suggest_batch_falls_back_to_single_item_calls(monkeypatch, tmp_path: Path, capsys):
    class FallbackProvider:
        def __init__(self):
            self.calls: list[ProviderRequest] = []

        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            self.calls.append(request)
            if request.evidence.get("batch"):
                return ProviderResponse(content="not json", confidence=0.9, model="ollama:test-model")
            return ProviderResponse(
                content=f"Clarifies fallback release-note behavior for {request.evidence['item_id']} reviewers",
                confidence=0.9,
                model="ollama:test-model",
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="ollama:test-model")

    provider = FallbackProvider()
    groups = [
        _mk_group(number=1, title="First change", commit_sha="a1", commit_message="first"),
        _mk_group(number=2, title="Second change", commit_sha="b2", commit_message="second"),
    ]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: groups)
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: provider)

    rc = main([
        "draft",
        "suggest",
        "--base",
        "v1.0.0",
        "--all-visible",
        "--preview",
        "--model-provider",
        "ollama",
        "--privacy",
        "local-summary-then-ai",
        "--strategy",
        "paired",
        "--batch-size",
        "2",
    ])
    preview = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert len(provider.calls) == 3
    assert provider.calls[0].evidence["batch"] is True
    assert all(not call.evidence.get("batch") for call in provider.calls[1:])
    assert preview["performance"]["model_call_count"] == 3
    assert preview["performance"]["batched_model_calls"] == 1
    assert preview["performance"]["single_model_calls"] == 2
    assert preview["performance"]["model_calls_saved"] == 0


def test_draft_suggest_batch_retries_missing_unusable_and_error_items(monkeypatch, tmp_path: Path, capsys):
    groups = [
        _mk_group(number=1, title="First change", commit_sha="a1", commit_message="first"),
        _mk_group(number=2, title="Second change", commit_sha="b2", commit_message="second"),
    ]
    item_ids = [Classifier().classify(group, "v1.0.0", "HEAD")[0].id for group in groups]

    cases = {
        "missing": (
            {
                "items": [
                    {
                        "item_id": item_ids[0],
                        "suggestions": [f"Clarifies batched release-note behavior for {item_ids[0]} reviewers"],
                    }
                ]
            },
            2,
            1,
            1,
        ),
        "unusable": (
            {
                "items": [
                    {
                        "item_id": item_ids[0],
                        "suggestions": [f"Clarifies batched release-note behavior for {item_ids[0]} reviewers"],
                    },
                    {
                        "item_id": item_ids[1],
                        "suggestions": ["Second change"],
                    },
                ]
            },
            2,
            1,
            1,
        ),
        "provider-error": ("[ollama-error] local model request failed", 3, 1, 2),
    }

    for case, (batch_content, expected_calls, expected_batched, expected_single) in cases.items():
        class FallbackProvider:
            def __init__(self):
                self.calls: list[ProviderRequest] = []

            def summarize(self, request: ProviderRequest) -> ProviderResponse:
                self.calls.append(request)
                if request.evidence.get("batch"):
                    content = batch_content if isinstance(batch_content, str) else json.dumps(batch_content)
                    return ProviderResponse(content=content, confidence=0.9, model="ollama:test-model")
                return ProviderResponse(
                    content=f"Clarifies fallback release-note behavior for {request.evidence['item_id']} reviewers",
                    confidence=0.9,
                    model="ollama:test-model",
                )

            def classify(self, request: ProviderRequest) -> ProviderResponse:
                return ProviderResponse(content="", confidence=0.0, model="ollama:test-model")

        provider = FallbackProvider()
        case_dir = tmp_path / case
        case_dir.mkdir()
        monkeypatch.chdir(case_dir)
        monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: groups)
        monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None, provider=provider: provider)

        rc = main([
            "draft",
            "suggest",
            "--base",
            "v1.0.0",
            "--all-visible",
            "--preview",
            "--model-provider",
            "ollama",
            "--privacy",
            "local-summary-then-ai",
            "--strategy",
            "paired",
            "--batch-size",
            "2",
        ])
        preview = json.loads(capsys.readouterr().out)

        assert rc == 0
        assert len(provider.calls) == expected_calls
        assert preview["performance"]["batched_model_calls"] == expected_batched
        assert preview["performance"]["single_model_calls"] == expected_single
        assert [item["item_id"] for item in preview["items"]] == item_ids


def test_draft_suggest_cache_hit_is_not_included_in_ollama_batch(monkeypatch, tmp_path: Path, capsys):
    class CacheAwareProvider:
        def __init__(self):
            self.calls: list[ProviderRequest] = []

        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            self.calls.append(request)
            if request.evidence.get("batch"):
                return ProviderResponse(
                    content=json.dumps(
                        {
                            "items": [
                                {
                                    "item_id": item_id,
                                    "suggestions": [
                                        f"Clarifies cache-aware release-note behavior for {item_id} reviewers"
                                    ],
                                }
                                for item_id in request.evidence["item_ids"]
                            ]
                        }
                    ),
                    confidence=0.9,
                    model="ollama:test-model",
                )
            return ProviderResponse(
                content=f"Clarifies cache-aware release-note behavior for {request.evidence['item_id']} reviewers",
                confidence=0.9,
                model="ollama:test-model",
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="ollama:test-model")

    provider = CacheAwareProvider()
    groups = [
        _mk_group(number=1, title="First change", commit_sha="a1", commit_message="first"),
        _mk_group(number=2, title="Second change", commit_sha="b2", commit_message="second"),
    ]
    first_id = Classifier().classify(groups[0], "v1.0.0", "HEAD")[0].id
    expected_ids = [Classifier().classify(group, "v1.0.0", "HEAD")[0].id for group in groups]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: groups)
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: provider)

    assert main([
        "draft",
        "suggest",
        "--base",
        "v1.0.0",
        "--id",
        first_id,
        "--preview",
        "--model-provider",
        "ollama",
        "--strategy",
        "paired",
    ]) == 0
    capsys.readouterr()
    provider.calls.clear()

    rc = main([
        "draft",
        "suggest",
        "--base",
        "v1.0.0",
        "--all-visible",
        "--preview",
        "--model-provider",
        "ollama",
        "--strategy",
        "paired",
        "--batch-size",
        "2",
    ])
    preview = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert len(provider.calls) == 1
    assert provider.calls[0].evidence.get("batch") is not True
    assert provider.calls[0].evidence["item_id"] == expected_ids[1]
    assert [item["item_id"] for item in preview["items"]] == expected_ids
    assert preview["items"][0]["cache"]["hit"] is True
    assert preview["items"][1]["cache"]["hit"] is False


def test_draft_suggest_parallelism_overlaps_model_calls(monkeypatch, tmp_path: Path, capsys):
    class BlockingProvider:
        def __init__(self):
            self.barrier = threading.Barrier(2, timeout=2)
            self.lock = threading.Lock()
            self.active = 0
            self.max_active = 0

        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                self.barrier.wait()
                time.sleep(0.02)
                return ProviderResponse(
                    content="Clarifies release-note behavior for reviewers without delaying workflow.",
                    confidence=0.9,
                    model="mock-local",
                )
            finally:
                with self.lock:
                    self.active -= 1

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="mock-local")

    provider = BlockingProvider()
    groups = [
        _mk_group(number=1, title="First change", commit_sha="a1", commit_message="first"),
        _mk_group(number=2, title="Second change", commit_sha="b2", commit_message="second"),
    ]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: groups)
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: provider)

    rc = main([
        "draft",
        "suggest",
        "--base",
        "v1.0.0",
        "--all-visible",
        "--preview",
        "--parallelism",
        "2",
        "--model-provider",
        "mock",
    ])
    preview = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert provider.max_active == 2
    assert preview["performance"]["parallelism"] == 2


def test_draft_suggest_parallelism_preserves_target_order(monkeypatch, tmp_path: Path, capsys):
    class StaggeredProvider:
        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            if request.evidence["title"] == "First change":
                time.sleep(0.05)
            return ProviderResponse(
                content="Clarifies release-note behavior for reviewers without delaying workflow.",
                confidence=0.9,
                model="mock-local",
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="mock-local")

    groups = [
        _mk_group(number=1, title="First change", commit_sha="a1", commit_message="first"),
        _mk_group(number=2, title="Second change", commit_sha="b2", commit_message="second"),
    ]
    expected_ids = [Classifier().classify(group, "v1.0.0", "HEAD")[0].id for group in groups]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VERLATO_TRACE_PROGRESS", "1")
    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: groups)
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: StaggeredProvider())

    rc = main([
        "draft",
        "suggest",
        "--base",
        "v1.0.0",
        "--all-visible",
        "--preview",
        "--parallelism",
        "2",
        "--model-provider",
        "mock",
    ])
    captured = capsys.readouterr()
    preview = json.loads(captured.out)
    result_events = [
        event for event in _suggestion_events(captured.err)
        if event["event"] == "suggestion_item_result"
    ]

    assert rc == 0
    assert [item["item_id"] for item in preview["items"]] == expected_ids
    assert [event["item_id"] for event in result_events] == [expected_ids[1], expected_ids[0]]
    assert [event["item_id"] for event in sorted(result_events, key=lambda event: event["order"])] == expected_ids


def test_draft_suggest_failure_event_keeps_prior_success_event(monkeypatch, tmp_path: Path, capsys):
    class PartiallyFailingProvider:
        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            if request.evidence["title"] == "Second change":
                return ProviderResponse(content="", confidence=0.0, model="mock-local")
            return ProviderResponse(
                content="Clarifies release-note behavior for reviewers without delaying workflow.",
                confidence=0.9,
                model="mock-local",
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="mock-local")

    groups = [
        _mk_group(number=1, title="First change", commit_sha="a1", commit_message="first"),
        _mk_group(number=2, title="Second change", commit_sha="b2", commit_message="second"),
    ]
    expected_ids = [Classifier().classify(group, "v1.0.0", "HEAD")[0].id for group in groups]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VERLATO_TRACE_PROGRESS", "1")
    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: groups)
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: PartiallyFailingProvider())

    try:
        main(
            [
                "draft",
                "suggest",
                "--base",
                "v1.0.0",
                "--all-visible",
                "--preview",
                "--model-provider",
                "mock",
            ]
        )
        raise AssertionError("draft suggest should fail when a target produces no suggestions")
    except SystemExit as exc:
        assert expected_ids[1] in str(exc)

    captured = capsys.readouterr()
    events = _suggestion_events(captured.err)
    result_events = [event for event in events if event["event"] == "suggestion_item_result"]
    provisional_events = [event for event in events if event["event"] == "suggestion_item_provisional"]
    failed_events = [event for event in events if event["event"] == "suggestion_item_failed"]

    assert captured.out == ""
    assert provisional_events == []
    assert result_events[0]["item_id"] == expected_ids[0]
    assert result_events[0]["item"]["item_id"] == expected_ids[0]
    assert failed_events[0]["order"] == 1
    assert failed_events[0]["item_id"] == expected_ids[1]
    assert expected_ids[1] in failed_events[0]["message"]


def test_draft_suggest_progress_reports_item_and_batch_counts(monkeypatch, tmp_path: Path, capsys):
    class CountingProvider:
        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(
                content="Adds a focused model-written suggestion for this release note.",
                confidence=0.9,
                model="mock-local",
                metadata={
                    "total_duration": 50_000_000,
                    "prompt_eval_duration": 20_000_000,
                    "eval_duration": 30_000_000,
                    "prompt_eval_count": 8,
                    "eval_count": 6,
                },
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="mock-local")

    groups = [
        _mk_group(number=1, title="First change", commit_sha="a1", commit_message="first"),
        _mk_group(number=2, title="Second change", commit_sha="b2", commit_message="second"),
    ]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VERLATO_TRACE_PROGRESS", "1")
    monkeypatch.setenv("VERLATO_MODEL_MAX_ITEMS", "1")
    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: groups)
    monkeypatch.setattr("verlato.cli.main.build_provider", lambda config, mode=None: CountingProvider())

    rc = main(["draft", "suggest", "--base", "v1.0.0", "--all-visible", "--preview", "--model-provider", "mock"])
    captured = capsys.readouterr()
    events = _progress_events(captured.err)

    assert rc == 0
    assert "[verlato:progress]" not in captured.out
    preview = json.loads(captured.out)
    assert preview["batch_count"] == 2
    assert preview["performance"]["parallelism"] == 1
    assert preview["performance"]["target_selection_ms"] >= 0
    assert preview["performance"]["preparation_ms"] >= 0
    assert preview["performance"]["prompt_chars"] >= 0
    assert preview["performance"]["model_ms"] >= 0
    assert preview["performance"]["parse_ms"] >= 0
    assert preview["performance"]["total_ms"] >= 0
    assert preview["performance"]["prompt_chars"] == sum(
        item["prompt_budget"]["prompt_chars"] for item in preview["items"]
    )
    assert preview["items"][0]["prompt_budget"]["profile"] == "suggestion-conservative"
    assert preview["items"][0]["prompt_budget"]["prompt_chars"] > 0
    assert preview["items"][0]["timings"]["evidence_ms"] >= 0
    assert preview["items"][0]["timings"]["prompt_ms"] >= 0
    assert preview["items"][0]["timings"]["model_ms"] >= 0
    assert preview["items"][0]["timings"]["parse_ms"] >= 0
    assert preview["items"][0]["timings"]["total_ms"] >= 0
    assert preview["items"][0]["timings"]["ollama_total_ms"] == 50
    assert preview["items"][0]["timings"]["prompt_eval_ms"] == 20
    assert preview["items"][0]["timings"]["eval_ms"] == 30
    assert preview["items"][0]["timings"]["prompt_tokens"] == 8
    assert preview["items"][0]["timings"]["eval_tokens"] == 6
    assert preview["items"][0]["timings"]["tokens_per_second"] == 200.0
    assert any(
        event["stage"] == "suggestion_model_request"
        and event["current"] == 2
        and event["total"] == 2
        and event["unit"] == "item"
        and event["batch"] == 2
        and event["batch_total"] == 2
        for event in events
    )
    assert any(
        event["stage"] == "suggestions_ready"
        and event["elapsed_ms"] >= 0
        and event["model_ms"] >= 0
        for event in events
    )


def test_draft_suggest_all_visible_zero_fanout_returns_no_suggestions(monkeypatch, tmp_path: Path, capsys):
    groups = [
        _mk_group(number=1, title="First change", commit_sha="a1", commit_message="first"),
    ]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VERLATO_MODEL_MAX_ITEMS", "0")
    monkeypatch.setenv("VERLATO_TRACE_PROGRESS", "1")
    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: groups)
    monkeypatch.setattr(
        "verlato.cli.main.build_provider",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("zero fanout should not build provider")),
    )

    rc = main(["draft", "suggest", "--base", "v1.0.0", "--all-visible", "--preview", "--model-provider", "mock"])
    captured = capsys.readouterr()
    preview = json.loads(captured.out)
    suggestion_events = _suggestion_events(captured.err)

    assert rc == 0
    assert preview["visible_item_count"] == 1
    assert preview["fanout_guard"] == 0
    assert preview["batch_size"] == 0
    assert preview["batch_count"] == 0
    assert preview["performance"]["parallelism"] == 1
    assert preview["performance"]["prompt_chars"] == 0
    assert preview["performance"]["model_ms"] == 0
    assert preview["items"] == []
    assert suggestion_events[0]["event"] == "suggestion_preview_started"
    assert suggestion_events[0]["preview"]["items"] == []


def test_draft_prompt_outputs_prompt_without_calling_provider(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    group = _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow")
    target_item = Classifier().classify(group, "v1.0.0", "HEAD")[0]

    monkeypatch.setattr("verlato.cli.main._collect_groups", lambda base, head, **_kwargs: [group])
    monkeypatch.setattr(
        "verlato.cli.main.build_provider",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("draft prompt should not build a provider")),
    )

    rc = main(["draft", "prompt", "--base", "v1.0.0", "--id", target_item.id])

    assert rc == 0
    prompt_preview = json.loads(capsys.readouterr().out)
    assert prompt_preview["item_id"] == target_item.id
    assert prompt_preview["provider"] == "ollama"
    assert "Lead with what changed and why it matters" in prompt_preview["prompt"]
    assert prompt_preview["prompt_budget"]["profile"] == "suggestion-conservative"
    assert prompt_preview["prompt_budget"]["prompt_chars"] == len(prompt_preview["prompt"])
    assert prompt_preview["prompt_budget"]["files_included"] == 1


def test_render_uses_edited_summary_and_skips_hidden_items(monkeypatch, tmp_path: Path, capsys):
    from verlato.state.store import StateData, StateStore

    state_path = tmp_path / "state.json"
    hidden = _mk_item("ci_hidden", title="Hidden change")
    hidden.status = "hidden"
    hidden.hidden = True
    edited = _mk_item("ci_edited", title="Edited change", summary="Original summary")
    edited.status = "edited"
    edited.edited_summary = "Custom reviewer summary"
    StateStore(str(state_path)).save(StateData(items=[hidden, edited]))
    monkeypatch.setenv("VERLATO_STATE_PATH", str(state_path))

    rc = main(["render", "--format", "changelog", "--version", "v1.2.0"])

    assert rc == 0
    rendered = capsys.readouterr().out
    assert "Hidden change" not in rendered
    assert "Custom reviewer summary" in rendered


def test_normalize_remote_url_for_github_links():
    assert normalize_remote_url("git@github.com:owner/repo.git") == "https://github.com/owner/repo"
    assert normalize_remote_url("https://github.com/owner/repo.git") == "https://github.com/owner/repo"


def test_repository_url_skips_git_subprocess_outside_worktree(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "verlato.core.drafts.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("git should not run outside a worktree")),
    )

    assert DraftService()._repository_url() is None
