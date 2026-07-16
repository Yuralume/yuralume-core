from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Protocol


class ImageInputRejectedError(Exception):
    """Raised by chat adapters when an upstream 4xx indicates the
    request's image parts were rejected; callers may retry once without
    images.

    Fires only when the adapter actually sent image parts and the
    upstream answered with a shape/size/unprocessable 4xx (e.g. a
    text-only model returning ``404 No endpoints found that support
    image input``). Auth / rate-limit / server-error statuses are never
    classified as image rejection.

    ``status_code`` and ``body`` are stored as plain values so this
    contract stays dependency-free — ``contracts/`` must not import
    httpx. The original ``httpx.HTTPStatusError`` is chained via
    ``__cause__`` (``raise ... from err``) at the raise site for
    diagnostics.
    """

    def __init__(self, *, status_code: int, body: str) -> None:
        super().__init__(
            f"upstream rejected image input (status {status_code}): "
            f"{body[:200]}",
        )
        self.status_code = status_code
        self.body = body


@dataclass(frozen=True, slots=True)
class ReasoningOverrides:
    """Routing-level reasoning posture for one LLM call path.

    Resolved from the LLM routing preferences (global per-feature
    override → feature-group override). When a routing entry carries
    reasoning settings, the resolver binds this WHOLE trio onto the
    resolved adapter, replacing the provider connection's own
    reasoning defaults for calls routed through that entry — the
    connection-level ``strip_think_tags`` / ``extra_request_params``
    stay untouched (they are endpoint properties, not per-task
    posture). Adapters consume only the fields their API understands
    and ignore the rest.
    """

    disable_reasoning: bool = False
    reasoning_effort: str | None = None
    thinking_budget_tokens: int | None = None


class ChatModelPort(Protocol):
    provider_id: str
    supports_vision: bool
    """Whether the underlying model can ingest images.

    Implementations set this to ``True`` when the operator has
    explicitly opted in (the provider adapter has no reliable way to
    auto-detect multimodal capability for self-hosted endpoints like
    LM Studio). Callers use it to decide whether to forward image URLs
    or to drop them and append a text placeholder instead. An LLM
    routing entry (per feature / group / active_model) may override
    this connection-level flag for calls resolved through it, since one
    aggregator connection can front both vision and text-only models."""

    async def generate(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> str:
        """Generate a chat reply.

        ``image_urls`` is a (possibly empty) list of HTTP(S) URLs the
        caller wants the model to see. Vision-capable implementations
        attach them to the user message; non-vision implementations
        ignore them silently (the caller is expected to downgrade with
        a text placeholder before calling, using ``supports_vision``).

        ``model`` optionally overrides the provider's default model
        for this one call. ``None`` uses whatever the adapter was
        constructed with. Adapters that don't support per-call
        overrides may ignore it."""

    async def generate_stream(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> AsyncIterator[str]:
        """Generate a chat reply as a stream of text chunks."""
        ...

    async def list_models(self) -> list[str]:
        """Return the model IDs the provider currently offers.

        Used by the UI to populate a second-level "which model" dropdown
        once the operator has picked a provider. Adapters that don't
        enumerate models (fake provider, single-model endpoints) may
        return a one-element list containing their default, or an
        empty list to signal "no choice — use whatever's configured"."""


class ChatModelRegistryPort(Protocol):
    def list_ids(self) -> list[str]:
        """List available provider ids."""

    def resolve(self, provider_id: str) -> ChatModelPort:
        """Resolve provider implementation."""
