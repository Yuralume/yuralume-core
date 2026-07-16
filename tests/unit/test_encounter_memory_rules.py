"""Phase 4 assertions: encounter memory rules at chat parity
(ENCOUNTER_CHAT_PARITY_PLAN) — reflect prompt quality red lines, LLM
salience/audience with clamps, and write-time dedup.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kokoro_link.application.services.character_encounter_service import (
    CharacterEncounterMemoryWriter,
    CharacterEncounterRunner,
    EncounterReflection,
    ReflectionMemoryEntry,
)
from kokoro_link.domain.entities.character_encounter import EncounterLine
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository

_NOW = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)


class _FakeModel:
    def __init__(self, response: str) -> None:
        self.prompts: list[str] = []
        self._response = response

    async def generate(self, prompt: str, *, model: str | None = None) -> str:
        self.prompts.append(prompt)
        return self._response


class _Provider:
    def __init__(self, response: str) -> None:
        self.model = _FakeModel(response)

    async def is_fake(self, feature_key=None, *, character=None) -> bool:
        return False

    async def resolve(self, feature_key=None, *, character=None):
        return self.model

    async def resolve_model_id(self, feature_key=None, *, character=None):
        return None


def _char(cid: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=cid, name=cid.upper(), summary=f"{cid} summary", user_id="u1",
        personality=(), speaking_style="", interests=(), boundaries=(),
    )


def _encounter() -> SimpleNamespace:
    return SimpleNamespace(
        id="enc-1",
        relationship_id="rel-1",
        location="神社前庭",
        trigger_reason="路過打招呼",
        max_turns=2,
        scheduled_for=_NOW,
    )


def _runner(response: str) -> tuple[CharacterEncounterRunner, _Provider]:
    provider = _Provider(response)
    runner = CharacterEncounterRunner(
        encounter_repository=MagicMock(),
        character_repository=MagicMock(),
        memory_writer=MagicMock(),
        relationship_service=MagicMock(),
        provider=provider,
        local_tz=timezone.utc,
    )
    return runner, provider


@pytest.mark.asyncio
async def test_reflect_prompt_carries_memory_quality_red_lines() -> None:
    runner, provider = _runner('{"summary_for_a": "x", "summary_for_b": "y"}')
    char_a, char_b = _char("a"), _char("b")
    await runner._reflect(
        _encounter(), char_a, char_b,
        (EncounterLine(speaker_character_id="a", text="哈囉"),),
        speaker_contexts={"a": [], "b": []},
    )
    prompt = provider.model.prompts[0]
    assert "禁止隱喻" in prompt
    assert "時間措辭必須中性" in prompt
    assert "不要寫「剛剛」" in prompt
    assert "主詞用具體名字" in prompt
    assert "salience 是 0.0-1.0" in prompt
    assert "audience" in prompt
    assert "不可改寫成事實" in prompt  # hearsay red line preserved


@pytest.mark.asyncio
async def test_reflect_parses_object_entries_with_salience_and_audience() -> None:
    runner, _ = _runner(
        '{"summary_for_a": {"content": "小英給A看了河堤的照片", '
        '"salience": 0.7, "audience": "shareable"}, '
        '"summary_for_b": "看了照片",'
        '"hearsay_for_a": [{"content": "主人最近在趕專案", '
        '"salience": 0.5, "audience": "private"}, "純字串傳聞"],'
        '"peer_facts_for_a": [{"content": "B 常去河堤拍照", "salience": 2.0}]}',
    )
    char_a, char_b = _char("a"), _char("b")
    reflection = await runner._reflect(
        _encounter(), char_a, char_b,
        (EncounterLine(speaker_character_id="a", text="哈囉"),),
        speaker_contexts={"a": [], "b": []},
    )
    assert reflection.summary_for_a == "小英給A看了河堤的照片"
    assert reflection.summary_salience_a == 0.7
    assert reflection.summary_audience_a == "shareable"
    # Legacy bare-string summary still works.
    assert reflection.summary_for_b == "看了照片"
    assert reflection.summary_salience_b is None
    # Object + legacy string entries coexist.
    assert reflection.hearsay_for_a[0].audience == "private"
    assert reflection.hearsay_for_a[1].content == "純字串傳聞"
    assert reflection.peer_facts_for_a[0].salience == 2.0


@pytest.mark.asyncio
async def test_writer_applies_llm_salience_audience_with_clamps() -> None:
    repository = InMemoryMemoryRepository()
    writer = CharacterEncounterMemoryWriter(repository=repository)
    char_a, char_b = _char("a"), _char("b")
    reflection = EncounterReflection(
        summary_for_a="A 和 B 聊了河堤拍照",
        summary_for_b="B 給 A 看了照片",
        summary_salience_a=0.99,  # above clamp hi → 0.9
        summary_audience_a="private",
        hearsay_for_a=(
            ReflectionMemoryEntry(
                content="主人最近在趕專案",
                salience=0.9,  # hearsay clamp hi → 0.7
                audience="private",
            ),
        ),
        peer_facts_for_a=(
            ReflectionMemoryEntry(content="B 常去河堤拍照", salience=0.1),
        ),
    )
    await writer.write(
        encounter=_encounter(), char_a=char_a, char_b=char_b,
        transcript=(), reflection=reflection,
    )
    stored = await repository.list_all_for_character("a", world_scope=None)
    by_kind = {item.kind: item for item in stored}
    assert by_kind[MemoryKind.EPISODIC].salience == 0.9
    assert by_kind[MemoryKind.EPISODIC].audience == "private"
    assert by_kind[MemoryKind.EPISODIC].is_shareable_to_feed is False
    assert by_kind[MemoryKind.HEARSAY].salience == 0.7
    assert by_kind[MemoryKind.HEARSAY].audience == "private"
    assert by_kind[MemoryKind.RELATIONSHIP].salience == 0.3  # clamp lo
    # Default-closed: the peer fact carried no explicit audience → private.
    assert by_kind[MemoryKind.RELATIONSHIP].audience == "private"
    assert by_kind[MemoryKind.RELATIONSHIP].is_shareable_to_feed is False


@pytest.mark.asyncio
async def test_gossip_memories_default_closed_unless_explicit_shareable() -> None:
    # Privacy red line (review fix): hearsay/peer facts about the peer or
    # the operator must fail CLOSED when the reflect LLM omits audience;
    # only an explicit "shareable" opens them to the public feed path.
    repository = InMemoryMemoryRepository()
    writer = CharacterEncounterMemoryWriter(repository=repository)
    char_a, char_b = _char("a"), _char("b")
    reflection = EncounterReflection(
        summary_for_a="A 和 B 打了招呼",
        summary_for_b="B 和 A 打了招呼",
        hearsay_for_a=(
            ReflectionMemoryEntry(content="主人最近在趕專案"),  # no audience
        ),
        peer_facts_for_a=(
            ReflectionMemoryEntry(content="B 常去河堤拍照", audience="shareable"),
        ),
    )
    await writer.write(
        encounter=_encounter(), char_a=char_a, char_b=char_b,
        transcript=(), reflection=reflection,
    )
    stored = await repository.list_all_for_character("a", world_scope=None)
    by_kind = {item.kind: item for item in stored}
    assert by_kind[MemoryKind.HEARSAY].audience == "private"
    assert by_kind[MemoryKind.RELATIONSHIP].audience == "shareable"


@pytest.mark.asyncio
async def test_writer_deduplicates_against_existing_memories() -> None:
    repository = InMemoryMemoryRepository()
    writer = CharacterEncounterMemoryWriter(repository=repository)
    char_a, char_b = _char("a"), _char("b")
    reflection = EncounterReflection(
        summary_for_a="A 和 B 在神社前庭聊到亮亮的東西",
        summary_for_b="B 陪 A 在神社前庭看亮亮的東西",
    )
    first = await writer.write(
        encounter=_encounter(), char_a=char_a, char_b=char_b,
        transcript=(), reflection=reflection,
    )
    assert len(first) == 2
    # Second encounter reflects into nearly identical summaries — the
    # write-time dedup must drop them instead of piling up duplicates.
    second = await writer.write(
        encounter=SimpleNamespace(
            id="enc-2", relationship_id="rel-1", location="神社前庭",
            trigger_reason="又碰到", max_turns=2, scheduled_for=_NOW,
        ),
        char_a=char_a, char_b=char_b,
        transcript=(),
        reflection=EncounterReflection(
            summary_for_a="A 和 B 在神社前庭聊到亮亮的東西。",
            summary_for_b="B 陪 A 在神社前庭看亮亮的東西。",
        ),
    )
    assert second == ()
    stored_a = await repository.list_all_for_character("a", world_scope=None)
    assert len(stored_a) == 1


def test_hearsay_is_structurally_excluded_from_feed_broadcast() -> None:
    # Red-line lock (mirrors the memoir surface's hard HEARSAY
    # exclusion): second-hand information must never seed a public feed
    # post, regardless of per-item audience tags. Loosening this
    # requires a deliberate product decision, not a refactor.
    from kokoro_link.application.services.feed_candidates import (
        _NON_BROADCAST_MEMORY_KINDS,
    )

    assert MemoryKind.HEARSAY in _NON_BROADCAST_MEMORY_KINDS
