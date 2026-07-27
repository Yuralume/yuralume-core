"""Unit coverage for the P3-Dedup visible-output slot claims + conversions.

Covers, without a database:

* slot-port semantics (in-memory twin: claim / dup / prune) + the SA
  narrow-constraint check that makes a non-claim IntegrityError re-raise;
* proactive dispatch records ``SLOT_TAKEN`` (no provider call) when it loses
  the tick slot, and claims + sends when it wins;
* feed-comment reply skips the whole pass when it loses the slot;
* schedule memorialize claim/release rowcount paths + one-memory-per-race;
* encounter run-claim: exactly one runner transitions ``planned → running``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.feed_comment_reply_service import (
    FeedCommentReplyService,
)
from kokoro_link.application.services.feed_comment_service import (
    FeedCommentService,
)
from kokoro_link.application.services.proactive_dispatcher import (
    ProactiveDispatcher,
)
from kokoro_link.contracts.feed_comment_reply import (
    FeedCommentReplyComposerPort,
    FeedCommentReplyInput,
    FeedCommentReplyOutput,
)
from kokoro_link.contracts.proactive import (
    ProactiveContext,
    ProactiveDecision,
    ProactiveDeciderPort,
)
from kokoro_link.contracts.visible_slots import (
    SLOT_KIND_FEED_REPLY,
    SLOT_KIND_PROACTIVE,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.feed_comment import (
    LOCAL_COMMENTER_ID,
    FeedComment,
)
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.proactive.heuristic_gate import (
    HeuristicProactiveGate,
)
from kokoro_link.infrastructure.persistence.sa_visible_slots import (
    _is_claim_violation,
)
from kokoro_link.infrastructure.repositories.in_memory_feed_comments import (
    InMemoryFeedCommentRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_feed_posts import (
    InMemoryFeedPostRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_proactive_attempts import (
    InMemoryProactiveAttemptRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_visible_slots import (
    InMemoryVisibleSlotRepository,
)
from tests.unit._messaging_harness import build_messaging_harness, create_character


UTC = timezone.utc


# ======================================================================
# Slot port semantics
# ======================================================================


@pytest.mark.asyncio
async def test_slot_claim_then_dup_loses() -> None:
    slots = InMemoryVisibleSlotRepository()
    assert await slots.claim("c1", SLOT_KIND_PROACTIVE, "b1") is True
    # Same triple → the second claimant loses.
    assert await slots.claim("c1", SLOT_KIND_PROACTIVE, "b1") is False
    # A different bucket / kind / character is an independent slot.
    assert await slots.claim("c1", SLOT_KIND_PROACTIVE, "b2") is True
    assert await slots.claim("c1", SLOT_KIND_FEED_REPLY, "b1") is True
    assert await slots.claim("c2", SLOT_KIND_PROACTIVE, "b1") is True


@pytest.mark.asyncio
async def test_slot_prune_removes_only_stale_claims() -> None:
    slots = InMemoryVisibleSlotRepository()
    await slots.claim("c1", SLOT_KIND_PROACTIVE, "old")
    now = datetime.now(UTC)
    # Nothing older than retention yet.
    assert await slots.prune(now=now, retention_days=7) == 0
    # Jump the clock 8 days forward: the claim is now prunable and the slot
    # frees up for re-claim.
    future = now + timedelta(days=8)
    assert await slots.prune(now=future, retention_days=7) == 1
    assert await slots.claim("c1", SLOT_KIND_PROACTIVE, "old") is True


def test_sa_claim_violation_is_narrow() -> None:
    class _Err:
        def __init__(self, text: str) -> None:
            self.orig = text

    assert _is_claim_violation(
        _Err("duplicate key value violates unique constraint "
             "\"uq_background_visible_slots_claim\""),
    )
    # A DIFFERENT integrity failure must NOT read as the benign dedup path —
    # the SA adapter re-raises it instead of swallowing a lost claim.
    assert not _is_claim_violation(_Err("some other fk violation"))


# ======================================================================
# Proactive dispatch slot claim
# ======================================================================


class _FixedDecider(ProactiveDeciderPort):
    def __init__(self, decision: ProactiveDecision) -> None:
        self._decision = decision
        self.calls: list[ProactiveContext] = []

    async def decide(self, context: ProactiveContext) -> ProactiveDecision:
        self.calls.append(context)
        return self._decision


def _build_slot_dispatcher(harness, decider, slots):
    attempts = InMemoryProactiveAttemptRepository()
    dispatcher = ProactiveDispatcher(
        character_repository=harness.character_repository,
        conversation_repository=harness.conversation_repository,
        account_repository=harness.account_repository,
        binding_repository=harness.binding_repository,
        attempt_repository=attempts,
        gate=HeuristicProactiveGate(
            local_tz=UTC, quiet_hour_start=0, quiet_hour_end=0,
        ),
        decider=decider,
        adapters={
            Platform.TELEGRAM: harness.telegram_adapter,
            Platform.LINE: harness.line_adapter,
        },
        visible_slot_port=slots,
    )
    return dispatcher, attempts


async def _enable_web_character(harness):
    dto = await create_character(harness)
    character = await harness.character_repository.get(dto.id)
    assert character is not None
    updated = character.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, aspirations=None, appearance=None,
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
            last_active_at=datetime.now(UTC) - timedelta(minutes=60),
        ),
        proactive_enabled=True,
        accepts_web_proactive=True,
    )
    await harness.character_repository.save(updated)
    return updated


@pytest.mark.asyncio
async def test_proactive_lost_slot_records_slot_taken_and_skips_provider() -> None:
    harness = build_messaging_harness()
    character = await _enable_web_character(harness)
    slots = InMemoryVisibleSlotRepository()
    # Another executor already owns this character's proactive slot.
    await slots.claim(character.id, SLOT_KIND_PROACTIVE, "bucket-42")
    decider = _FixedDecider(ProactiveDecision(True, "ok", "hi"))
    dispatcher, _ = _build_slot_dispatcher(harness, decider, slots)

    attempt = await dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
        logical_slot="bucket-42",
    )

    assert attempt.outcome == ProactiveOutcome.SLOT_TAKEN
    # The gate passed, but the lost slot short-circuits BEFORE the decider /
    # any provider call.
    assert decider.calls == []
    assert harness.telegram_adapter.sent == []


@pytest.mark.asyncio
async def test_proactive_won_slot_sends_and_locks_the_bucket() -> None:
    harness = build_messaging_harness()
    character = await _enable_web_character(harness)
    slots = InMemoryVisibleSlotRepository()
    decider = _FixedDecider(ProactiveDecision(True, "ok", "早安"))
    dispatcher, _ = _build_slot_dispatcher(harness, decider, slots)

    first = await dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
        logical_slot="bucket-7",
    )
    assert first.outcome == ProactiveOutcome.SENT
    # The winning send took the slot, so a racing second runner on the SAME
    # bucket now loses it.
    assert await slots.claim(character.id, SLOT_KIND_PROACTIVE, "bucket-7") is False


@pytest.mark.asyncio
async def test_proactive_none_slot_is_unconstrained() -> None:
    harness = build_messaging_harness()
    character = await _enable_web_character(harness)
    slots = InMemoryVisibleSlotRepository()
    decider = _FixedDecider(ProactiveDecision(True, "ok", "hi"))
    dispatcher, _ = _build_slot_dispatcher(harness, decider, slots)

    # Manual / API path passes logical_slot=None → no claim taken at all.
    attempt = await dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.from_string("manual"),
    )
    assert attempt.outcome == ProactiveOutcome.SENT
    # No slot was consumed for the manual path.
    assert await slots.claim(character.id, SLOT_KIND_PROACTIVE, "anything") is True


# ======================================================================
# Feed comment reply slot claim
# ======================================================================


class _ScriptedReplyComposer(FeedCommentReplyComposerPort):
    def __init__(self, text: str) -> None:
        self._text = text
        self.inputs: list[FeedCommentReplyInput] = []

    async def compose(
        self, payload: FeedCommentReplyInput,
    ) -> FeedCommentReplyOutput:
        self.inputs.append(payload)
        return FeedCommentReplyOutput(content_text=self._text)


def _feed_character() -> Character:
    char = Character.create(
        name="Aiko", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=20, trust=50, energy=80,
        ),
        feed_daily_limit=3,
    )
    return replace(char, id="aiko")


@pytest.mark.asyncio
async def test_feed_reply_lost_slot_skips_pass() -> None:
    char = _feed_character()
    posts = InMemoryFeedPostRepository()
    comments = InMemoryFeedCommentRepository()
    post = FeedPost.create(
        character_id=char.id, kind=FeedKind.MOOD, content_text="咖啡好喝",
        source=FeedSource.silence(),
        created_at=datetime(2026, 4, 28, 9, 0, tzinfo=UTC),
    )
    await posts.add(post)
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    await comments.add(FeedComment.create(
        post_id=post.id, author_id=LOCAL_COMMENTER_ID,
        content_text="好想喝", created_at=now - timedelta(minutes=10),
    ))
    composer = _ScriptedReplyComposer("改天請你 ☕")
    slots = InMemoryVisibleSlotRepository()
    await slots.claim(char.id, SLOT_KIND_FEED_REPLY, "bucket-1")
    service = FeedCommentReplyService(
        post_repository=posts, comment_repository=comments,
        comment_service=FeedCommentService(
            post_repository=posts, comment_repository=comments,
        ),
        composer=composer,
        visible_slot_port=slots,
    )

    result = await service.tick(char, now=now, logical_slot="bucket-1")

    assert result is None
    # The pass was skipped before any scan/compose.
    assert composer.inputs == []


@pytest.mark.asyncio
async def test_feed_reply_won_slot_composes() -> None:
    char = _feed_character()
    posts = InMemoryFeedPostRepository()
    comments = InMemoryFeedCommentRepository()
    post = FeedPost.create(
        character_id=char.id, kind=FeedKind.MOOD, content_text="咖啡好喝",
        source=FeedSource.silence(),
        created_at=datetime(2026, 4, 28, 9, 0, tzinfo=UTC),
    )
    await posts.add(post)
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    await comments.add(FeedComment.create(
        post_id=post.id, author_id=LOCAL_COMMENTER_ID,
        content_text="好想喝", created_at=now - timedelta(minutes=10),
    ))
    composer = _ScriptedReplyComposer("改天請你 ☕")
    slots = InMemoryVisibleSlotRepository()
    service = FeedCommentReplyService(
        post_repository=posts, comment_repository=comments,
        comment_service=FeedCommentService(
            post_repository=posts, comment_repository=comments,
        ),
        composer=composer,
        visible_slot_port=slots,
    )

    reply = await service.tick(char, now=now, logical_slot="bucket-1")

    assert reply is not None
    assert reply.author_id == char.id
    # The winning pass consumed the feed-reply slot for that bucket.
    assert await slots.claim(char.id, SLOT_KIND_FEED_REPLY, "bucket-1") is False


# ======================================================================
# Schedule memorialize claim / release
# ======================================================================

from datetime import date  # noqa: E402

from kokoro_link.application.services.schedule_memorializer import (  # noqa: E402
    ScheduleMemorializer,
)
from kokoro_link.domain.entities.schedule import (  # noqa: E402
    DailySchedule,
    ScheduleActivity,
)
from kokoro_link.infrastructure.memory.in_memory import (  # noqa: E402
    InMemoryMemoryRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_schedules import (  # noqa: E402
    InMemoryScheduleRepository,
)


def _past_activity(desc: str) -> ScheduleActivity:
    day = date(2026, 4, 18)
    return ScheduleActivity.create(
        start_at=datetime(day.year, day.month, day.day, 9, 0, tzinfo=UTC),
        end_at=datetime(day.year, day.month, day.day, 12, 0, tzinfo=UTC),
        description=desc,
        category="work",
        busy_score=0.8,
    )


@pytest.mark.asyncio
async def test_schedule_claim_memorialize_rowcount_and_release() -> None:
    repo = InMemoryScheduleRepository()
    a1 = _past_activity("剪輯")
    a2 = _past_activity("開會")
    schedule = DailySchedule.create(
        character_id="c1", date_=date(2026, 4, 18), activities=[a1, a2],
    )
    await repo.save(schedule)

    # First claim flips both false→true; returns exactly those ids.
    claimed = await repo.claim_memorialize([a1.id, a2.id])
    assert claimed == {a1.id, a2.id}
    # Second claim of the same ids returns nothing (already true).
    assert await repo.claim_memorialize([a1.id, a2.id]) == set()

    # Release resets, so they become claimable again — this is the retry path.
    await repo.release_memorialize([a1.id])
    assert await repo.claim_memorialize([a1.id, a2.id]) == {a1.id}


@pytest.mark.asyncio
async def test_memorialize_skips_activity_a_racer_already_claimed() -> None:
    repo = InMemoryScheduleRepository()
    memories = InMemoryMemoryRepository()
    a1 = _past_activity("剪輯")
    a2 = _past_activity("開會")
    schedule = DailySchedule.create(
        character_id="c1", date_=date(2026, 4, 18), activities=[a1, a2],
    )
    await repo.save(schedule)
    # Simulate a racing runner that already owns a1's memorialize claim.
    assert await repo.claim_memorialize([a1.id]) == {a1.id}

    memorializer = ScheduleMemorializer(
        schedule_repository=repo, memory_repository=memories, local_tz=UTC,
    )
    now = datetime(2026, 4, 18, 20, 0, tzinfo=UTC)
    count = await memorializer.memorialize(character_id="c1", now=now)

    # Only the un-raced activity (a2) produced a visible memory.
    assert count == 1
    written = await memories.query("c1", limit=10)
    assert len(written) == 1
    assert "開會" in written[0].content


# ======================================================================
# Encounter run-claim
# ======================================================================

from kokoro_link.domain.entities.character_encounter import (  # noqa: E402
    CharacterEncounter,
)
from kokoro_link.infrastructure.repositories.in_memory_character_encounters import (  # noqa: E402
    InMemoryCharacterEncounterRepository,
)


def _planned_encounter() -> CharacterEncounter:
    return CharacterEncounter.plan(
        relationship_id="rel-1",
        character_a_id="a",
        character_b_id="b",
        scheduled_for=datetime(2026, 4, 18, 12, 0, tzinfo=UTC),
        location="咖啡店",
        trigger_reason="約好了",
    )


@pytest.mark.asyncio
async def test_encounter_claim_for_run_is_single_winner() -> None:
    repo = InMemoryCharacterEncounterRepository()
    enc = _planned_encounter()
    await repo.save(enc)
    now = datetime(2026, 4, 18, 12, 5, tzinfo=UTC)

    # First claim wins the planned→running transition.
    assert await repo.claim_for_run(enc.id, now=now) is True
    # Second claim loses — the row is no longer 'planned'.
    assert await repo.claim_for_run(enc.id, now=now) is False

    reloaded = await repo.get(enc.id)
    assert reloaded is not None
    assert reloaded.status == "running"
    assert reloaded.started_at is not None


@pytest.mark.asyncio
async def test_encounter_claim_for_run_missing_returns_false() -> None:
    repo = InMemoryCharacterEncounterRepository()
    now = datetime(2026, 4, 18, 12, 5, tzinfo=UTC)
    assert await repo.claim_for_run("nope", now=now) is False
