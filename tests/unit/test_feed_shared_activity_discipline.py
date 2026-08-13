"""CF4 feed side: the wall must not publish an unproven joint outing.

The 7/27 post — "和木木一起跑到那間說好要來的刨冰店，終於吃到啦～" — was
composed from a schedule activity whose only operator link was a pending
invite. Two defences, tested here:

1. **Input**: the schedule collector projects the structured involvement
   verdict into the candidate the composer reads, so the model is never
   left to infer "we went together" from a planned description.
2. **Prompt**: the composer template carries the rule outright, which is
   the only defence available when the material is an older, already
   polluted memory rather than a live activity.

A tuned prompt overlay may carry the same rule in its own wording; this
pins the baseline that ships with Core, per the CF6b convention.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from kokoro_link.application.services.feed_candidates import (
    FeedCandidateCollector,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import (
    OPERATOR_CONFIRMED_SHARED_ROLE,
    OPERATOR_INVITE_PENDING_ROLE,
    DailySchedule,
    ScheduleActivity,
)
from kokoro_link.domain.value_objects.actor import ParticipantRef
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.prompts import get_default_loader
from kokoro_link.infrastructure.repositories.in_memory_feed_posts import (
    InMemoryFeedPostRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_schedules import (
    InMemoryScheduleRepository,
)

UTC = timezone.utc

_DAY = date(2026, 7, 27)
_START = datetime(2026, 7, 27, 14, 0, tzinfo=UTC)
_END = datetime(2026, 7, 27, 15, 0, tzinfo=UTC)
_NOW = datetime(2026, 7, 27, 15, 30, tzinfo=UTC)


def _character(last_active_at: datetime | None) -> Character:
    character = Character.create(
        name="芊璃",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="平靜",
            affection=60,
            fatigue=10,
            trust=60,
            energy=90,
            last_active_at=last_active_at,
        ),
    )
    return replace(character, user_id="owner-1")


async def _collect(
    *, role: str | None, last_active_at: datetime | None,
):  # noqa: ANN201 - tuple[FeedCandidate, ...]
    refs = (
        (
            ParticipantRef(
                actor_kind="operator",
                actor_id=None,
                display_name="木木",
                role=role,
            ),
        )
        if role is not None
        else ()
    )
    character = _character(last_active_at)
    schedules = InMemoryScheduleRepository()
    await schedules.save(DailySchedule.create(
        character_id=character.id,
        date_=_DAY,
        activities=[ScheduleActivity.create(
            start_at=_START,
            end_at=_END,
            description="與木木一起前往先前約定的刨冰店，吃刨冰",
            category="social",
            location="刨冰店",
            participant_refs=refs,
        )],
    ))
    collector = FeedCandidateCollector(
        feed_posts=InMemoryFeedPostRepository(), schedules=schedules,
    )
    cands = await collector.collect(character, now=_NOW)
    return tuple(c for c in cands if c.source.kind == "schedule")


@pytest.mark.asyncio
async def test_pending_invite_candidate_states_the_user_did_not_take_part() -> None:
    cands = await _collect(
        role=OPERATOR_INVITE_PENDING_ROLE,
        last_active_at=datetime(2026, 7, 26, 21, 0, tzinfo=UTC),
    )

    assert len(cands) == 1
    snippets = "\n".join(cands[0].context_snippets)
    assert "使用者參與狀態" in snippets
    assert "從沒答應" in snippets
    assert "不可以" in cands[0].hint


@pytest.mark.asyncio
async def test_confirmed_without_evidence_still_blocks_a_joint_claim() -> None:
    cands = await _collect(
        role=OPERATOR_CONFIRMED_SHARED_ROLE,
        last_active_at=datetime(2026, 7, 26, 21, 0, tzinfo=UTC),
    )

    snippets = "\n".join(cands[0].context_snippets)
    assert "沒有任何證據顯示他真的參與" in snippets
    assert "不可以" in cands[0].hint
    # …and it must not swing to publicly blaming the user either.
    assert "指責他失約" in cands[0].hint


@pytest.mark.asyncio
async def test_confirmed_with_interaction_evidence_leaves_the_hint_alone() -> None:
    cands = await _collect(
        role=OPERATOR_CONFIRMED_SHARED_ROLE,
        last_active_at=datetime(2026, 7, 27, 14, 30, tzinfo=UTC),
    )

    snippets = "\n".join(cands[0].context_snippets)
    assert "確實有互動紀錄" in snippets
    assert "不可以" not in cands[0].hint


@pytest.mark.asyncio
async def test_solo_activity_candidate_is_unchanged() -> None:
    cands = await _collect(role=None, last_active_at=None)

    assert not any(
        "使用者參與狀態" in s for s in cands[0].context_snippets
    )
    assert "不可以" not in cands[0].hint


def test_composer_prompt_forbids_unproven_joint_claims() -> None:
    prompt = get_default_loader().render(
        "feed/composer",
        schema_line="{}",
        max_body_chars=280,
        prompts_clause="",
        persona_block="",
        knowledge_boundary_block="",
        fact_block="",
        kind_value="work",
        source_kind="schedule",
        hint="",
        snippet_block="",
    )

    assert "一起完成" in prompt
    assert "確實參與" in prompt
    # Old memories are the second entry point — a memory saying "一起" is
    # not evidence, and the rule has to say so.
    assert "記憶" in prompt
