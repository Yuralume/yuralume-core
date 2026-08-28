"""HV4 — chat is audited after delivery, and a caught lie is always owed.

Chat streams token by token, so the honesty gate cannot stand in front of
it (§3.6). What D6 buys instead is a promise with a number on it: every
dishonest reply the post-turn judge catches produces a **durable** repair
follow-up — 100%, no fire-and-forget task that a restart can forget.

The tests are grouped by the way this can fail in production:

* the judge said "dishonest" and a row landed, anchored to the turn, due a
  beat later, released through the ordinary promise machinery;
* the judge said nothing useful (unavailable) or nothing at all (no tools
  were on the table) and no row landed — chat fails **open**, unlike every
  background surface;
* the player undid the turn while the judge was upstream, and the row is
  not owed after all — nor resurrected by undo's restore pass;
* the row could not be written, and the miss is loud on three surfaces
  (alert counter, ERROR log, audit record) rather than silent;
* the row, once released, honours PF's three endings — attachment,
  honest failure, words-only when the deployment renders nothing.

The last group drives the real ``PendingFollowUpDispatcher`` over the row
this ticket writes rather than a hand-built one, because the claim under
test is precisely that the repair *is* an ordinary scheduled promise.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.dto.chat import SendChatMessageRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.application.services.chat_outcome_claim_auditor import (
    CHAT_HONESTY_TURN_KIND,
    REPAIR_STATUS_NO_REPOSITORY,
    REPAIR_STATUS_NO_VERDICT,
    REPAIR_STATUS_NOT_NEEDED,
    REPAIR_STATUS_QUEUED,
    REPAIR_STATUS_TURN_UNDONE,
    REPAIR_STATUS_WRITE_FAILED,
    ChatOutcomeClaimAuditor,
)
from kokoro_link.application.services.composer_tool_loop import ComposerToolLoop
from kokoro_link.application.services.outcome_claim_guard import (
    OutcomeClaimGuard,
)
from kokoro_link.application.services.pending_follow_up_dispatcher import (
    PendingFollowUpDispatcher,
)
from kokoro_link.application.services.tool_orchestrator import ToolOrchestrator
from kokoro_link.application.services.undone_turn_gate import UndoneTurnGate
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.due_jobs import (
    PENDING_FOLLOW_UP_RELEASE_KIND,
    KnobGate,
    kind_spec,
)
from kokoro_link.contracts.outcome_claim import (
    OutcomeClaimEvidence,
    OutcomeClaimVerdict,
)
from kokoro_link.contracts.pending_follow_up_composer import (
    PendingFollowUpComposeOutput,
    PendingFollowUpComposerPort,
)
from kokoro_link.contracts.prompt import ToolOutcomeMessage
from kokoro_link.contracts.scheduled_promise_composer import (
    ScheduledPromiseComposeInput,
    ScheduledPromiseComposeOutput,
    ScheduledPromiseComposerPort,
)
from kokoro_link.contracts.tool import TOOL_CAPABILITY_IMAGE, ToolPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.pending_follow_up import (
    PendingFollowUpKind,
    PendingFollowUpStatus,
)
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.domain.value_objects.tool_call import ToolCall, ToolResult
from kokoro_link.infrastructure.prompt.outcome_claim_honesty import (
    REPAIR_INTENT_MAX_CHARS,
    render_repair_promise_intent,
)
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.post_turn.null_processor import (
    NullPostTurnProcessor,
)
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_pending_follow_ups import (
    InMemoryPendingFollowUpRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_tool_invocations import (
    InMemoryToolInvocationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_undone_turns import (
    InMemoryUndoneTurnRepository,
)
from kokoro_link.infrastructure.state.simple import SimpleStateEngine
from kokoro_link.infrastructure.tools.fake_tools import FakeImageTool
from kokoro_link.infrastructure.tools.registry import InMemoryToolRegistry

_TURN_ID = "turn-1"
_CONV_ID = "conv-1"


def _now() -> datetime:
    return datetime(2026, 8, 25, 9, 36, tzinfo=timezone.utc)


def _character(cid: str = "char-1") -> Character:
    return replace(
        Character.create(
            name="Aki", summary="", personality=[], interests=[],
            speaking_style="", boundaries=[],
            state=CharacterState(
                emotion="neutral", affection=50, fatigue=20, trust=50,
                energy=70,
            ),
        ),
        id=cid,
        allowed_tools=["fake_image"],
    )


class _ScriptedJudge:
    """Answers with the queued verdicts; the last one repeats."""

    def __init__(self, *verdicts: OutcomeClaimVerdict) -> None:
        self._verdicts = list(verdicts) or [OutcomeClaimVerdict.ok()]
        self.seen: list[OutcomeClaimEvidence] = []

    async def judge(
        self, *, message_text, evidence, character=None,
        operator_primary_language="",
    ):
        self.seen.append(evidence)
        if len(self._verdicts) > 1:
            return self._verdicts.pop(0)
        return self._verdicts[0]


class _Recorder:
    def __init__(self) -> None:
        self.drafts: list[Any] = []

    async def record(self, draft) -> None:
        self.drafts.append(draft)


class _ExplodingFollowUps(InMemoryPendingFollowUpRepository):
    async def add(self, follow_up) -> None:
        raise RuntimeError("pending_follow_ups is down")


class _RecordingEnqueuer:
    def __init__(self) -> None:
        self.rows: list[Any] = []

    async def enqueue(self, row, *, now=None) -> bool:
        self.rows.append(row)
        return True


def _auditor(
    *,
    judge: _ScriptedJudge,
    repository=None,
    recorder: _Recorder | None = None,
) -> tuple[ChatOutcomeClaimAuditor, OutcomeClaimGuard]:
    guard = OutcomeClaimGuard(judge=judge)
    return (
        ChatOutcomeClaimAuditor(
            guard=guard,
            pending_follow_up_repository=repository,
            turn_recorder=recorder,
        ),
        guard,
    )


async def _audit(
    auditor: ChatOutcomeClaimAuditor,
    *,
    text: str = "拍好囉，照片傳給你了！",
    offered: tuple[str, ...] = ("fake_image",),
    outcomes: tuple[ToolOutcomeMessage, ...] = (),
    attachments: int = 0,
    gate: UndoneTurnGate | None = None,
    enqueuer=None,
):
    return await auditor.audit(
        character=_character(),
        conversation_id=_CONV_ID,
        turn_record_id=_TURN_ID,
        assistant_text=text,
        offered_tools=offered,
        tool_outcomes=outcomes,
        delivered_attachments=attachments,
        undone_turn_gate=gate,
        release_enqueuer=enqueuer,
        now=_now(),
    )


# --- the headline: a caught lie is always owed --------------------------


@pytest.mark.asyncio
async def test_dishonest_reply_owes_a_durable_repair_row() -> None:
    """The 2026-08-25 shape, on the surface that cannot be gated: the
    reply is already on screen, so the only remedy left is the character
    coming back — and it has to survive the process noticing it."""
    repo = InMemoryPendingFollowUpRepository()
    judge = _ScriptedJudge(OutcomeClaimVerdict.blocked(("照片傳給你了",)))
    auditor, guard = _auditor(judge=judge, repository=repo)

    result = await _audit(auditor)

    assert result.repaired
    assert result.repair_status == REPAIR_STATUS_QUEUED
    rows = await repo.list_open_for_character("char-1")
    assert len(rows) == 1
    row = rows[0]
    # An ordinary scheduled promise — that is the whole point. Everything
    # downstream (release job, D5 exemption, HV1 gate on the re-compose,
    # PF's three endings) comes from being one, not from new machinery.
    assert row.kind == PendingFollowUpKind.SCHEDULED_PROMISE
    assert row.status == PendingFollowUpStatus.QUEUED
    assert row.conversation_id == _CONV_ID
    # Anchored to the turn: the identity undo deletes by (TU4).
    assert row.turn_record_id == _TURN_ID
    # Due a beat later, not immediately — it must not interrupt the very
    # exchange it is settling.
    assert row.scheduled_for > _now()
    assert row.promise_intent
    assert guard.counters.chat_audited == 1
    assert guard.counters.chat_repair_queued == 1
    assert guard.counters.chat_repair_missed == 0


@pytest.mark.asyncio
async def test_repair_row_is_handed_to_the_release_queue() -> None:
    """Hosted mints the due-time job at the write point, exactly like the
    promise writer does; embedded (no enqueuer) leaves it to the tick."""
    repo = InMemoryPendingFollowUpRepository()
    enqueuer = _RecordingEnqueuer()
    auditor, _ = _auditor(
        judge=_ScriptedJudge(OutcomeClaimVerdict.blocked(("查到了",))),
        repository=repo,
    )

    result = await _audit(auditor, enqueuer=enqueuer)

    assert [row.id for row in enqueuer.rows] == [result.repair_follow_up_id]


@pytest.mark.asyncio
async def test_repair_release_is_exempt_from_the_tier_multiplier() -> None:
    """D5, as a fact to hold still rather than code to write: the release
    kind the repair rides is already ``KnobGate.NONE``, so a low-tier
    player's repair is not stretched by the background cadence knob — and
    it is deliberately NOT dormancy-exempt, because nobody is waiting on a
    repair for a player who has not been seen in a week."""
    spec = kind_spec(PENDING_FOLLOW_UP_RELEASE_KIND)
    assert spec is not None
    assert spec.knob_gate is KnobGate.NONE
    assert spec.dormancy_exempt is False


def test_repair_intent_names_the_claim_and_fits_the_row() -> None:
    """The whole brief the composer will get, hours later, in one field —
    so it has to name what was owed *and* survive the entity's 500-char
    cap without losing its last sentence (the one about telling the truth
    when the tool fails)."""
    intent = render_repair_promise_intent(("照片已經傳過去了", "我剛查了新聞"))
    assert "照片已經傳過去了" in intent
    assert len(intent) <= REPAIR_INTENT_MAX_CHARS
    assert intent[-1] != "…"


def test_repair_intent_without_quoted_claims_still_says_what_to_do() -> None:
    """A judge that flagged the message but quoted nothing must not leave
    the composer pointed at a draft it cannot see — the correction's
    fallback ("re-read the previous version") is written for a retry that
    still has one, and this reader does not."""
    intent = render_repair_promise_intent(())
    assert "上一版訊息" not in intent
    assert intent.strip()


# --- when nothing is owed ------------------------------------------------


@pytest.mark.asyncio
async def test_honest_reply_owes_nothing_but_is_still_recorded() -> None:
    """The denominator: a dishonesty rate needs the cleared turns too."""
    repo = InMemoryPendingFollowUpRepository()
    recorder = _Recorder()
    auditor, guard = _auditor(
        judge=_ScriptedJudge(OutcomeClaimVerdict.ok()),
        repository=repo,
        recorder=recorder,
    )

    result = await _audit(auditor, text="等等幫你拍一張", outcomes=())

    assert result.audited
    assert result.repair_status == REPAIR_STATUS_NOT_NEEDED
    assert await repo.list_open_for_character("char-1") == []
    assert guard.counters.chat_audited == 1
    draft = recorder.drafts[0]
    assert draft.kind == CHAT_HONESTY_TURN_KIND
    assert draft.post_turn_refs["parent_turn_record_id"] == _TURN_ID
    assert draft.post_turn_refs["repair_status"] == REPAIR_STATUS_NOT_NEEDED
    assert draft.post_turn_refs["outcome_claim_judge"]["final_verdict"] == (
        "consistent"
    )


@pytest.mark.asyncio
async def test_turn_with_no_tools_offered_never_calls_the_judge() -> None:
    """Nothing could have been called, so nothing could have been lied
    about calling — the prompt-side red line covers that shape, and a
    model call here would buy nothing."""
    repo = InMemoryPendingFollowUpRepository()
    judge = _ScriptedJudge(OutcomeClaimVerdict.blocked(("查到了",)))
    auditor, guard = _auditor(judge=judge, repository=repo)

    result = await _audit(auditor, offered=())

    assert result.audited is False
    assert judge.seen == []
    assert guard.counters.chat_audited == 0
    assert await repo.list_open_for_character("char-1") == []


@pytest.mark.asyncio
async def test_unavailable_verdict_fails_open_on_chat() -> None:
    """The asymmetry §3.6 makes deliberate: background parks on a missing
    verdict, chat cannot — the message is already delivered, and inventing
    a repair without evidence would have the character apologise for a
    reply that may have been perfectly honest."""
    repo = InMemoryPendingFollowUpRepository()
    auditor, guard = _auditor(
        judge=_ScriptedJudge(OutcomeClaimVerdict.failed()),
        repository=repo,
    )

    result = await _audit(auditor)

    assert result.repair_status == REPAIR_STATUS_NO_VERDICT
    assert await repo.list_open_for_character("char-1") == []
    # The outage alarm still counts it — that is where a broken judge
    # becomes visible, not here.
    assert guard.counters.judge_failed == 1
    assert guard.counters.chat_repair_missed == 0


@pytest.mark.asyncio
async def test_judge_evidence_carries_the_turns_tool_facts() -> None:
    """The audit is only as honest as what it is told: the attachments
    that actually shipped and the outcomes that actually happened."""
    repo = InMemoryPendingFollowUpRepository()
    judge = _ScriptedJudge(OutcomeClaimVerdict.ok())
    auditor, _ = _auditor(judge=judge, repository=repo)

    await _audit(
        auditor,
        outcomes=(
            ToolOutcomeMessage(
                tool_name="fake_image", ok=False, output_text="",
                error="ComfyUI timeout",
            ),
        ),
        attachments=0,
    )

    evidence = judge.seen[0]
    assert evidence.offered_tools == ("fake_image",)
    assert evidence.outcomes[0].ok is False
    assert evidence.delivered_attachments == 0


# --- undo -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_undone_turn_owes_no_repair() -> None:
    """The audit runs behind a model call the player never waited for. An
    undo landing in that window has already deleted the message being
    repaired, so writing the row would have the character apologise for
    something no longer in the transcript."""
    repo = InMemoryPendingFollowUpRepository()
    tombstones = InMemoryUndoneTurnRepository()
    gate = UndoneTurnGate(tombstones)
    await gate.record(
        turn_record_id=_TURN_ID, conversation_id=_CONV_ID, now=_now(),
    )
    auditor, guard = _auditor(
        judge=_ScriptedJudge(OutcomeClaimVerdict.blocked(("照片傳了",))),
        repository=repo,
    )

    result = await _audit(auditor, gate=gate)

    assert result.repair_status == REPAIR_STATUS_TURN_UNDONE
    assert await repo.list_open_for_character("char-1") == []
    # Not a miss: nothing is owed, so the alert line must stay clean.
    assert guard.counters.chat_repair_missed == 0


# --- never silent ---------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_repair_write_is_loud_on_every_surface(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A judge that finds a lie and then loses the repair is worse than no
    judge: the audit trail looks clean. The counter is an alert line, the
    log is ERROR, and the audit record names the failure."""
    recorder = _Recorder()
    auditor, guard = _auditor(
        judge=_ScriptedJudge(OutcomeClaimVerdict.blocked(("照片傳了",))),
        repository=_ExplodingFollowUps(),
        recorder=recorder,
    )

    with caplog.at_level(logging.ERROR):
        result = await _audit(auditor)

    assert result.repair_status == REPAIR_STATUS_WRITE_FAILED
    assert result.repair_follow_up_id is None
    assert guard.counters.chat_repair_missed == 1
    assert guard.counters.chat_repair_queued == 0
    assert any(rec.levelno >= logging.ERROR for rec in caplog.records)
    assert recorder.drafts[0].post_turn_refs["repair_status"] == (
        REPAIR_STATUS_WRITE_FAILED
    )


