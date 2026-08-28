"""G2 — how a reunion push is framed, and what the judge is told about it.

Three code-side prompt facts, all of them injected in Python rather than
in ``data/prompts/``: the baseline pack is hashed into
``prompt-packs/baseline.lock.json`` and shadowed by a hosted tuned
overlay, so a wording change there is a prompt-pack release, not a code
change.

1. ``ADMIN_REACTIVATION`` gets its own framing block in the decider and
   its own relaxed-but-not-removed bar in the intention judge. Every
   other trigger must render byte-identically to before — the control
   assertions here are as load-bearing as the positive ones.
2. The dialogue-summary heading stops claiming the thread is live once
   the idle gap crosses ``_STALE_DIALOGUE_IDLE_HOURS``. This is not
   reactivation-specific: a two-month-old TICK push has the same defect.
3. The quality gate's 時間座標 anchor for 「玩家最後一次說話」 carries the
   player's actual words **dated by the turn they came out of**, so the
   judge can tell a concern that expired with the gap from one that did
   not — and cannot be handed a fresh timestamp on stale words. 「說話」
   and 「互動」 are separate anchors because ``last_active_at`` (the only
   input to ``idle_minutes``) is advanced in cloud mode by 分歧劇場／起幕／
   融合故事, none of which write a ``USER`` message.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from kokoro_link.application.services.proactive_dispatcher import (
    ProactiveDispatcher,
    _RecentDialogue,
    _last_player_turn,
    _proactive_temporal_lines,
)
from kokoro_link.contracts.proactive import ProactiveContext, ProactiveDecision
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import (
    Message,
    MessageRole,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.proactive.llm_decider import (
    _STALE_DIALOGUE_IDLE_HOURS,
    _build_prompt as _build_decider_prompt,
)
from kokoro_link.infrastructure.proactive.llm_intention_judge import (
    _build_prompt as _build_judge_prompt,
)

TPE = ZoneInfo("Asia/Taipei")
NOW = datetime(2026, 8, 28, 10, 30, tzinfo=TPE)

#: Every trigger that must keep rendering exactly as it did before G2.
OTHER_TRIGGERS = (
    ProactiveTrigger.TICK,
    ProactiveTrigger.POST_TURN,
    ProactiveTrigger.ACTIVITY_TRANSITION,
    ProactiveTrigger.ARC_BEAT,
    ProactiveTrigger.PENDING_FOLLOW_UP,
    ProactiveTrigger.SCHEDULED_PROMISE,
)

#: Phrases that only the reactivation framing may put in either prompt.
_DECIDER_FRAMING_MARKERS = (
    "久違地重新聯繫",
    "突然想起這個人",
    "不要假裝知道",
    "被安排來找你",
)
_JUDGE_FRAMING_MARKERS = (
    "久別重逢的重新聯繫",
    "判準放寬",
    "放寬不是取消",
)


def _character() -> Character:
    return replace(
        Character.create(
            name="Mio",
            summary="咖啡店打工的大學生。",
            personality=["溫柔"],
            interests=["吉他"],
            speaking_style="輕柔自然",
            boundaries=[],
            state=CharacterState(
                emotion="平靜", affection=60, fatigue=20, trust=65, energy=75,
            ),
            proactive_enabled=True,
        ),
        id="char-1",
        user_id="op-1",
    )


def _context(**overrides) -> ProactiveContext:
    base = dict(
        character=_character(),
        trigger=ProactiveTrigger.TICK,
        now=NOW,
        current_activity=None,
        upcoming_activities=[],
        schedule=None,
        idle_minutes=90.0,
        sent_today=0,
        last_proactive_at=None,
        local_tz=TPE,
    )
    base.update(overrides)
    return ProactiveContext(**base)


# -- 1a. decider framing -----------------------------------------------


def test_decider_frames_a_reactivation_push_as_the_characters_own_impulse() -> None:
    prompt = _build_decider_prompt(
        _context(trigger=ProactiveTrigger.ADMIN_REACTIVATION),
    )

    for marker in _DECIDER_FRAMING_MARKERS:
        assert marker in prompt
    # catch-up first, and no pretending to know what happened meanwhile.
    assert "catch-up 優先" in prompt


def test_decider_forbids_naming_the_console_that_started_the_push() -> None:
    """The operator picked this character off a list; the character did
    not. Every operational concept is named, including the raw trigger
    code the 互動近況 block prints two lines above."""
    prompt = _build_decider_prompt(
        _context(trigger=ProactiveTrigger.ADMIN_REACTIVATION),
    )

    ban = prompt.split("絕對不要", 1)[1]
    for banned in ("系統", "後台", "營運", "名單", "推播"):
        assert banned in ban
    assert "英文代號只是內部標記" in prompt


def test_the_ban_is_on_the_operational_framing_not_on_everyday_words() -> None:
    """「最近有沒有什麼活動」 is the most natural catch-up question there
    is. An earlier draft listed 活動 / 通知 as banned *words* alongside
    系統 / 後台 / 營運 and suppressed exactly the opening this framing asks
    for; the ban has to be stated as a meaning, not a vocabulary."""
    prompt = _build_decider_prompt(
        _context(trigger=ProactiveTrigger.ADMIN_REACTIVATION),
    )

    ban = prompt.split("絕對不要", 1)[1]
    # The forbidden thing is the framing, spelled out as one.
    assert "被安排來找你" in ban
    assert "描述成被系統、後台或營運安排" in ban
    # …and the two everyday words are carved out rather than listed.
    assert "「活動」「通知」不在此限" in ban


@pytest.mark.parametrize("trigger", OTHER_TRIGGERS)
def test_decider_renders_no_framing_block_for_other_triggers(
    trigger: ProactiveTrigger,
) -> None:
    prompt = _build_decider_prompt(_context(trigger=trigger))

    for marker in _DECIDER_FRAMING_MARKERS:
        assert marker not in prompt


def test_decider_prompt_for_other_triggers_is_unchanged_by_the_new_block() -> None:
    """Control: the reactivation block is *additive*. A TICK prompt and a
    reactivation prompt must differ only by that block."""
    tick = _build_decider_prompt(_context(trigger=ProactiveTrigger.TICK))
    reactivation = _build_decider_prompt(
        _context(trigger=ProactiveTrigger.ADMIN_REACTIVATION),
    )

    # Same sections, same order; the reactivation one has exactly one more.
    tick_sections = tick.split("\n\n")
    reactivation_sections = reactivation.split("\n\n")
    assert len(reactivation_sections) == len(tick_sections) + 1
    extra = [s for s in reactivation_sections if s not in tick_sections]
    # The trigger line itself differs too, so two blocks are new: the
    # 互動近況 section and the framing. Nothing else may move.
    assert len(extra) == 2
    assert any("久違地重新聯繫" in section for section in extra)


# -- 1b. intention judge framing ---------------------------------------


def test_judge_relaxes_the_bar_for_a_reunion_without_removing_the_skip() -> None:
    prompt = _build_judge_prompt(
        _context(trigger=ProactiveTrigger.ADMIN_REACTIVATION),
    )

    for marker in _JUDGE_FRAMING_MARKERS:
        assert marker in prompt
    # The skip verdict has to survive the relaxation — D3 keeps the four
    # semantic gates on precisely so an empty recall message is stopped.
    assert "仍然應該 skip" in prompt


@pytest.mark.parametrize("trigger", OTHER_TRIGGERS)
def test_judge_renders_no_framing_block_for_other_triggers(
    trigger: ProactiveTrigger,
) -> None:
    prompt = _build_judge_prompt(_context(trigger=trigger))

    for marker in _JUDGE_FRAMING_MARKERS:
        assert marker not in prompt


def test_judge_framing_rides_the_interaction_block_next_to_the_trigger() -> None:
    """Placement is the contract: the block qualifies the trigger line, so
    it must sit with it rather than drift to the end of the prompt (and it
    must not need a new template placeholder, which would fork the hosted
    tuned overlay)."""
    prompt = _build_judge_prompt(
        _context(trigger=ProactiveTrigger.ADMIN_REACTIVATION),
    )

    trigger_at = prompt.index("觸發來源：admin_reactivation")
    framing_at = prompt.index("久別重逢的重新聯繫")
    assert trigger_at < framing_at
    assert framing_at - trigger_at < 200


# -- 2. dialogue-summary heading ---------------------------------------


_SUMMARY = "對方說在準備一場面試，有點緊張。"

#: The live heading *with its hint* — the exact opening the stale variant
#: replaces. Matching on the bare heading would false-positive on the
#: pointer the stale heading deliberately keeps (``decider_instructions``
#: is a baseline pack file and still refers to the block by that name).
_LIVE_HEADING = "最近你和對方正在聊的事（請避免再主動提同一件事"


def _summary_context(idle_hours: float | None, **overrides) -> ProactiveContext:
    return _context(
        idle_minutes=None if idle_hours is None else idle_hours * 60.0,
        recent_dialogue_summary=_SUMMARY,
        **overrides,
    )


def test_live_thread_keeps_the_in_progress_wording() -> None:
    prompt = _build_decider_prompt(_summary_context(3.0))

    assert _LIVE_HEADING in prompt
    assert "你們上次聊到的事" not in prompt


def test_a_stale_thread_is_headed_as_history_not_as_a_live_topic() -> None:
    prompt = _build_decider_prompt(
        _summary_context(_STALE_DIALOGUE_IDLE_HOURS + 1.0),
    )

    assert "你們上次聊到的事" in prompt
    assert "已經隔了一段時間" in prompt
    assert "當作 catch-up 的引子" in prompt
    assert _LIVE_HEADING not in prompt
    # The material itself is unchanged — only how it is introduced.
    assert _SUMMARY in prompt
    # …and the instructions file's quoted reference still resolves.
    assert "下方指示裡說的「最近你和對方正在聊的事」就是這一段" in prompt


def test_the_stale_heading_switches_exactly_at_the_threshold() -> None:
    """A named constant nobody pins drifts. Both sides asserted so a
    future change to the number has to be deliberate."""
    just_under = _build_decider_prompt(
        _summary_context(_STALE_DIALOGUE_IDLE_HOURS - 0.1),
    )
    exactly_at = _build_decider_prompt(
        _summary_context(_STALE_DIALOGUE_IDLE_HOURS),
    )

    assert _LIVE_HEADING in just_under
    assert "你們上次聊到的事" in exactly_at


def test_no_prior_conversation_is_not_treated_as_a_stale_thread() -> None:
    """``idle_minutes=None`` means the pair never spoke. "Never" is not
    "long ago", and claiming 「你們上次聊到的事」 would invent a history."""
    prompt = _build_decider_prompt(_summary_context(None))

    assert "你們上次聊到的事" not in prompt


@pytest.mark.parametrize(
    "trigger", (*OTHER_TRIGGERS, ProactiveTrigger.ADMIN_REACTIVATION),
)
def test_the_stale_heading_applies_to_every_trigger(
    trigger: ProactiveTrigger,
) -> None:
    """Gating this on the reactivation trigger would fix the rare case and
    leave the common one — a long-idle TICK push reads the same summary."""
    prompt = _build_decider_prompt(
        _summary_context(_STALE_DIALOGUE_IDLE_HOURS + 24.0, trigger=trigger),
    )

    assert "你們上次聊到的事" in prompt


# -- 3. the player-quote in the 時間座標 anchor -------------------------


def _joined(lines: tuple[str, ...]) -> str:
    return "\n".join(lines)


_SPOKE = "玩家最後一次說話"
_INTERACTED = "玩家最後一次互動（不一定是說話）"


def _speech_line(lines: tuple[str, ...]) -> str:
    return next((line for line in lines if line.startswith(_SPOKE + "「")), "")


def _interaction_line(lines: tuple[str, ...]) -> str:
    return next((line for line in lines if line.startswith(_INTERACTED)), "")


def test_the_player_anchor_carries_what_the_player_actually_said() -> None:
    lines = _proactive_temporal_lines(
        _context(idle_minutes=16 * 60.0),
        last_player_message="我要回家了",
        last_player_at=NOW - timedelta(hours=16),
    )

    assert "玩家最後一次說話「我要回家了」" in _joined(lines)
    assert "約 16 小時前（1 天前）" in _joined(lines)


def test_the_quote_is_dated_by_its_own_turn_not_by_the_idle_reading() -> None:
    """The defect this pairing exists to prevent.

    ``idle_minutes`` comes off ``last_active_at``, which in cloud mode is
    also advanced by 分歧劇場／起幕／融合故事 — surfaces that never write a
    ``USER`` message. Dating the quote from it stamped a three-day-old
    「我要回家了」 as 「約 5 分鐘前」 and handed the
    ``temporal_inconsistency`` axis manufactured evidence that 「回家了嗎？」
    was still timely.
    """
    lines = _proactive_temporal_lines(
        # Drama press five minutes ago; the last thing actually *said* was
        # three days back.
        _context(idle_minutes=5.0),
        last_player_message="我要回家了",
        last_player_at=NOW - timedelta(days=3),
    )

    speech = _speech_line(lines)
    assert "我要回家了" in speech
    assert "3 天前" in speech
    assert "分鐘前" not in speech
    # …and the idle reading is still reported, as the different fact it is.
    assert "分鐘前" in _interaction_line(lines)


def test_the_interaction_anchor_never_claims_the_player_spoke() -> None:
    """Its instant is ``last_active_at``, so its label may not say 說話 —
    the whole point is that a drama press is an interaction and not a
    line of dialogue."""
    lines = _proactive_temporal_lines(_context(idle_minutes=5.0))

    interaction = _interaction_line(lines)
    assert interaction
    assert not interaction.startswith(_SPOKE + "：")
    # Never quoted: there are no words behind this instant to quote.
    assert "「" not in interaction


def test_the_speech_anchor_is_dropped_rather_than_left_undated() -> None:
    """No summariser wired, load failed, the last turn was the
    character's — the quote goes away with its timestamp instead of
    borrowing the idle reading's."""
    lines = _proactive_temporal_lines(_context(idle_minutes=16 * 60.0))

    joined = _joined(lines)
    assert _SPOKE + "「" not in joined
    assert _SPOKE + "：" not in joined
    assert _INTERACTED in joined


