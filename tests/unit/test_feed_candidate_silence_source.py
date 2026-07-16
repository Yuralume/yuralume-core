"""Silence candidate uses cross-source history.

The character is one person across web / telegram / line — so the
silence collector must use the unified timeline (``recent_messages_for_character``),
not pick a single per-source ``Conversation`` and ignore the others.

These tests guard against regression to "silence only fires when the
web thread has a message" — which would mean a heavy TG user never
sees a feed silence post even after going dark for hours.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.feed_candidates import (
    FeedCandidateCollector,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageRole,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.feed_source import FeedSource
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_feed_posts import (
    InMemoryFeedPostRepository,
)


def _character() -> Character:
    state = CharacterState(
        emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
    )
    silent_for = timedelta(hours=12)
    state = state.with_active_now(datetime.now(timezone.utc) - silent_for)
    return Character.create(
        name="Mio",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=state,
    )


@pytest.mark.asyncio
async def test_silence_candidate_fires_when_only_telegram_history_exists() -> None:
    convos = InMemoryConversationRepository()
    character = _character()

    # User has only ever messaged this character on Telegram. The
    # character is still the same person, so silence (no message in
    # 12h) should still trigger a feed post — anything else means
    # heavy-TG users never see silence posts.
    tg_conv = Conversation.start(
        character_id=character.id, source="telegram",
    ).append(
        Message(role=MessageRole.USER, content="hi from tg"),
    )
    await convos.save(tg_conv)

    collector = FeedCandidateCollector(
        feed_posts=InMemoryFeedPostRepository(),
        conversations=convos,
        silence_hours=8.0,
    )

    now = datetime.now(timezone.utc)
    cands = await collector.collect(character, now=now)

    assert any(c.source == FeedSource.silence() for c in cands), (
        "Silence candidate must fire on cross-source history — a TG-only "
        "user is still a real user and the character is still being "
        "ignored."
    )


@pytest.mark.asyncio
async def test_silence_candidate_skipped_when_no_history_anywhere() -> None:
    convos = InMemoryConversationRepository()
    character = _character()
    # No conversation in any source — character has never been spoken
    # to, so there's nobody to be silent toward.

    collector = FeedCandidateCollector(
        feed_posts=InMemoryFeedPostRepository(),
        conversations=convos,
        silence_hours=8.0,
    )

    now = datetime.now(timezone.utc)
    cands = await collector.collect(character, now=now)
    assert not any(c.source == FeedSource.silence() for c in cands)
