"""TU4 — undo puts the deferred-reply queue back where the turn found it.

Four things a turn can do to that queue, one test each:

* opened a busy-defer row → undo deletes it, and withdraws the release
  job that would have fired it
* opened a scheduled-promise row (post-turn extraction) → same
* merged into a row that was already open → undo restores the pre-merge
  row instead of deleting it, so the *earlier* turn's queued message
  survives
* cancelled a row that was already open → undo brings it back queued, so
  the reply the player was waiting for does not vanish

Plus the negative that keeps the other four honest: an ordinary turn
that touched nothing must leave an unrelated open row exactly as it was.

The first two directions run end-to-end through a real ``ChatService``
(shared harness in ``busy_defer_harness``) because the thing under test
is a *pairing* — what the chat path writes against what the journal
recorded — and a hand-built journal could agree with a hand-built row
while both disagreed with production. The merge case cannot be reached
that way (a normal turn cancels the open row before the upsert can merge
into it; the merge survives only as a race fallback), so it drives the
real ``_upsert_pending_follow_up`` over a hand-written journal instead —
the same merge behaviour pinned in
``test_chat_service_busy_defer.test_upsert_merges_into_the_open_row_instead_of_opening_a_second``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.dto.chat import SendChatMessageRequest
from kokoro_link.application.services.chat_outcome_claim_auditor import (
    REPAIR_STATUS_COALESCED,
    REPAIR_STATUS_QUEUED,
    ChatOutcomeClaimAuditor,
)
from kokoro_link.application.services.outcome_claim_guard import (
    OutcomeClaimGuard,
)
from kokoro_link.application.services.pending_follow_up_release import (
    release_idempotency_keys,
)
from kokoro_link.contracts.outcome_claim import OutcomeClaimVerdict
from kokoro_link.application.services.turn_journal_snapshots import (
    follow_up_to_dict,
)
from kokoro_link.application.services.turn_undo_service import TurnUndoService
from kokoro_link.contracts.background_jobs import (
    BackgroundJobSpec, JobStatus,
)
from kokoro_link.contracts.busy_reply_decider import (
    BusyDecision, BusyReplyMode,
)
from kokoro_link.domain.entities.pending_follow_up import (
    PendingFollowUp,
    PendingFollowUpMessage,
    PendingFollowUpStatus,
)
from kokoro_link.domain.entities.turn_journal import TurnJournal
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryBackgroundJobQueue,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_pending_follow_ups import (
    InMemoryPendingFollowUpRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_turn_journals import (
    InMemoryTurnJournalRepository,
)
from tests.unit.busy_defer_harness import (
    ScriptedDecider,
    StubScheduleService,
    build_chat_service,
    busy_activity,
)


class _Wiring:
    """One conversation's worth of stores, shared by chat and by undo.

    Undo has to look at the very rows the turn wrote, so the two services
    are built over the same repository instances rather than over
    equivalent ones.
    """

    def __init__(self, *, decider, schedule_service, queue=None) -> None:
        self.characters = InMemoryCharacterRepository()
        self.conversations = InMemoryConversationRepository()
        self.memories = InMemoryMemoryRepository()
        self.journals = InMemoryTurnJournalRepository()
        self.follow_ups = InMemoryPendingFollowUpRepository()
        self.queue = queue
        self.chat, self.character_service, _ = build_chat_service(
            decider=decider,
            schedule_service=schedule_service,
            pending_repo=self.follow_ups,
            journal_repository=self.journals,
            character_repository=self.characters,
            conversation_repository=self.conversations,
            memory_repository=self.memories,
        )
        self.undo = TurnUndoService(
            journal_repository=self.journals,
            conversation_repository=self.conversations,
            character_repository=self.characters,
            memory_repository=self.memories,
            pending_follow_up_repository=self.follow_ups,
            follow_up_release_queue=queue,
        )

    async def create_character(self, name: str = "Airi"):
        return await self.character_service.create_character(
            CreateCharacterRequest(name=name, personality=[], interests=[]),
        )


class _DishonestJudge:
    """Always finds the reply overclaimed (HV4's write path under test)."""

    async def judge(self, **_kwargs) -> OutcomeClaimVerdict:
        return OutcomeClaimVerdict.blocked(("照片傳給你了",))


class _ScriptedDishonestJudge:
    """Finds a *different* lie each turn; the last verdict repeats.

    The coalesce path only writes when the merged intent actually differs
    from the row's, so pinning "undo rewinds a merge" needs two distinct
    claims — one judge answering the same thing twice would take the
    already-owed short-circuit and never exercise the CAS at all.
    """

    def __init__(self, *claims: str) -> None:
        self._claims = list(claims)

    async def judge(self, **_kwargs) -> OutcomeClaimVerdict:
        claim = self._claims[0]
        if len(self._claims) > 1:
            claim = self._claims.pop(0)
        return OutcomeClaimVerdict.blocked((claim,))


def _defer_decision(activity) -> BusyDecision:
    return BusyDecision(
        mode=BusyReplyMode.BRIEF_DEFER,
        brief_reply="先回，等會議結束我再好好回你",
        defer_until=activity.end_at,
        defer_reason="會議中",
    )


async def _enqueue_release_jobs(queue, row) -> list[str]:
    """Mint the release jobs hosted would have minted for ``row``."""
    job_ids: list[str] = []
    for key in release_idempotency_keys(row):
        job_id = await queue.enqueue(
            BackgroundJobSpec(
                kind="pending_follow_up_release",
                idempotency_key=key,
                due_at=row.scheduled_for,
                fencing_epoch=1,
                character_id=row.character_id,
                payload={"follow_up_id": row.id},
            ),
            now=datetime.now(timezone.utc),
        )
        assert job_id is not None
        job_ids.append(job_id)
    return job_ids


@pytest.mark.asyncio
async def test_undo_deletes_the_defer_row_the_turn_opened_and_its_job() -> None:
    """The headline failure: without this, the character comes back hours
    later and earnestly answers a message the player already took back."""
    activity = busy_activity()
    queue = InMemoryBackgroundJobQueue()
    wiring = _Wiring(
        decider=ScriptedDecider([_defer_decision(activity)]),
        schedule_service=StubScheduleService(current_activity=activity),
        queue=queue,
    )
    created = await wiring.create_character()

    reply = await wiring.chat.send_message(
        SendChatMessageRequest(character_id=created.id, message="晚餐想吃什麼"),
    )
    row = await wiring.follow_ups.find_open_for_conversation(
        reply.conversation_id,
    )
    assert row is not None
    job_ids = await _enqueue_release_jobs(queue, row)

    result = await wiring.undo.undo_last_turn(reply.conversation_id)

    assert result.deleted_follow_ups == 1
    assert result.restored_follow_ups == 0
    assert await wiring.follow_ups.get(row.id) is None
    # And the queue no longer advertises work for a row that is gone.
    assert result.cancelled_follow_up_jobs == len(job_ids)
    for job_id in job_ids:
        job = await queue.get(job_id)
        assert job is not None
        assert job.status == JobStatus.SUPERSEDED


@pytest.mark.asyncio
async def test_undo_deletes_the_scheduled_promise_the_turn_extracted() -> None:
    """Same direction, other write point: the promise row post-turn
    extraction wrote ("明天早上叫我起床") belongs to the reverted turn."""
    wiring = _Wiring(
        decider=ScriptedDecider([]),
        schedule_service=StubScheduleService(current_activity=None),
    )
    created = await wiring.create_character()

    reply = await wiring.chat.send_message(
        SendChatMessageRequest(character_id=created.id, message="明天早上叫我起床"),
    )
    # The post-turn promise extractor's write point, called with what the
    # extractor would have produced for this turn — under the very turn
    # record id the journal recorded, which is how the row is anchored.
    journal = await wiring.journals.get_latest(reply.conversation_id)
    assert journal is not None and journal.turn_record_id
    await wiring.chat._persist_message_promises(
        character_id=created.id,
        conversation_id=reply.conversation_id,
        promises=[SimpleNamespace(
            scheduled_for_iso=(
                datetime.now(timezone.utc) + timedelta(hours=8)
            ).isoformat(),
            intent="叫使用者起床",
            source_text="明天早上叫我起床",
        )],
        turn_record_id=journal.turn_record_id,
    )
    rows = await wiring.follow_ups.list_open_for_character(created.id)
    assert len(rows) == 1

    result = await wiring.undo.undo_last_turn(reply.conversation_id)

    assert result.deleted_follow_ups == 1
    assert await wiring.follow_ups.list_open_for_character(created.id) == []


@pytest.mark.asyncio
async def test_undo_resurrects_the_row_the_turn_cancelled() -> None:
    """The reverse direction. Turn 1 defers; turn 2 replies immediately,
    which cancels the queued row; undoing turn 2 has to give the player
    back the reply they were still waiting for."""
    activity = busy_activity()
    wiring = _Wiring(
        decider=ScriptedDecider([_defer_decision(activity)]),
        schedule_service=StubScheduleService(current_activity=activity),
    )
    created = await wiring.create_character()

    first = await wiring.chat.send_message(
        SendChatMessageRequest(character_id=created.id, message="晚餐想吃什麼"),
    )
    queued = await wiring.follow_ups.find_open_for_conversation(
        first.conversation_id,
    )
    assert queued is not None

    await wiring.chat.send_message(
        SendChatMessageRequest(
            character_id=created.id,
            conversation_id=first.conversation_id,
            message="還在忙嗎",
        ),
    )
    cancelled = await wiring.follow_ups.get(queued.id)
    assert cancelled is not None
    assert cancelled.status == PendingFollowUpStatus.CANCELLED

    result = await wiring.undo.undo_last_turn(first.conversation_id)

    assert result.restored_follow_ups == 1
    assert result.deleted_follow_ups == 0
    restored = await wiring.follow_ups.get(queued.id)
    assert restored is not None
    assert restored.status == PendingFollowUpStatus.QUEUED
    # The audit copy of the second message goes with the turn that wrote it.
    assert [m.content for m in restored.messages] == ["晚餐想吃什麼"]
    assert restored.scheduled_for == queued.scheduled_for


@pytest.mark.asyncio
async def test_undo_takes_back_the_hv4_repair_the_turn_earned() -> None:
    """HV4's repair row is the turn's, both directions.

    A dishonest chat reply owes a repair follow-up, and the character will
    come back minutes later to settle it. If the player takes the turn
    back first, the claim is no longer in the transcript — so the repair
    must go with it, and (the direction that is easy to miss) undo's
    *restore* pass must not put it back: the row is absent from the
    pre-turn snapshot precisely because the turn created it.

    Driven the same way this file drives the promise writer: a real turn
    for the journal and the anchor, then the production write point called
    with what the judge would have produced for it.
    """
    wiring = _Wiring(
        decider=ScriptedDecider([]),
        schedule_service=StubScheduleService(current_activity=None),
    )
    created = await wiring.create_character()

    reply = await wiring.chat.send_message(
        SendChatMessageRequest(character_id=created.id, message="幫我拍張照"),
    )
    journal = await wiring.journals.get_latest(reply.conversation_id)
    assert journal is not None and journal.turn_record_id

    character = await wiring.characters.get(created.id)
    assert character is not None
    auditor = ChatOutcomeClaimAuditor(
        guard=OutcomeClaimGuard(judge=_DishonestJudge()),
        pending_follow_up_repository=wiring.follow_ups,
    )
    audit = await auditor.audit(
        character=character,
        conversation_id=reply.conversation_id,
        turn_record_id=journal.turn_record_id,
        assistant_text="拍好囉，照片傳給你了！",
        offered_tools=("fake_image",),
    )
    assert audit.repaired
    assert len(await wiring.follow_ups.list_open_for_character(created.id)) == 1

    result = await wiring.undo.undo_last_turn(reply.conversation_id)

    assert result.deleted_follow_ups == 1
    assert result.restored_follow_ups == 0
    assert await wiring.follow_ups.get(audit.repair_follow_up_id) is None
    assert await wiring.follow_ups.list_open_for_character(created.id) == []


@pytest.mark.asyncio
async def test_undo_rewinds_a_merge_instead_of_deleting_the_row() -> None:
    """A merge is not the turn's row to delete.

    Deleting it would take the *previous* turn's queued message with it —
    the row is older than the turn being reverted, and only the appended
    message belongs to it.
    """
    activity = busy_activity()
    wiring = _Wiring(
        decider=ScriptedDecider([]),
        schedule_service=StubScheduleService(current_activity=activity),
    )
    created = await wiring.create_character()
    started_at = datetime.now(timezone.utc)
    existing = PendingFollowUp.new(
        character_id=created.id,
        conversation_id="conv-merge",
        first_message=PendingFollowUpMessage.new(
            content="第一則", queued_at=started_at - timedelta(minutes=5),
        ),
        brief_reply="先回，等會議結束",
        defer_reason="會議中",
        scheduled_for=activity.end_at,
        activity_id=activity.id,
        now=started_at - timedelta(minutes=5),
    )
    await wiring.follow_ups.add(existing)
    await wiring.journals.add(TurnJournal.new(
        conversation_id="conv-merge",
        character_id=created.id,
        turn_index=0,
        turn_started_at=started_at,
        prev_character_state={},
        prev_open_follow_ups=[follow_up_to_dict(existing)],
    ))

    # The real merge path (race fallback), driven directly.
    merged = await wiring.chat._upsert_pending_follow_up(
        character_id=created.id,
        conversation_id="conv-merge",
        user_message_text="第二則",
        decision=_defer_decision(activity),
        current_activity=activity,
        now=started_at,
    )
    assert len(merged.messages) == 2

    result = await wiring.undo.undo_last_turn("conv-merge")

    assert result.restored_follow_ups == 1
    assert result.deleted_follow_ups == 0
    rewound = await wiring.follow_ups.get(existing.id)
    assert rewound is not None
    assert [m.content for m in rewound.messages] == ["第一則"]
    assert rewound.status == PendingFollowUpStatus.QUEUED


@pytest.mark.asyncio
async def test_undo_leaves_an_untouched_open_row_alone() -> None:
    """The guard on the other four: an open row this turn never touched
    is neither deleted nor rewritten, and the result says so."""
    activity = busy_activity()
    wiring = _Wiring(
        decider=ScriptedDecider([]),
        schedule_service=StubScheduleService(current_activity=activity),
    )
    created = await wiring.create_character()
    started_at = datetime.now(timezone.utc)
    untouched = PendingFollowUp.new_promise(
        character_id=created.id,
        conversation_id="conv-quiet",
        promise_intent="叫使用者起床",
        scheduled_for=started_at + timedelta(hours=8),
        source_message_content="明天早上叫我起床",
        now=started_at - timedelta(minutes=5),
    )
    await wiring.follow_ups.add(untouched)
    await wiring.journals.add(TurnJournal.new(
        conversation_id="conv-quiet",
        character_id=created.id,
        turn_index=0,
        turn_started_at=started_at,
        prev_character_state={},
        prev_open_follow_ups=[follow_up_to_dict(untouched)],
    ))

    result = await wiring.undo.undo_last_turn("conv-quiet")

    assert result.deleted_follow_ups == 0
    assert result.restored_follow_ups == 0
    assert result.cancelled_follow_up_jobs == 0
    assert await wiring.follow_ups.get(untouched.id) == untouched


@pytest.mark.asyncio
async def test_undo_rewinds_a_repair_coalesced_into_an_earlier_turns_row(
) -> None:
    """F5's merge is reversible even though the row is not this turn's.

    Turn N lies and earns repair row R, anchored at N. Turn N+1 lies
    again and F5 folds the second claim into R rather than opening a
    second row — so R now owes a claim from a turn whose anchor it does
    not carry. Undoing N+1 must take that claim back, or the character
    apologises for a sentence the player has already erased.

    The anchor pass cannot do it (R is anchored at N, and deleting R
    outright would throw away turn N's still-visible lie). The restore
    pass does: N+1's journal snapshots *every* open row of the
    conversation, R included, as it stood before the merge — so writing
    the snapshot back rewinds the merge exactly the way it rewinds a
    busy-defer merge.
    """
    wiring = _Wiring(
        decider=ScriptedDecider([]),
        schedule_service=StubScheduleService(current_activity=None),
    )
    created = await wiring.create_character()
    auditor = ChatOutcomeClaimAuditor(
        guard=OutcomeClaimGuard(
            judge=_ScriptedDishonestJudge("照片傳給你了", "這張也畫好了"),
        ),
        pending_follow_up_repository=wiring.follow_ups,
    )

    async def _audit(conversation_id: str, text: str):
        journal = await wiring.journals.get_latest(conversation_id)
        assert journal is not None and journal.turn_record_id
        character = await wiring.characters.get(created.id)
        assert character is not None
        return await auditor.audit(
            character=character,
            conversation_id=conversation_id,
            turn_record_id=journal.turn_record_id,
            assistant_text=text,
            offered_tools=("fake_image",),
            # Exactly what ``_schedule_outcome_claim_audit`` passes: the
            # merge precondition is part of the shape under test, so
            # leaving it out would pin a wiring production does not have.
            turn_started_at=journal.turn_started_at,
        )

    # --- turn N: the lie that opens the row -----------------------------
    first = await wiring.chat.send_message(
        SendChatMessageRequest(character_id=created.id, message="幫我拍張照"),
    )
    conversation_id = first.conversation_id
    audit_n = await _audit(conversation_id, "拍好囉，照片傳給你了！")
    assert audit_n.repair_status == REPAIR_STATUS_QUEUED
    row_id = audit_n.repair_follow_up_id
    assert row_id is not None
    before_merge = await wiring.follow_ups.get(row_id)
    assert before_merge is not None

    # --- turn N+1: the lie that merges into it --------------------------
    await wiring.chat.send_message(
        SendChatMessageRequest(
            character_id=created.id,
            conversation_id=conversation_id,
            message="再拍一張",
        ),
    )
    audit_n1 = await _audit(conversation_id, "這張也畫好了，看看！")
    assert audit_n1.repair_status == REPAIR_STATUS_COALESCED
    assert audit_n1.repair_follow_up_id == row_id
    merged = await wiring.follow_ups.get(row_id)
    assert merged is not None
    assert "照片傳給你了" in merged.promise_intent
    assert "這張也畫好了" in merged.promise_intent

    # --- undo N+1 -------------------------------------------------------
    result = await wiring.undo.undo_last_turn(conversation_id)

    rewound = await wiring.follow_ups.get(row_id)
    assert rewound is not None, "turn N's own lie must still be owed"
    assert rewound.status == PendingFollowUpStatus.QUEUED
    assert "照片傳給你了" in rewound.promise_intent
    assert "這張也畫好了" not in rewound.promise_intent, (
        "the erased turn's claim is still owed — the character will "
        "apologise for a sentence the player took back"
    )
    assert rewound.promise_intent == before_merge.promise_intent
    assert rewound.scheduled_for == before_merge.scheduled_for
    assert result.deleted_follow_ups == 0
    assert result.restored_follow_ups == 1


@pytest.mark.asyncio
async def test_out_of_order_audits_never_merge_a_claim_undo_would_destroy(
) -> None:
    """B-3's real hole: two audits finishing in the wrong order.

    Both audits are fire-and-forget tasks behind a judge call with no
    bound, so turn N+1's can land before turn N's. Turn N+1's opens the
    repair row — anchored at N+1 — and turn N's slow audit then arrives
    and would merge into it. One undo of N+1 deletes that row by anchor,
    and turn N's lie loses its repair while the sentence is still on the
    player's screen: a D6 breach, and one merging introduced (before F5,
    the late audit simply opened its own row).

    So a merge target must predate the merging turn. Here it does not,
    the coalesce declines, and each turn keeps a row of its own.
    """
    wiring = _Wiring(
        decider=ScriptedDecider([]),
        schedule_service=StubScheduleService(current_activity=None),
    )
    created = await wiring.create_character()
    auditor = ChatOutcomeClaimAuditor(
        guard=OutcomeClaimGuard(
            judge=_ScriptedDishonestJudge("這張也畫好了", "照片傳給你了"),
        ),
        pending_follow_up_repository=wiring.follow_ups,
    )

    first = await wiring.chat.send_message(
        SendChatMessageRequest(character_id=created.id, message="幫我拍張照"),
    )
    conversation_id = first.conversation_id
    journal_n = await wiring.journals.get_latest(conversation_id)
    assert journal_n is not None and journal_n.turn_record_id

    # Turn N+1 runs while turn N's judge is still upstream.
    await wiring.chat.send_message(
        SendChatMessageRequest(
            character_id=created.id,
            conversation_id=conversation_id,
            message="再拍一張",
        ),
    )
    journal_n1 = await wiring.journals.get_latest(conversation_id)
    assert journal_n1 is not None and journal_n1.turn_record_id
    character = await wiring.characters.get(created.id)
    assert character is not None

    # N+1's verdict comes back first and opens the row...
    audit_n1 = await auditor.audit(
        character=character, conversation_id=conversation_id,
        turn_record_id=journal_n1.turn_record_id,
        assistant_text="這張也畫好了，看看！", offered_tools=("fake_image",),
        turn_started_at=journal_n1.turn_started_at,
    )
    # ...and only then does turn N's.
    audit_n = await auditor.audit(
        character=character, conversation_id=conversation_id,
        turn_record_id=journal_n.turn_record_id,
        assistant_text="拍好囉，照片傳給你了！", offered_tools=("fake_image",),
        turn_started_at=journal_n.turn_started_at,
    )

    assert audit_n1.repair_status == REPAIR_STATUS_QUEUED
    assert audit_n.repair_status == REPAIR_STATUS_QUEUED, (
        "merging into a row born after this turn started is one-way"
    )
    assert audit_n.repair_follow_up_id != audit_n1.repair_follow_up_id
    assert audit_n.repaired and audit_n1.repaired

    await wiring.undo.undo_last_turn(conversation_id)

    # N+1's row went with N+1. N's is untouched and still owed.
    assert await wiring.follow_ups.get(audit_n1.repair_follow_up_id) is None
    surviving = await wiring.follow_ups.get(audit_n.repair_follow_up_id)
    assert surviving is not None, (
        "turn N is still on screen — its caught lie is still owed (D6)"
    )
    assert surviving.status == PendingFollowUpStatus.QUEUED
    assert "照片傳給你了" in surviving.promise_intent
    assert "這張也畫好了" not in surviving.promise_intent
