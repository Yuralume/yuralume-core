"""QG5 — chat's half of the player-visible output quality gate.

Three things are pinned here, and they are deliberately separate:

*Routing* (D3). Whether a turn is buffered and reviewed at all is decided
before generation from deterministic signals. The new one is script mix:
a character whose recent replies have drifted into 晶晶體 gets the
following turns routed through the gate. Nothing here reads meaning and
nothing rejects text — the only consequence is which code path runs, the
same standing the embedding self-similarity number already had.

*Disposal* (D1, chat row). A player is watching, so chat regenerates once
and ships whatever it has. The lock is that a **hard** failure still ships
— and is counted as ``hard_published_best_effort``, because that counter
is the only thing that can tell an operator how many players saw a defect.

*The record*. Chat shipping a hard failure means the turn's record is the
only place the defect is ever written down.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import kokoro_link.application.services.chat_service as chat_module
from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.dto.chat import SendChatMessageRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.application.services.output_quality import (
    OUTCOME_HARD_PUBLISHED_BEST_EFFORT,
    OUTCOME_PASS,
    OutputQualityCounters,
    OutputQualityOrchestrator,
)
from kokoro_link.contracts.novelty_gate import NoveltyGateContext, NoveltyVerdict
from kokoro_link.contracts.register_profile import RegisterProfile
from kokoro_link.contracts.reply_quality import ReplyDiversityEvidence
from kokoro_link.domain.entities.conversation import Message, MessageRole
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.observability.turn_recorder import (
    BackgroundTurnRecorder,
)
from kokoro_link.infrastructure.post_turn.null_processor import NullPostTurnProcessor
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_turn_records import (
    InMemoryTurnRecordRepository,
)
from kokoro_link.infrastructure.state.simple import SimpleStateEngine

_DEFAULT_RISK_THRESHOLD = 0.65
"""The shipped ``reply_quality_gate_risk_threshold`` default.

