"""Background turn-recorder adapter.

Hands the foreground path back to the caller immediately and writes the
turn record on a fire-and-forget task. Failures are logged and swallowed
— a recorder outage must never break a chat turn.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Final

from kokoro_link.contracts.observability import (
    TurnRecorderPort,
    TurnRecordingDraft,
    TurnRecordRepositoryPort,
)
from kokoro_link.domain.entities.turn_record import TurnRecord
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)

_FEATURE_FLAG_ENV: Final[str] = "KOKORO_ENABLE_TURN_RECORDING"
_FEATURE_FLAG_DEFAULT: Final[bool] = True


def turn_recording_enabled() -> bool:
    """Read the feature flag.

    Re-read each call so toggling the env var at runtime (e.g. for
    integration tests) takes effect without restarting the process.
    """
    raw = os.environ.get(_FEATURE_FLAG_ENV)
    if raw is None:
        return _FEATURE_FLAG_DEFAULT
    return raw.strip().lower() not in {"0", "false", "no", "off"}


_MAX_PROMPT_CHARS: Final[int] = 200_000
"""Hard cap on persisted ``prompt_assembled`` length. Real prompts top
out at ~30k chars; this guards against pathological inputs (e.g. a
malformed loop) blowing up the DB row."""

_MAX_RESPONSE_CHARS: Final[int] = 100_000


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated {len(text) - limit} chars]"


class BackgroundTurnRecorder(TurnRecorderPort):
    """Fire-and-forget recorder backed by a ``TurnRecordRepositoryPort``.

    ``record`` returns the assigned ``TurnRecord.id`` synchronously so
    callers can stitch refs back to the record (the dashboard's
    "open this turn" link works as soon as the row lands). The actual
    DB write happens on a background task that the recorder owns.
    """

    def __init__(self, repository: TurnRecordRepositoryPort) -> None:
        self._repository = repository
        self._pending: set[asyncio.Task[None]] = set()

    async def record(self, draft: TurnRecordingDraft) -> str:
        if not turn_recording_enabled():
            return ""
        record = TurnRecord.new(
            character_id=draft.character_id,
            kind=draft.kind,
            id=draft.id,
            model_id=draft.model_id,
            prompt_pack_hash=(
                draft.prompt_pack_hash or get_default_loader().prompt_pack_hash()
            ),
            prompt_assembled=_truncate(draft.prompt_assembled, _MAX_PROMPT_CHARS),
            response_text=_truncate(draft.response_text, _MAX_RESPONSE_CHARS),
            conversation_id=draft.conversation_id,
            response_json=draft.response_json,
            latency_ms=draft.latency_ms,
            prompt_tokens=draft.prompt_tokens,
            completion_tokens=draft.completion_tokens,
            error=draft.error,
            post_turn_refs=draft.post_turn_refs,
        )
        task = asyncio.create_task(self._persist_safely(record))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return record.id

    async def _persist_safely(self, record: TurnRecord) -> None:
        try:
            await self._repository.add(record)
        except Exception:  # noqa: BLE001 — recorder must never bubble
            _LOGGER.exception(
                "turn_recorder failed to persist record %s (kind=%s, character=%s)",
                record.id, record.kind, record.character_id,
            )

    async def flush(self) -> None:
        """Await any in-flight writes. Test-only convenience."""
        if not self._pending:
            return
        await asyncio.gather(*list(self._pending), return_exceptions=True)


class NullTurnRecorder(TurnRecorderPort):
    """No-op recorder for tests / when the feature flag is off."""

    async def record(self, draft: TurnRecordingDraft) -> str:
        return ""
