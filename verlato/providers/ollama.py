from __future__ import annotations

import json
import os
from http import client
from urllib import error, request

from verlato.providers.base import ProviderRequest, ProviderResponse, VerlatoProvider
from verlato.providers.model_logging import (
    ModelLogSink,
    default_model_log_sink,
    model_log_record,
    model_payload_redaction_enabled,
    redact_model_payload,
)

DEFAULT_OLLAMA_ENDPOINT = "http://127.0.0.1:11434/api/chat"
DEFAULT_OLLAMA_MODEL = "gemma4:latest"
DEFAULT_OLLAMA_TIMEOUT_SECONDS = 15.0
DEFAULT_OLLAMA_KEEP_ALIVE = "10m"
DEFAULT_OLLAMA_SUMMARY_NUM_PREDICT = 180
DEFAULT_OLLAMA_CLASSIFY_NUM_PREDICT = 24
DEFAULT_OLLAMA_MIN_SUGGEST_NUM_PREDICT = 96
DEFAULT_OLLAMA_MAX_SUGGEST_NUM_PREDICT = 180


class OllamaProvider(VerlatoProvider):
    """
    Local provider backed by a local Ollama server.
    """

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        model: str | None = None,
        timeout_seconds: float = DEFAULT_OLLAMA_TIMEOUT_SECONDS,
        stream: bool | None = None,
        keep_alive: str | None = DEFAULT_OLLAMA_KEEP_ALIVE,
        num_ctx: int | None = None,
        num_predict: int | None = None,
        summary_num_predict: int | None = DEFAULT_OLLAMA_SUMMARY_NUM_PREDICT,
        classify_num_predict: int | None = DEFAULT_OLLAMA_CLASSIFY_NUM_PREDICT,
        suggest_num_predict: int | None = None,
        suggest_think: bool | None = False,
        log_sink: ModelLogSink | None = None,
    ) -> None:
        self.endpoint: str = endpoint or os.getenv("VERLATO_OLLAMA_ENDPOINT") or DEFAULT_OLLAMA_ENDPOINT
        self.model: str = model or os.getenv("VERLATO_OLLAMA_MODEL") or DEFAULT_OLLAMA_MODEL
        self.timeout_seconds = _resolve_timeout_seconds(timeout_seconds)
        self.stream = _env_flag("VERLATO_OLLAMA_STREAM") if stream is None else stream
        self.keep_alive = _resolve_keep_alive(keep_alive)
        self.num_ctx = _resolve_int_env("VERLATO_OLLAMA_NUM_CTX", num_ctx)
        self.num_predict = _resolve_int_env("VERLATO_OLLAMA_NUM_PREDICT", num_predict)
        self.summary_num_predict = _resolve_int_env("VERLATO_OLLAMA_SUMMARY_NUM_PREDICT", summary_num_predict)
        self.classify_num_predict = _resolve_int_env("VERLATO_OLLAMA_CLASSIFY_NUM_PREDICT", classify_num_predict)
        self.suggest_num_predict = _resolve_int_env("VERLATO_OLLAMA_SUGGEST_NUM_PREDICT", suggest_num_predict)
        self.suggest_think = _resolve_bool_env("VERLATO_OLLAMA_SUGGEST_THINK", suggest_think)
        self.log_sink = log_sink or default_model_log_sink()

    def summarize(self, request_data: ProviderRequest) -> ProviderResponse:
        task = "suggest" if request_data.task == "suggest" else "summarize"
        return self._invoke(task=task, request_data=request_data)

    def classify(self, request_data: ProviderRequest) -> ProviderResponse:
        return self._invoke(task="classify", request_data=request_data)

    def _invoke(self, *, task: str, request_data: ProviderRequest) -> ProviderResponse:
        system_prompt = (
            "You are Verlato. Write a useful release note with enough context for downstream consumers. "
            "Use paragraphs or bullet points when they make the change clearer, but stay focused and avoid padding."
            if task in {"summarize", "suggest"}
            else "You are Verlato. Return one line: kind/area/audience/semver."
        )
        self._trace(
            f"provider=ollama task={task} model={self.model} endpoint={self.endpoint} "
            f"prompt_chars={len(request_data.prompt)} evidence_keys={len(request_data.evidence)} "
            f"timeout_seconds={self.timeout_seconds:g} stream={self.stream} keep_alive={self.keep_alive or 'default'}"
        )
        body: dict[str, object] = {
            "model": self.model,
            "stream": self.stream,
            "options": self._options_for_task(task, request_data=request_data),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": request_data.prompt},
            ],
        }
        if request_data.response_format is not None:
            body["format"] = request_data.response_format
        if task == "suggest" and self.suggest_think is not None:
            body["think"] = self.suggest_think
        if self.keep_alive:
            body["keep_alive"] = self.keep_alive

        try:
            self._trace(f"post provider=ollama task={task} endpoint={self.endpoint} mode=chat")
            data = self._post_streaming_json(self.endpoint, body, task=task) if self.stream else self._post_json(self.endpoint, body)
        except client.RemoteDisconnected:
            self._trace(
                f"fallback provider=ollama task={task} from=chat to=generate reason=remote-disconnected"
            )
            try:
                data = self._invoke_generate(task=task, system_prompt=system_prompt, request_data=request_data)
            except (client.RemoteDisconnected, error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                self._trace(
                    f"error provider=ollama task={task} stage=generate reason={type(exc).__name__}",
                    error=True,
                )
                return self._unavailable_response(exc)
        except error.HTTPError as exc:
            self._trace(f"error provider=ollama task={task} stage=chat http_status={exc.code}", error=True)
            return ProviderResponse(
                content=f"[ollama-error] local model request failed ({exc.code}): {exc.reason}",
                confidence=0.0,
                model=f"ollama:{self.model}",
            )
        except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            self._trace(f"error provider=ollama task={task} stage=chat reason={type(exc).__name__}", error=True)
            return self._unavailable_response(exc)

        raw_message = data.get("message")
        message = raw_message if isinstance(raw_message, dict) else {}
        content = str(message.get("content") or data.get("response") or "").strip()
        reasoning = self._response_reasoning(data)
        metadata = self._response_metadata(data)
        metadata_text = " ".join(f"{key}={value}" for key, value in sorted(metadata.items()))
        metadata_suffix = f" {metadata_text}" if metadata_text else ""
        self._trace(
            f"response provider=ollama task={task} content_chars={len(content)} empty={not bool(content)}"
            f" reasoning_chars={len(reasoning)}{metadata_suffix}"
        )
        if not content:
            return ProviderResponse(
                content="[ollama-empty] no content",
                confidence=0.0,
                model=f"ollama:{self.model}",
                metadata=metadata,
                reasoning=reasoning,
            )
        return ProviderResponse(
            content=content,
            confidence=0.75,
            model=f"ollama:{self.model}",
            metadata=metadata,
            reasoning=reasoning,
        )

    def _invoke_generate(self, *, task: str, system_prompt: str, request_data: ProviderRequest) -> dict[str, object]:
        body: dict[str, object] = {
            "model": self.model,
            "stream": self.stream,
            "options": self._options_for_task(task, request_data=request_data),
            "system": system_prompt,
            "prompt": request_data.prompt,
        }
        if request_data.response_format is not None:
            body["format"] = request_data.response_format
        if task == "suggest" and self.suggest_think is not None:
            body["think"] = self.suggest_think
        if self.keep_alive:
            body["keep_alive"] = self.keep_alive
        self._trace(f"post provider=ollama task=generate endpoint={self._generate_endpoint()} mode=generate")
        return (
            self._post_streaming_json(self._generate_endpoint(), body, task="generate")
            if self.stream
            else self._post_json(self._generate_endpoint(), body)
        )

    def _post_json(self, endpoint: str, body: dict[str, object]) -> dict[str, object]:
        payload = json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
        }
        req = request.Request(endpoint, data=payload, headers=headers, method="POST")
        with request.urlopen(req, timeout=self.timeout_seconds) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not isinstance(data, dict):
            raise json.JSONDecodeError("expected JSON object", str(data), 0)
        return data

    def _post_streaming_json(self, endpoint: str, body: dict[str, object], *, task: str) -> dict[str, object]:
        payload = json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
        }
        req = request.Request(endpoint, data=payload, headers=headers, method="POST")
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        final_data: dict[str, object] = {}
        chunk_count = 0
        with request.urlopen(req, timeout=self.timeout_seconds) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8").strip()
                if not line:
                    continue
                chunk = json.loads(line)
                if not isinstance(chunk, dict):
                    raise json.JSONDecodeError("expected JSON object", line, 0)
                chunk_count += 1
                final_data.update(chunk)
                message = chunk.get("message")
                if isinstance(message, dict):
                    content_delta = str(message.get("content") or "")
                    reasoning_delta = self._message_reasoning(message)
                else:
                    content_delta = str(chunk.get("response") or "")
                    reasoning_delta = self._response_reasoning(chunk)
                if content_delta:
                    content_parts.append(content_delta)
                    self._trace_chunk(task=task, kind="content", chunk=content_delta)
                if reasoning_delta:
                    reasoning_parts.append(reasoning_delta)
                    self._trace_chunk(task=task, kind="reasoning", chunk=reasoning_delta)
        if final_data.get("message") and isinstance(final_data["message"], dict):
            message = dict(final_data["message"])
            message["content"] = "".join(content_parts)
            if reasoning_parts:
                message["reasoning"] = "".join(reasoning_parts)
            final_data["message"] = message
        else:
            final_data["response"] = "".join(content_parts)
            if reasoning_parts:
                final_data["reasoning"] = "".join(reasoning_parts)
        self._trace(
            f"stream-complete provider=ollama task={task} chunks={chunk_count} "
            f"content_chars={sum(len(part) for part in content_parts)} reasoning_chars={sum(len(part) for part in reasoning_parts)}"
        )
        return final_data

    def _generate_endpoint(self) -> str:
        if self.endpoint.rstrip("/").endswith("/api/chat"):
            return f"{self.endpoint.rstrip('/')[:-len('/api/chat')]}/api/generate"
        return self.endpoint

    def _options_for_task(self, task: str, *, request_data: ProviderRequest | None = None) -> dict[str, object]:
        options: dict[str, object] = {"temperature": 0.1}
        if self.num_ctx is not None:
            options["num_ctx"] = self.num_ctx
        num_predict = self.num_predict
        if num_predict is None:
            if task == "classify":
                num_predict = self.classify_num_predict
            elif task == "suggest":
                num_predict = self.suggest_num_predict
                if num_predict is None:
                    num_predict = _adaptive_suggest_num_predict(request_data)
            else:
                num_predict = self.summary_num_predict
        if num_predict is not None:
            options["num_predict"] = num_predict
        return options

    @staticmethod
    def _response_metadata(data: dict[str, object]) -> dict[str, object]:
        keys = [
            "total_duration",
            "load_duration",
            "prompt_eval_count",
            "prompt_eval_duration",
            "eval_count",
            "eval_duration",
            "done_reason",
        ]
        return {key: data[key] for key in keys if key in data}

    @staticmethod
    def _response_reasoning(data: dict[str, object]) -> str:
        message = data.get("message")
        candidates: list[object] = []
        if isinstance(message, dict):
            candidates.extend(
                message.get(key)
                for key in ("thinking", "reasoning", "reasoning_content")
                if message.get(key)
            )
        candidates.extend(data.get(key) for key in ("thinking", "reasoning", "reasoning_content") if data.get(key))
        return "\n".join(str(candidate).strip() for candidate in candidates if str(candidate).strip())

    @staticmethod
    def _message_reasoning(message: dict[str, object]) -> str:
        candidates = [
            message.get(key)
            for key in ("thinking", "reasoning", "reasoning_content")
            if message.get(key)
        ]
        return "".join(str(candidate) for candidate in candidates)

    def _unavailable_response(self, exc: BaseException) -> ProviderResponse:
        return ProviderResponse(
            content=f"[ollama-unavailable] unable to reach local Ollama at {self.endpoint}: {exc}",
            confidence=0.0,
            model=f"ollama:{self.model}",
        )

    def _trace(self, message: str, *, error: bool = False) -> None:
        self.log_sink.emit(
            model_log_record(
                message,
                event_name=f"verlato.model.ollama.{_event_suffix(message)}",
                attributes={"provider": "ollama", "model": self.model},
                error=error,
            )
        )

    def _trace_chunk(self, *, task: str, kind: str, chunk: str) -> None:
        if kind == "reasoning" and not _env_flag("VERLATO_TRACE_MODEL_REASONING"):
            payload = ""
            redacted = False
        else:
            safe_chunk, redacted = redact_model_payload(chunk) if model_payload_redaction_enabled() else (chunk, False)
            payload = safe_chunk
        self.log_sink.emit(
            model_log_record(
                "json "
                + json.dumps(
                    {
                        "event": "stream_chunk",
                        "message": f"stream {kind} chunk",
                        "attributes": {
                            "provider": "ollama",
                            "model": self.model,
                            "task": task,
                            "kind": kind,
                            "chars": len(chunk),
                            "chunks": 1,
                            "redacted": redacted,
                        },
                        "payload": payload,
                    },
                    sort_keys=True,
                ),
                event_name="verlato.model.ollama.stream_chunk",
                attributes={"provider": "ollama", "model": self.model},
            )
        )


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _resolve_timeout_seconds(configured: float | None) -> float:
    raw = os.getenv("VERLATO_OLLAMA_TIMEOUT_SECONDS")
    if raw is not None and raw.strip():
        return float(raw)
    return configured if configured is not None else DEFAULT_OLLAMA_TIMEOUT_SECONDS


