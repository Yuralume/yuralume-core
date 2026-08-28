"""The turn anchor: undo deletes rows it can *name*, not rows it can date.

Three subsystems used to answer "did this turn write this row?" with a
time window floored on ``turn_started_at``. All three of their writers
run in the background post-turn, which is a different clock from the
turn's, so all three were wrong — and wrong in ways a player produces by
accident.

What is pinned here:

* a promise from a turn whose post-turn was still queued when the *next*
  turn started survives that next turn's undo (it used to be
  hard-deleted, so the release reconciler could not rescue it either);
* an undo that lands before its own post-turn writes still stops the
  promise, because the TU2 tombstone gate refuses the write;
* undoing a turn in one conversation does not delete the encounter
  intent another conversation just agreed to (that table has no
  conversation column, so the old window was character-scoped);
* a journal that snapshots several open follow-up rows can put back the
  busy-defer row the turn cancelled, even when a newer scheduled promise
  exists — the case where the snapshot used to name the wrong row;
* rows written before the anchor column existed still behave the way
  they did, so a rolling deployment degrades rather than breaks.

Every test drives the real ``ChatService`` write points
(``_persist_message_promises`` / ``_persist_peer_meet_intents``) rather
than hand-building rows, because the defects were in the *pairing* of
what the writer stamps against what undo looks for — two hand-built
halves can agree with each other while both disagree with production.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.dto.chat import SendChatMessageRequest
from kokoro_link.application.services.turn_undo_service import TurnUndoService
from kokoro_link.application.services.undone_turn_gate import UndoneTurnGate
from kokoro_link.contracts.busy_reply_decider import (
    BusyDecision, BusyReplyMode,
)
from kokoro_link.domain.entities.character_encounter_intent import (
    CharacterEncounterIntent,
)
from kokoro_link.domain.entities.pending_follow_up import (
    PendingFollowUp, PendingFollowUpKind, PendingFollowUpStatus,
)
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.repositories.in_memory_character_encounter_intents import (
    InMemoryCharacterEncounterIntentRepository,
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
from kokoro_link.infrastructure.repositories.in_memory_undone_turns import (
    InMemoryUndoneTurnRepository,
)
from tests.unit.busy_defer_harness import (
    ScriptedDecider,
    StubScheduleService,
    build_chat_service,
    busy_activity,
)

pytestmark = pytest.mark.asyncio


class _Wiring:
    """Chat and undo over the *same* stores, plus the tombstone gate.

    The gate is wired here and not only in the tombstone suite because
    one of the directions below is closed by the gate rather than by a
    delete, and a test that omitted it would report a pass for the wrong
    reason.
    """

    def __init__(self, *, decider=None, schedule_service=None) -> None:
        self.characters = InMemoryCharacterRepository()
        self.conversations = InMemoryConversationRepository()
        self.memories = InMemoryMemoryRepository()
        self.journals = InMemoryTurnJournalRepository()
        self.follow_ups = InMemoryPendingFollowUpRepository()
        self.intents = InMemoryCharacterEncounterIntentRepository()
        self.tombstones = InMemoryUndoneTurnRepository()
        self.chat, self.character_service, _ = build_chat_service(
            decider=decider or ScriptedDecider([]),
            schedule_service=(
                schedule_service
                or StubScheduleService(current_activity=None)
            ),
            pending_repo=self.follow_ups,
            journal_repository=self.journals,
            character_repository=self.characters,
            conversation_repository=self.conversations,
            memory_repository=self.memories,
            encounter_intent_repository=self.intents,
        )
        self.chat.set_undone_turn_gate(UndoneTurnGate(self.tombstones))
        self.undo = TurnUndoService(
            journal_repository=self.journals,
            conversation_repository=self.conversations,
            character_repository=self.characters,
            memory_repository=self.memories,
            pending_follow_up_repository=self.follow_ups,
            encounter_intent_repository=self.intents,
            undone_turn_repository=self.tombstones,
        )

    async def create_character(self, name: str = "Airi"):
        return await self.character_service.create_character(
            CreateCharacterRequest(name=name, personality=[], interests=[]),
        )

    async def anchor_of_last_turn(self, conversation_id: str) -> str:
        journal = await self.journals.get_latest(conversation_id)
        assert journal is not None
        assert journal.turn_record_id, "a replying turn always mints one"
        return journal.turn_record_id


def _promise(hours_ahead: int = 8, intent: str = "叫使用者起床"):
    """What the post-turn extractor hands the promise writer."""
    return SimpleNamespace(
        scheduled_for_iso=(
            datetime.now(timezone.utc) + timedelta(hours=hours_ahead)
        ).isoformat(),
        intent=intent,
        source_text="明天早上叫我起床",
    )


def _meet_intent(peer_id: str, *, topic: str = "一起去看展"):
    """What the post-turn extractor hands the encounter-intent writer."""
    return SimpleNamespace(
        peer_character_id=peer_id,
        desired_after_iso=(
            datetime.now(timezone.utc) + timedelta(days=2)
        ).isoformat(),
        topic=topic,
        source_text="那我們週末去看展吧",
    )


def _defer_decision(activity) -> BusyDecision:
    return BusyDecision(
        mode=BusyReplyMode.BRIEF_DEFER,
        brief_reply="先回，等會議結束我再好好回你",
        defer_until=activity.end_at,
        defer_reason="會議中",
    )


# --------------------------------------------------------------------------- #
# defect one — the promise of an earlier turn, landing late
# --------------------------------------------------------------------------- #

async def test_a_previous_turns_late_promise_survives_this_turns_undo() -> None:
    """The headline race, in the order a player produces it.

    Turn A's reply promises to message at 22:00 and its post-turn goes to
    the background. The player types again before that write lands, so
    turn B's journal snapshots a queue that is still empty. A's promise
    row then appears — inside B's window, absent from B's snapshot — and
    the player undoes B.

    Under the old time-window delete that row was *hard*-deleted: the
    character's promise stayed on screen in turn A, the reminder never
    fired, and because it was a delete rather than a cancel the release
    reconciler had nothing to rescue.
    """
    wiring = _Wiring()
    created = await wiring.create_character()

    turn_a = await wiring.chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="明天早上叫我起床",
    ))
    anchor_a = await wiring.anchor_of_last_turn(turn_a.conversation_id)

    # The player types again while A's post-turn is still queued.
    await wiring.chat.send_message(SendChatMessageRequest(
        character_id=created.id,
        conversation_id=turn_a.conversation_id,
        message="對了 今天天氣如何",
    ))
    anchor_b = await wiring.anchor_of_last_turn(turn_a.conversation_id)
    assert anchor_b != anchor_a

    # Only now does A's background post-turn write its promise.
    await wiring.chat._persist_message_promises(
        character_id=created.id,
        conversation_id=turn_a.conversation_id,
        promises=[_promise()],
        turn_record_id=anchor_a,
    )
    rows = await wiring.follow_ups.list_open_for_character(created.id)
    assert len(rows) == 1
    promise_row = rows[0]
    assert promise_row.turn_record_id == anchor_a

    result = await wiring.undo.undo_last_turn(turn_a.conversation_id)

    assert result.deleted_follow_ups == 0
    survivor = await wiring.follow_ups.get(promise_row.id)
    assert survivor is not None
    assert survivor.status == PendingFollowUpStatus.QUEUED


async def test_undoing_the_turn_that_promised_still_takes_its_promise() -> None:
    """The guard on the test above: anchoring must not turn the delete
    off, only aim it. Same late write, but undo the turn that *made* the
    promise and the row goes."""
    wiring = _Wiring()
    created = await wiring.create_character()

    turn_a = await wiring.chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="明天早上叫我起床",
    ))
    anchor_a = await wiring.anchor_of_last_turn(turn_a.conversation_id)
    await wiring.chat._persist_message_promises(
        character_id=created.id,
        conversation_id=turn_a.conversation_id,
        promises=[_promise()],
        turn_record_id=anchor_a,
    )

    result = await wiring.undo.undo_last_turn(turn_a.conversation_id)

    assert result.deleted_follow_ups == 1
    assert await wiring.follow_ups.list_open_for_character(created.id) == []


async def test_an_undo_before_the_post_turn_lands_stops_the_promise() -> None:
    """The other direction of the same race, which no delete can close.

    Undo the turn while its post-turn is still upstream and there is
    nothing in the table yet to delete — a window finds nothing, an
    anchor finds nothing, and the promise would be written moments later
    and stay for ever. The TU2 tombstone is what closes it: the post-turn
    refuses to run at all once the turn is reversed.
    """
    wiring = _Wiring()
    created = await wiring.create_character()

    reply = await wiring.chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="明天早上叫我起床",
    ))
    anchor = await wiring.anchor_of_last_turn(reply.conversation_id)

    await wiring.undo.undo_last_turn(reply.conversation_id)
    assert await wiring.tombstones.is_undone(anchor) is True

    # The post-turn wakes up now, holding the reversed turn's id.
    character = await wiring.characters.get(created.id)
    outcome = await wiring.chat._do_post_turn(
        character=character,
        conversation_id=reply.conversation_id,
        turn_record_id=anchor,
        user_text="明天早上叫我起床",
        assistant_text="好，我明天叫你",
        prior_messages=[],
    )

    assert outcome.get("post_turn_skipped")
    assert await wiring.follow_ups.list_open_for_character(created.id) == []


# --------------------------------------------------------------------------- #
# defect two — encounter intents across conversations
# --------------------------------------------------------------------------- #

async def test_undo_in_one_conversation_spares_another_ones_meeting() -> None:
    """``character_encounter_intents`` has no conversation column, so the
    old delete was scoped to ``character_id`` and a timestamp. A character
    living in a web thread and a LINE thread at once — which
    ``recent_messages_for_character`` supports by design — had undo in one
    thread deleting the meeting the other thread had just agreed to, with
    nothing in that thread undone."""
    wiring = _Wiring()
    created = await wiring.create_character()

    web = await wiring.chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="今天過得怎樣",
    ))
    line = await wiring.chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="週末有空嗎",
    ))
    assert line.conversation_id != web.conversation_id
    line_anchor = await wiring.anchor_of_last_turn(line.conversation_id)

    # The other thread's post-turn agrees to a meeting.
    await wiring.chat._persist_peer_meet_intents(
        character_id=created.id,
        intents=[_meet_intent("peer-character-id")],
        turn_record_id=line_anchor,
    )
    agreed = await wiring.intents.list_pending_for_character(
        created.id, now=datetime.now(timezone.utc),
    )
    assert len(agreed) == 1

    result = await wiring.undo.undo_last_turn(web.conversation_id)

    assert result.deleted_encounter_intents == 0
    assert await wiring.intents.get(agreed[0].id) is not None


async def test_undo_in_the_agreeing_conversation_deletes_the_meeting() -> None:
    """Guard for the test above — the scoping must still delete."""
    wiring = _Wiring()
    created = await wiring.create_character()

    line = await wiring.chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="週末有空嗎",
    ))
    anchor = await wiring.anchor_of_last_turn(line.conversation_id)
    await wiring.chat._persist_peer_meet_intents(
        character_id=created.id,
        intents=[_meet_intent("peer-character-id")],
        turn_record_id=anchor,
    )
    agreed = await wiring.intents.list_pending_for_character(
        created.id, now=datetime.now(timezone.utc),
    )
    assert len(agreed) == 1

    result = await wiring.undo.undo_last_turn(line.conversation_id)

    assert result.deleted_encounter_intents == 1
    assert await wiring.intents.get(agreed[0].id) is None


async def test_an_anchorless_intent_is_left_alone(caplog) -> None:  # noqa: ANN001
    """Rolling-deployment behaviour, stated rather than discovered.

    An intent written by a build that had no anchor to stamp cannot be
    attributed to a turn, so undo leaves it. The cost is a stale
    appointment the character may mention; the alternative — falling back
    to the character-scoped window — is deleting another conversation's
    real agreement, which is strictly worse.
    """
    wiring = _Wiring()
    created = await wiring.create_character()

    reply = await wiring.chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="週末有空嗎",
    ))
    await wiring.chat._persist_peer_meet_intents(
        character_id=created.id,
        intents=[_meet_intent("peer-character-id")],
        turn_record_id="",  # what a pre-migration writer effectively had
    )
    legacy = await wiring.intents.list_pending_for_character(
        created.id, now=datetime.now(timezone.utc),
    )
    assert len(legacy) == 1
    assert legacy[0].turn_record_id is None

    result = await wiring.undo.undo_last_turn(reply.conversation_id)

    assert result.deleted_encounter_intents == 0
    assert await wiring.intents.get(legacy[0].id) is not None


# --------------------------------------------------------------------------- #
# defect three — the snapshot has to name every open row
# --------------------------------------------------------------------------- #

async def test_a_cancelled_defer_comes_back_even_behind_a_newer_promise() -> None:
    """The snapshot took one open row; the cancel takes a different one.

    ``_build_pre_turn_journal`` used ``find_open_for_conversation``, which
    returns the newest open row of any kind, while
    ``_cancel_existing_pending_follow_up_for_immediate_reply`` cancels the
    open **busy-defer** row. With a scheduled promise queued after the
    defer the two disagree: the snapshot names the promise (untouched, so
    restoring it is a no-op) and the cancelled defer is named by nothing —
    not by the snapshot, and not by the delete window either, since it was
    queued before the turn began. It stayed cancelled through the undo,
    and the reply the player was waiting for was gone for good.
    """
    activity = busy_activity()
    wiring = _Wiring(
        decider=ScriptedDecider([_defer_decision(activity)]),
        schedule_service=StubScheduleService(current_activity=activity),
    )
    created = await wiring.create_character()

    # Turn 1 defers: the busy-defer row the player is waiting on.
    first = await wiring.chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="晚餐想吃什麼",
    ))
    defer_rows = await wiring.follow_ups.list_open_for_conversation(
        first.conversation_id,
    )
    assert len(defer_rows) == 1
    defer_row = defer_rows[0]
    assert defer_row.kind == PendingFollowUpKind.BUSY_DEFER

    # An earlier turn's post-turn lands a promise *after* it — newer by
    # ``queued_at``, which is the only thing the old snapshot sorted on.
    await wiring.chat._persist_message_promises(
        character_id=created.id,
        conversation_id=first.conversation_id,
        promises=[_promise()],
        turn_record_id="turn-from-an-earlier-reply",
    )
    open_now = await wiring.follow_ups.list_open_for_conversation(
        first.conversation_id,
    )
    assert len(open_now) == 2
    assert open_now[-1].kind == PendingFollowUpKind.SCHEDULED_PROMISE

    # Turn 2 replies immediately, which cancels the defer row.
    await wiring.chat.send_message(SendChatMessageRequest(
        character_id=created.id,
        conversation_id=first.conversation_id,
        message="還在忙嗎",
    ))
    cancelled = await wiring.follow_ups.get(defer_row.id)
    assert cancelled is not None
    assert cancelled.status == PendingFollowUpStatus.CANCELLED

    result = await wiring.undo.undo_last_turn(first.conversation_id)

    assert result.restored_follow_ups == 1
    restored = await wiring.follow_ups.get(defer_row.id)
    assert restored is not None
    assert restored.status == PendingFollowUpStatus.QUEUED
    # And the promise that was merely standing nearby is untouched.
    promise = [
        r for r in await wiring.follow_ups.list_open_for_conversation(
            first.conversation_id,
        )
        if r.kind == PendingFollowUpKind.SCHEDULED_PROMISE
    ]
    assert len(promise) == 1
    assert promise[0].status == PendingFollowUpStatus.QUEUED


async def test_the_snapshot_round_trips_the_anchor_of_a_restored_row() -> None:
    """A restore writes the snapshot back verbatim. If the codec dropped
    ``turn_record_id`` the restored row would come back anchorless — and
    the next undo in that conversation would then claim it by time window,
    which is the bug this whole change removes."""
    activity = busy_activity()
    wiring = _Wiring(
        decider=ScriptedDecider([_defer_decision(activity)]),
        schedule_service=StubScheduleService(current_activity=activity),
    )
    created = await wiring.create_character()

    first = await wiring.chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="晚餐想吃什麼",
    ))
    await wiring.chat._persist_message_promises(
        character_id=created.id,
        conversation_id=first.conversation_id,
        promises=[_promise()],
        turn_record_id="turn-from-an-earlier-reply",
    )
    await wiring.chat.send_message(SendChatMessageRequest(
        character_id=created.id,
        conversation_id=first.conversation_id,
        message="還在忙嗎",
    ))

    await wiring.undo.undo_last_turn(first.conversation_id)

    promise = [
        r for r in await wiring.follow_ups.list_open_for_conversation(
            first.conversation_id,
        )
        if r.kind == PendingFollowUpKind.SCHEDULED_PROMISE
    ]
    assert len(promise) == 1
    assert promise[0].turn_record_id == "turn-from-an-earlier-reply"


# --------------------------------------------------------------------------- #
# rolling deployment — anchorless follow-up rows keep the old behaviour
# --------------------------------------------------------------------------- #

async def test_an_anchorless_promise_in_the_window_is_still_deleted() -> None:
    """A promise row written by a build that could not stamp an anchor is
    exactly what the time window still exists for. Skipping it would leak
    every promise written during the deploy window; the fallback keeps
    those rows behaving the way they did before."""
    wiring = _Wiring()
    created = await wiring.create_character()

    reply = await wiring.chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="明天早上叫我起床",
    ))
    legacy = PendingFollowUp.new_promise(
        character_id=created.id,
        conversation_id=reply.conversation_id,
        promise_intent="叫使用者起床",
        scheduled_for=datetime.now(timezone.utc) + timedelta(hours=8),
        source_message_content="明天早上叫我起床",
    )
    assert legacy.turn_record_id is None
    await wiring.follow_ups.add(legacy)

    result = await wiring.undo.undo_last_turn(reply.conversation_id)

    assert result.deleted_follow_ups == 1
    assert await wiring.follow_ups.get(legacy.id) is None


async def test_a_busy_defer_row_has_no_anchor_and_is_still_deleted() -> None:
    """The permanently anchorless case, not a legacy one: the busy-defer
    branch runs no post-turn and mints no turn record, so its journal has
    no anchor either. It is also written inline during the turn, which is
    the one case the window was always exact for — so the window has to
    keep covering it."""
    activity = busy_activity()
    wiring = _Wiring(
        decider=ScriptedDecider([_defer_decision(activity)]),
        schedule_service=StubScheduleService(current_activity=activity),
    )
    created = await wiring.create_character()

    reply = await wiring.chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="晚餐想吃什麼",
    ))
    rows = await wiring.follow_ups.list_open_for_conversation(
        reply.conversation_id,
    )
    assert len(rows) == 1 and rows[0].turn_record_id is None
    journal = await wiring.journals.get_latest(reply.conversation_id)
    assert journal is not None and journal.turn_record_id is None

    result = await wiring.undo.undo_last_turn(reply.conversation_id)

    assert result.deleted_follow_ups == 1
    assert await wiring.follow_ups.get(rows[0].id) is None


async def test_a_journal_without_an_anchor_deletes_no_intent() -> None:
    """The busy-defer journal, which is anchorless for ever.

    That branch runs no post-turn, so it can have recorded no meeting —
    and "no anchor" must therefore mean "delete nothing", never "delete
    everything this character has". Two guards say so, the step's and the
    repository's, because between them sits a query whose wildcard
    reading would empty the table for one character across every thread
    they are live in.
    """
    activity = busy_activity()
    wiring = _Wiring(
        decider=ScriptedDecider([_defer_decision(activity)]),
        schedule_service=StubScheduleService(current_activity=activity),
    )
    created = await wiring.create_character()
    await wiring.chat._persist_peer_meet_intents(
        character_id=created.id,
        intents=[_meet_intent("peer-character-id")],
        turn_record_id="turn-from-some-other-thread",
    )
    standing = await wiring.intents.list_pending_for_character(
        created.id, now=datetime.now(timezone.utc),
    )
    assert len(standing) == 1

    reply = await wiring.chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="晚餐想吃什麼",
    ))
    journal = await wiring.journals.get_latest(reply.conversation_id)
    assert journal is not None and journal.turn_record_id is None

    result = await wiring.undo.undo_last_turn(reply.conversation_id)

    assert result.deleted_encounter_intents == 0
    assert await wiring.intents.get(standing[0].id) is not None


async def test_an_empty_anchor_is_not_a_wildcard_at_the_repository() -> None:
    """The inner guard, on its own terms.

    ``delete_by_turn_record`` is one predicate away from meaning "every
    row this character owns": pass it a falsy anchor and a naive
    implementation deletes the anchorless rows a rolling deployment left
    behind, across every conversation at once."""
    repo = InMemoryCharacterEncounterIntentRepository()
    anchorless = CharacterEncounterIntent.create(
        character_id="char-a", peer_character_id="char-b",
        desired_after=datetime.now(timezone.utc) + timedelta(days=1),
        topic="部署前就約好的",
    )
    await repo.add(anchorless)

    assert await repo.delete_by_turn_record("char-a", "") == 0
    assert await repo.get(anchorless.id) is not None
