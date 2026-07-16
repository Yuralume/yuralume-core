"""Usage metering decorator for active LLM routing."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import datetime, timezone

from kokoro_link.application.services.feature_keys import FEATURE_CHAT
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.generation_usage import (
    UsageEventDraft,
    UsageEventRecorderPort,
)
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.generation_usage import (
    CAPABILITY_LLM,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    UsageQuantity,
)

_LOGGER = logging.getLogger(__name__)

UsageRecorderFactory = Callable[[], UsageEventRecorderPort | None]


class MeteredActiveLLMProvider(ActiveLLMProviderPort):
    """Decorate active LLM resolutions with Core-local usage rows.

    The player-visible main chat path already records one aggregate row
    in ``ChatService`` after turn recording. This decorator therefore
    skips ``FEATURE_CHAT`` and covers every auxiliary/background feature
    that resolves through ``ActiveLLMProviderPort``.
    """

    def __init__(
        self,
        *,
        inner: ActiveLLMProviderPort,
        recorder: UsageRecorderFactory,
        skipped_feature_keys: set[str] | None = None,
    ) -> None:
        self._inner = inner
        self._recorder = recorder
        self._skipped = set(skipped_feature_keys or {FEATURE_CHAT})

    @property
    def inner(self) -> ActiveLLMProviderPort:
        return self._inner

    async def resolve(
        self,
        feature_key: str | None = None,
        *,
        character: Character | None = None,
        operator_id: str | None = None,
        content_tolerance: str | None = None,
    ) -> ChatModelPort:
        model = await self._inner.resolve(
            feature_key,
            character=character,
            operator_id=operator_id,
            content_tolerance=content_tolerance,
        )
        if feature_key in self._skipped:
            return model
        return MeteredChatModel(
            inner=model,
            recorder=self._recorder,
            feature_key=feature_key or "auxiliary_llm",
            character=character,
            operator_id=operator_id,
            content_tolerance=content_tolerance,
        )

    async def resolve_model_id(
        self,
        feature_key: str | None = None,
        *,
        character: Character | None = None,
        operator_id: str | None = None,
        content_tolerance: str | None = None,
    ) -> str | None:
        return await self._inner.resolve_model_id(
            feature_key,
            character=character,
            operator_id=operator_id,
            content_tolerance=content_tolerance,
        )

    async def is_fake(
        self,
        feature_key: str | None = None,
        *,
        character: Character | None = None,
        operator_id: str | None = None,
        content_tolerance: str | None = None,
    ) -> bool:
        return await self._inner.is_fake(
            feature_key,
            character=character,
            operator_id=operator_id,
            content_tolerance=content_tolerance,
        )


class MeteredChatModel(ChatModelPort):
    def __init__(
        self,
        *,
        inner: ChatModelPort,
        recorder: UsageRecorderFactory,
        feature_key: str,
        character: Character | None,
        operator_id: str | None,
        content_tolerance: str | None,
        source_surface: str | None = None,
        routing_mode: str = "active_provider",
        conversation_id: str | None = None,
        turn_record_id: str | None = None,
        metered_by: str = "active_llm_provider",
    ) -> None:
        self._inner = inner
        self._recorder = recorder
        self._feature_key = feature_key
        self._character = character
        self._operator_id = (operator_id or "").strip()
        self._content_tolerance = content_tolerance
        self._source_surface = source_surface or feature_key
        self._routing_mode = routing_mode
        self._conversation_id = conversation_id
        self._turn_record_id = turn_record_id
        self._metered_by = metered_by
        self.provider_id = inner.provider_id
        self.supports_vision = inner.supports_vision

    @property
    def last_request_id(self) -> str:
        return _last_request_id(self._inner)

    async def generate(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> str:
        recorder = self._recorder()
        if recorder is None:
            return await self._inner.generate(
                prompt,
                image_urls=image_urls,
                model=model,
            )
        start = time.monotonic()
        output = ""
        error: Exception | None = None
        try:
            output = await self._inner.generate(
                prompt,
                image_urls=image_urls,
                model=model,
            )
            return output
        except Exception as exc:  # noqa: BLE001 - record then re-raise
            error = exc
            raise
        finally:
            await self._record_usage(
                recorder=recorder,
                prompt=prompt,
                output=output,
                elapsed_ms=int((time.monotonic() - start) * 1000),
                model=model,
                image_urls=image_urls,
                stream=False,
                error=error,
            )

    async def generate_stream(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> AsyncIterator[str]:
        recorder = self._recorder()
        if recorder is None:
            async for chunk in self._inner.generate_stream(
                prompt,
                image_urls=image_urls,
                model=model,
            ):
                yield chunk
            return
        start = time.monotonic()
        chunks: list[str] = []
        error: Exception | None = None
        try:
            async for chunk in self._inner.generate_stream(
                prompt,
                image_urls=image_urls,
                model=model,
            ):
                chunks.append(chunk)
                yield chunk
        except Exception as exc:  # noqa: BLE001 - record then re-raise
            error = exc
            raise
        finally:
            await self._record_usage(
                recorder=recorder,
                prompt=prompt,
                output="".join(chunks),
                elapsed_ms=int((time.monotonic() - start) * 1000),
                model=model,
                image_urls=image_urls,
                stream=True,
                error=error,
            )

    async def list_models(self) -> list[str]:
        return await self._inner.list_models()

    async def _record_usage(
        self,
        *,
        recorder: UsageEventRecorderPort,
        prompt: str,
        output: str,
        elapsed_ms: int,
        model: str | None,
        image_urls: Sequence[str],
        stream: bool,
        error: Exception | None,
    ) -> None:
        prompt_tokens = _estimate_tokens(prompt)
        completion_tokens = _estimate_tokens(output)
        input_quantity = int(prompt_tokens or 0)
        output_quantity = int(completion_tokens or 0)
        total_quantity = input_quantity + output_quantity
        completed_at = datetime.now(timezone.utc)
        try:
            await recorder.record(UsageEventDraft(
                capability=CAPABILITY_LLM,
                upstream_request_id=_last_request_id(self._inner),
                turn_record_id=self._turn_record_id,
                conversation_id=self._conversation_id,
                character_id=_character_id(self._character),
                operator_id=_operator_id(self._character, self._operator_id),
                feature_key=self._feature_key,
                source_surface=self._source_surface,
                routing_mode=self._routing_mode,
                provider_id=self.provider_id,
                model_id=_resolve_model_id(self._inner, model),
                quantity=UsageQuantity(
                    usage_unit="token",
                    input_quantity=input_quantity,
                    output_quantity=output_quantity,
                    total_quantity=total_quantity,
                    billable_quantity=total_quantity,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    usage_is_estimated=True,
                ),
                latency_ms=elapsed_ms,
                status=STATUS_FAILED if error else STATUS_SUCCEEDED,
                error_code=type(error).__name__ if error else None,
                error_message=str(error)[:500] if error else None,
                metadata={
                    "metered_by": self._metered_by,
                    "stream": stream,
                    "image_url_count": len(tuple(image_urls)),
                    "content_tolerance": self._content_tolerance or "",
                },
                completed_at=completed_at,
            ))
        except Exception:  # noqa: BLE001 - usage must never break generation
            _LOGGER.exception(
                "auxiliary LLM usage metering failed "
                "(feature=%s, character=%s)",
                self._feature_key,
                _character_id(self._character),
            )


def _resolve_model_id(inner: ChatModelPort, override: str | None) -> str:
    if override and override.strip():
        return override.strip()
    default = getattr(inner, "_model", "")
    return str(default) if default else inner.provider_id


def _last_request_id(inner: ChatModelPort) -> str:
    return str(getattr(inner, "last_request_id", "") or "")


def _character_id(character: Character | None) -> str | None:
    if character is None:
        return None
    value = getattr(character, "id", None)
    return str(value) if value else None


def _operator_id(character: Character | None, fallback: str = "") -> str:
    if character is None:
        return fallback
    return str(getattr(character, "user_id", "") or "")


def _estimate_tokens(text: str) -> int | None:
    if not text:
        return None
    return max(1, len(text) // 4)
