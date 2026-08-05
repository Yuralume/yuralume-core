"""OP2-E — beat realized → participant provenance (ARC_PLAYER_POSITION_PLAN).

A beat's ``operator_position`` is dramatic staging metadata; once the
beat is realized into a ``StoryEvent`` and memorialized, the memory
should carry a structural fact — "the player was in this scene" —
rather than leaving it for a future reader to re-derive from prose.
This is a strictly one-way projection (plan red line 3): it must never
touch ``ScheduleActivity``/schedule's own ``operator_involvement``
``ParticipantRef`` role vocabulary (``operator_confirmed_shared`` et
al.), which is invitation-lifecycle semantics, not dramatic position.

Two production paths turn a beat into a realized memory:

- ``StoryEventService.record_arc_beat_realization`` — used both by the
  chat post-turn ``mark_realized`` adjustment and by the autonomous
  ``StoryBeatSceneService.play_beat`` scene writer.
- ``StoryEventService._build_and_persist_from_beat`` — the expander
  scene path. OP2-C's own tests note this seam is currently orphaned
  in production (no caller since ``ensure_today``'s arc branch moved to
  the attempt/recheck flow), but it is still one of the plan's two
  named realize paths, so its behaviour is asserted here too.

Both funnel through the same private ``_memorialize``, so the
"identical behaviour across both realize paths" acceptance criterion
is a structural guarantee, not a coincidence this suite has to
maintain by hand — these tests exist to pin that down, not to
duplicate logic per path.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.story_arc_service import StoryArcService
from kokoro_link.application.services.story_event_service import (
    StoryEventService,
    _operator_participants_for_position,
)
from kokoro_link.application.services.story_gacha import StoryGachaService
from kokoro_link.contracts.story import StoryEventExpanderPort
from kokoro_link.contracts.story_arc import StoryArcPlannerPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import (
    OPERATOR_CONFIRMED_SHARED_ROLE,
    OPERATOR_INVITE_PENDING_ROLE,
    OPERATOR_WISH_ROLE,
)
from kokoro_link.domain.entities.story_arc import (
    OPERATOR_POSITION_ABSENT,
    OPERATOR_POSITION_CENTRAL,
    OPERATOR_POSITION_PRESENT,
    StoryArc,
    StoryArcBeat,
    TENSION_SETUP,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.repositories.in_memory_stories import (
    InMemoryStoryEventRepository,
    InMemoryStorySeedRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_story_arcs import (
    InMemoryStoryArcRepository,
)


class _NullExpander(StoryEventExpanderPort):
    """Expander used by paths this suite doesn't exercise via the LLM."""

    async def expand(
        self, *, seed, character_name, character_summary, speaking_style,
        world_frame, scene=None, character=None,
    ):
        return (f"展開：{seed.seed_text}", None)


class _OnePositionedBeatPlanner(StoryArcPlannerPort):
    """Planner that always produces one beat on ``today`` with a fixed
    ``operator_position`` — the only knob this suite needs."""

    def __init__(self, today: date, *, position: str | None) -> None:
        self._today = today
        self._position = position

    async def plan_arc(
        self,
        *,
        character: Character,
        start_date: date,
        duration_days: int = 21,
        beat_count_hint: int = 5,
        hint: str | None = None,
        recent_dialogue_summary: str = "",
    ) -> StoryArc:
        arc = StoryArc.create(
            character_id=character.id,
            title="test arc",
            premise="setup premise",
            theme="custom",
            start_date=start_date,
            end_date=start_date + timedelta(days=duration_days),
        )
        beat = StoryArcBeat.create(
            arc_id=arc.id, sequence=0,
            scheduled_date=self._today,
            title="today beat", summary="今天要發生的事",
            tension=TENSION_SETUP,
            operator_position=self._position,
        )
        return arc.with_beats([beat])


