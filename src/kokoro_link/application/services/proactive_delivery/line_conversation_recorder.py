"""Durable history append for an accepted hosted proactive turn (M8).

The hosted proactive push is delivered to the user by the Channel, but the
matching row in the character's ``source="line"`` conversation is a SEPARATE
write. Previously that write was fire-and-forget: if it crashed, the user saw
the message but Core's history silently lost it and nothing retried.

This recorder is the single place that append happens, so both the dispatcher
(right after acceptance) and the pre-send-ledger retry worker (sweeping accepted
rows whose append never landed) share one idempotent implementation. The append
is keyed on the delivery ``event_id`` so a retry after a crashed
``mark_conversation_recorded`` adopts the existing row instead of duplicating
the turn.
"""

from __future__ import annotations

import logging
from datetime import datetime

from kokoro_link.application.services.external_chat.line_conversation import (
    resolve_or_create_line_conversation,
)
from kokoro_link.contracts.repositories import ConversationRepositoryPort
from kokoro_link.domain.entities.conversation import (
    Message,
    MessageRole,
)

_LOGGER = logging.getLogger(__name__)


class HostedLineConversationRecorder:
    def __init__(
        self, *, conversation_repository: ConversationRepositoryPort,
    ) -> None:
        self._conversations = conversation_repository

    async def record(
        self,
        *,
        event_id: str,
        character_id: str,
        text: str,
        when: datetime,
    ) -> bool:
        """CAS-append the accepted proactive turn to the character's
        ``source="line"`` conversation (creating it when absent).

        Returns ``True`` when the turn is durably recorded (freshly appended or
        already present under this ``event_id``), ``False`` when the append
        could not land (a lost CAS race or a repository error) so the caller
        leaves the ledger row for the next retry sweep. Idempotent: the append
        is keyed on ``event_id``.
        """
        message = Message(
            role=MessageRole.ASSISTANT, content=text, created_at=when,
        )
        key = f"proactive:{event_id}"
        for _ in range(2):
            try:
                # M9: single shared resolver so a proactive-first and an
                # inbound-first ordering converge on ONE line conversation.
                conversation = await resolve_or_create_line_conversation(
                    self._conversations, character_id=character_id,
                )
                result = await self._conversations.append_messages(
                    conversation.id,
                    expected_next_position=len(conversation.messages),
                    messages=[message],
                    idempotency_key=key,
                )
            except Exception:
                _LOGGER.exception(
                    "proactive hosted: line conversation append crashed "
                    "character=%s event=%s", character_id, event_id,
                )
                return False
            if result.ok:
                return True
        _LOGGER.warning(
            "proactive hosted: line conversation append conflict after retry "
            "character=%s event=%s", character_id, event_id,
        )
        return False
