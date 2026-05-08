from __future__ import annotations

from verlato.providers.base import ProviderRequest, ProviderResponse, VerlatoProvider


class FallbackProvider(VerlatoProvider):
    """
    Explicit provider chain. Failures are recognized by Verlato's provider error markers.
    """

    def __init__(self, providers: list[VerlatoProvider]) -> None:
        self.providers = providers
        self.model = "fallback-chain"

    def summarize(self, request: ProviderRequest) -> ProviderResponse:
        return self._invoke("summarize", request)

    def classify(self, request: ProviderRequest) -> ProviderResponse:
        return self._invoke("classify", request)

    def _invoke(self, method_name: str, request: ProviderRequest) -> ProviderResponse:
        attempts: list[dict[str, object]] = []
        last_response: ProviderResponse | None = None
        for index, provider in enumerate(self.providers, start=1):
            method = getattr(provider, method_name)
            response = method(request)
            last_response = response
            attempts.append(
                {
                    "index": index,
                    "model": response.model,
                    "failed": _is_provider_error_marker(response.content),
                }
            )
            if response.content.strip() and not _is_provider_error_marker(response.content):
                metadata = dict(response.metadata)
                metadata["fallback_attempts"] = attempts
                metadata["fallback_selected_index"] = index
                return ProviderResponse(
                    content=response.content,
                    confidence=response.confidence,
                    model=response.model,
                    metadata=metadata,
                    reasoning=response.reasoning,
                )
        if last_response is not None:
            metadata = dict(last_response.metadata)
            metadata["fallback_attempts"] = attempts
            return ProviderResponse(
                content=last_response.content,
                confidence=last_response.confidence,
                model=last_response.model,
                metadata=metadata,
                reasoning=last_response.reasoning,
            )
        return ProviderResponse(
            content="[fallback-unavailable] no providers configured",
            confidence=0.0,
            model="fallback-chain",
            metadata={"fallback_attempts": attempts},
        )


def _is_provider_error_marker(content: str) -> bool:
    lowered = (content or "").strip().lower()
    return lowered.startswith(
        (
            "[github-copilot-",
            "[ollama-",
            "[codex-cli-",
            "[claude-code-",
            "[litellm-",
            "[plugin-",
            "[fallback-",
            "[stub-",
        )
    )