def test_a_quote_without_its_own_timestamp_is_not_dated_from_idle() -> None:
    """The theoretical path — a turn that reached us with no
    ``created_at``. Losing the quote is correct; re-deriving its instant
    from ``idle_minutes`` is the bug."""
    lines = _proactive_temporal_lines(
        _context(idle_minutes=16 * 60.0), last_player_message="我要回家了",
    )

    assert "我要回家了" not in _joined(lines)


def test_the_interaction_anchor_is_omitted_when_there_was_never_any() -> None:
    """``idle_minutes=None`` means the pair never interacted at all."""
    lines = _proactive_temporal_lines(_context(idle_minutes=None))

    assert _INTERACTED not in _joined(lines)
    assert len(lines) == 1  # 現在 only


def test_a_long_player_turn_is_clipped_rather_than_pasted_whole() -> None:
    lines = _proactive_temporal_lines(
        _context(idle_minutes=60.0),
        last_player_message="唉" * 400,
        last_player_at=NOW - timedelta(hours=1),
    )

    # ``quoted_event`` owns the limit; what matters here is that this
    # surface goes through it instead of inlining the raw turn.
    assert len(_joined(lines)) < 400


def test_last_player_turn_picks_the_newest_non_empty_user_turn() -> None:
    wanted = Message(role=MessageRole.USER, content="我要回家了")
    messages = [
        Message(role=MessageRole.USER, content="舊的一句"),
        wanted,
        Message(role=MessageRole.ASSISTANT, content="路上小心"),
        Message(role=MessageRole.USER, content="   "),
    ]

    assert _last_player_turn(messages) is wanted


