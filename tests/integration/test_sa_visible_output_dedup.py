"""PostgreSQL integration for the P3-Dedup visible-output claims (§3.4/§6.2).

Proves the behaviours only a real database can: under genuine concurrency
(``asyncio.gather`` on real SA adapters) each visible per-character surface
that a reclaimed distributed job could race is produced at most once —

* (a) two proactive dispatches on the same (char, slot) → one SENT, one
  SLOT_TAKEN, the visible-message path invoked exactly once;
* (b) two feed-comment reply passes on the same slot → one composes;
* (c) two schedule upserts on the same (char, date) → exactly one row survives
  the UNIQUE constraint under concurrent upsert;
* (d) two memorialize passes → exactly one episodic memory written;
* (e) two encounter run-claims → exactly one wins ``planned → running``.

Requires Docker (testcontainers); skipped automatically when unavailable.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from kokoro_link.application.services.feed_comment_reply_service import (
    FeedCommentReplyService,
)
from kokoro_link.application.services.feed_comment_service import (
    FeedCommentService,
)
from kokoro_link.application.services.proactive_dispatcher import (
    ProactiveDispatcher,
)
from kokoro_link.application.services.schedule_memorializer import (
    ScheduleMemorializer,
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
from kokoro_link.domain.entities.character_encounter import CharacterEncounter
from kokoro_link.domain.entities.feed_comment import (
    LOCAL_COMMENTER_ID,
    FeedComment,
)
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.persistence.models import (
    BackgroundVisibleSlotRow,
    CharacterEncounterRow,
    DailyScheduleRow,
)
from kokoro_link.infrastructure.persistence.sa_character_encounter_repository import (
    SACharacterEncounterRepository,
)
from kokoro_link.infrastructure.persistence.sa_schedule_repository import (
    SAScheduleRepository,
)
from kokoro_link.infrastructure.persistence.sa_visible_slots import (
    SAVisibleSlotRepository,
)
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.proactive.heuristic_gate import (
    HeuristicProactiveGate,
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
from tests.unit._messaging_harness import build_messaging_harness, create_character


pytestmark = pytest.mark.asyncio

UTC = timezone.utc


# ----------------------------------------------------------------------
# (a) proactive dispatch race
# ----------------------------------------------------------------------


class _FixedDecider(ProactiveDeciderPort):
    def __init__(self, decision: ProactiveDecision) -> None:
        self._decision = decision
        self.calls: list[ProactiveContext] = []

    async def decide(self, context: ProactiveContext) -> ProactiveDecision:
        self.calls.append(context)
        return self._decision


async def test_two_proactive_dispatches_same_slot_send_once(
    session_factory,
) -> None:
    harness = build_messaging_harness()
    dto = await create_character(harness)
    character = await harness.character_repository.get(dto.id)
    character = character.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, aspirations=None, appearance=None,
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
            last_active_at=datetime.now(UTC) - timedelta(minutes=60),
        ),
        proactive_enabled=True,
        accepts_web_proactive=True,
    )
    await harness.character_repository.save(character)

    slots = SAVisibleSlotRepository(session_factory)
    decider = _FixedDecider(ProactiveDecision(True, "ok", "早安"))

    def _dispatcher() -> ProactiveDispatcher:
        return ProactiveDispatcher(
            character_repository=harness.character_repository,
            conversation_repository=harness.conversation_repository,
            account_repository=harness.account_repository,
            binding_repository=harness.binding_repository,
            attempt_repository=InMemoryProactiveAttemptRepository(),
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

    results = await asyncio.gather(
        _dispatcher().evaluate(
            character_id=character.id,
            trigger=ProactiveTrigger.TICK,
            logical_slot="bucket-1",
        ),
        _dispatcher().evaluate(
            character_id=character.id,
            trigger=ProactiveTrigger.TICK,
            logical_slot="bucket-1",
        ),
    )
    outcomes = sorted(str(r.outcome) for r in results)
    assert outcomes == [
        str(ProactiveOutcome.SENT), str(ProactiveOutcome.SLOT_TAKEN),
    ]
    # Exactly one visible-message path (decider → deliver) ran.
    assert len(decider.calls) == 1
    async with session_factory() as session:
        rows = int((await session.execute(
            select(func.count()).select_from(BackgroundVisibleSlotRow),
        )).scalar_one())
    assert rows == 1


# ----------------------------------------------------------------------
# (b) feed comment reply race
# ----------------------------------------------------------------------


class _ScriptedReplyComposer(FeedCommentReplyComposerPort):
    def __init__(self, text: str) -> None:
        self._text = text
        self.inputs: list[FeedCommentReplyInput] = []

    async def compose(
        self, payload: FeedCommentReplyInput,
    ) -> FeedCommentReplyOutput:
        self.inputs.append(payload)
        return FeedCommentReplyOutput(content_text=self._text)


async def test_two_feed_reply_passes_same_slot_compose_once(
    session_factory,
) -> None:
    from kokoro_link.domain.entities.character import Character

    char = replace(
        Character.create(
            name="Aiko", summary="", personality=[], interests=[],
            speaking_style="", boundaries=[],
            state=CharacterState(
                emotion="neutral", affection=50, fatigue=20, trust=50, energy=80,
            ),
            feed_daily_limit=3,
        ),
        id="aiko",
    )
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
    slots = SAVisibleSlotRepository(session_factory)

    def _service() -> FeedCommentReplyService:
        return FeedCommentReplyService(
            post_repository=posts, comment_repository=comments,
            comment_service=FeedCommentService(
                post_repository=posts, comment_repository=comments,
            ),
            composer=composer,
            visible_slot_port=slots,
        )

    results = await asyncio.gather(
        _service().tick(char, now=now, logical_slot="bucket-1"),
        _service().tick(char, now=now, logical_slot="bucket-1"),
    )
    landed = [r for r in results if r is not None]
    assert len(landed) == 1
    # Only the winning pass scanned + composed.
    assert len(composer.inputs) == 1


# ----------------------------------------------------------------------
# (c) schedule upsert race
# ----------------------------------------------------------------------


async def test_two_schedule_upserts_same_day_keep_one_row(
    session_factory,
) -> None:
    repo = SAScheduleRepository(session_factory)
    day = date(2026, 4, 18)

    def _schedule() -> DailySchedule:
        return DailySchedule.create(
            character_id="c1", date_=day,
            activities=[
                ScheduleActivity.create(
                    start_at=datetime(2026, 4, 18, 9, 0, tzinfo=UTC),
                    end_at=datetime(2026, 4, 18, 12, 0, tzinfo=UTC),
                    description="剪輯", category="work", busy_score=0.8,
                ),
            ],
        )

    # Two distinct schedule objects (fresh UUIDs) for the same civil day.
    await asyncio.gather(repo.save(_schedule()), repo.save(_schedule()))

    async with session_factory() as session:
        rows = int((await session.execute(
            select(func.count()).select_from(DailyScheduleRow).where(
                DailyScheduleRow.character_id == "c1",
                DailyScheduleRow.date == day.isoformat(),
            ),
        )).scalar_one())
    assert rows == 1


# ----------------------------------------------------------------------
# (d) memorialize race → one memory
# ----------------------------------------------------------------------


async def test_two_memorialize_passes_write_one_memory(session_factory) -> None:
    schedule_repo = SAScheduleRepository(session_factory)
    memories = InMemoryMemoryRepository()
    day = date(2026, 4, 18)
    schedule = DailySchedule.create(
        character_id="c1", date_=day,
        activities=[
            ScheduleActivity.create(
                start_at=datetime(2026, 4, 18, 9, 0, tzinfo=UTC),
                end_at=datetime(2026, 4, 18, 12, 0, tzinfo=UTC),
                description="剪輯", category="work", busy_score=0.8,
            ),
        ],
    )
    await schedule_repo.save(schedule)

    def _memorializer() -> ScheduleMemorializer:
        return ScheduleMemorializer(
            schedule_repository=schedule_repo,
            memory_repository=memories,
            local_tz=UTC,
        )

    now = datetime(2026, 4, 18, 20, 0, tzinfo=UTC)
    counts = await asyncio.gather(
        _memorializer().memorialize(character_id="c1", now=now),
        _memorializer().memorialize(character_id="c1", now=now),
    )
    assert sorted(counts) == [0, 1]
    written = await memories.query("c1", limit=10)
    assert len(written) == 1


# ----------------------------------------------------------------------
# (e) encounter run-claim race
# ----------------------------------------------------------------------


async def test_two_encounter_run_claims_one_wins(session_factory) -> None:
    repo = SACharacterEncounterRepository(session_factory)
    enc = CharacterEncounter.plan(
        relationship_id="rel-1",
        character_a_id="a",
        character_b_id="b",
        scheduled_for=datetime(2026, 4, 18, 12, 0, tzinfo=UTC),
        location="咖啡店",
        trigger_reason="約好了",
    )
    await repo.save(enc)
    now = datetime(2026, 4, 18, 12, 5, tzinfo=UTC)

    claims = await asyncio.gather(
        repo.claim_for_run(enc.id, now=now),
        repo.claim_for_run(enc.id, now=now),
    )
    assert sorted(claims) == [False, True]

    async with session_factory() as session:
        row = await session.get(CharacterEncounterRow, enc.id)
        assert row is not None
        assert row.status == "running"
        running_rows = int((await session.execute(
            select(func.count()).select_from(CharacterEncounterRow).where(
                CharacterEncounterRow.status == "running",
            ),
        )).scalar_one())
    assert running_rows == 1