Restated here rather than imported so a settings change that lowers the
bar cannot silently make these tests pass for the wrong reason."""


# -- fakes ----------------------------------------------------------------


class _PromptBuilder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def build(self, **kwargs) -> str:  # noqa: ANN003
        self.calls.append(dict(kwargs))
        return f"最新使用者訊息：{kwargs.get('latest_user_message', '')}"


class _SequenceModel:
    provider_id = "openai"
    supports_vision = False

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[str] = []

    async def generate(self, prompt: str, *, image_urls=(), model=None):  # noqa: ANN001
        self.calls.append(prompt)
        return self.replies.pop(0) if self.replies else ""

    async def generate_stream(self, prompt: str, *, image_urls=(), model=None):  # noqa: ANN001
        self.calls.append(prompt)
        yield self.replies.pop(0) if self.replies else ""

    async def list_models(self) -> list[str]:
        return ["gpt-4o-mini"]


class _ActiveProvider:
    def __init__(self, model: _SequenceModel) -> None:
        self.model = model

    async def resolve(self, feature_key=None, *, character=None):  # noqa: ANN001
        return self.model

    async def resolve_model_id(self, feature_key=None, *, character=None):  # noqa: ANN001
        return "gpt-4o-mini"

    async def is_fake(self, feature_key=None, *, character=None):  # noqa: ANN001
        return False


class _Gate:
    def __init__(self, verdicts: list[NoveltyVerdict]) -> None:
        self.verdicts = list(verdicts)
        self.calls: list[NoveltyGateContext] = []

    async def evaluate(self, context: NoveltyGateContext, *, character=None):  # noqa: ANN001
        self.calls.append(context)
        return self.verdicts.pop(0) if self.verdicts else NoveltyVerdict(passes=True)


class _OperatorProfiles:
    def __init__(self, profile: OperatorProfile) -> None:
        self.profile = profile

    async def get_for_user(self, user_id: str) -> OperatorProfile:
        return self.profile

    async def get_current(self) -> OperatorProfile:
        return self.profile


class _RegisterProfiler:
    def __init__(self, profile: RegisterProfile) -> None:
        self.result = profile

    async def profile(self, context, *, character=None):  # noqa: ANN001
        return self.result


# -- harness --------------------------------------------------------------


def _build_service(
    *,
    replies: list[str],
    gate: _Gate | None = None,
    counters: OutputQualityCounters | None = None,
    risk_threshold: float = 0.0,
    operator_profile: OperatorProfile | None = None,
    register_profiler: _RegisterProfiler | None = None,
    turn_records: InMemoryTurnRecordRepository | None = None,
    turn_recorder: BackgroundTurnRecorder | None = None,
) -> SimpleNamespace:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    model = _SequenceModel(replies)
    prompt_builder = _PromptBuilder()
    gate = gate if gate is not None else _Gate([])
    counters = counters if counters is not None else OutputQualityCounters()
    service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=prompt_builder,
        model_registry=registry,
        active_llm_provider=_ActiveProvider(model),
        state_engine=SimpleStateEngine(),
        turn_recorder=turn_recorder,
        operator_profile_service=(
            _OperatorProfiles(operator_profile)
            if operator_profile is not None else None
        ),
        register_profiler=register_profiler,
        register_profile_enabled=register_profiler is not None,
        reply_quality_gate=gate,
        reply_quality_gate_enabled=True,
        reply_quality_gate_max_retries=1,
        reply_quality_gate_risk_threshold=risk_threshold,
        output_quality_orchestrator=OutputQualityOrchestrator(
            gate=gate, counters=counters,
        ),
    )
    return SimpleNamespace(
        service=service,
        model=model,
        gate=gate,
        counters=counters,
        prompt_builder=prompt_builder,
        turn_records=turn_records,
        character_service=CharacterService(
            character_repository,
            conversation_repository=conversation_repository,
            memory_repository=memory_repository,
        ),
    )


def _assistant_messages(texts: list[str]) -> list[Message]:
    return [Message(role=MessageRole.ASSISTANT, content=text) for text in texts]


_MIXED_SCRIPT_HISTORY = [
    "今天的 meeting 真的超 exhausting，但 the coffee was surprisingly good",
    "我 totally 同意你說的 point，that approach makes sense",
    "等等我先 check 一下 schedule，然後 reply you okay",
]
_CLEAN_HISTORY = [
    "今天的會議真的很累，不過咖啡意外地好喝。",
    "我完全同意你說的那一點，那個做法說得通。",
    "等一下我先看看行程，再回你。",
]
_ENGLISH_HISTORY = [
    "Today's meeting really wore me out, but the coffee was good.",
    "I completely agree with that point — the approach makes sense.",
    "Let me check my schedule first and I'll get back to you.",
]


# -- the deterministic signal itself --------------------------------------


def test_script_mix_flag_needs_the_summary_to_name_an_actual_line() -> None:
    """One summary line is "I counted"; a second is "and it was mixed"."""
    counted_nothing = ReplyDiversityEvidence()
    counted_only = ReplyDiversityEvidence(language_mix_lines=("近 3 則輸出…",))
    named_a_line = ReplyDiversityEvidence(
        language_mix_lines=("近 3 則輸出…", "其中 2 則的拉丁字母占比超過 40%…"),
    )

    assert counted_nothing.has_script_mix_evidence is False
    assert counted_only.has_script_mix_evidence is False
    assert named_a_line.has_script_mix_evidence is True


def test_mixed_script_history_alone_clears_the_default_risk_threshold() -> None:
    evidence = chat_module._with_script_mix_evidence(
        ReplyDiversityEvidence(),
        recent_messages=_assistant_messages(_MIXED_SCRIPT_HISTORY),
        primary_language="zh-TW",
    )

    assert evidence.has_script_mix_evidence is True
    # No register profile, no embedding similarity, no phrase frequency —
    # the script mix has to carry the whole score on its own, or D3's
    # escalation would only ever fire alongside signals that were already
    # enough to route the turn.
    score = chat_module._reply_quality_risk_score(
        register_profile=None,
        diversity_evidence=evidence,
        similarity_threshold=0.88,
    )
    assert score >= _DEFAULT_RISK_THRESHOLD


def test_clean_history_leaves_the_risk_score_at_zero() -> None:
    evidence = chat_module._with_script_mix_evidence(
        ReplyDiversityEvidence(),
        recent_messages=_assistant_messages(_CLEAN_HISTORY),
        primary_language="zh-TW",
    )

    assert evidence.language_mix_lines  # it still counted and reported
    assert evidence.has_script_mix_evidence is False
    assert chat_module._reply_quality_risk_score(
        register_profile=None,
        diversity_evidence=evidence,
        similarity_threshold=0.88,
    ) == 0.0


def test_a_latin_operators_own_language_is_not_a_script_mix() -> None:
    """The mix is measured against *this operator's* script.

    Read as "contains Latin", every reply an en/vi/id/es operator receives
    is a 100% mix — the escalation would fire on their every turn and the
    reply would never stream token by token again. The signal has to be
    silent here and loud in the two tests around it, or it is not a signal.
    """
    evidence = chat_module._with_script_mix_evidence(
        ReplyDiversityEvidence(),
        recent_messages=_assistant_messages(_ENGLISH_HISTORY),
        primary_language="en",
    )

    assert evidence.language_mix_lines  # it counted, and said so
    assert evidence.has_script_mix_evidence is False
    assert chat_module._reply_quality_risk_score(
        register_profile=None,
        diversity_evidence=evidence,
        similarity_threshold=0.88,
    ) == 0.0


def test_a_latin_operator_receiving_cjk_still_escalates() -> None:
    """Mirrored direction: for an en operator, drifting into Chinese is the
    drift, and it must route the next turn into the gate."""
    evidence = chat_module._with_script_mix_evidence(
        ReplyDiversityEvidence(),
        recent_messages=_assistant_messages([
            *_ENGLISH_HISTORY, *_CLEAN_HISTORY,
        ]),
        primary_language="en",
    )

    assert evidence.has_script_mix_evidence is True
    assert chat_module._reply_quality_risk_score(
        register_profile=None,
        diversity_evidence=evidence,
        similarity_threshold=0.88,
    ) >= _DEFAULT_RISK_THRESHOLD


def test_a_cjk_operator_receiving_english_still_escalates() -> None:
    """The zero-regression half: pure English to a zh operator is the
    original 語言不符 case and keeps firing."""
    evidence = chat_module._with_script_mix_evidence(
        ReplyDiversityEvidence(),
        recent_messages=_assistant_messages(_ENGLISH_HISTORY),
        primary_language="zh-TW",
    )

    assert evidence.has_script_mix_evidence is True
    assert chat_module._reply_quality_risk_score(
        register_profile=None,
        diversity_evidence=evidence,
        similarity_threshold=0.88,
    ) >= _DEFAULT_RISK_THRESHOLD


def test_script_mix_window_ignores_older_messages() -> None:
    """The window is short so the escalation lets go again (D3).

    A character that mixed scripts ten turns ago and has written clean
    Traditional Chinese since must stop being routed into the buffered
    path, or the "sticky" in sticky risk would mean permanent."""
    history = _assistant_messages([*_MIXED_SCRIPT_HISTORY, *_CLEAN_HISTORY, *_CLEAN_HISTORY])

    evidence = chat_module._with_script_mix_evidence(
        ReplyDiversityEvidence(), recent_messages=history,
        primary_language="zh-TW",
    )

    assert evidence.has_script_mix_evidence is False


# -- routing: does the turn get buffered? ---------------------------------


@pytest.mark.asyncio
async def test_mixed_script_history_routes_the_next_turn_into_the_gate() -> None:
    harness = _build_service(replies=[], risk_threshold=_DEFAULT_RISK_THRESHOLD)

    assert harness.service._reply_quality_gate_required(
        register_profile=None,
        diversity_evidence=chat_module._with_script_mix_evidence(
            ReplyDiversityEvidence(),
            recent_messages=_assistant_messages(_MIXED_SCRIPT_HISTORY),
            primary_language="zh-TW",
        ),
    ) is True


@pytest.mark.asyncio
async def test_clean_history_leaves_the_next_turn_unrouted() -> None:
    harness = _build_service(replies=[], risk_threshold=_DEFAULT_RISK_THRESHOLD)

    assert harness.service._reply_quality_gate_required(
        register_profile=None,
        diversity_evidence=chat_module._with_script_mix_evidence(
            ReplyDiversityEvidence(),
            recent_messages=_assistant_messages(_CLEAN_HISTORY),
            primary_language="zh-TW",
        ),
    ) is False


@pytest.mark.asyncio
async def test_low_risk_turn_still_streams_token_by_token() -> None:
    """Zero-regression lock: routing must not quietly buffer everything.

    Buffering is what the gate costs the player — the reply stops arriving
    as it is written. A low-risk turn paying that price would make D3's
    whole risk score pointless."""
    gate = _Gate([NoveltyVerdict(passes=False, over_warm=True)])
    harness = _build_service(
        replies=["低風險的一句回覆。"],
        gate=gate,
        risk_threshold=0.9,
        register_profiler=_RegisterProfiler(RegisterProfile(
            axes={
                "emotional_intensity": 0.1,
                "seriousness": 0.1,
                "intimacy": 0.2,
                "humor_latitude": 0.5,
                "help_seeking": 0.0,
            },
            confidence=0.9,
            note="日常閒聊",
        )),
    )
    created = await harness.character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )

    token_stream, finalizer = await harness.service.send_message_stream(
        SendChatMessageRequest(character_id=created.id, message="今天想聊天"),
    )
    chunks = [chunk async for chunk in token_stream]
    await finalizer.finish("".join(chunks))

    assert chunks == ["低風險的一句回覆。"]
    assert gate.calls == []
    assert harness.counters.snapshot() == {}


async def _two_streamed_turns(harness, character_id: str) -> None:
    """Two streamed turns, so the second one sees the first one's reply."""
    for message in ("How was your day?", "And then?"):
        token_stream, finalizer = await harness.service.send_message_stream(
            SendChatMessageRequest(character_id=character_id, message=message),
        )
        chunks = [chunk async for chunk in token_stream]
        await finalizer.finish("".join(chunks))


