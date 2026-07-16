"""Fail-soft recorder for generation usage events."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Final

from kokoro_link.contracts.generation_usage import (
    PriceEstimatorPort,
    UsageEventDraft,
    UsageEventRecorderPort,
    UsageEventRepositoryPort,
)
from kokoro_link.domain.entities.generation_usage import GenerationUsageEvent

_LOGGER = logging.getLogger(__name__)

_FEATURE_FLAG_ENV: Final[str] = "KOKORO_ENABLE_USAGE_LEDGER"
_FEATURE_FLAG_DEFAULT: Final[bool] = True


def usage_ledger_enabled() -> bool:
    raw = os.environ.get(_FEATURE_FLAG_ENV)
    if raw is None:
        return _FEATURE_FLAG_DEFAULT
    return raw.strip().lower() not in {"0", "false", "no", "off"}


class BackgroundUsageEventRecorder(UsageEventRecorderPort):
    def __init__(
        self,
        repository: UsageEventRepositoryPort,
        price_estimator: PriceEstimatorPort | None = None,
    ) -> None:
        self._repository = repository
        self._price_estimator = price_estimator
        self._pending: set[asyncio.Task[None]] = set()

    async def record(self, draft: UsageEventDraft) -> str:
        if not usage_ledger_enabled():
            return ""
        cost = draft.cost
        if cost is None and self._price_estimator is not None:
            try:
                cost = await self._price_estimator.estimate(draft)
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "usage_recorder price estimation failed "
                    "(capability=%s, feature=%s)",
                    draft.capability,
                    draft.feature_key,
                )
        event = GenerationUsageEvent.new(
            request_id=draft.request_id,
            upstream_request_id=draft.upstream_request_id,
            turn_record_id=draft.turn_record_id,
            conversation_id=draft.conversation_id,
            character_id=draft.character_id,
            operator_id=draft.operator_id,
            capability=draft.capability,
            feature_key=draft.feature_key,
            source_surface=draft.source_surface,
            routing_mode=draft.routing_mode,
            provider_id=draft.provider_id,
            model_id=draft.model_id,
            profile_id=draft.profile_id,
            voice_id=draft.voice_id,
            prompt_pack_hash=draft.prompt_pack_hash,
            quantity=draft.quantity,
            cached=draft.cached,
            cost=cost,
            latency_ms=draft.latency_ms,
            status=draft.status,
            error_code=draft.error_code,
            error_message=draft.error_message,
            artifact_count=draft.artifact_count,
            output_bytes=draft.output_bytes,
            duration_seconds=draft.duration_seconds,
            content_hash=draft.content_hash,
            metadata=draft.metadata,
            completed_at=draft.completed_at,
        )
        task = asyncio.create_task(self._persist_safely(event))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return event.id

    async def _persist_safely(self, event: GenerationUsageEvent) -> None:
        try:
            await self._repository.add(event)
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "usage_recorder failed to persist event %s "
                "(capability=%s, feature=%s, character=%s)",
                event.id,
                event.capability,
                event.feature_key,
                event.character_id,
            )

    async def flush(self) -> None:
        if not self._pending:
            return
        await asyncio.gather(*list(self._pending), return_exceptions=True)


class NullUsageEventRecorder(UsageEventRecorderPort):
    async def record(self, draft: UsageEventDraft) -> str:
        return ""