def _resolve_keep_alive(configured: str | None) -> str | None:
    raw = os.getenv("VERLATO_OLLAMA_KEEP_ALIVE")
    if raw is not None:
        normalized = raw.strip()
        return normalized or None
    if configured is None:
        return None
    normalized = configured.strip()
    return normalized or None


def _resolve_int_env(name: str, configured: int | None) -> int | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return configured
    return int(raw)


def _resolve_bool_env(name: str, configured: bool | None) -> bool | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return configured
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _adaptive_suggest_num_predict(request_data: ProviderRequest | None) -> int:
    count = 1
    raw_count = request_data.evidence.get("suggestion_count") if request_data else None
    if isinstance(raw_count, (int, float)):
        count = int(raw_count)
    elif raw_count is not None:
        try:
            count = int(str(raw_count))
        except ValueError:
            count = 1
    return max(DEFAULT_OLLAMA_MIN_SUGGEST_NUM_PREDICT, min(DEFAULT_OLLAMA_MAX_SUGGEST_NUM_PREDICT, 60 * count))


def _event_suffix(message: str) -> str:
    if message.startswith("stream-chunk"):
        return "stream_chunk"
    if message.startswith("stream-complete"):
        return "stream_complete"
    if message.startswith("response "):
        return "response"
    if message.startswith("post "):
        return "request"
    if message.startswith("error "):
        return "error"
    if message.startswith("fallback "):
        return "fallback"
    return "log"
