"""Writing 起幕 scene messages into the ordinary conversation.

A scene is a frame around part of the normal thread, not a separate
transcript, so both ends of it (the opening's narration + first line, the
wrap-up's closing narration) are CAS appends onto whatever the thread
looks like *right now*. The turn lease excludes concurrent chat turns but
proactive delivery writes without taking it, so the tail can move under a
long model call; one re-read and one re-attempt covers that, while a loop
would only turn a rare conflict into an invisible stall.

The helper answers ``True``/``False`` rather than raising because its two
callers disagree about what a failed append means — the opening must fail
the whole action and unwind, the wrap-up must close the session anyway
and simply go without prose — and encoding one of those two answers in an
exception type would force the other caller to catch it just to ignore it.
"""

from __future__ import annotations

import logging
from typing import Sequence

from kokoro_link.contracts.repositories import ConversationRepositoryPort
from kokoro_link.domain.entities.conversation import Message


_LOGGER = logging.getLogger(__name__)

APPEND_ATTEMPTS = 2
"""Bounded CAS retries. See the module docstring for why it is not a loop."""


async def append_scene_messages(
    conversations: ConversationRepositoryPort,
    conversation_id: str,
    messages: Sequence[Message],
    *,
    attempts: int = APPEND_ATTEMPTS,
) -> bool:
    """Append ``messages`` to the end of the thread. Did they land?"""
    for attempt in range(attempts):
        conversation = await conversations.get(conversation_id)
        if conversation is None:
            _LOGGER.warning(
                "story scene append: conversation %s does not exist",
                conversation_id,
            )
            return False
        result = await conversations.append_messages(
            conversation_id,
            expected_next_position=len(conversation.messages),
            messages=list(messages),
        )
        if result.ok:
            return True
        _LOGGER.info(
            "story scene append conflicted conversation=%s attempt=%d",
            conversation_id,
            attempt + 1,
        )
    return False
