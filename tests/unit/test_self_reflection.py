"""Unit tests for the SelfReflection stack (HUMANIZATION_ROADMAP §3.2)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from kokoro_link.application.services.self_reflection_service import (
    SelfReflectionService,
    render_reflection_lines,
)
from kokoro_link.bootstrap.settings import HumanizationSettings
from kokoro_link.contracts.self_reflection import (
    NullSelfReflectionGenerator,
    ReflectionGeneratorInput,
)
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.self_reflection import (
    PERIOD_MONTH,
    PERIOD_WEEK,
    SelfReflection,
)
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.reflection.llm_generator import (
    LLMSelfReflectionGenerator,
)
from kokoro_link.infrastructure.repositories.in_memory_self_reflections import (
    InMemorySelfReflectionRepository,
)


_CHAR = "char-A"
_OP = "default"
_NOW = datetime(2026, 5, 21, 4, 0, tzinfo=timezone.utc)
_TODAY = _NOW.date()


# ---- entity ----------------------------------------------------------------


def test_entity_rejects_invalid_period():
    with pytest.raises(ValueError, match="period"):
        SelfReflection.new(
            character_id=_CHAR,
            operator_id=_OP,
            period="day",
            narrative="x",
            period_start=_TODAY,
            period_end=_TODAY,
        )


def test_entity_caps_themes_and_quotes():
    reflection = SelfReflection.new(
        character_id=_CHAR,
        operator_id=_OP,
        period=PERIOD_WEEK,
        narrative="本週的心情筆記",
        dominant_themes=["a", "b", "c", "d", "e", "f", "g"],
        evidence_quotes=["q1", "q2", "q3", "q4"],
        period_start=_TODAY - timedelta(days=7),
        period_end=_TODAY,
    )
    assert len(reflection.dominant_themes) == 5
    assert len(reflection.evidence_quotes) == 3


# ---- repository ------------------------------------------------------------


@pytest.mark.asyncio
async def test_repo_upsert_replaces_same_period():
    repo = InMemorySelfReflectionRepository()
    r1 = SelfReflection.new(
        character_id=_CHAR, operator_id=_OP, period=PERIOD_WEEK,
        narrative="第一次", period_start=_TODAY - timedelta(days=7),
        period_end=_TODAY,
    )
    r2 = SelfReflection.new(
        character_id=_CHAR, operator_id=_OP, period=PERIOD_WEEK,
        narrative="重寫過", period_start=_TODAY - timedelta(days=7),
        period_end=_TODAY,
    )
    await repo.upsert_latest(r1)
    await repo.upsert_latest(r2)

    listed = await repo.latest_for(_CHAR, _OP)
    assert len(listed) == 1
    assert listed[0].narrative == "重寫過"


@pytest.mark.asyncio
async def test_repo_isolates_by_operator():
    repo = InMemorySelfReflectionRepository()
    await repo.upsert_latest(SelfReflection.new(
        character_id=_CHAR, operator_id=_OP, period=PERIOD_WEEK,
        narrative="A 的反思",
        period_start=_TODAY - timedelta(days=7), period_end=_TODAY,
    ))
    await repo.upsert_latest(SelfReflection.new(
        character_id=_CHAR, operator_id="other-op", period=PERIOD_WEEK,
        narrative="B 的反思",
        period_start=_TODAY - timedelta(days=7), period_end=_TODAY,
    ))
    a_only = await repo.latest_for(_CHAR, _OP)
    assert {r.narrative for r in a_only} == {"A 的反思"}


# ---- service ---------------------------------------------------------------


def _memory(content: str, salience: float, kind: MemoryKind = MemoryKind.EPISODIC) -> MemoryItem:
    return MemoryItem.create(
        character_id=_CHAR,
        kind=kind,
        content=content,
        salience=salience,
    )


def _settings(enabled: bool = True) -> HumanizationSettings:
    return HumanizationSettings(self_reflection_enabled=enabled)


def _build_service(
    *,
    memories: list[MemoryItem] | None = None,
    generator=None,
    settings: HumanizationSettings | None = None,
    emotion_events=None,
) -> tuple[SelfReflectionService, InMemorySelfReflectionRepository, MagicMock]:
    repo = InMemorySelfReflectionRepository()
    memory_repo = AsyncMock()
    memory_repo.list_all_for_character = AsyncMock(
        return_value=memories if memories is not None else [],
    )
    emotion_repo = None
    if emotion_events is not None:
        emotion_repo = AsyncMock()
        emotion_repo.list_recent = AsyncMock(return_value=emotion_events)
    gen = generator if generator is not None else NullSelfReflectionGenerator()
    svc = SelfReflectionService(
        repository=repo,
        memory_repository=memory_repo,
        emotion_event_repository=emotion_repo,
        generator=gen,
        settings=settings or _settings(),
    )
    return svc, repo, memory_repo


@pytest.mark.asyncio
async def test_service_returns_empty_when_disabled():
    svc, repo, memory_repo = _build_service(settings=_settings(enabled=False))
    out = await svc.run_for_pair(_CHAR, _OP, now=_NOW)
    assert out == []
    memory_repo.list_all_for_character.assert_not_called()


@pytest.mark.asyncio
async def test_service_skips_when_too_few_memories():
    # Below ``_MIN_MEMORIES_TO_REFLECT`` threshold → no narrative attempt.
    fewer = [_memory("x", 0.6) for _ in range(2)]
    svc, repo, _ = _build_service(memories=fewer)
    out = await svc.run_for_pair(_CHAR, _OP, now=_NOW)
    assert out == []


@pytest.mark.asyncio
async def test_service_generates_when_pool_large_enough():
    memories = [_memory(f"記憶 {i}", 0.8) for i in range(10)]
    fake_reflection_week = SelfReflection.new(
        character_id=_CHAR, operator_id=_OP, period=PERIOD_WEEK,
        narrative="這週狀態還可以", period_start=_TODAY - timedelta(days=7),
        period_end=_TODAY,
    )
    fake_reflection_month = SelfReflection.new(
        character_id=_CHAR, operator_id=_OP, period=PERIOD_MONTH,
        narrative="這個月折騰但結尾好轉", period_start=_TODAY - timedelta(days=30),
        period_end=_TODAY,
    )
    gen = MagicMock()
    gen.generate = AsyncMock(
        side_effect=[fake_reflection_week, fake_reflection_month],
    )
    svc, repo, _ = _build_service(memories=memories, generator=gen)
    out = await svc.run_for_pair(_CHAR, _OP, now=_NOW)
    assert {r.period for r in out} == {PERIOD_WEEK, PERIOD_MONTH}
    rows = await repo.latest_for(_CHAR, _OP)
    assert {r.narrative for r in rows} == {
        "這週狀態還可以",
        "這個月折騰但結尾好轉",
    }


# ---- LLM generator anti-hallucination -------------------------------------


@pytest.mark.asyncio
async def test_llm_generator_rejects_hallucinated_quote():
    """The LLM emits a quote not present in the memory pool → drop the
    whole reflection (verbatim guard, mirror of persona extraction)."""

    class _FakeModel:
        async def generate(self, prompt: str) -> str:
            return (
                '{"narrative": "本週還算平靜", '
                '"themes": ["生活"], '
                '"quotes": ["這句話沒有出現在記憶池"]}'
            )

        async def generate_stream(self, prompt: str):  # pragma: no cover
            yield ""

    gen = LLMSelfReflectionGenerator(model=_FakeModel())
    payload = ReflectionGeneratorInput(
        character_id=_CHAR,
        operator_id=_OP,
        character_name="Mio",
        period=PERIOD_WEEK,
        period_start=_TODAY - timedelta(days=7),
        period_end=_TODAY,
        high_salience_memories=(
            _memory("使用者今天提到工作壓力", 0.9),
        ),
    )
    result = await gen.generate(payload)
    assert result is None


@pytest.mark.asyncio
async def test_llm_generator_keeps_only_verbatim_quotes():
    class _FakeModel:
        async def generate(self, prompt: str) -> str:
            return (
                '{"narrative": "本週還算平靜，使用者卻提到壓力。", '
                '"themes": ["關懷"], '
                '"quotes": ["使用者今天提到工作壓力", "想像出來的話"]}'
            )

        async def generate_stream(self, prompt: str):  # pragma: no cover
            yield ""

    gen = LLMSelfReflectionGenerator(model=_FakeModel())
    payload = ReflectionGeneratorInput(
        character_id=_CHAR,
        operator_id=_OP,
        character_name="Mio",
        period=PERIOD_WEEK,
        period_start=_TODAY - timedelta(days=7),
        period_end=_TODAY,
        high_salience_memories=(
            _memory("使用者今天提到工作壓力", 0.9),
        ),
    )
    result = await gen.generate(payload)
    assert result is not None
    # Hallucinated quote dropped; verbatim one kept.
    assert result.evidence_quotes == ("使用者今天提到工作壓力",)


# ---- render --------------------------------------------------------------


def test_render_returns_empty_when_no_reflections():
    assert render_reflection_lines([]) == []


def test_render_includes_no_weaponisation_rail():
    reflection = SelfReflection.new(
        character_id=_CHAR, operator_id=_OP, period=PERIOD_WEEK,
        narrative="本週使用者揭露了一些低潮，我記得了。",
        dominant_themes=["關懷"],
        period_start=_TODAY - timedelta(days=7),
        period_end=_TODAY,
    )
    lines = render_reflection_lines([reflection])
    rendered = "\n".join(lines)
    assert "禁止情勒" in rendered
    assert "禁止當笑點戳對方" in rendered
    assert "本週使用者揭露了一些低潮，我記得了。" in rendered