@pytest.mark.asyncio
async def test_undo_gate_crash_after_a_caught_lie_is_loud_too(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """B-1: the judge already found the turn dishonest — a caught lie —
    when the very next call, the undo-tombstone check, blows up. That used
    to escape ``_owe_repair`` uncaught, past the ``CancelledError`` branch
    (which does count), straight into ``audit()``'s top-level
    ``except Exception`` — which returns ``audited=False`` and never
    touches the alert counter. The lie was caught and then the record of
    catching it evaporated."""

    class _ExplodingGate:
        async def is_undone(self, turn_record_id: str | None) -> bool:
            raise RuntimeError("tombstone store exploded")

    recorder = _Recorder()
    auditor, guard = _auditor(
        judge=_ScriptedJudge(OutcomeClaimVerdict.blocked(("照片傳了",))),
        repository=InMemoryPendingFollowUpRepository(),
        recorder=recorder,
    )

    with caplog.at_level(logging.ERROR):
        result = await _audit(auditor, gate=_ExplodingGate())

    assert result.audited is True
    assert result.repair_status == REPAIR_STATUS_WRITE_FAILED
    assert result.repair_follow_up_id is None
    assert guard.counters.chat_repair_missed == 1
    assert any(rec.levelno >= logging.ERROR for rec in caplog.records)
    # The third silent surface: the audit turn record still lands, and
    # names the failure rather than reading "not_needed" by omission.
    assert recorder.drafts[0].post_turn_refs["repair_status"] == (
        REPAIR_STATUS_WRITE_FAILED
    )


@pytest.mark.asyncio
async def test_missing_follow_up_store_is_loud_too(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A deployment that cannot owe anything must say so, not pass."""
    auditor, guard = _auditor(
        judge=_ScriptedJudge(OutcomeClaimVerdict.blocked(("查到了",))),
        repository=None,
    )

    with caplog.at_level(logging.ERROR):
        result = await _audit(auditor)

    assert result.repair_status == REPAIR_STATUS_NO_REPOSITORY
    assert guard.counters.chat_repair_missed == 1
    assert any(rec.levelno >= logging.ERROR for rec in caplog.records)


@pytest.mark.asyncio
async def test_audit_never_raises_into_the_chat_tail() -> None:
    """It runs as a background task off the write point; an exception
    escaping would surface as an unretrieved-task warning and nothing
    else, which is the silent failure this whole file is about."""

    class _Exploding:
        async def judge(self, **_kwargs):
            raise RuntimeError("provider down")

    guard = OutcomeClaimGuard(judge=_Exploding())
    # The guard converts a raising judge into ``unavailable``; force the
    # failure past it by breaking the repository the result path uses.
    auditor = ChatOutcomeClaimAuditor(
        guard=guard, pending_follow_up_repository=None,
    )
    result = await _audit(auditor)
    assert result.repair_status == REPAIR_STATUS_NO_VERDICT


@pytest.mark.asyncio
async def test_cancellation_mid_judge_is_counted_and_re_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """F2: a task killed mid-judge (the shutdown drain giving up on a
    still-running audit past its bound, or any other cancellation) must
    not vanish silently. ``CancelledError`` is a ``BaseException`` — the
    ``except Exception`` guard that makes ``audit()`` "never raise" does
    not see it, so it needs its own boundary that counts the loss the
    same way a failed repair write is counted, logs it, and — unlike
    every other path here — still lets the cancellation through."""

    class _Cancelling:
        async def judge(self, **_kwargs):
            raise asyncio.CancelledError()

    guard = OutcomeClaimGuard(judge=_Cancelling())
    auditor = ChatOutcomeClaimAuditor(
        guard=guard, pending_follow_up_repository=None,
    )

    with caplog.at_level(logging.WARNING):
        with pytest.raises(asyncio.CancelledError):
            await _audit(auditor)

    assert guard.counters.chat_repair_missed == 1
    assert any(rec.levelno >= logging.WARNING for rec in caplog.records)


# --- the write point: the audit is actually reached ----------------------


class _ScriptedModel(ChatModelPort):
    supports_vision = False

    def __init__(self, replies: list[str]) -> None:
        self.provider_id = "fake"
        self._replies = replies
        self.calls: list[str] = []

    async def generate(self, prompt: str, **kwargs) -> str:
        self.calls.append(prompt)
        return self._replies.pop(0) if self._replies else "（沒有更多腳本）"

    async def generate_stream(self, prompt: str, **kwargs):
        text = await self.generate(prompt)

        async def _iter():
            yield text

        return _iter()


def _chat_service(*, replies: list[str], auditor: ChatOutcomeClaimAuditor):
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(_ScriptedModel(replies))
    tool_registry = InMemoryToolRegistry([FakeImageTool()])
    character_repository = InMemoryCharacterRepository()
    chat = ChatService(
        character_repository=character_repository,
        conversation_repository=InMemoryConversationRepository(),
        memory_repository=InMemoryMemoryRepository(),
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        tool_registry=tool_registry,
        tool_orchestrator=ToolOrchestrator(
            registry=tool_registry,
            invocation_repository=InMemoryToolInvocationRepository(),
        ),
    )
    chat.set_outcome_claim_auditor(auditor)
    return chat, CharacterService(character_repository)


@pytest.mark.asyncio
async def test_write_point_audits_a_delivered_reply_and_owes_the_repair() -> None:
    """End to end over the real chat write point — the seam a refactor
    would drop silently. The model answers in fluent prose, calls nothing,
    and the reply ships with no attachment: the 2026-08-25 shape, arriving
    on the surface where it cannot be stopped."""
    repo = InMemoryPendingFollowUpRepository()
    judge = _ScriptedJudge(OutcomeClaimVerdict.blocked(("照片傳給你了",)))
    auditor, guard = _auditor(judge=judge, repository=repo)
    chat, characters = _chat_service(
        replies=["拍好囉，照片傳給你了！"], auditor=auditor,
    )
    created = await characters.create_character(
        CreateCharacterRequest(name="Yuki", allowed_tools=["fake_image"]),
    )

    response = await chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="傳一張你現在的照片給我",
    ))
    # The player already has the reply; the audit is a background tail.
    assert response.assistant_message.attachments == []
    await chat.wait_for_pending()

    assert guard.counters.chat_audited == 1
    # The judge was told the truth about the turn: a camera was offered
    # and nothing was called.
    assert judge.seen[0].offered_tools == ("fake_image",)
    assert judge.seen[0].outcomes == ()
    assert judge.seen[0].delivered_attachments == 0
    rows = await repo.list_open_for_character(created.id)
    assert len(rows) == 1
    assert rows[0].turn_record_id == response.assistant_message.turn_record_id


@pytest.mark.asyncio
async def test_write_point_skips_the_audit_when_the_character_has_no_tools(
) -> None:
    """No camera on the table, no judge call, no cost."""
    repo = InMemoryPendingFollowUpRepository()
    judge = _ScriptedJudge(OutcomeClaimVerdict.blocked(("照片傳給你了",)))
    auditor, guard = _auditor(judge=judge, repository=repo)
    chat, characters = _chat_service(
        replies=["拍好囉，照片傳給你了！"], auditor=auditor,
    )
    created = await characters.create_character(
        CreateCharacterRequest(name="Yuki", allowed_tools=[]),
    )

    await chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="傳一張你現在的照片給我",
    ))
    await chat.wait_for_pending()

    assert judge.seen == []
    assert guard.counters.chat_audited == 0
    assert await repo.list_open_for_character(created.id) == []


@pytest.mark.asyncio
async def test_streaming_write_point_audits_too() -> None:
    """The surface the whole design exists for.

    On the tool path the finalizer is built before the generation exists
    and has it late-bound; if that hand-over ever stops carrying the tool
    facts, the audit silently sees "no tools were offered" and every
    streamed reply becomes unauditable — with nothing turning red."""
    repo = InMemoryPendingFollowUpRepository()
    judge = _ScriptedJudge(OutcomeClaimVerdict.blocked(("照片傳給你了",)))
    auditor, guard = _auditor(judge=judge, repository=repo)
    chat, characters = _chat_service(
        replies=["拍好囉，照片傳給你了！"], auditor=auditor,
    )
    created = await characters.create_character(
        CreateCharacterRequest(name="Yuki", allowed_tools=["fake_image"]),
    )

    token_stream, finalizer = await chat.send_message_stream(
        SendChatMessageRequest(
            character_id=created.id, message="傳一張你現在的照片給我",
        ),
    )
    chunks = [item async for item in token_stream if isinstance(item, str)]
    await finalizer.finish("".join(chunks))
    await chat.wait_for_pending()

    assert guard.counters.chat_audited == 1
    assert judge.seen[0].offered_tools == ("fake_image",)
    assert len(await repo.list_open_for_character(created.id)) == 1


# --- the repair row released: PF's three endings, unchanged --------------


class _FailingImageTool(ToolPort):
    name: str = "fake_image"
    capability: str = TOOL_CAPABILITY_IMAGE
    description: str = "產生一張角色圖片（失敗用 stub）。"
    parameters_schema: dict[str, Any] = {"type": "object", "properties": {}}

    async def invoke(self, ctx) -> ToolResult:
        return ToolResult.failure(error="ComfyUI 連不上")


class _ScriptedPromiseComposer(ScheduledPromiseComposerPort):
    """Asks for the image on pass 1, writes prose on pass 2.

    Which pass it is is read off the payload (``tool_results`` present =
    second), so the assertions describe the loop's data shape rather than
    a counter the composer could fake."""

    def __init__(self) -> None:
        self.inputs: list[ScheduledPromiseComposeInput] = []

    async def compose(self, payload):
        self.inputs.append(payload)
        if payload.tool_results:
            return ScheduledPromiseComposeOutput(
                content_text="相機出問題了，沒拍成，晚點再補給你",
            )
        if payload.available_tools:
            return ScheduledPromiseComposeOutput(
                content_text="",
                tool_calls=(ToolCall(name="fake_image", arguments={}),),
            )
        # No tools offered at all (capability closed on this deployment)
        # → the promise is kept in words. PF's third ending.
        return ScheduledPromiseComposeOutput(content_text="我沒辦法拍，抱歉騙了你")


class _StubBusyComposer(PendingFollowUpComposerPort):
    async def compose(self, payload):  # pragma: no cover - never reached
        return PendingFollowUpComposeOutput(content_text="unused")


@dataclass
class _StubCharacterRepo:
    characters: dict[str, Character]

    async def get(self, character_id: str) -> Character | None:
        return self.characters.get(character_id)

    async def list(self) -> list[Character]:  # pragma: no cover
        return list(self.characters.values())

    async def save(self, c: Character) -> None:  # pragma: no cover
        self.characters[c.id] = c

    async def delete(self, c: str) -> bool:  # pragma: no cover
        return self.characters.pop(c, None) is not None


class _StubScheduleService:
    async def ensure_schedule(self, character):
        return object()

    def resolve_current(self, schedule, *, now):
        return None, [], None


class _StubProactiveDispatcher:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def deliver_pre_composed(
        self, *, character_id, text, trigger, reason="", attachments=(),
        now=None,
    ):
        self.calls.append({
            "text": text, "trigger": trigger,
            "attachments": tuple(attachments),
        })
        return ProactiveAttempt.record(
            character_id=character_id, trigger=trigger,
            outcome=ProactiveOutcome.SENT, reason=reason or "stub",
            message=text, now=now or _now(),
        )


class _StubCapabilityEnqueuer:
    """Stands in for the distributed hand-off queue.

    Its presence is what makes the loop treat the deployment's closed
    capabilities as real — the embedded path runs every tool inline and is
    deliberately unaffected by ``BG_CAP_IMAGE``."""

    async def defer_capability(self, row, *, capability, now=None) -> bool:
        return False


async def _repair_row(repo, judge_claims=("照片傳給你了",)):
    auditor, _ = _auditor(
        judge=_ScriptedJudge(OutcomeClaimVerdict.blocked(judge_claims)),
        repository=repo,
    )
    result = await _audit(auditor)
    row = await repo.get(result.repair_follow_up_id)
    assert row is not None
    return row


def _dispatcher(repo, *, composer, loop, capability_enqueuer=None):
    char = _character()
    proactive = _StubProactiveDispatcher()
    dispatcher = PendingFollowUpDispatcher(
        repository=repo,
        composer=_StubBusyComposer(),
        proactive_dispatcher=proactive,
        character_repository=_StubCharacterRepo({char.id: char}),
        schedule_service=_StubScheduleService(),
        scheduled_promise_composer=composer,
        tool_loop=loop,
    )
    if capability_enqueuer is not None:
        dispatcher.set_capability_release_enqueuer(capability_enqueuer)
    return dispatcher, proactive


def _loop(tool: ToolPort, *, capability_caps=None) -> ComposerToolLoop:
    registry = InMemoryToolRegistry([tool])
    return ComposerToolLoop(
        tool_registry=registry,
        tool_orchestrator=ToolOrchestrator(
            registry=registry,
            invocation_repository=InMemoryToolInvocationRepository(),
        ),
        public_base_url="https://example.test",
        capability_caps=capability_caps,
    )


@pytest.mark.asyncio
async def test_repair_delivers_the_artifact_when_the_tool_works() -> None:
    """兌現 at its best: the character really goes and does the thing."""
    repo = InMemoryPendingFollowUpRepository()
    row = await _repair_row(repo)
    composer = _ScriptedPromiseComposer()
    dispatcher, proactive = _dispatcher(
        repo, composer=composer, loop=_loop(FakeImageTool()),
    )

    released = await dispatcher.release_row(
        row, now=row.scheduled_for + timedelta(seconds=1),
    )

    assert released is True
    assert proactive.calls[0]["trigger"] == ProactiveTrigger.SCHEDULED_PROMISE
    assert proactive.calls[0]["attachments"]
    # The repair brief is what the composer was briefed with.
    assert composer.inputs[0].promise_intent == row.promise_intent


@pytest.mark.asyncio
async def test_repair_tells_the_truth_when_the_tool_fails() -> None:
    """PF's second ending, inherited whole: the failure fact reaches pass
    2, so the character accounts for it instead of going quiet again."""
    repo = InMemoryPendingFollowUpRepository()
    row = await _repair_row(repo)
    composer = _ScriptedPromiseComposer()
    dispatcher, proactive = _dispatcher(
        repo, composer=composer, loop=_loop(_FailingImageTool()),
    )

    released = await dispatcher.release_row(
        row, now=row.scheduled_for + timedelta(seconds=1),
    )

    assert released is True
    second_pass = composer.inputs[-1]
    assert second_pass.tool_results
    assert second_pass.tool_results[0].ok is False
    assert proactive.calls[0]["text"] == "相機出問題了，沒拍成，晚點再補給你"
    assert proactive.calls[0]["attachments"] == ()


@pytest.mark.asyncio
async def test_repair_is_kept_in_words_when_the_capability_is_off() -> None:
    """``BG_CAP_IMAGE=0``: this deployment renders nothing in the
    background, so the image tool is never offered and 兌現 means saying
    so. The repair is settled either way — "必達" is the account, not the
    picture."""
    repo = InMemoryPendingFollowUpRepository()
    row = await _repair_row(repo)
    composer = _ScriptedPromiseComposer()
    dispatcher, proactive = _dispatcher(
        repo,
        composer=composer,
        loop=_loop(FakeImageTool(), capability_caps={"image": 0}),
        capability_enqueuer=_StubCapabilityEnqueuer(),
    )

    released = await dispatcher.release_row(
        row,
        now=row.scheduled_for + timedelta(seconds=1),
        defer_capabilities=True,
    )

    assert released is True
    assert composer.inputs[0].available_tools == ()
    assert proactive.calls[0]["text"] == "我沒辦法拍，抱歉騙了你"
    assert proactive.calls[0]["attachments"] == ()
    stored = await repo.get(row.id)
    assert stored is not None
    assert stored.status == PendingFollowUpStatus.RESOLVED