def _character() -> Character:
    return Character.create(
        name="Yui", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


def _services(today: date, *, position: str | None):
    seed_repo = InMemoryStorySeedRepository()
    event_repo = InMemoryStoryEventRepository()
    memory_repo = InMemoryMemoryRepository()
    arc_repo = InMemoryStoryArcRepository()
    arc_service = StoryArcService(
        repository=arc_repo,
        planner=_OnePositionedBeatPlanner(today, position=position),
    )
    gacha = StoryGachaService(seed_repository=seed_repo, event_repository=event_repo)
    event_service = StoryEventService(
        gacha=gacha,
        expander=_NullExpander(),
        event_repository=event_repo,
        memory_repository=memory_repo,
        embedder=None,
        local_tz=timezone.utc,
        arc_service=arc_service,
    )
    return event_service, arc_service, memory_repo


# --- the pure translation table ----------------------------------------


def test_absent_produces_no_operator_participant() -> None:
    assert _operator_participants_for_position(OPERATOR_POSITION_ABSENT) == ()


def test_unjudged_produces_no_operator_participant() -> None:
    """None must not be fabricated into evidence — the beat's position
    has never been classified, so the memory cannot claim otherwise."""
    assert _operator_participants_for_position(None) == ()


def test_present_produces_one_operator_participant_with_matching_role() -> None:
    refs = _operator_participants_for_position(OPERATOR_POSITION_PRESENT)
    assert len(refs) == 1
    ref = refs[0]
    assert ref.actor_kind == "operator"
    assert ref.actor_id is None
    assert ref.role == OPERATOR_POSITION_PRESENT


def test_central_produces_one_operator_participant_with_matching_role() -> None:
    refs = _operator_participants_for_position(OPERATOR_POSITION_CENTRAL)
    assert len(refs) == 1
    ref = refs[0]
    assert ref.actor_kind == "operator"
    assert ref.actor_id is None
    assert ref.role == OPERATOR_POSITION_CENTRAL


def test_an_unrecognised_position_degrades_to_no_participant() -> None:
    """Same forgiving-read posture as the storage decoders (plan §3.1):
    a stale/hand-edited value must not blow up realization, it just
    reads like unjudged."""
    assert _operator_participants_for_position("bystander") == ()


def test_role_vocabulary_is_disjoint_from_schedule_operator_involvement() -> None:
    """Red line 3: dramatic position and schedule's invitation-lifecycle
    ``operator_involvement`` are different semantics and must not share
    role strings, or a role scan would conflate the two subsystems."""
    schedule_roles = {
        OPERATOR_CONFIRMED_SHARED_ROLE,
        OPERATOR_INVITE_PENDING_ROLE,
        OPERATOR_WISH_ROLE,
    }
    position_roles = {
        ref.role
        for position in (OPERATOR_POSITION_PRESENT, OPERATOR_POSITION_CENTRAL)
        for ref in _operator_participants_for_position(position)
    }
    assert schedule_roles.isdisjoint(position_roles)


# --- integration: realize a beat, check the memory's participants ------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("position", "expected_role"),
    [
        (OPERATOR_POSITION_CENTRAL, OPERATOR_POSITION_CENTRAL),
        (OPERATOR_POSITION_PRESENT, OPERATOR_POSITION_PRESENT),
    ],
)
async def test_record_arc_beat_realization_writes_operator_participant(
    position: str, expected_role: str,
) -> None:
    today = date(2026, 5, 10)
    now = datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)
    event_service, arc_service, memory_repo = _services(today, position=position)
    character = _character()
    arc = await arc_service.start_new_arc(character, today=today)
    beat = arc.beats[0]

    await event_service.record_arc_beat_realization(
        character, beat_id=beat.id, narrative="今天發生了那件事。", now=now,
    )

    memories = await memory_repo.query(character.id)
    memory = next(m for m in memories if m.content == "今天發生了那件事。")
    assert len(memory.participants) == 1
    ref = memory.participants[0]
    assert ref.actor_kind == "operator"
    assert ref.role == expected_role


@pytest.mark.asyncio
async def test_record_arc_beat_realization_writes_no_participant_for_absent() -> None:
    today = date(2026, 5, 10)
    now = datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)
    event_service, arc_service, memory_repo = _services(
        today, position=OPERATOR_POSITION_ABSENT,
    )
    character = _character()
    arc = await arc_service.start_new_arc(character, today=today)
    beat = arc.beats[0]

    await event_service.record_arc_beat_realization(
        character, beat_id=beat.id, narrative="今天發生了那件事。", now=now,
    )

    memories = await memory_repo.query(character.id)
    memory = next(m for m in memories if m.content == "今天發生了那件事。")
    assert memory.participants == ()


@pytest.mark.asyncio
async def test_record_arc_beat_realization_writes_no_participant_for_unjudged() -> None:
    today = date(2026, 5, 10)
    now = datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)
    event_service, arc_service, memory_repo = _services(today, position=None)
    character = _character()
    arc = await arc_service.start_new_arc(character, today=today)
    beat = arc.beats[0]

    await event_service.record_arc_beat_realization(
        character, beat_id=beat.id, narrative="今天發生了那件事。", now=now,
    )

    memories = await memory_repo.query(character.id)
    memory = next(m for m in memories if m.content == "今天發生了那件事。")
    assert memory.participants == ()


# --- both realize paths must agree --------------------------------------


@pytest.mark.asyncio
async def test_expander_path_lands_the_same_participant_as_the_recheck_path() -> None:
    """OP2-E's third acceptance line: the autonomous/chat realize path
    (``record_arc_beat_realization``) and the expander path
    (``_build_and_persist_from_beat``) must not read like two different
    worlds for the same beat. Both go through ``_memorialize``, so this
    pins the guarantee rather than re-deriving it per path."""
    today = date(2026, 5, 10)
    character = _character()

    # Path 1: record_arc_beat_realization (chat/autonomous realize).
    event_service_a, arc_service_a, memory_repo_a = _services(
        today, position=OPERATOR_POSITION_CENTRAL,
    )
    arc_a = await arc_service_a.start_new_arc(character, today=today)
    beat_a = arc_a.beats[0]
    await event_service_a.record_arc_beat_realization(
        character, beat_id=beat_a.id, narrative="她終於說了。",
        now=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
    )
    memories_a = await memory_repo_a.query(character.id)
    memory_a = next(m for m in memories_a if m.content == "她終於說了。")

    # Path 2: the expander scene path, called directly (OP2-C found it
    # orphaned in production — no live caller — but it remains one of
    # the plan's two named realize paths). The beat must be persisted
    # through a real arc_service, same as path A — ``_memorialize`` re-
    # resolves the beat's arc via ``arc_service.get_arc_by_beat`` to
    # derive memory shape, it does not trust the ``beat`` object the
    # caller already has in hand.
    event_service_b, arc_service_b, memory_repo_b = _services(
        today, position=OPERATOR_POSITION_CENTRAL,
    )
    arc_b = await arc_service_b.start_new_arc(character, today=today)
    beat_b = arc_b.beats[0]
    event_b = await event_service_b._build_and_persist_from_beat(  # noqa: SLF001
        character, today, beat_b,
    )
    assert event_b is not None
    memories_b = await memory_repo_b.query(character.id)
    memory_b = next(m for m in memories_b if m.content == event_b.narrative)

    assert memory_a.participants == memory_b.participants
