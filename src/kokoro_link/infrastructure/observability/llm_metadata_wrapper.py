"""Decorator over ``ChatModelPort`` that exposes latency + token usage.

Plain ``ChatModelPort.generate`` returns just a string — fine for the
chat path that already had what it needed, but the turn recorder also
wants latency / token counts. Rather than break every existing adapter
by widening the Protocol return type, this decorator wraps an inner
model and stores the most-recent call's metadata on the *returned
context object*, not on a shared attribute. Each caller gets its own
context so concurrent turns can't trample each other's metadata.

Usage::

    captured = await wrapper.generate_capturing(prompt)
    # captured.text, captured.metadata.latency_ms, etc.

For streaming::

    async with wrapper.generate_stream_capturing(prompt) as stream:
        async for chunk in stream.chunks():
            yield chunk
        # stream.metadata available after the iterator drains
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from kokoro_link.contracts.llm import ChatModelPort

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LLMCallMetadata:
    model_id: str
    """The model the adapter actually used (after override resolution).
    Empty string if the adapter can't tell us."""
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CapturedGeneration:
    text: str
    metadata: LLMCallMetadata


@dataclass(slots=True)
class _StreamCapture:
    """Mutable holder for streaming metadata.

    The chunk iterator can't return metadata (it returns ``str``), so
    callers grab it off this object once the iterator drains.
    """
    _wrapper: MetadataCapturingChatModel
    _prompt: str
    _image_urls: tuple[str, ...]
    _model: str | None
    _accumulated: list[str] = field(default_factory=list)
    metadata: LLMCallMetadata | None = None

    async def chunks(self) -> AsyncIterator[str]:
        start = time.monotonic()
        error: str | None = None
        try:
            async for chunk in self._wrapper._inner.generate_stream(
                self._prompt,
                image_urls=self._image_urls,
                model=self._model,
            ):
                self._accumulated.append(chunk)
                yield chunk
        except Exception as exc:  # noqa: BLE001 — record + re-raise
            error = repr(exc)
            raise
        finally:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            self.metadata = LLMCallMetadata(
                model_id=self._resolve_model_id(),
                latency_ms=elapsed_ms,
                error=error,
            )

    def accumulated_text(self) -> str:
        return "".join(self._accumulated)

    def _resolve_model_id(self) -> str:
        if self._model and self._model.strip():
            return self._model.strip()
        provider = getattr(self._wrapper._inner, "_model", "")
        return str(provider) if provider else self._wrapper._inner.provider_id


class MetadataCapturingChatModel(ChatModelPort):
    """``ChatModelPort`` decorator that exposes latency / token metadata.

    Delegates the protocol surface unchanged so this can be dropped in
    wherever a plain ``ChatModelPort`` is expected. Callers that *want*
    metadata use the extra ``generate_capturing`` /
    ``generate_stream_capturing`` methods.
    """

    def __init__(self, inner: ChatModelPort) -> None:
        self._inner = inner
        self.provider_id = inner.provider_id
        self.supports_vision = inner.supports_vision

    async def generate(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> str:
        return await self._inner.generate(
            prompt, image_urls=image_urls, model=model,
        )

    async def generate_stream(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> AsyncIterator[str]:
        async for chunk in self._inner.generate_stream(
            prompt, image_urls=image_urls, model=model,
        ):
            yield chunk

    async def list_models(self) -> list[str]:
        return await self._inner.list_models()

    async def generate_capturing(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> CapturedGeneration:
        start = time.monotonic()
        error: str | None = None
        text = ""
        try:
            text = await self._inner.generate(
                prompt, image_urls=image_urls, model=model,
            )
        except Exception as exc:  # noqa: BLE001
            error = repr(exc)
            raise
        finally:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            metadata = LLMCallMetadata(
                model_id=_resolve_model_id(self._inner, model),
                latency_ms=elapsed_ms,
                prompt_tokens=_estimate_tokens(prompt),
                completion_tokens=_estimate_tokens(text) if text else None,
                error=error,
            )
        return CapturedGeneration(text=text, metadata=metadata)

    @asynccontextmanager
    async def generate_stream_capturing(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> AsyncIterator[_StreamCapture]:
        capture = _StreamCapture(
            _wrapper=self,
            _prompt=prompt,
            _image_urls=tuple(image_urls),
            _model=model,
        )
        try:
            yield capture
        finally:
            if capture.metadata is not None and capture.metadata.completion_tokens is None:
                capture.metadata = LLMCallMetadata(
                    model_id=capture.metadata.model_id,
                    latency_ms=capture.metadata.latency_ms,
                    prompt_tokens=_estimate_tokens(prompt),
                    completion_tokens=_estimate_tokens(capture.accumulated_text())
                    if capture.accumulated_text()
                    else None,
                    error=capture.metadata.error,
                )


def _resolve_model_id(inner: ChatModelPort, override: str | None) -> str:
    if override and override.strip():
        return override.strip()
    default = getattr(inner, "_model", "")
    return str(default) if default else inner.provider_id


def _estimate_tokens(text: str) -> int | None:
    """Best-effort token count.

    Real adapters that return usage from the server (OpenAI / Anthropic
    response payloads) should be preferred — but the OpenAI-compatible
    adapter currently drops that info. ~4 chars per token is the
    standard rough heuristic for English / mixed-CJK input; close enough
    for cost / latency dashboards, never used for billing.
    """
    if not text:
        return None
    return max(1, len(text) // 4)


def parse_usage_from_response_json(payload: object) -> tuple[int | None, int | None]:
    """Try to lift ``(prompt_tokens, completion_tokens)`` from a raw
    OpenAI-style response payload.

    Used by the recorder when an adapter starts surfacing usage info via
    ``response_json``. Returns ``(None, None)`` on any parse failure —
    the heuristic in ``_estimate_tokens`` is the fallback.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return (None, None)
    if not isinstance(payload, dict):
        return (None, None)
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return (None, None)
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    return (
        prompt if isinstance(prompt, int) else None,
        completion if isinstance(completion, int) else None,
    )