@pytest.mark.asyncio
async def test_an_english_operators_english_reply_keeps_streaming() -> None:
    """The regression that made D3's "keep streaming" ruling a dead letter.

    At the *shipped* threshold — deliberately not the 0.9 the older lock
    used, which was high enough to hide this — an operator who writes
    English gets English replies, and English replies must not read as a
    script drift. If they do, every turn this operator ever takes is
    buffered and pays for a judge call, and they never see a token arrive
    as it is written again."""
    gate = _Gate([])
    harness = _build_service(
        replies=[
            "It rained all afternoon, so I stayed in and read.",
            "Then the sky cleared up right before sunset.",
        ],
        gate=gate,
        risk_threshold=_DEFAULT_RISK_THRESHOLD,
        operator_profile=OperatorProfile(
            id="u1", display_name="Sam", primary_language="en",
        ),
    )
    created = await harness.character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )

    await _two_streamed_turns(harness, created.id)

    # Neither turn was routed into the buffered path: no judge call, and
    # nothing for the band to count.
    assert gate.calls == []
    assert harness.counters.snapshot() == {}


@pytest.mark.asyncio
async def test_a_chinese_operators_english_reply_still_gets_gated() -> None:
    """Same replies, same threshold, different operator — the signal has to
    still fire, or the fix above would just be switching D3 off."""
    gate = _Gate([])
    harness = _build_service(
        replies=[
            "It rained all afternoon, so I stayed in and read.",
            "Then the sky cleared up right before sunset.",
        ],
        gate=gate,
        risk_threshold=_DEFAULT_RISK_THRESHOLD,
        operator_profile=OperatorProfile(
            id="u1", display_name="Alex", primary_language="zh-TW",
        ),
    )
    created = await harness.character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )

    await _two_streamed_turns(harness, created.id)

    # Turn 1 had nothing to count; turn 2 saw turn 1's English reply.
    assert len(gate.calls) == 1
    assert gate.calls[0].operator_primary_language == "zh-TW"


