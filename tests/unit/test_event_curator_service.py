"""Unit tests for EventCuratorService.

The curator is the LLM-first / embedding-first matcher; tests focus on:

* world_awareness opt-out short-circuits
* high-similarity candidates are written; low-similarity ones are skipped
* excluded_topics filter drops near-vector candidates
* per-character cap is enforced
* duplicate seeds (already in inbox) are skipped
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from kokoro_link.application.services.event_curator_service import (
    EventCuratorService,
)
from kokoro_link.contracts.embedder import EmbedderPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.domain.entities.world_event import WorldEvent
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_character_event_inbox import (
    InMemoryCharacterEventInboxRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_world_events import (
    InMemoryWorldEventRepository,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_character(
    *,
    awareness: bool = True,
    interests: list[str] | None = None,
    excluded: list[str] | None = None,
) -> Character:
    return Character.create(
        name="Test",
        summary="A test character",
        personality=["平靜"],
        interests=interests or ["遊戲", "動漫"],
        speaking_style="casual",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        world_awareness_enabled=awareness,
        excluded_topics=excluded or [],
    )


class _FakePersonaService:
    def __init__(
        self,
        lines: list[str],
        *,
        familiarity: str = "close",
    ) -> None:
        self._lines = lines
        self._familiarity = familiarity

    async def get_current(self, character_id: str, operator_id: str):
        return SimpleNamespace(
            layer4_interaction=SimpleNamespace(
                familiarity_band=SimpleNamespace(value=self._familiarity),
            ),
        )

    def render_world_event_relevance(self, persona) -> list[str]:  # noqa: ANN001
        return list(self._lines)


class _FakeRelationshipSeedRepository:
    async def get(
        self,
        character_id: str,
        operator_id: str,
    ) -> CharacterOperatorRelationshipSeed:
        return CharacterOperatorRelationshipSeed(
            character_id=character_id,
            operator_id=operator_id,
            relationship_label="老朋友",
        )


class _FakeEmbedder(EmbedderPort):
    """Maps text to a fixed unit vector by topic substring.

    Tests pass in a topic→vector mapping; texts containing that topic
    pick the matching vector. Lets us simulate "this event is about
    gaming, character cares about gaming" with deterministic cosine.
    """

    def __init__(self, mapping: dict[str, tuple[float, ...]]) -> None:
        self._mapping = mapping
        self._default = tuple(0.0 for _ in next(iter(mapping.values())))

    @property
    def dimension(self) -> int:
        return len(self._default)

    @property
    def is_operational(self) -> bool:
        return True

    async def embed(self, text: str) -> tuple[float, ...] | None:
        for keyword, vec in self._mapping.items():
            if keyword in text:
                return vec
        return self._default

    async def embed_many(
        self, texts: Sequence[str],
    ) -> list[tuple[float, ...] | None]:
        return [await self.embed(t) for t in texts]


async def _seed_event(
    events: InMemoryWorldEventRepository,
    *,
    title: str,
    embedding: tuple[float, ...],
    category: str = "news",
    age_days: int = 1,
) -> WorldEvent:
    event = WorldEvent(
        id=str(uuid4()),
        source="src",
        title=title,
        summary="...",
        url=f"https://example.com/{uuid4()}",
        published_at=_now() - timedelta(days=age_days),
        fetched_at=_now(),
        category=category,
        topic_tags=(),
        embedding=list(embedding),
    )
    await events.upsert(event)
    return event


@pytest.mark.asyncio
async def test_disabled_awareness_short_circuits() -> None:
    events = InMemoryWorldEventRepository()
    inbox = InMemoryCharacterEventInboxRepository()
    embedder = _FakeEmbedder({"x": (1.0, 0.0)})
    curator = EventCuratorService(
        world_event_repository=events,
        inbox_repository=inbox,
        embedder=embedder,
    )
    char = _make_character(awareness=False)
    added = await curator.curate(char)
    assert added == 0


@pytest.mark.asyncio
async def test_high_similarity_candidates_pass() -> None:
    events = InMemoryWorldEventRepository()
    inbox = InMemoryCharacterEventInboxRepository()
    # Interest text contains "遊戲" → matched_vec; events with "遊戲"
    # in title also map to matched_vec. Cosine = 1.
    matched_vec = (1.0, 0.0)
    far_vec = (0.0, 1.0)
    embedder = _FakeEmbedder({"遊戲": matched_vec, "汽車": far_vec})
    char = _make_character(interests=["遊戲"])
    await _seed_event(events, title="遊戲新聞", embedding=matched_vec)
    await _seed_event(events, title="汽車快訊", embedding=far_vec)

    curator = EventCuratorService(
        world_event_repository=events,
        inbox_repository=inbox,
        embedder=embedder,
        match_threshold=0.5,
    )
    added = await curator.curate(char)
    assert added == 1
    rows = await inbox.list_for_character(char.id)
    assert len(rows) == 1
    assert rows[0].similarity > 0.9


@pytest.mark.asyncio
async def test_excluded_topics_filter() -> None:
    events = InMemoryWorldEventRepository()
    inbox = InMemoryCharacterEventInboxRepository()
    matched_vec = (1.0, 0.0)
    avoid_vec = (1.0, 0.0)  # Same vec as match → exclusion wins
    embedder = _FakeEmbedder({"遊戲": matched_vec, "政治": avoid_vec})
    char = _make_character(interests=["遊戲"], excluded=["政治"])
    # Event matches both interest AND excluded — should drop.
    await _seed_event(events, title="遊戲與政治", embedding=matched_vec)

    curator = EventCuratorService(
        world_event_repository=events,
        inbox_repository=inbox,
        embedder=embedder,
        match_threshold=0.5,
        exclude_threshold=0.5,
    )
    added = await curator.curate(char)
    assert added == 0


@pytest.mark.asyncio
async def test_dedupe_existing_inbox_rows() -> None:
    events = InMemoryWorldEventRepository()
    inbox = InMemoryCharacterEventInboxRepository()
    matched_vec = (1.0, 0.0)
    embedder = _FakeEmbedder({"遊戲": matched_vec})
    char = _make_character(interests=["遊戲"])
    event = await _seed_event(events, title="遊戲新聞", embedding=matched_vec)

    curator = EventCuratorService(
        world_event_repository=events,
        inbox_repository=inbox,
        embedder=embedder,
        match_threshold=0.5,
    )
    first = await curator.curate(char)
    second = await curator.curate(char)
    assert first == 1
    assert second == 0, "rerunning the curator must not duplicate inbox rows"


@pytest.mark.asyncio
async def test_operator_relevant_candidates_pass_without_character_interest() -> None:
    """A character can notice public topics that matter to a familiar user
    even when the character's own interests point elsewhere."""
    events = InMemoryWorldEventRepository()
    inbox = InMemoryCharacterEventInboxRepository()
    user_vec = (1.0, 0.0)
    char_vec = (0.0, 1.0)
    embedder = _FakeEmbedder({"3C": user_vec, "動漫": char_vec})
    char = _make_character(interests=["動漫"])
    await _seed_event(events, title="3C 發表會重點整理", embedding=user_vec)

    curator = EventCuratorService(
        world_event_repository=events,
        inbox_repository=inbox,
        embedder=embedder,
        operator_persona_service=_FakePersonaService(
            ["- 使用者興趣：3C 發表會、手機新品"],
        ),
        match_threshold=0.5,
    )
    added = await curator.curate(char)

    assert added == 1
    rows = await inbox.list_for_character(char.id)
    assert len(rows) == 1
    assert rows[0].similarity > 0.9


