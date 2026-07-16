"""Unit tests for ``MemoirService`` (docs/MEMOIR_PLAN.md).

Covers the three-source aggregation, privacy exclusion rules, pin
ordering, per-(character, operator) isolation, and pin-limit enforcement.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from kokoro_link.application.services.memoir_service import (
    MemoirPinLimitExceededError,
    MemoirService,
)
from kokoro_link.bootstrap.settings import MemoirSettings
from kokoro_link.domain.entities.emotion_event import (
    CAUSE_IDLE_DRIFT,
    CAUSE_TURN,
    EmotionEvent,
)
from kokoro_link.domain.entities.memoir import (
    MemoirChapter,
    MemoirEntry,
    MemoirView,
    ENTRY_EMOTION,
    ENTRY_MEMORY,
    ENTRY_MILESTONE,
)
from kokoro_link.domain.entities.memoir_pin import MemoirPin
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.self_reflection import SelfReflection
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.repositories.in_memory_memoir_pins import (
    InMemoryMemoirPinRepository,
)


_CHAR_ID = "char-A"
_OP_ID = "operator-1"
_OTHER_OP = "operator-2"
_NOW = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)


class _OperatorProfile:
    def __init__(self, primary_language: str) -> None:
        self.primary_language = primary_language


class _OperatorProfileService:
    def __init__(self, primary_language: str) -> None:
        self.primary_language = primary_language
        self.calls: list[str] = []

    async def get_for_user(self, operator_id: str) -> _OperatorProfile:
        self.calls.append(operator_id)
        return _OperatorProfile(self.primary_language)


class _RecordingLocalizer:
    def __init__(self) -> None:
        self.calls: list[tuple[MemoirView, str]] = []

    async def localize_view(
        self,
        view: MemoirView,
        *,
        target_language: str,
    ) -> MemoirView:
        self.calls.append((view, target_language))
        return MemoirView(
            chapters=tuple(
                MemoirChapter(
                    period=chapter.period,
                    period_start=chapter.period_start,
                    period_end=chapter.period_end,
                    narrative=f"[{target_language}] {chapter.narrative}",
                    dominant_themes=chapter.dominant_themes,
                    evidence_quotes=chapter.evidence_quotes,
                )
                for chapter in view.chapters
            ),
            timeline=tuple(
                MemoirEntry(
                    kind=entry.kind,
                    entry_id=entry.entry_id,
                    occurred_at=entry.occurred_at,
                    summary=f"[{target_language}] {entry.summary}",
                    score=entry.score,
                    pinned=entry.pinned,
                    extras=entry.extras,
                )
                for entry in view.timeline
            ),
            pin_count=view.pin_count,
            pin_limit=view.pin_limit,
        )


def _settings(
    *,
    memory_min_salience: float = 0.7,
    emotion_min_intensity: float = 0.65,
    pin_max_per_pair: int = 32,
    timeline_limit: int = 80,
) -> MemoirSettings:
    return MemoirSettings(
        memory_min_salience=memory_min_salience,
        emotion_min_intensity=emotion_min_intensity,
        emotion_lookback_days=90,
        timeline_limit=timeline_limit,
        pin_max_per_pair=pin_max_per_pair,
    )


def _memory(
    *,
    id_: str,
    kind: MemoryKind = MemoryKind.EPISODIC,
    salience: float = 0.8,
    content: str = "聊到了愛吃辣的事",
    created_at: datetime | None = None,
    tags: tuple[str, ...] = (),
) -> MemoryItem:
    return MemoryItem(
        id=id_,
        character_id=_CHAR_ID,
        conversation_id=None,
        kind=kind,
        content=content,
        salience=salience,
        tags=tags,
        created_at=created_at or _NOW,
    )


def _emotion(
    *,
    id_: str,
    intensity: float = 0.7,
    cause: str = CAUSE_TURN,
    label: str = "被理解了",
    created_at: datetime | None = None,
) -> EmotionEvent:
    return EmotionEvent(
        id=id_,
        character_id=_CHAR_ID,
        operator_id=_OP_ID,
        cause_ref_kind=cause,
        intensity=intensity,
        emotion_label=label,
        created_at=created_at or _NOW,
    )


def _reflection(period: str, created_at: datetime) -> SelfReflection:
    return SelfReflection.new(
        character_id=_CHAR_ID,
        operator_id=_OP_ID,
        period=period,
        narrative=f"這{period}過得很豐富",
        dominant_themes=("relationships",),
        period_start=created_at.date() - timedelta(days=7 if period == "week" else 30),
        period_end=created_at.date(),
        evidence_quotes=("一段引用",),
        now=created_at,
    )


def _build_service(
    *,
    memories: list[MemoryItem] | None = None,
    emotions: list[EmotionEvent] | None = None,
    reflections: list[SelfReflection] | None = None,
    pin_repo: InMemoryMemoirPinRepository | None = None,
    settings: MemoirSettings | None = None,
    localizer: object | None = None,
    operator_profile_service: object | None = None,
) -> tuple[MemoirService, InMemoryMemoirPinRepository]:
    pins = pin_repo or InMemoryMemoirPinRepository()
    memory_repo = AsyncMock()
    memory_repo.list_all_for_character = AsyncMock(return_value=memories or [])
    reflection_repo = AsyncMock()
    reflection_repo.latest_for = AsyncMock(return_value=reflections or [])
    emotion_repo = AsyncMock()
    emotion_repo.list_recent = AsyncMock(return_value=emotions or [])
    service = MemoirService(
        memory_repository=memory_repo,
        self_reflection_repository=reflection_repo,
        emotion_event_repository=emotion_repo,
        pin_repository=pins,
        settings=settings or _settings(),
        localizer=localizer,
        operator_profile_service=operator_profile_service,
    )
    return service, pins


@pytest.mark.asyncio
async def test_build_view_empty_when_nothing_persisted() -> None:
    service, _ = _build_service()
    view = await service.build_view(_CHAR_ID, _OP_ID)
    assert view.chapters == ()
    assert view.timeline == ()
    assert view.pin_count == 0
    assert view.pin_limit == 32


@pytest.mark.asyncio
async def test_build_view_renders_chapters_from_reflections() -> None:
    service, _ = _build_service(
        reflections=[
            _reflection("week", _NOW),
            _reflection("month", _NOW - timedelta(days=1)),
        ],
    )
    view = await service.build_view(_CHAR_ID, _OP_ID)
    periods = [c.period for c in view.chapters]
    assert periods == ["week", "month"]
    assert view.chapters[0].narrative == "這week過得很豐富"


@pytest.mark.asyncio
async def test_build_view_localizes_player_visible_text_for_english_user() -> None:
    localizer = _RecordingLocalizer()
    service, _ = _build_service(
        reflections=[_reflection("week", _NOW)],
        memories=[_memory(id_="m1", content="一起聊了咖啡")],
        localizer=localizer,
        operator_profile_service=_OperatorProfileService("en-US"),
    )

    view = await service.build_view(_CHAR_ID, _OP_ID)

    assert [call[1] for call in localizer.calls] == ["en-US"]
    assert view.chapters[0].narrative == "[en-US] 這week過得很豐富"
    assert view.timeline[0].summary == "[en-US] 一起聊了咖啡"
    assert view.timeline[0].entry_id == "m1"


@pytest.mark.asyncio
async def test_build_view_does_not_localize_default_primary_language() -> None:
    localizer = _RecordingLocalizer()
    service, _ = _build_service(
        reflections=[_reflection("week", _NOW)],
        localizer=localizer,
        operator_profile_service=_OperatorProfileService("zh-TW"),
    )

    view = await service.build_view(_CHAR_ID, _OP_ID)

    assert localizer.calls == []
    assert view.chapters[0].narrative == "這week過得很豐富"


@pytest.mark.asyncio
async def test_build_view_only_week_reflection() -> None:
    service, _ = _build_service(
        reflections=[_reflection("week", _NOW)],
    )
    view = await service.build_view(_CHAR_ID, _OP_ID)
    assert [c.period for c in view.chapters] == ["week"]


@pytest.mark.asyncio
async def test_excludes_hearsay_memory_kind() -> None:
    service, _ = _build_service(
        memories=[
            _memory(id_="m1", kind=MemoryKind.EPISODIC, salience=0.8),
            _memory(id_="m2", kind=MemoryKind.HEARSAY, salience=0.9),
        ],
    )
    view = await service.build_view(_CHAR_ID, _OP_ID)
    ids = {e.entry_id for e in view.timeline}
    assert ids == {"m1"}, "HEARSAY rows must be hard-excluded"


@pytest.mark.asyncio
async def test_excludes_idle_drift_emotion_cause() -> None:
    service, _ = _build_service(
        emotions=[
            _emotion(id_="e1", intensity=0.9, cause=CAUSE_TURN),
            _emotion(id_="e2", intensity=0.95, cause=CAUSE_IDLE_DRIFT),
        ],
    )
    view = await service.build_view(_CHAR_ID, _OP_ID)
    ids = {e.entry_id for e in view.timeline}
    assert ids == {"e1"}, "idle_drift emotion events must be hard-excluded"


@pytest.mark.asyncio
async def test_low_salience_memory_filtered_out() -> None:
    service, _ = _build_service(
        memories=[
            _memory(id_="hi", salience=0.85),
            _memory(id_="lo", salience=0.4),
        ],
    )
    view = await service.build_view(_CHAR_ID, _OP_ID)
    assert {e.entry_id for e in view.timeline} == {"hi"}


@pytest.mark.asyncio
async def test_milestone_memory_gets_milestone_entry_kind() -> None:
    service, _ = _build_service(
        memories=[
            _memory(
                id_="ms1",
                kind=MemoryKind.RELATIONSHIP_MILESTONE,
                salience=1.0,
                content="陌生 → 朋友",
            ),
        ],
    )
    view = await service.build_view(_CHAR_ID, _OP_ID)
    assert len(view.timeline) == 1
    assert view.timeline[0].kind == ENTRY_MILESTONE


@pytest.mark.asyncio
async def test_pinned_entries_sort_first_then_by_recency() -> None:
    old = _memory(id_="old", salience=0.9, created_at=_NOW - timedelta(days=10))
    new = _memory(id_="new", salience=0.8, created_at=_NOW)
    pins = InMemoryMemoirPinRepository()
    await pins.add(MemoirPin.new(
        character_id=_CHAR_ID,
        operator_id=_OP_ID,
        entry_kind=ENTRY_MEMORY,
        entry_id="old",
    ))
    service, _ = _build_service(memories=[new, old], pin_repo=pins)
    view = await service.build_view(_CHAR_ID, _OP_ID)
    ids = [e.entry_id for e in view.timeline]
    assert ids == ["old", "new"]
    assert view.timeline[0].pinned is True
    assert view.timeline[1].pinned is False
    assert view.pin_count == 1


@pytest.mark.asyncio
async def test_pin_isolation_across_operators() -> None:
    pins = InMemoryMemoirPinRepository()
    # operator_1 pins something; operator_2 must not see the pin.
    await pins.add(MemoirPin.new(
        character_id=_CHAR_ID,
        operator_id=_OP_ID,
        entry_kind=ENTRY_MEMORY,
        entry_id="shared",
    ))
    memories = [_memory(id_="shared", salience=0.9)]
    service, _ = _build_service(memories=memories, pin_repo=pins)
    view_op1 = await service.build_view(_CHAR_ID, _OP_ID)
    view_op2 = await service.build_view(_CHAR_ID, _OTHER_OP)
    assert view_op1.timeline[0].pinned is True
    assert view_op2.timeline[0].pinned is False
    assert view_op1.pin_count == 1
    assert view_op2.pin_count == 0


@pytest.mark.asyncio
async def test_pin_is_idempotent() -> None:
    service, pins = _build_service(memories=[_memory(id_="m1")])
    first = await service.pin(
        character_id=_CHAR_ID,
        operator_id=_OP_ID,
        entry_kind=ENTRY_MEMORY,
        entry_id="m1",
    )
    second = await service.pin(
        character_id=_CHAR_ID,
        operator_id=_OP_ID,
        entry_kind=ENTRY_MEMORY,
        entry_id="m1",
    )
    assert first.id == second.id
    assert await pins.count_for(_CHAR_ID, _OP_ID) == 1


@pytest.mark.asyncio
async def test_pin_limit_raises_after_cap_reached() -> None:
    service, _ = _build_service(settings=_settings(pin_max_per_pair=2))
    await service.pin(
        character_id=_CHAR_ID, operator_id=_OP_ID,
        entry_kind=ENTRY_MEMORY, entry_id="a",
    )
    await service.pin(
        character_id=_CHAR_ID, operator_id=_OP_ID,
        entry_kind=ENTRY_MEMORY, entry_id="b",
    )
    with pytest.raises(MemoirPinLimitExceededError) as exc:
        await service.pin(
            character_id=_CHAR_ID, operator_id=_OP_ID,
            entry_kind=ENTRY_MEMORY, entry_id="c",
        )
    assert exc.value.limit == 2
    assert exc.value.current == 2


@pytest.mark.asyncio
async def test_pin_after_cap_idempotent_for_existing_entry() -> None:
    service, _ = _build_service(settings=_settings(pin_max_per_pair=2))
    pin_a = await service.pin(
        character_id=_CHAR_ID, operator_id=_OP_ID,
        entry_kind=ENTRY_MEMORY, entry_id="a",
    )
    await service.pin(
        character_id=_CHAR_ID, operator_id=_OP_ID,
        entry_kind=ENTRY_MEMORY, entry_id="b",
    )
    # Re-pinning 'a' must still succeed (idempotent) even at cap.
    pin_a_again = await service.pin(
        character_id=_CHAR_ID, operator_id=_OP_ID,
        entry_kind=ENTRY_MEMORY, entry_id="a",
    )
    assert pin_a.id == pin_a_again.id


@pytest.mark.asyncio
async def test_unpin_returns_false_when_no_match() -> None:
    service, _ = _build_service()
    result = await service.unpin(
        character_id=_CHAR_ID, operator_id=_OP_ID,
        entry_kind=ENTRY_MEMORY, entry_id="never-pinned",
    )
    assert result is False


@pytest.mark.asyncio
async def test_unpin_removes_existing() -> None:
    service, pins = _build_service()
    await service.pin(
        character_id=_CHAR_ID, operator_id=_OP_ID,
        entry_kind=ENTRY_MEMORY, entry_id="x",
    )
    assert await pins.count_for(_CHAR_ID, _OP_ID) == 1
    removed = await service.unpin(
        character_id=_CHAR_ID, operator_id=_OP_ID,
        entry_kind=ENTRY_MEMORY, entry_id="x",
    )
    assert removed is True
    assert await pins.count_for(_CHAR_ID, _OP_ID) == 0


@pytest.mark.asyncio
async def test_emotion_entry_carries_label_and_valence_extras() -> None:
    service, _ = _build_service(
        emotions=[_emotion(id_="e1", intensity=0.8, label="被理解了")],
    )
    view = await service.build_view(_CHAR_ID, _OP_ID)
    assert len(view.timeline) == 1
    entry = view.timeline[0]
    assert entry.kind == ENTRY_EMOTION
    assert entry.summary == "被理解了"
    assert entry.extras["emotion_label"] == "被理解了"
    assert entry.extras["cause_ref_kind"] == CAUSE_TURN


@pytest.mark.asyncio
async def test_timeline_limit_truncates() -> None:
    memories = [
        _memory(
            id_=f"m{i}",
            salience=0.9,
            created_at=_NOW - timedelta(hours=i),
        )
        for i in range(10)
    ]
    service, _ = _build_service(
        memories=memories,
        settings=_settings(timeline_limit=3),
    )
    view = await service.build_view(_CHAR_ID, _OP_ID)
    assert len(view.timeline) == 3
    # Newest first when nothing is pinned.
    assert [e.entry_id for e in view.timeline] == ["m0", "m1", "m2"]