# -- disposal: chat ships, and says so ------------------------------------


@pytest.mark.asyncio
async def test_hard_failure_regenerates_once_then_ships_and_is_counted() -> None:
    turn_records = InMemoryTurnRecordRepository()
    turn_recorder = BackgroundTurnRecorder(turn_records)
    gate = _Gate([NoveltyVerdict(
        passes=False,
        language_mismatch=True,
        feedback="整段回成英文，用玩家的繁體中文重寫。",
        gate_metadata={"provider_id": "gate-provider"},
    )])
    harness = _build_service(
        replies=["Sure! Let me tell you about my day.", "今天過得很普通，但午後有陣雨。"],
        gate=gate,
        turn_records=turn_records,
        turn_recorder=turn_recorder,
    )
    created = await harness.character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )

    reply = await harness.service.send_message(
        SendChatMessageRequest(character_id=created.id, message="今天過得怎樣"),
    )
    await turn_recorder.flush()

    # Shipped, not withheld: chat has no "send nothing" move.
    assert reply.assistant_message is not None
    assert reply.assistant_message.content == "今天過得很普通，但午後有陣雨。"
    # One regeneration, and the regenerated draft is NOT re-reviewed —
    # the 2026-06-17 D5 latency call, unchanged by QG5.
    assert len(harness.model.calls) == 2
    assert len(gate.calls) == 1
    assert harness.counters.total("chat", OUTCOME_HARD_PUBLISHED_BEST_EFFORT) == 1