def test_last_player_turn_ignores_turns_the_player_did_not_write() -> None:
    messages = [
        Message(role=MessageRole.ASSISTANT, content="在忙嗎"),
        Message(role=MessageRole.SYSTEM, content="系統通知"),
    ]

    assert _last_player_turn(messages) is None


def test_last_player_turn_returns_the_newest_turn_even_if_it_is_undated() -> None:
    """An undated newest turn must cost the block its quote, not silently
    promote an older line into the 「最後一次說話」 slot."""
    messages = [
        Message(role=MessageRole.USER, content="舊的一句"),
        replace(
            Message(role=MessageRole.USER, content="最新的一句"),
            created_at=None,
        ),
    ]

    turn = _last_player_turn(messages)
    assert turn is not None
    assert turn.content == "最新的一句"


def test_gate_context_threads_the_quote_with_its_own_instant() -> None:
    """The wiring, not the rendering: a quote nobody passes down is a
    quote the judge never sees — and one passed down without its instant
    is the defect all over again."""
    dispatcher = ProactiveDispatcher.__new__(ProactiveDispatcher)

    gate_context = dispatcher._proactive_gate_context(  # noqa: SLF001
        context=_context(idle_minutes=5.0),
        decision=ProactiveDecision(
            should_send=True, reason="r", message="回家了嗎？",
        ),
        character=_character(),
        register_profile=None,
        diversity_evidence=None,
        recent_dialogue=_RecentDialogue(
            summary="摘要",
            last_player_text="我要回家了",
            last_player_at=NOW - timedelta(days=3),
        ),
    )

    speech = _speech_line(gate_context.temporal_context_lines)
    assert "玩家最後一次說話「我要回家了」" in speech
    assert "3 天前" in speech


