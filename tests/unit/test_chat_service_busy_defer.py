"""Integration tests for the busy-defer branch in :class:`ChatService`.

Exercises the happy paths the user-facing flow promises:

* When the decider says ``BRIEF_DEFER``, the user sees the brief reply
  inline and a ``PendingFollowUp`` row appears in the repository.
* A second user message in the same conversation is appended for audit,
  then cancels the row so the normal chat path replies immediately.
* When the decider says ``IMMEDIATE``, the chat path runs normally
  (no pending row written).
* When the current activity is below the perf floor, the decider is
  not even invoked.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.dto.chat import SendChatMessageRequest
from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.contracts.busy_reply_decider import (
    BusyDecision,
    BusyReplyMode,
)
from kokoro_link.domain.entities.pending_follow_up import (
    PendingFollowUp,
    PendingFollowUpMessage,
    PendingFollowUpStatus,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.entities.player_persona_note import PlayerPersonaNote
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.busy.llm_decider import (
    _build_prompt as build_busy_decider_prompt,
)
from kokoro_link.infrastructure.prompt.player_persona_note_lines import (
    PLAYER_PERSONA_NOTE_HEADER,
)
from kokoro_link.infrastructure.repositories.in_memory_pending_follow_ups import (
    InMemoryPendingFollowUpRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_player_persona_notes import (
    InMemoryPlayerPersonaNoteRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_proactive_attempts import (
    InMemoryProactiveAttemptRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_turn_journals import (
    InMemoryTurnJournalRepository,
)
from tests.unit.busy_defer_harness import (
    ScriptedDecider,
    SpyReleaseEnqueuer,
    StubOperatorPersonaService,
    StubOperatorProfileService,
    StubPersonaExtractionService,
    StubRelationshipSeedRepository,
    StubScheduleService,
    build_chat_service,
    busy_activity,
)



@pytest.mark.asyncio
async def test_brief_defer_persists_inline_reply_and_pending_row() -> None:
    activity = busy_activity()
    decider = ScriptedDecider([
        BusyDecision(
            mode=BusyReplyMode.BRIEF_DEFER,
            brief_reply="先回，等會議結束我再好好回你",
            defer_until=activity.end_at,
            defer_reason="會議中",
        ),
    ])
    pending_repo = InMemoryPendingFollowUpRepository()
    schedule = StubScheduleService(current_activity=activity)
    chat, character_service, conversation_repository = build_chat_service(
        decider=decider, schedule_service=schedule, pending_repo=pending_repo,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(
            name="Airi", personality=["責任感重"], interests=[],
        ),
    )
    reply = await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id,
            message="晚餐想吃什麼",
        ),
    )

    assert reply.assistant_message.content == "先回，等會議結束我再好好回你"
    # Conversation now has exactly user + assistant
    conv = await conversation_repository.get(reply.conversation_id)
    assert conv is not None
    assert len(conv.messages) == 2
    # Pending row created
    open_row = await pending_repo.find_open_for_conversation(reply.conversation_id)
    assert open_row is not None
    assert open_row.status == PendingFollowUpStatus.QUEUED
    assert open_row.messages[0].content == "晚餐想吃什麼"
    assert open_row.scheduled_for == activity.end_at


@pytest.mark.asyncio
async def test_busy_decider_receives_owner_timezone() -> None:
    activity = busy_activity()
    decider = ScriptedDecider([
        BusyDecision(
            mode=BusyReplyMode.BRIEF_DEFER,
            brief_reply="先回，晚點找你",
            defer_until=activity.end_at,
        ),
    ])
    pending_repo = InMemoryPendingFollowUpRepository()
    chat, character_service, _ = build_chat_service(
        decider=decider,
        schedule_service=StubScheduleService(current_activity=activity),
        pending_repo=pending_repo,
        operator_profile_service=StubOperatorProfileService(),
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=[], interests=[]),
    )

    await chat.send_message(
        SendChatMessageRequest(character_id=created.id, message="你在忙嗎"),
    )

    assert decider.calls
    local_tz = decider.calls[0]["local_tz"]
    assert local_tz is not None
    assert datetime(2026, 6, 14, 16, 30, tzinfo=timezone.utc).astimezone(
        local_tz,
    ) == datetime(2026, 6, 15, 0, 30, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.mark.asyncio
async def test_busy_decider_keeps_seed_relationship_above_low_interaction_band() -> None:
    activity = busy_activity()
    decider = ScriptedDecider([BusyDecision()])
    pending_repo = InMemoryPendingFollowUpRepository()
    chat, character_service, _ = build_chat_service(
        decider=decider,
        schedule_service=StubScheduleService(current_activity=activity),
        pending_repo=pending_repo,
        operator_profile_service=StubOperatorProfileService(),
        operator_persona_service=StubOperatorPersonaService(),
        relationship_seed_repository=StubRelationshipSeedRepository(),
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=[], interests=[]),
    )

    await chat.send_message(
        SendChatMessageRequest(character_id=created.id, message="等等有空嗎"),
    )

    relationship_lines = "\n".join(decider.calls[0]["relationship_context_lines"])
    interaction_lines = "\n".join(decider.calls[0]["interaction_context_lines"])
    assert "關係：老朋友" in relationship_lines
    assert "互動量還很少" in interaction_lines
    assert "起始關係設定是關係主述" in interaction_lines
    assert "破冰期" not in interaction_lines
    assert "全新" not in interaction_lines
    assert "剛認識" not in interaction_lines


@pytest.mark.asyncio
async def test_busy_decider_receives_recent_proactive_outreach() -> None:
    """The character's own just-sent proactive push is threaded into the
    busy decider so it can tell "the user is replying to outreach I just
    initiated" from "an unsolicited interruption mid-focus" — the bug
    where replying to a proactive ping got a busy brush-off."""
    activity = busy_activity()
    # IMMEDIATE: we only assert what the decider was *given*, not its call.
    decider = ScriptedDecider([BusyDecision()])
    pending_repo = InMemoryPendingFollowUpRepository()
    proactive_repo = InMemoryProactiveAttemptRepository()
    chat, character_service, _ = build_chat_service(
        decider=decider,
        schedule_service=StubScheduleService(current_activity=activity),
        pending_repo=pending_repo,
        proactive_attempt_repository=proactive_repo,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=[], interests=[]),
    )
    await proactive_repo.add(
        ProactiveAttempt.record(
            character_id=created.id,
            trigger=ProactiveTrigger.TICK,
            outcome=ProactiveOutcome.SENT,
            message="在開會但突然好想你",
            now=datetime.now(timezone.utc) - timedelta(minutes=4),
        ),
    )

    await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id, message="我也想你，會議加油",
        ),
    )

    assert decider.calls
    attempts = decider.calls[0]["recent_proactive_attempts"]
    assert [a.message for a in attempts] == ["在開會但突然好想你"]


@pytest.mark.asyncio
async def test_brief_defer_records_journal_and_runs_persona_extraction() -> None:
    activity = busy_activity()
    decider = ScriptedDecider([
        BusyDecision(
            mode=BusyReplyMode.BRIEF_DEFER,
            brief_reply="先回，等等找你",
            defer_until=activity.end_at,
            defer_reason="會議中",
        ),
    ])
    pending_repo = InMemoryPendingFollowUpRepository()
    journal_repo = InMemoryTurnJournalRepository()
    persona = StubPersonaExtractionService()
    chat, character_service, _conversation_repository = build_chat_service(
        decider=decider,
        schedule_service=StubScheduleService(current_activity=activity),
        pending_repo=pending_repo,
        persona_extraction_service=persona,
        journal_repository=journal_repo,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=[], interests=[]),
    )

    reply = await chat.send_message(
        SendChatMessageRequest(character_id=created.id, message="我是工程師"),
    )

    assert persona.calls
    assert persona.calls[0]["character_id"] == created.id
    assert persona.calls[0]["user_text"] == "我是工程師"
    assert await journal_repo.get_latest(reply.conversation_id) is not None


@pytest.mark.asyncio
async def test_brief_defer_respects_persona_disabled_flag() -> None:
    activity = busy_activity()
    decider = ScriptedDecider([
        BusyDecision(
            mode=BusyReplyMode.BRIEF_DEFER,
            brief_reply="先回",
            defer_until=activity.end_at,
            defer_reason="會議中",
        ),
    ])
    pending_repo = InMemoryPendingFollowUpRepository()
    persona = StubPersonaExtractionService()
    chat, character_service, _ = build_chat_service(
        decider=decider,
        schedule_service=StubScheduleService(current_activity=activity),
        pending_repo=pending_repo,
        persona_extraction_service=persona,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=[], interests=[]),
    )

    await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id,
            message="我是工程師",
            operator_persona_enabled=False,
        ),
    )

    assert persona.calls == []


@pytest.mark.asyncio
async def test_second_message_is_appended_before_pending_row_cancels() -> None:
    activity = busy_activity()
    decider = ScriptedDecider([
        BusyDecision(
            mode=BusyReplyMode.BRIEF_DEFER,
            brief_reply="先回，等等",
            defer_until=activity.end_at,
            defer_reason="會議中",
        ),
        BusyDecision(
            mode=BusyReplyMode.BRIEF_DEFER,
            brief_reply="嗯嗯收到",
            defer_until=activity.end_at,
            defer_reason="會議中",
        ),
    ])
    pending_repo = InMemoryPendingFollowUpRepository()
    schedule = StubScheduleService(current_activity=activity)
    chat, character_service, _ = build_chat_service(
        decider=decider, schedule_service=schedule, pending_repo=pending_repo,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=[], interests=[]),
    )
    first = await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id, message="晚餐想吃什麼",
        ),
    )
    pending = await pending_repo.find_open_for_conversation(first.conversation_id)
    assert pending is not None
    await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id,
            conversation_id=first.conversation_id,
            message="不然吃義大利麵好了",
        ),
    )

    open_rows = await pending_repo.list_open_for_character(created.id)
    assert open_rows == []
    merged = await pending_repo.get(pending.id)
    assert merged is not None
    assert merged.status == PendingFollowUpStatus.CANCELLED
    assert len(merged.messages) == 2
    assert merged.messages[0].content == "晚餐想吃什麼"
    assert merged.messages[1].content == "不然吃義大利麵好了"
    # scheduled_for preserved for audit even though normal reply takes over.
    assert merged.scheduled_for == activity.end_at


@pytest.mark.asyncio
async def test_existing_pending_follow_up_is_cancelled_and_next_turn_replies() -> None:
    activity = busy_activity()
    decider = ScriptedDecider([
        BusyDecision(
            mode=BusyReplyMode.BRIEF_DEFER,
            brief_reply="先回，等等",
            defer_until=activity.end_at,
            defer_reason="會議中",
        ),
        BusyDecision(mode=BusyReplyMode.IMMEDIATE),
    ])
    pending_repo = InMemoryPendingFollowUpRepository()
    schedule = StubScheduleService(current_activity=activity)
    chat, character_service, conv_repo = build_chat_service(
        decider=decider, schedule_service=schedule, pending_repo=pending_repo,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=[], interests=[]),
    )
    first = await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id, message="晚餐想吃什麼",
        ),
    )
    pending = await pending_repo.find_open_for_conversation(first.conversation_id)
    assert pending is not None

    second = await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id,
            conversation_id=first.conversation_id,
            message="不然吃義大利麵好了",
        ),
    )

    assert second.assistant_message is not None
    assert second.assistant_message.content
    assert len(decider.calls) == 1
    open_rows = await pending_repo.list_open_for_character(created.id)
    assert open_rows == []
    merged = await pending_repo.get(pending.id)
    assert merged is not None
    assert merged.status == PendingFollowUpStatus.CANCELLED
    assert [m.content for m in merged.messages] == [
        "晚餐想吃什麼",
        "不然吃義大利麵好了",
    ]
    conversation = await conv_repo.get(first.conversation_id)
    assert conversation is not None
    assert [m.role.value for m in conversation.messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert conversation.messages[-2].content == "不然吃義大利麵好了"


@pytest.mark.asyncio
async def test_existing_pending_follow_up_prevents_consecutive_defer() -> None:
    activity = busy_activity()
    decider = ScriptedDecider([
        BusyDecision(
            mode=BusyReplyMode.BRIEF_DEFER,
            brief_reply="先回，等等",
            defer_until=activity.end_at,
            defer_reason="會議中",
        ),
        BusyDecision(
            mode=BusyReplyMode.BRIEF_DEFER,
            brief_reply="又延後",
            defer_until=activity.end_at,
            defer_reason="會議中",
        ),
    ])
    pending_repo = InMemoryPendingFollowUpRepository()
    schedule = StubScheduleService(current_activity=activity)
    chat, character_service, _ = build_chat_service(
        decider=decider, schedule_service=schedule, pending_repo=pending_repo,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=[], interests=[]),
    )
    first = await chat.send_message(
        SendChatMessageRequest(character_id=created.id, message="晚餐想吃什麼"),
    )

    second = await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id,
            conversation_id=first.conversation_id,
            message="你真的有看到嗎",
        ),
    )

    assert second.assistant_message is not None
    assert second.assistant_message.content != "又延後"
    assert len(decider.calls) == 1


@pytest.mark.asyncio
async def test_immediate_decision_runs_normal_path() -> None:
    activity = busy_activity()
    decider = ScriptedDecider([BusyDecision()])  # immediate
    pending_repo = InMemoryPendingFollowUpRepository()
    schedule = StubScheduleService(current_activity=activity)
    chat, character_service, conv_repo = build_chat_service(
        decider=decider, schedule_service=schedule, pending_repo=pending_repo,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=[], interests=[]),
    )
    reply = await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id, message="你在嗎",
        ),
    )

    # No defer → fake provider's reply, not the brief
    assert reply.assistant_message.content != "先回"
    open_rows = await pending_repo.list_open_for_character(created.id)
    assert open_rows == []


@pytest.mark.asyncio
async def test_low_busy_activity_skips_decider_invocation() -> None:
    """Perf gate: skip the LLM call when patently idle."""
    activity = busy_activity(busy=0.3)  # below the floor
    decider = ScriptedDecider([
        # If the decider WERE invoked, this would defer — but we expect
        # the perf floor to skip the call entirely.
        BusyDecision(
            mode=BusyReplyMode.BRIEF_DEFER,
            brief_reply="should not fire",
            defer_until=activity.end_at,
        ),
    ])
    pending_repo = InMemoryPendingFollowUpRepository()
    schedule = StubScheduleService(current_activity=activity)
    chat, character_service, _ = build_chat_service(
        decider=decider, schedule_service=schedule, pending_repo=pending_repo,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=[], interests=[]),
    )
    await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id, message="你在嗎",
        ),
    )

    assert decider.calls == []  # never invoked
    open_rows = await pending_repo.list_open_for_character(created.id)
    assert open_rows == []


@pytest.mark.asyncio
async def test_no_schedule_skips_decider() -> None:
    """No current activity → no busy context to defer on."""
    decider = ScriptedDecider([
        BusyDecision(
            mode=BusyReplyMode.BRIEF_DEFER,
            brief_reply="should not fire",
            defer_until=datetime.now(timezone.utc) + timedelta(minutes=10),
        ),
    ])
    pending_repo = InMemoryPendingFollowUpRepository()
    schedule = StubScheduleService(current_activity=None)
    chat, character_service, _ = build_chat_service(
        decider=decider, schedule_service=schedule, pending_repo=pending_repo,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=[], interests=[]),
    )
    await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id, message="你在嗎",
        ),
    )
    assert decider.calls == []


@pytest.mark.asyncio
async def test_brief_defer_enqueues_event_driven_release_job() -> None:
    activity = busy_activity()
    decider = ScriptedDecider([
        BusyDecision(
            mode=BusyReplyMode.BRIEF_DEFER,
            brief_reply="先回，等會議結束我再好好回你",
            defer_until=activity.end_at,
            defer_reason="會議中",
        ),
    ])
    pending_repo = InMemoryPendingFollowUpRepository()
    enqueuer = SpyReleaseEnqueuer()
    chat, character_service, conversation_repository = build_chat_service(
        decider=decider,
        schedule_service=StubScheduleService(current_activity=activity),
        pending_repo=pending_repo,
        release_enqueuer=enqueuer,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=["責任感重"], interests=[]),
    )

    reply = await chat.send_message(
        SendChatMessageRequest(character_id=created.id, message="晚餐想吃什麼"),
    )

    # The new pending row was handed to the distributed enqueuer exactly once.
    open_row = await pending_repo.find_open_for_conversation(reply.conversation_id)
    assert open_row is not None
    assert [r.id for r in enqueuer.rows] == [open_row.id]
    assert enqueuer.rows[0].scheduled_for == activity.end_at


@pytest.mark.asyncio
async def test_embedded_path_writes_row_without_enqueuer() -> None:
    # No enqueuer wired (self-host / embedded): the row is still persisted and the
    # turn succeeds — zero path difference from before Phase 5.
    activity = busy_activity()
    decider = ScriptedDecider([
        BusyDecision(
            mode=BusyReplyMode.BRIEF_DEFER,
            brief_reply="先回，晚點找你",
            defer_until=activity.end_at,
            defer_reason="會議中",
        ),
    ])
    pending_repo = InMemoryPendingFollowUpRepository()
    chat, character_service, conversation_repository = build_chat_service(
        decider=decider,
        schedule_service=StubScheduleService(current_activity=activity),
        pending_repo=pending_repo,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=[], interests=[]),
    )

    reply = await chat.send_message(
        SendChatMessageRequest(character_id=created.id, message="晚餐想吃什麼"),
    )

    open_row = await pending_repo.find_open_for_conversation(reply.conversation_id)
    assert open_row is not None
    assert open_row.status == PendingFollowUpStatus.QUEUED


@pytest.mark.asyncio
async def test_scheduled_promise_write_point_enqueues_release_job() -> None:
    # The second write point (_persist_message_promises) also enqueues the
    # event-driven release for the promised instant.
    pending_repo = InMemoryPendingFollowUpRepository()
    enqueuer = SpyReleaseEnqueuer()
    chat, character_service, _ = build_chat_service(
        decider=ScriptedDecider([]),
        schedule_service=StubScheduleService(current_activity=None),
        pending_repo=pending_repo,
        release_enqueuer=enqueuer,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=[], interests=[]),
    )
    scheduled = datetime.now(timezone.utc) + timedelta(hours=6)
    promise = SimpleNamespace(
        scheduled_for_iso=scheduled.isoformat(),
        intent="叫使用者起床",
        source_text="明天早上叫我起床",
    )

    await chat._persist_message_promises(
        character_id=created.id,
        conversation_id="conv-promise",
        promises=[promise],
        turn_record_id="turn-promise",
    )

    assert len(enqueuer.rows) == 1
    row = enqueuer.rows[0]
    assert row.is_scheduled_promise is True
    assert row.promise_intent == "叫使用者起床"


# ── the *decision* is player-facing too (計畫 §3.2) ────────────────────


_PLAYER_NOTE = "我是隱瞞身分的超能力者，白天在同一間事務所上班。"


class _FixedOperatorProfileService:
    """Always the same owner, whatever ``user_id`` the character carries.

    The note is stored per ``(character_id, operator_id)``, so the test
    needs the id the chat path will resolve — pinning it here is simpler
    than reaching into the character's default ``user_id``.
    """

    OPERATOR_ID = "owner-1"

    async def get_current(self) -> OperatorProfile:
        return OperatorProfile(
            id=self.OPERATOR_ID, display_name="Alex", timezone_id="Asia/Taipei",
        )

    async def get_for_user(self, user_id: str) -> OperatorProfile:
        return await self.get_current()


async def _run_busy_turn(
    *,
    note: str | None,
    operator_persona_enabled: bool = True,
) -> ScriptedDecider:
    """One busy turn that reaches the decider; returns what it was given."""
    activity = busy_activity()
    decider = ScriptedDecider([BusyDecision()])  # IMMEDIATE — prompt only
    notes = InMemoryPlayerPersonaNoteRepository()
    chat, character_service, _ = build_chat_service(
        decider=decider,
        schedule_service=StubScheduleService(current_activity=activity),
        pending_repo=InMemoryPendingFollowUpRepository(),
        operator_profile_service=_FixedOperatorProfileService(),
        player_persona_note_repository=notes,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=[], interests=[]),
    )
    if note:
        await notes.upsert(
            PlayerPersonaNote.create(
                character_id=created.id,
                operator_id=_FixedOperatorProfileService.OPERATOR_ID,
                note=note,
            ),
        )

    await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id,
            message="在忙嗎？",
            operator_persona_enabled=operator_persona_enabled,
        ),
    )

    assert decider.calls, "the decider was never consulted"
    return decider


def _decider_prompt(decider: ScriptedDecider) -> str:
    """The prompt the decider would actually build from what it was handed.

    Asserting on the rendered prompt rather than on the argument tuple is
    the point: the argument is only a leak-free way in if the renderer
    still puts it in front of the model.
    """
    call = decider.calls[0]
    return build_busy_decider_prompt(
        character=_bare_character(),
        user_message=call["user_message"],
        current_activity=call["current_activity"],
        recent_dialogue_summary=None,
        recent_proactive_attempts=(),
        relationship_context_lines=tuple(call["relationship_context_lines"]),
        interaction_context_lines=tuple(call["interaction_context_lines"]),
        now=datetime.now(timezone.utc),
        local_tz=timezone.utc,
    )


def _bare_character():
    from kokoro_link.domain.entities.character import Character
    from kokoro_link.domain.value_objects.character_state import CharacterState

    return Character.create(
        name="Airi",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="平靜", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


@pytest.mark.asyncio
async def test_busy_decision_prompt_carries_the_player_declaration() -> None:
    """§3.2 promised the note to the busy-defer *decision*, not only the
    compose that follows it.

    The decision is the one that picks the ack the player reads, and the
    one that judges whether this player is worth interrupting an activity
    for. Making that call without the player's declared identity judges a
    stranger — and left the two halves of the same feature reading two
    different players.
    """
    decider = await _run_busy_turn(note=_PLAYER_NOTE)

    prompt = _decider_prompt(decider)
    assert PLAYER_PERSONA_NOTE_HEADER in prompt
    assert _PLAYER_NOTE in prompt


@pytest.mark.asyncio
async def test_busy_decision_prompt_is_traceless_without_a_declaration() -> None:
    decider = await _run_busy_turn(note=None)

    prompt = _decider_prompt(decider)
    assert PLAYER_PERSONA_NOTE_HEADER not in prompt


@pytest.mark.asyncio
async def test_busy_decision_prompt_obeys_the_operator_persona_gate() -> None:
    """Same flag, same answer as the main chat prompt.

    A turn typed by someone who is not the account owner must not have the
    owner's declared setting staged as the current speaker's — the decision
    prompt is no more exempt from that than the reply prompt is.
    """
    decider = await _run_busy_turn(
        note=_PLAYER_NOTE, operator_persona_enabled=False,
    )

    prompt = _decider_prompt(decider)
    assert PLAYER_PERSONA_NOTE_HEADER not in prompt
    assert _PLAYER_NOTE not in prompt


@pytest.mark.asyncio
async def test_upsert_merges_into_the_open_row_instead_of_opening_a_second(
) -> None:
    """Characterization pin for the upsert-merge branch (TU4 pre-work).

    Reached directly rather than through ``send_message``: a normal turn
    cancels the open row before it ever gets here, so the merge is a
    race-condition fallback (another request opened a row between that
    check and this upsert). Undo has to reverse it correctly all the
    same, and reversing it correctly means knowing exactly what it does —
    hence this pin, written before the rollback step that depends on it.

    What it does: keeps the row (id, schedule, ack, reason, activity all
    untouched), appends the new message after the existing ones, moves
    ``updated_at``, and mints no second release job — the existing job
    already covers this id at this instant.
    """
    activity = busy_activity()
    pending_repo = InMemoryPendingFollowUpRepository()
    enqueuer = SpyReleaseEnqueuer()
    chat, character_service, _ = build_chat_service(
        decider=ScriptedDecider([]),
        schedule_service=StubScheduleService(current_activity=activity),
        pending_repo=pending_repo,
        release_enqueuer=enqueuer,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=[], interests=[]),
    )
    first_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    existing = PendingFollowUp.new(
        character_id=created.id,
        conversation_id="conv-merge",
        first_message=PendingFollowUpMessage.new(
            content="第一則", queued_at=first_at,
        ),
        brief_reply="先回，等會議結束",
        defer_reason="會議中",
        scheduled_for=activity.end_at,
        activity_id=activity.id,
        now=first_at,
    )
    await pending_repo.add(existing)

    merged_at = datetime.now(timezone.utc)
    merged = await chat._upsert_pending_follow_up(
        character_id=created.id,
        conversation_id="conv-merge",
        user_message_text="第二則",
        decision=BusyDecision(
            mode=BusyReplyMode.BRIEF_DEFER,
            brief_reply="這句不會被採用",
            defer_until=merged_at + timedelta(hours=3),
            defer_reason="這個理由也不會被採用",
        ),
        current_activity=activity,
        now=merged_at,
    )

    assert merged.id == existing.id
    assert [m.content for m in merged.messages] == ["第一則", "第二則"]
    # The merge inherits the *original* defer terms — the new decision's
    # brief reply / reason / defer_until are discarded.
    assert merged.brief_reply == "先回，等會議結束"
    assert merged.defer_reason == "會議中"
    assert merged.scheduled_for == activity.end_at
    assert merged.activity_id == activity.id
    assert merged.status == PendingFollowUpStatus.QUEUED
    assert merged.queued_at == existing.queued_at
    assert merged.updated_at == merged_at
    # One row, and no second release job for it.
    assert len(await pending_repo.list_created_since(
        "conv-merge", first_at,
    )) == 1
    assert enqueuer.rows == []