@pytest.mark.asyncio
async def test_a_blank_regeneration_ships_the_original_not_the_blank() -> None:
    """The band's blank check only reads strings, and chat's candidate is a
    ``_ChatDraft`` — so an empty retry would sail through as "usable" and
    the player would receive nothing at all in place of a merely flawed
    reply. Chat answers ``None`` instead, which is the failed-regeneration
    path the band already handles."""
    gate = _Gate([NoveltyVerdict(
        passes=False, language_mismatch=True, feedback="請用繁體中文重寫。",
    )])
    harness = _build_service(
        replies=["Sure! Let me tell you about my day.", "   "],
        gate=gate,
    )
    created = await harness.character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )

    reply = await harness.service.send_message(
        SendChatMessageRequest(character_id=created.id, message="今天過得怎樣"),
    )

    assert reply.assistant_message is not None
    assert reply.assistant_message.content == "Sure! Let me tell you about my day."
    assert len(harness.model.calls) == 2  # it did try
    assert harness.counters.total("chat", OUTCOME_HARD_PUBLISHED_BEST_EFFORT) == 1


@pytest.mark.asyncio
async def test_turn_record_carries_the_hard_axes_and_the_disposal() -> None:
    turn_records = InMemoryTurnRecordRepository()
    turn_recorder = BackgroundTurnRecorder(turn_records)
    gate = _Gate([NoveltyVerdict(
        passes=False,
        visible_truncation=True,
        tool_prompt_defect=True,
        feedback="句子斷在半路。",
    )])
    harness = _build_service(
        replies=["前半句就斷在這裡然", "重寫過的完整一句話。"],
        gate=gate,
        turn_records=turn_records,
        turn_recorder=turn_recorder,
    )
    created = await harness.character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )

    await harness.service.send_message(
        SendChatMessageRequest(character_id=created.id, message="今天過得怎樣"),
    )
    await turn_recorder.flush()

    records = await turn_records.list_recent(character_id=created.id)
    refs = next(r for r in records if r.kind == "chat").post_turn_refs["novelty_gate"]
    assert refs["visible_truncation"] is True
    assert refs["tool_prompt_defect"] is True
    assert refs["structural_leak"] is False
    assert refs["language_mismatch"] is False
    assert refs["hard_fail"] is True
    assert refs["outcome"] == OUTCOME_HARD_PUBLISHED_BEST_EFFORT
    assert refs["retry_count"] == 1


@pytest.mark.asyncio
async def test_a_clean_turn_records_the_pass_outcome() -> None:
    turn_records = InMemoryTurnRecordRepository()
    turn_recorder = BackgroundTurnRecorder(turn_records)
    harness = _build_service(
        replies=["今天過得很普通。"],
        gate=_Gate([NoveltyVerdict(passes=True)]),
        turn_records=turn_records,
        turn_recorder=turn_recorder,
    )
    created = await harness.character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )

    await harness.service.send_message(
        SendChatMessageRequest(character_id=created.id, message="今天過得怎樣"),
    )
    await turn_recorder.flush()

    records = await turn_records.list_recent(character_id=created.id)
    refs = next(r for r in records if r.kind == "chat").post_turn_refs["novelty_gate"]
    assert refs["outcome"] == OUTCOME_PASS
    assert refs["hard_fail"] is False
    assert refs["retry_count"] == 0
    assert harness.counters.total("chat", OUTCOME_PASS) == 1


# -- what the judge is shown ----------------------------------------------


@pytest.mark.asyncio
async def test_gate_context_carries_operator_language_and_script_mix_evidence() -> None:
    """The judge cannot rule on ``language_mismatch`` without both.

    The language label is its *sole* reference for that axis — with an
    empty one the rubric leaves the axis false rather than guessing — and
    the mix summary is the deterministic fact it weighs against the draft.
    Both are read off things chat had already loaded for this turn."""
    gate = _Gate([NoveltyVerdict(passes=True), NoveltyVerdict(passes=True)])
    harness = _build_service(
        replies=[
            "今天的 meeting 超 exhausting，but the coffee was really good",
            "第二則回覆。",
        ],
        gate=gate,
        operator_profile=OperatorProfile(
            id="u1", display_name="Alex", primary_language="zh-TW",
        ),
    )
    created = await harness.character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )
    request = SendChatMessageRequest(character_id=created.id, message="今天過得怎樣")

    await harness.service.send_message(request)
    await harness.service.send_message(
        SendChatMessageRequest(character_id=created.id, message="然後呢"),
    )

    first, second = gate.calls
    assert first.operator_primary_language == "zh-TW"
    # Turn 1 had no prior assistant message to count.
    assert first.mechanical_evidence_lines == ()
    # Turn 2 sees turn 1's mixed-script reply, and sees it as *evidence* —
    # the axis is still the judge's to fire.
    assert second.mechanical_evidence_lines
    assert any("拉丁字母" in line for line in second.mechanical_evidence_lines)
    assert (
        second.mechanical_evidence_lines
        == second.diversity_evidence.language_mix_lines
    )