class _Summarizer:
    def __init__(self) -> None:
        self.calls = 0

    async def summarize(self, *, character, messages, now=None, local_tz=None):  # noqa: ANN001
        self.calls += 1
        return "摘要"


async def _dispatcher_over_one_conversation(
    *,
    said_at: datetime,
    last_active_at: datetime,
    summarizer: _Summarizer,
):
    """A real dispatcher over a two-turn conversation, wired end to end.

    ``said_at`` and ``last_active_at`` are deliberately independent knobs:
    the whole class of defect here is the two being conflated.
    """
    from kokoro_link.domain.entities.channel_binding import ChannelBinding
    from kokoro_link.domain.entities.conversation import Conversation
    from kokoro_link.domain.value_objects.platform import Platform
    from kokoro_link.infrastructure.proactive.heuristic_gate import (
        HeuristicProactiveGate,
    )
    from kokoro_link.infrastructure.repositories.in_memory_proactive_attempts import (
        InMemoryProactiveAttemptRepository,
    )
    from tests.unit._messaging_harness import (
        build_messaging_harness,
        create_character,
        create_telegram_account,
    )

    harness = build_messaging_harness()
    dto = await create_character(harness)
    character = await harness.character_repository.get(dto.id)
    assert character is not None
    character = character.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, aspirations=None,
        appearance=None,
        state=CharacterState(
            emotion="平靜", affection=50, fatigue=0, trust=60, energy=80,
            last_active_at=last_active_at,
        ),
        proactive_enabled=True,
    )
    await harness.character_repository.save(character)
    account = await create_telegram_account(harness, character_id=character.id)
    await harness.binding_repository.save(
        ChannelBinding.create(
            account_id=account.id, chat_ref="c1", accepts_proactive=True,
        ),
    )
    convo = Conversation.start(character_id=character.id)
    convo = convo.append(
        Message(
            role=MessageRole.USER, content="我要回家了", created_at=said_at,
        ),
    )
    convo = convo.append(
        Message(
            role=MessageRole.ASSISTANT,
            content="路上小心",
            created_at=said_at + timedelta(minutes=1),
        ),
    )
    await harness.conversation_repository.save(convo)

    dispatcher = ProactiveDispatcher(
        character_repository=harness.character_repository,
        conversation_repository=harness.conversation_repository,
        account_repository=harness.account_repository,
        binding_repository=harness.binding_repository,
        attempt_repository=InMemoryProactiveAttemptRepository(),
        gate=HeuristicProactiveGate(
            local_tz=timezone.utc, quiet_hour_start=0, quiet_hour_end=0,
        ),
        decider=None,
        adapters={
            Platform.TELEGRAM: harness.telegram_adapter,
            Platform.LINE: harness.line_adapter,
        },
        dialogue_summarizer=summarizer,
    )
    return dispatcher, character