@pytest.mark.asyncio
async def test_operator_relevance_seed_floor_prevents_stranger_underweight() -> None:
    events = InMemoryWorldEventRepository()
    inbox = InMemoryCharacterEventInboxRepository()
    user_vec = (1.0, 0.0)
    embedder = _FakeEmbedder({"3C": user_vec})
    char = _make_character(interests=[], awareness=True)
    await _seed_event(events, title="3C 發表會重點整理", embedding=user_vec)

    curator = EventCuratorService(
        world_event_repository=events,
        inbox_repository=inbox,
        embedder=embedder,
        operator_persona_service=_FakePersonaService(
            ["- 使用者興趣：3C 發表會、手機新品"],
            familiarity="stranger",
        ),
        relationship_seed_repository=_FakeRelationshipSeedRepository(),
        match_threshold=0.5,
    )

    added = await curator.curate(char)

    assert added == 1
    rows = await inbox.list_for_character(char.id)
    assert rows[0].similarity >= 0.6


@pytest.mark.asyncio
async def test_no_character_or_operator_relevance_noops() -> None:
    events = InMemoryWorldEventRepository()
    inbox = InMemoryCharacterEventInboxRepository()
    embedder = _FakeEmbedder({"雜訊": (1.0, 0.0)})
    char = _make_character(interests=[], awareness=True)
    await _seed_event(events, title="雜訊新聞", embedding=(1.0, 0.0))

    curator = EventCuratorService(
        world_event_repository=events,
        inbox_repository=inbox,
        embedder=embedder,
        match_threshold=0.5,
    )

    assert await curator.curate(char) == 0
