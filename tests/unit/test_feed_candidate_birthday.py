"""Tests for the birthday feed-candidate collector.

Confirms:

- Birthday fires exactly when ``now.date() == character.date_of_birth``
  (month/day match, ignoring the year).
- No candidate emitted when the operator hasn't set a birthday.
- The per-civil-year dedup actually fires through the feed-posts repo's
  ``find_by_source`` unique probe (the collector should not double-emit
  after a birthday post has already been written).
- The candidate's ``hint`` carries the character's first-person voice
  request (no third-person narration), age, and zodiac so the LLM has
  enough context for a tone-appropriate post.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from kokoro_link.application.services.feed_candidates import (
    FeedCandidateCollector,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource
from kokoro_link.infrastructure.repositories.in_memory_feed_posts import (
    InMemoryFeedPostRepository,
)


def _character(dob: date | None) -> Character:
    return Character.create(
        name="Mio",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        date_of_birth=dob,
    )


def _collector(repo: InMemoryFeedPostRepository) -> FeedCandidateCollector:
    # Other adapters left None — birthday collector only needs the repo
    # for the cross-source dedup probe.
    return FeedCandidateCollector(feed_posts=repo)


@pytest.mark.asyncio
async def test_birthday_collector_skipped_when_dob_unset() -> None:
    collector = _collector(InMemoryFeedPostRepository())
    now = datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)
    cands = await collector.collect(_character(None), now=now)
    assert cands == ()


@pytest.mark.asyncio
async def test_birthday_collector_skipped_on_non_birthday_day() -> None:
    collector = _collector(InMemoryFeedPostRepository())
    character = _character(date(2000, 6, 15))
    now = datetime(2026, 6, 14, 9, 0, tzinfo=timezone.utc)
    cands = await collector.collect(character, now=now)
    assert cands == ()


@pytest.mark.asyncio
async def test_birthday_collector_fires_on_birthday() -> None:
    collector = _collector(InMemoryFeedPostRepository())
    character = _character(date(2000, 6, 15))
    now = datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)
    cands = await collector.collect(character, now=now)
    assert len(cands) == 1
    cand = cands[0]
    assert cand.source == FeedSource.birthday(2026)
    assert cand.kind == FeedKind.MOOD
    # The hint should carry age + zodiac so the LLM has tonal context.
    assert "26" in cand.hint
    assert "雙子座" in cand.hint
    # Sanity-check the snippet bundle the composer relies on.
    assert any("年齡" in s for s in cand.context_snippets)


@pytest.mark.asyncio
async def test_birthday_collector_uses_explicit_local_day() -> None:
    collector = _collector(InMemoryFeedPostRepository())
    character = _character(date(2000, 6, 15))
    now = datetime(2026, 6, 14, 16, 30, tzinfo=timezone.utc)

    cands = await collector.collect(
        character, now=now, local_tz=ZoneInfo("Asia/Taipei"),
    )

    assert len(cands) == 1
    assert cands[0].source == FeedSource.birthday(2026)


@pytest.mark.asyncio
async def test_birthday_collector_dedups_after_post_exists() -> None:
    repo = InMemoryFeedPostRepository()
    character = _character(date(2000, 6, 15))
    now = datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)
    # Pre-seed: a birthday post already landed earlier today.
    await repo.add(FeedPost.create(
        character_id=character.id,
        kind=FeedKind.MOOD,
        content_text="生日快樂自己。",
        source=FeedSource.birthday(2026),
        created_at=datetime(2026, 6, 15, 8, 0, tzinfo=timezone.utc),
    ))
    cands = await _collector(repo).collect(character, now=now)
    assert cands == ()


@pytest.mark.asyncio
async def test_birthday_collector_leap_baby_observed_mar1() -> None:
    """A character born Feb 29 should still get a birthday on Mar 1 of
    a non-leap year — the ``birthday_context`` rule keeps every civil
    year populated."""
    collector = _collector(InMemoryFeedPostRepository())
    character = _character(date(2000, 2, 29))
    now = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)  # 2026 is not a leap year
    cands = await collector.collect(character, now=now)
    assert len(cands) == 1
    assert cands[0].source == FeedSource.birthday(2026)
