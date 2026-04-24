from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, TextIO

from releasepilot.mcp.service import MCPService, ReleasePreviewRequest


class ServerRequestError(RuntimeError):
    pass


@dataclass
class ServerResponse:
    id: str | int | None
    result: Any | None = None
    error: dict[str, Any] | None = None

    def to_json(self) -> str:
        payload: dict[str, Any] = {"id": self.id}
        if self.error is not None:
            payload["error"] = self.error
        else:
            payload["result"] = self.result
        return json.dumps(payload)


class ReleasePilotServer:
    def __init__(self, service: MCPService | None = None) -> None:
        self.service = service or MCPService()

    def handle_request(self, payload: dict[str, Any]) -> ServerResponse:
        request_id = payload.get("id")
        method = payload.get("method")
        params = payload.get("params") or {}
        if not isinstance(params, dict):
            return self._error(request_id, "invalid-params", "params must be an object")

        try:
            if method == "draft.getDocument":
                result = self.service.get_draft_document(self._preview_request(params))
            elif method == "draft.preview":
                result = self.service.preview_release_notes(self._preview_request(params))
            elif method == "draft.apply":
                preview = self._preview_request(params)
                document = self.service.get_draft_document(preview)
                result = self.service.apply_draft_document(
                    document=document,
                    target_name=str(params.get("target") or "changelog"),
                    target_path=self._optional_str(params.get("file")),
                )
            elif method == "draft.review":
                item_id = self._required_str(params, "id")
                result = self.service.update_review_state(
                    base=self._required_str(params, "base"),
                    head=self._optional_str(params.get("head")) or "HEAD",
                    item_id=item_id,
                    status=self._optional_str(params.get("status")),
                    summary=self._optional_str(params.get("summary")),
                    reviewer=self._optional_str(params.get("reviewer")),
                )
            elif method == "scan.unpublished":
                result = self.service.list_unpublished_changes(
                    base=self._required_str(params, "base"),
                    head=self._optional_str(params.get("head")) or "HEAD",
                )
            else:
                return self._error(request_id, "unknown-method", f"Unknown method: {method}")
        except (ServerRequestError, SystemExit, KeyError, ValueError) as exc:
            return self._error(request_id, "request-failed", str(exc))
        except Exception as exc:  # pragma: no cover - safety net
            return self._error(request_id, "internal-error", str(exc))

        return ServerResponse(id=request_id, result=result)

    def serve_stdio(self, *, input_stream: TextIO | None = None, output_stream: TextIO | None = None) -> int:
        input_stream = input_stream or sys.stdin
        output_stream = output_stream or sys.stdout

        for raw_line in input_stream:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                response = self._error(None, "invalid-json", str(exc))
            else:
                if not isinstance(payload, dict):
                    response = self._error(None, "invalid-request", "request must be a JSON object")
                else:
                    response = self.handle_request(payload)
            output_stream.write(response.to_json() + "\n")
            output_stream.flush()
        return 0

    def _preview_request(self, params: dict[str, Any]) -> ReleasePreviewRequest:
        return ReleasePreviewRequest(
            base=self._required_str(params, "base"),
            head=self._optional_str(params.get("head")) or "HEAD",
            version=self._optional_str(params.get("version")) or "Unreleased",
            include_confidence=bool(params.get("include_confidence") or params.get("includeConfidence")),
            include_provenance=bool(params.get("include_provenance") or params.get("includeProvenance")),
        )

    def _required_str(self, params: dict[str, Any], key: str) -> str:
        value = self._optional_str(params.get(key))
        if not value:
            raise ServerRequestError(f"Missing required parameter: {key}")
        return value

    @staticmethod
    def _optional_str(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _error(request_id: str | int | None, code: str, message: str) -> ServerResponse:
        return ServerResponse(id=request_id, error={"code": code, "message": message})
