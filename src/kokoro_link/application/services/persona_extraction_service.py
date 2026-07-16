"""Drives the post-turn persona extraction pass.

Called from ``ChatService._do_post_turn`` after the existing memory /
state / arc work finishes. Runs in its own LLM call (separate from
post-turn) and stages results as ``state='pending'`` rows; the dream
service later decides which to promote.

The service is thin — most of the heavy lifting lives in the extractor
(prompt + guard) and the repository (de-dup logic). Keeping the
orchestration here means the chat service only needs to know "I have a
persona extraction service to fire", not the internals.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from kokoro_link.contracts.operator_persona import OperatorPersonaRepositoryPort
from kokoro_link.contracts.persona_extractor import PersonaExtractorPort
from kokoro_link.domain.entities.conversation import Message, MessageContentMode
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.resolved_address import ResolvedAddress

from kokoro_link.application.services.operator_persona_service import (
    OperatorPersonaService,
)

_LOGGER = logging.getLogger(__name__)


class PersonaExtractionService:
    def __init__(
        self,
        *,
        extractor: PersonaExtractorPort,
        repository: OperatorPersonaRepositoryPort,
        persona_service: OperatorPersonaService,
    ) -> None:
        self._extractor = extractor
        self._repository = repository
        self._persona_service = persona_service

    async def run_after_turn(
        self,
        *,
        character_id: str,
        operator: OperatorProfile,
        conversation_id: str,
        user_message_id: str | None,
        user_text: str,
        assistant_text: str,
        recent_messages: list[Message] | None = None,
        content_mode: MessageContentMode | str = MessageContentMode.NORMAL,
        resolved_player_address: ResolvedAddress | None = None,
    ) -> int:
        """Pull candidates from the latest turn and stage them under
        ``(character_id, operator.id)``.

        Returns the number of candidates persisted. Never raises — a
        single failed turn shouldn't take the chat path with it.

        ``user_message_id`` may be ``None`` for paths that don't have
        a persisted message id (rare; only used by some sync-test
        harnesses). We skip extraction in that case because evidence
        without a turn_id is useless for later auditing.
        """
        if not user_message_id:
            return 0
        if not (user_text and user_text.strip()):
            return 0
        try:
            current = await self._persona_service.get_current(
                character_id, operator.id,
            )
            candidates = await self._extractor.extract(
                character_id=character_id,
                operator=operator,
                current_persona=current,
                conversation_id=conversation_id,
                user_message_id=user_message_id,
                user_message=user_text,
                assistant_message=assistant_text,
                recent_messages=recent_messages,
                resolved_player_address=resolved_player_address,
            )
        except Exception:
            _LOGGER.exception("Persona extraction failed; skipping turn")
            return 0
        if not candidates:
            return 0
        written = 0
        mode = _coerce_content_mode(content_mode)
        for candidate in candidates:
            try:
                candidate = replace(candidate, content_mode=mode)
                await self._repository.upsert_candidate(
                    character_id, operator.id, candidate,
                )
                written += 1
            except Exception:
                _LOGGER.exception(
                    "Persona candidate upsert failed (field_key=%s)",
                    candidate.field_key,
                )
        if written > 0:
            # Layer 4 doesn't change based on extraction, but flushing
            # the cache keeps things consistent if a future signal
            # (e.g. message count) is added that depends on the turn.
            self._persona_service.invalidate_cache(character_id, operator.id)
        return written


def _coerce_content_mode(value: MessageContentMode | str) -> MessageContentMode:
    if isinstance(value, MessageContentMode):
        return value
    try:
        return MessageContentMode(str(value).strip().lower())
    except ValueError:
        return MessageContentMode.NORMAL
