from __future__ import annotations

import json
import sys
from typing import IO, Any

from verlato.mcp.service import MCPService, ReleasePreviewRequest


class ServiceProtocolError(ValueError):
    pass


def run_service_stdio(
    *,
    service: MCPService | None = None,
    input_stream: IO[str] | None = None,
    output_stream: IO[str] | None = None,
) -> int:
    svc = service or MCPService()
    stdin = input_stream or sys.stdin
    stdout = output_stream or sys.stdout

    for line in stdin:
        if not line.strip():
            continue
        should_shutdown = _handle_line(svc, line, stdout)
        stdout.flush()
        if should_shutdown:
            break
    return 0


def _handle_line(service: MCPService, line: str, output_stream: IO[str]) -> bool:
    request_id: object = None
    should_shutdown = False
    try:
        request = json.loads(line)
        if not isinstance(request, dict):
            raise ServiceProtocolError("Request must be a JSON object.")
        request_id = request.get("id")
        method = request.get("method")
        if not isinstance(method, str) or not method.strip():
            raise ServiceProtocolError("Request method is required.")
        params = request.get("params", {})
        if not isinstance(params, dict):
            raise ServiceProtocolError("Request params must be an object.")
        if method == "shutdown":
            result: object = {"shutdown": True}
            should_shutdown = True
        else:
            result = _dispatch(service, method, params)
        _write_response(output_stream, {"id": request_id, "ok": True, "result": result})
    except json.JSONDecodeError as exc:
        _write_error(output_stream, request_id, "invalid_json", f"Invalid JSON request: {exc}")
    except ServiceProtocolError as exc:
        _write_error(output_stream, request_id, "invalid_request", str(exc))
    except SystemExit as exc:
        message = str(exc) or "Service handler exited."
        _write_error(output_stream, request_id, "handler_error", message)
    except Exception as exc:  # pragma: no cover - defensive boundary for the long-lived service loop
        _write_error(output_stream, request_id, exc.__class__.__name__, str(exc))
    return should_shutdown


def _dispatch(service: MCPService, method: str, params: dict[str, object]) -> object:
    if method == "health":
        return service.health()
    if method == "info":
        return service.info()
    if method == "draft.get":
        return service.get_draft_document(_release_request(params))
    if method == "draft.preview":
        return service.preview_release_notes(_release_request(params))
    if method == "draft.apply":
        return service.apply_draft_request(
            request=_release_request(params),
            target_name=str(params.get("target") or "changelog"),
            target_path=_optional_string(params.get("file")),
        )
    if method == "draft.review":
        request = _release_request(params)
        summary_provenance = params.get("summary_provenance")
        return service.update_review_change_item(
            base=request.base,
            head=request.head,
            item_id=_required_string(params, "item_id"),
            status=_optional_string(params.get("status")),
            summary=_optional_string(params.get("summary")),
            reviewer=_optional_string(params.get("reviewer")),
            model_provider=request.model_provider,
            summary_provenance=summary_provenance if isinstance(summary_provenance, dict) else None,
        )
    if method == "feedback.record":
        metadata = params.get("metadata")
        return service.record_feedback(
            event=_required_string(params, "event"),
            item_id=_required_string(params, "item_id"),
            metadata=metadata if isinstance(metadata, dict) else {},
        )
    if method == "state.show":
        return service.state_show()
    if method == "state.clear_working":
        return service.clear_working_state()
    if method == "state.clear_suggestion_cache":
        return service.clear_suggestion_cache()
    if method == "suggestion.items":
        return service.list_suggestion_items(
            base=_required_string(params, "base"),
            head=str(params.get("head") or "HEAD"),
            include_files=bool(params.get("include_files") or params.get("includeFiles")),
        )
    if method == "suggestion.item":
        return service.get_suggestion_item(
            base=_required_string(params, "base"),
            head=str(params.get("head") or "HEAD"),
            item_id=_required_string(params, "item_id"),
        )
    if method == "suggestion.patch":
        return service.get_file_patch(
            base=_required_string(params, "base"),
            head=str(params.get("head") or "HEAD"),
            item_id=_required_string(params, "item_id"),
            path=_required_string(params, "path"),
            offset=_optional_int(params.get("offset"), 0),
            limit=_optional_int(params.get("limit"), 12000),
        )
    if method == "suggestion.semantic_diff":
        return service.get_file_semantic_diff(
            base=_required_string(params, "base"),
            head=str(params.get("head") or "HEAD"),
            item_id=_required_string(params, "item_id"),
            path=_required_string(params, "path"),
        )
    if method == "suggestion.semantic_search":
        return service.search_semantic_diff(
            base=_required_string(params, "base"),
            head=str(params.get("head") or "HEAD"),
            item_id=_required_string(params, "item_id"),
            query=_required_string(params, "query"),
            limit=_optional_int(params.get("limit"), 5),
        )
    if method == "suggestion.search":
        return service.search_item_diff(
            base=_required_string(params, "base"),
            head=str(params.get("head") or "HEAD"),
            item_id=_required_string(params, "item_id"),
            query=_required_string(params, "query"),
            limit=_optional_int(params.get("limit"), 5),
        )
    if method == "commit.conventional":
        return service.draft_conventional_commit(
            base=_required_string(params, "base"),
            head=str(params.get("head") or "HEAD"),
            sha=_required_string(params, "sha"),
            model_provider=_optional_string(params.get("model_provider") or params.get("modelProvider")),
            model_privacy=_optional_string(params.get("model_privacy") or params.get("modelPrivacy")),
            allow_remote_patches=bool(params.get("allow_remote_patches") or params.get("allowRemotePatches")),
            evidence_mode=_optional_string(params.get("evidence_mode") or params.get("evidenceMode")) or "diff-only",
        )
    if method == "commit.conventional_many":
        return service.draft_conventional_commits(
            base=_required_string(params, "base"),
            head=str(params.get("head") or "HEAD"),
            shas=_optional_string_list(params.get("shas")),
            model_provider=_optional_string(params.get("model_provider") or params.get("modelProvider")),
            model_privacy=_optional_string(params.get("model_privacy") or params.get("modelPrivacy")),
            allow_remote_patches=bool(params.get("allow_remote_patches") or params.get("allowRemotePatches")),
            evidence_mode=_optional_string(params.get("evidence_mode") or params.get("evidenceMode")) or "diff-only",
        )
    raise ServiceProtocolError(f"Unknown service method: {method}")


def _release_request(params: dict[str, object]) -> ReleasePreviewRequest:
    raw = params.get("request")
    request = raw if isinstance(raw, dict) else params
    return ReleasePreviewRequest(
        base=_required_string(request, "base"),
        head=str(request.get("head") or "HEAD"),
        version=str(request.get("version") or "Unreleased"),
        include_confidence=bool(request.get("include_confidence") or request.get("includeConfidence")),
        include_provenance=bool(request.get("include_provenance") or request.get("includeProvenance")),
        model_provider=_optional_string(request.get("model_provider") or request.get("modelProvider")),
    )


def _required_string(params: dict[str, object], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ServiceProtocolError(f"Missing required string param: {key}")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_string_list(value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ServiceProtocolError("Expected list param: shas")
    return [str(item) for item in value if str(item).strip()]


def _optional_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _write_response(output_stream: IO[str], response: dict[str, object]) -> None:
    output_stream.write(json.dumps(response, separators=(",", ":")) + "\n")


def _write_error(output_stream: IO[str], request_id: object, code: str, message: str) -> None:
    _write_response(
        output_stream,
        {
            "id": request_id,
            "ok": False,
            "error": {
                "code": code,
                "message": message,
            },
        },
    )