@pytest.mark.asyncio
async def test_the_quote_comes_from_the_dialogue_load_the_tick_already_did(
) -> None:
    """One query, three products. Re-reading the conversation for the
    quote (or for its timestamp) would add a DB round-trip to every
    proactive tick."""
    now = datetime.now(timezone.utc)
    said_at = now - timedelta(hours=2)
    summarizer = _Summarizer()
    dispatcher, character = await _dispatcher_over_one_conversation(
        said_at=said_at, last_active_at=said_at, summarizer=summarizer,
    )

    dialogue = await dispatcher._summarize_recent_dialogue(  # noqa: SLF001
        character, now=now, local_tz=timezone.utc,
    )

    assert dialogue.last_player_text == "我要回家了"
    assert dialogue.last_player_at == said_at
    assert dialogue.summary.startswith("摘要")
    assert summarizer.calls == 1


@pytest.mark.asyncio
async def test_a_drama_press_moves_the_anchor_but_not_the_quotes_timestamp(
) -> None:
    """The reported scenario, end to end.

    The player last *spoke* three days ago and has been pressing 分歧劇場
    since, so ``last_active_at`` (hence ``idle_minutes``) reads minutes
    while the words the push is built out of are three days stale. The
    judge must be shown the words at their own age.
    """
    now = datetime.now(timezone.utc)
    said_at = now - timedelta(days=3)
    dispatcher, character = await _dispatcher_over_one_conversation(
        said_at=said_at,
        last_active_at=now - timedelta(minutes=5),
        summarizer=_Summarizer(),
    )

    dialogue = await dispatcher._summarize_recent_dialogue(  # noqa: SLF001
        character, now=now, local_tz=timezone.utc,
    )
    gate_context = dispatcher._proactive_gate_context(  # noqa: SLF001
        context=_context(now=now, local_tz=timezone.utc, idle_minutes=5.0),
        decision=ProactiveDecision(
            should_send=True, reason="r", message="回家了嗎？",
        ),
        character=character,
        register_profile=None,
        diversity_evidence=None,
        recent_dialogue=dialogue,
    )

    speech = _speech_line(gate_context.temporal_context_lines)
    assert "我要回家了" in speech
    assert "3 天前" in speech
    assert "分鐘前" not in speech
    # The drama press is still reported — as an interaction, not as speech.
    assert "分鐘前" in _interaction_line(gate_context.temporal_context_lines)


@pytest.mark.asyncio
async def test_no_summarizer_yields_no_quote_rather_than_a_second_query() -> None:
    dispatcher = ProactiveDispatcher.__new__(ProactiveDispatcher)
    dispatcher._dialogue_summarizer = None  # noqa: SLF001

    dialogue = await dispatcher._summarize_recent_dialogue(  # noqa: SLF001
        _character(), now=NOW, local_tz=TPE,
    )

    assert dialogue.summary == ""
    assert dialogue.last_player_text == ""
    assert dialogue.last_player_at is None
