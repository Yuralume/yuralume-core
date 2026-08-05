"""SC0 — beat retry budget semantics at the service layer.

Three fixes from BACKGROUND_COST_CONTROL_PLAN §10, exercised through
``StoryArcService`` against the in-memory repository:

- **#3** only *failed* staging attempts charge the autonomous retry
  budget; a player's chat turn surfacing the beat is bookkeeping only.
- **#4** ``next_beat_due`` walks past a candidate the policy blocks
  instead of returning ``None`` and freezing the whole arc.
- **#2** a beat whose failure budget is exhausted is retired to
  ``skipped`` (idempotently, with a warning) so the arc moves on.

§1 is characterization of behaviour that must survive the change — most
importantly the chat path, which passes no policy and must keep seeing
the earliest pending beat no matter what its retry history looks like.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.beat_retry_policy import BeatRetryPolicy
from kokoro_link.application.services.story_arc_service import StoryArcService
from kokoro_link.contracts.story_arc import StoryArcPlannerPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_arc import (
    ARC_COMPLETED,
    BEAT_PENDING,
    BEAT_SKIPPED,
    PLAY_RESULT_FAILED,
    PLAY_RESULT_RETRY_EXHAUSTED,
    StoryArc,
    StoryArcBeat,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_story_arcs import (
    InMemoryStoryArcRepository,
)

UTC = timezone.utc
START = date(2026, 5, 1)
T0 = datetime(2026, 5, 1, 8, 0, tzinfo=UTC)

POLICY = BeatRetryPolicy(
    max_attempts=3,
    initial_backoff=timedelta(hours=1),
    maximum_backoff=timedelta(hours=4),
)


class _NeverPlanner(StoryArcPlannerPort):
    """These tests operate on hand-built arcs; planning would be a bug."""

    async def plan_arc(self, **kwargs) -> StoryArc:  # noqa: ANN003
        raise AssertionError("retry-budget tests must not plan an arc")


def _character() -> Character:
    return Character.create(
        name="Yui", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


async def _arc_with_beats(
    repo: InMemoryStoryArcRepository,
    character_id: str,
    *,
    day_offsets: tuple[int, ...] = (0, 5, 10),
) -> StoryArc:
    arc = StoryArc.create(
        character_id=character_id,
        title="主軸",
        premise="前情。",
        theme="custom",
        start_date=START,
        end_date=START + timedelta(days=21),
    )
    arc = arc.with_beats([
        StoryArcBeat.create(
            arc_id=arc.id,
            sequence=index,
            scheduled_date=START + timedelta(days=offset),
            title=f"beat {index}",
            summary=f"summary {index}",
        )
        for index, offset in enumerate(day_offsets)
    ])
    await repo.add(arc)
    return arc


def _service(repo: InMemoryStoryArcRepository) -> StoryArcService:
    return StoryArcService(repository=repo, planner=_NeverPlanner())


async def _fail(
    svc: StoryArcService, beat_id: str, *, times: int, at: datetime = T0,
) -> None:
    for index in range(times):
        await svc.mark_beat_play_attempted(
            beat_id=beat_id,
            attempted_at=at + timedelta(minutes=index),
            source="scene_simulation",
            result=PLAY_RESULT_FAILED,
            push_intensity="autonomous_scene",
        )


async def _prompt(
    svc: StoryArcService, beat_id: str, *, times: int, at: datetime = T0,
) -> None:
    for index in range(times):
        await svc.mark_beat_play_attempted(
            beat_id=beat_id,
            attempted_at=at + timedelta(days=index),
            source="chat_scene_directive",
            result="prompted",
            push_intensity="scene_directive",
        )


# --- §1 characterization ---------------------------------------------


@pytest.mark.asyncio
async def test_chat_path_sees_the_earliest_pending_beat_whatever_its_history() -> None:
    repo = InMemoryStoryArcRepository()
    svc = _service(repo)
    character = _character()
    arc = await _arc_with_beats(repo, character.id)
    await _fail(svc, arc.beats[0].id, times=9)

    # No retry policy: the chat path is not rate-limited by the
    # autonomous scene budget, so an exhausted beat still surfaces as a
    # scene directive.
    due = await svc.next_beat_due(character.id, today=START)

    assert due is not None
    assert due[1].id == arc.beats[0].id


@pytest.mark.asyncio
async def test_every_attempt_is_still_bookkept_for_the_llm() -> None:
    repo = InMemoryStoryArcRepository()
    svc = _service(repo)
    character = _character()
    arc = await _arc_with_beats(repo, character.id)
    await _prompt(svc, arc.beats[0].id, times=4)

    stored = await repo.get(arc.id)
    assert stored is not None
    beat = stored.find_beat(arc.beats[0].id)
    assert beat is not None
    # ``play_attempt_count`` keeps its "how often was this surfaced"
    # meaning — the prompt layer renders it.
    assert beat.play_attempt_count == 4
    assert beat.last_play_attempt_source == "chat_scene_directive"
    assert beat.last_play_attempt_result == "prompted"


@pytest.mark.asyncio
async def test_next_beat_due_is_none_when_every_candidate_is_blocked() -> None:
    repo = InMemoryStoryArcRepository()
    svc = _service(repo)
    character = _character()
    arc = await _arc_with_beats(repo, character.id)
    for beat in arc.beats:
        await _fail(svc, beat.id, times=1)

    assert await svc.next_beat_due(
        character.id,
        today=START + timedelta(days=10),
        retry_policy=POLICY,
        retry_at=T0,  # inside every beat's backoff window
    ) is None


@pytest.mark.asyncio
async def test_next_beat_due_without_active_arc_is_none() -> None:
    repo = InMemoryStoryArcRepository()
    svc = _service(repo)

    assert await svc.next_beat_due("nobody", today=START) is None


# --- §2 #3 only failures charge the budget ----------------------------


@pytest.mark.asyncio
async def test_chat_attempts_do_not_consume_the_autonomous_retry_budget() -> None:
    repo = InMemoryStoryArcRepository()
    svc = _service(repo)
    character = _character()
    arc = await _arc_with_beats(repo, character.id)
    # Six days of the player chatting — twice the policy's budget.
    await _prompt(svc, arc.beats[0].id, times=6)

    due = await svc.next_beat_due(
        character.id,
        today=START,
        retry_policy=POLICY,
        retry_at=T0 + timedelta(days=6),
    )

    assert due is not None
    assert due[1].id == arc.beats[0].id
    assert due[1].play_attempt_count == 6
    assert due[1].play_failure_count == 0


@pytest.mark.asyncio
async def test_a_chat_attempt_does_not_push_out_the_failure_backoff() -> None:
    repo = InMemoryStoryArcRepository()
    svc = _service(repo)
    character = _character()
    arc = await _arc_with_beats(repo, character.id)
    beat_id = arc.beats[0].id
    await _fail(svc, beat_id, times=1, at=T0)
    await svc.mark_beat_play_attempted(
        beat_id=beat_id,
        attempted_at=T0 + timedelta(minutes=30),
        source="chat_scene_directive",
        result="prompted",
        push_intensity="scene_directive",
    )

    # Retry window is still anchored on the failure at T0.
    assert await svc.next_beat_due(
        character.id, today=START, retry_policy=POLICY,
        retry_at=T0 + timedelta(minutes=59),
    ) is None
    assert await svc.next_beat_due(
        character.id, today=START, retry_policy=POLICY,
        retry_at=T0 + timedelta(hours=1),
    ) is not None


# --- §3 #4 walk down the candidate list -------------------------------


@pytest.mark.asyncio
async def test_next_beat_due_walks_past_a_blocked_candidate() -> None:
    repo = InMemoryStoryArcRepository()
    svc = _service(repo)
    character = _character()
    arc = await _arc_with_beats(repo, character.id)
    await _fail(svc, arc.beats[0].id, times=3)  # exhausted, still pending

    due = await svc.next_beat_due(
        character.id,
        today=START + timedelta(days=10),
        retry_policy=POLICY,
        retry_at=T0 + timedelta(days=10),
    )

    assert due is not None
    assert due[1].id == arc.beats[1].id


@pytest.mark.asyncio
async def test_walk_down_respects_schedule_order() -> None:
    repo = InMemoryStoryArcRepository()
    svc = _service(repo)
    character = _character()
    arc = await _arc_with_beats(repo, character.id)
    await _fail(svc, arc.beats[0].id, times=3)
    await _fail(svc, arc.beats[1].id, times=3)

    due = await svc.next_beat_due(
        character.id,
        today=START + timedelta(days=10),
        retry_policy=POLICY,
        retry_at=T0 + timedelta(days=10),
    )

    assert due is not None
    assert due[1].id == arc.beats[2].id


# --- §4 #2 exhausted beats are retired --------------------------------


@pytest.mark.asyncio
async def test_retire_exhausted_beats_skips_and_warns(caplog) -> None:  # noqa: ANN001
    repo = InMemoryStoryArcRepository()
    svc = _service(repo)
    character = _character()
    arc = await _arc_with_beats(repo, character.id)
    await _fail(svc, arc.beats[0].id, times=3)

    with caplog.at_level(logging.WARNING):
        updated = await svc.retire_exhausted_beats(
            character.id,
            retry_policy=POLICY,
            today=START,
        )

    assert updated is not None
    retired = updated.find_beat(arc.beats[0].id)
    assert retired is not None
    assert retired.status == BEAT_SKIPPED
    assert retired.last_play_attempt_result == PLAY_RESULT_RETRY_EXHAUSTED
    # Untouched neighbours stay pending.
    assert updated.find_beat(arc.beats[1].id).status == BEAT_PENDING
    # Persisted, not just returned.
    stored = await repo.get(arc.id)
    assert stored.find_beat(arc.beats[0].id).status == BEAT_SKIPPED
    # Traceable without an alert service: character / arc / beat ids.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings
    message = warnings[-1].getMessage()
    assert character.id in message
    assert arc.id in message
    assert arc.beats[0].id in message


@pytest.mark.asyncio
async def test_retire_exhausted_beats_is_idempotent() -> None:
    repo = InMemoryStoryArcRepository()
    svc = _service(repo)
    character = _character()
    arc = await _arc_with_beats(repo, character.id)
    await _fail(svc, arc.beats[0].id, times=3)

    first = await svc.retire_exhausted_beats(
        character.id, retry_policy=POLICY, today=START,
    )
    second = await svc.retire_exhausted_beats(
        character.id, retry_policy=POLICY, today=START,
    )

    assert first is not None
    assert second is None  # nothing left to retire → no write


@pytest.mark.asyncio
async def test_retire_exhausted_beats_leaves_backoff_blocked_beats_alone() -> None:
    repo = InMemoryStoryArcRepository()
    svc = _service(repo)
    character = _character()
    arc = await _arc_with_beats(repo, character.id)
    await _fail(svc, arc.beats[0].id, times=1)  # blocked, not exhausted

    assert await svc.retire_exhausted_beats(
        character.id, retry_policy=POLICY, today=START,
    ) is None
    stored = await repo.get(arc.id)
    assert stored.find_beat(arc.beats[0].id).status == BEAT_PENDING


@pytest.mark.asyncio
async def test_retire_exhausted_beats_ignores_beats_not_yet_due() -> None:
    repo = InMemoryStoryArcRepository()
    svc = _service(repo)
    character = _character()
    arc = await _arc_with_beats(repo, character.id)
    await _fail(svc, arc.beats[2].id, times=3)  # scheduled 10 days out

    assert await svc.retire_exhausted_beats(
        character.id, retry_policy=POLICY, today=START,
    ) is None


@pytest.mark.asyncio
async def test_retire_exhausted_beats_completes_the_arc_when_last_pending() -> None:
    repo = InMemoryStoryArcRepository()
    svc = _service(repo)
    character = _character()
    arc = await _arc_with_beats(repo, character.id, day_offsets=(0,))
    await _fail(svc, arc.beats[0].id, times=3)

    updated = await svc.retire_exhausted_beats(
        character.id, retry_policy=POLICY, today=START,
    )

    assert updated is not None
    assert updated.status == ARC_COMPLETED


@pytest.mark.asyncio
async def test_retire_exhausted_beats_without_active_arc_is_none() -> None:
    repo = InMemoryStoryArcRepository()
    svc = _service(repo)

    assert await svc.retire_exhausted_beats(
        "nobody", retry_policy=POLICY, today=START,
    ) is None


@pytest.mark.asyncio
async def test_retired_beat_is_gone_from_every_read_path() -> None:
    """Retirement is permanent — the arc moves on for chat too.

    Plan §2 #6 拍板: "劇情不卡住" is the value proposition, so a retired
    beat disappearing from the chat path is the intended trade, not a
    regression of the "chat path is not policy-gated" rule above.
    """
    repo = InMemoryStoryArcRepository()
    svc = _service(repo)
    character = _character()
    arc = await _arc_with_beats(repo, character.id)
    await _fail(svc, arc.beats[0].id, times=3)
    await svc.retire_exhausted_beats(
        character.id, retry_policy=POLICY, today=START + timedelta(days=10),
    )

    due = await svc.next_beat_due(character.id, today=START + timedelta(days=10))

    assert due is not None
    assert due[1].id == arc.beats[1].id
