"""Unit tests for LLMProactiveDecider.

We stub the ``ChatModelPort`` directly so the tests don't hit any real
LLM. Focus areas:

* happy paths (should_send true / false)
* tolerant JSON parsing (code fences, preambles)
* failure modes (unparseable, LLM raises, message missing / too long)
* prompt content carries the important signals (name, sent_today,
  idle_minutes, memories / goals when provided)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.persona_curiosity import PersonaCuriosityPlan
from kokoro_link.contracts.proactive import ProactiveContext
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.personality_type import CharacterPersonalityType
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.proactive.llm_decider import LLMProactiveDecider


class _StubModel(ChatModelPort):
    def __init__(self, response: str, *, raise_on_call: Exception | None = None) -> None:
        self._response = response
        self._raise = raise_on_call
        self.captured_prompt: str | None = None

    async def generate(self, prompt: str) -> str:
        self.captured_prompt = prompt
        if self._raise is not None:
            raise self._raise
        return self._response

    async def generate_stream(self, prompt: str) -> AsyncIterator[str]:  # pragma: no cover
        yield self._response


def _context(
    *,
    sent_today: int = 0,
    idle_minutes: float | None = 90.0,
    recent_memories_text: str = "",
    active_goals_text: str = "",
    last_proactive_at: datetime | None = None,
    recent_sent_attempts: tuple = (),
    unanswered_streak: int = 0,
    operator_persona_lines: tuple[str, ...] = (),
    world_event_seed_title: str = "",
    world_event_seed_summary: str = "",
    world_event_seed_locale: str = "",
    operator_location_context: str = "",
    persona_curiosity_plan: PersonaCuriosityPlan | None = None,
    initial_relationship_lines: tuple[str, ...] = (),
    personality_type: CharacterPersonalityType | None = None,
    now: datetime | None = None,
) -> ProactiveContext:
    character = Character.create(
        name="Mio",
        summary="一個在咖啡店打工的大學生。",
        personality=["溫柔", "害羞", "喜歡音樂"],
        interests=["吉他", "咖啡"],
        speaking_style="輕柔自然、偶爾用表情符號",
        boundaries=["不談政治"],
        personality_type=personality_type or CharacterPersonalityType.DEFAULT,
        state=CharacterState(
            emotion="平靜",
            affection=65,
            fatigue=20,
            trust=70,
            energy=80,
        ),
        proactive_enabled=True,
    )
    return ProactiveContext(
        character=character,
        trigger=ProactiveTrigger.TICK,
        now=now or datetime(2026, 4, 18, 14, 30, tzinfo=timezone.utc),
        current_activity=None,
        upcoming_activities=[],
        schedule=None,
        idle_minutes=idle_minutes,
        sent_today=sent_today,
        last_proactive_at=last_proactive_at,
        recent_memories_text=recent_memories_text,
        active_goals_text=active_goals_text,
        recent_sent_attempts=recent_sent_attempts,
        unanswered_streak=unanswered_streak,
        operator_persona_lines=operator_persona_lines,
        initial_relationship_lines=initial_relationship_lines,
        world_event_seed_title=world_event_seed_title,
        world_event_seed_summary=world_event_seed_summary,
        world_event_seed_locale=world_event_seed_locale,
        operator_location_context=operator_location_context,
        persona_curiosity_plan=persona_curiosity_plan,
    )


@pytest.mark.asyncio
async def test_should_send_true_returns_trimmed_message() -> None:
    model = _StubModel(
        '{"should_send": true, "reason": "想分享剛練完的曲子", '
        '"message": "剛練完那首歌，想傳一段給你聽 🎸"}',
    )
    decider = LLMProactiveDecider(model=model)
    decision = await decider.decide(_context())
    assert decision.should_send is True
    assert decision.message == "剛練完那首歌，想傳一段給你聽 🎸"
    assert "分享" in decision.reason


@pytest.mark.asyncio
async def test_should_send_false_sets_message_none() -> None:
    model = _StubModel(
        '{"should_send": false, "reason": "沒什麼特別想講的", "message": null}',
    )
    decider = LLMProactiveDecider(model=model)
    decision = await decider.decide(_context())
    assert decision.should_send is False
    assert decision.message is None


@pytest.mark.asyncio
async def test_prompt_includes_operator_persona_lines() -> None:
    model = _StubModel(
        '{"should_send": false, "reason": "no need", "message": null}',
    )
    decider = LLMProactiveDecider(model=model)
    await decider.decide(
        _context(operator_persona_lines=("- 對方資料：興趣是爵士樂。",)),
    )

    assert model.captured_prompt is not None
    assert "興趣是爵士樂" in model.captured_prompt
    assert "不要每次都主動提起" in model.captured_prompt


@pytest.mark.asyncio
async def test_prompt_includes_initial_relationship_boundaries() -> None:
    model = _StubModel(
        '{"should_send": false, "reason": "no need", "message": null}',
    )
    decider = LLMProactiveDecider(model=model)

    await decider.decide(
        _context(
            initial_relationship_lines=(
                "使用者創角時確認的起始關係設定：",
                "- 關係：剛認識但允許主動打招呼",
                "- 稱呼使用者：小夏",
                "- 未提供的共同經歷不得補完",
            ),
        ),
    )

    assert model.captured_prompt is not None
    assert "剛認識但允許主動打招呼" in model.captured_prompt
    assert "小夏" in model.captured_prompt
    assert "不可說成你們已經在系統內聊過" in model.captured_prompt


@pytest.mark.asyncio
async def test_prompt_includes_personality_type_without_engineering_fields() -> None:
    model = _StubModel(
        '{"should_send": false, "reason": "no need", "message": null}',
    )
    decider = LLMProactiveDecider(model=model)

    await decider.decide(
        _context(
            personality_type=CharacterPersonalityType(
                code="INFP",
                source="llm_inferred",
                confidence=0.77,
                rationale="重視內在價值與柔軟表達。",
                consistency_notes=("不要蓋過具體說話風格。",),
            ),
        ),
    )

    assert model.captured_prompt is not None
    assert "16 型性格參考" in model.captured_prompt
    assert "INFP" in model.captured_prompt
    assert "重視內在價值" in model.captured_prompt
    assert "confidence" not in model.captured_prompt
    assert "personality_type_json" not in model.captured_prompt


@pytest.mark.asyncio
async def test_prompt_includes_persona_curiosity_plan_with_proactive_restraint() -> None:
    model = _StubModel(
        '{"should_send": false, "reason": "no need", "message": null}',
    )
    decider = LLMProactiveDecider(model=model)
    await decider.decide(
        _context(
            unanswered_streak=3,
            persona_curiosity_plan=PersonaCuriosityPlan(
                should_ask=True,
                target_layer=2,
                target_topic="companion_preference",
                tone_strategy="低壓、像想更懂朋友",
                question_intent="自然了解對方希望角色怎麼陪伴",
                safety_reason="只碰低壓偏好",
                avoid=("不要像問卷", "不要提資料蒐集"),
            ),
        ),
    )

    assert model.captured_prompt is not None
    assert "自然認識對方的候選意圖" in model.captured_prompt
    assert "companion_preference" in model.captured_prompt
    assert "主動訊息要比聊天更克制" in model.captured_prompt
    assert "連續未回覆" in model.captured_prompt
    assert "最多一個輕問題" in model.captured_prompt


@pytest.mark.asyncio
async def test_parses_json_inside_code_fence_with_preamble() -> None:
    noisy = (
        "好的，我的判斷如下：\n"
        "```json\n"
        '{"should_send": true, "reason": "想打招呼", "message": "嗨"}\n'
        "```"
    )
    decider = LLMProactiveDecider(model=_StubModel(noisy))
    decision = await decider.decide(_context())
    assert decision.should_send is True
    assert decision.message == "嗨"


@pytest.mark.asyncio
async def test_unparseable_output_becomes_skip_with_reason() -> None:
    decider = LLMProactiveDecider(model=_StubModel("我今天想說的就這樣。"))
    decision = await decider.decide(_context())
    assert decision.should_send is False
    assert "no JSON object" in decision.reason


@pytest.mark.asyncio
async def test_invalid_json_becomes_skip_with_reason() -> None:
    decider = LLMProactiveDecider(
        model=_StubModel('{"should_send": true, reason: bad}'),
    )
    decision = await decider.decide(_context())
    assert decision.should_send is False
    assert "unparseable" in decision.reason


@pytest.mark.asyncio
async def test_should_send_true_without_message_is_demoted_to_skip() -> None:
    decider = LLMProactiveDecider(
        model=_StubModel(
            '{"should_send": true, "reason": "ok", "message": ""}',
        ),
    )
    decision = await decider.decide(_context())
    assert decision.should_send is False
    assert "no message" in decision.reason


@pytest.mark.asyncio
async def test_message_is_truncated_when_too_long() -> None:
    long_text = "喵" * 500
    decider = LLMProactiveDecider(
        model=_StubModel(
            '{"should_send": true, "reason": "ok", "message": "' + long_text + '"}',
        ),
        max_message_chars=50,
    )
    decision = await decider.decide(_context())
    assert decision.should_send is True
    assert decision.message is not None
    # truncated + ellipsis marker
    assert len(decision.message) <= 51
    assert decision.message.endswith("…")


@pytest.mark.asyncio
async def test_model_exception_is_caught() -> None:
    decider = LLMProactiveDecider(
        model=_StubModel("", raise_on_call=RuntimeError("timeout")),
    )
    decision = await decider.decide(_context())
    assert decision.should_send is False
    assert "RuntimeError" in decision.reason


@pytest.mark.asyncio
async def test_prompt_carries_identity_and_signals() -> None:
    model = _StubModel(
        '{"should_send": false, "reason": "checked", "message": null}',
    )
    decider = LLMProactiveDecider(model=model)
    await decider.decide(
        _context(
            sent_today=2,
            idle_minutes=120.0,
            recent_memories_text="- [semantic] 使用者今天去了咖啡店",
            active_goals_text="- 練好那首新歌（優先 3）",
            last_proactive_at=datetime(2026, 4, 18, 13, 0, tzinfo=timezone.utc),
        ),
    )
    prompt = model.captured_prompt or ""
    assert "Mio" in prompt
    assert "溫柔" in prompt
    assert "輕柔自然" in prompt  # speaking style
    assert "使用者今天去了咖啡店" in prompt
    assert "練好那首新歌" in prompt
    assert "已主動開口 2 次" in prompt
    assert "2.0 小時前" in prompt  # idle hours formatting
    assert "tick" in prompt  # trigger value


@pytest.mark.asyncio
async def test_prompt_has_role_knowledge_boundary_for_user_related_events() -> None:
    model = _StubModel(
        '{"should_send": false, "reason": "checked", "message": null}',
    )
    decider = LLMProactiveDecider(model=model)
    await decider.decide(
        _context(
            operator_persona_lines=("- 職業：後端工程師",),
            world_event_seed_title="Cloudflare 大規模故障",
            world_event_seed_summary="多個網站與 API 服務回報異常。",
        ),
    )

    prompt = model.captured_prompt or ""
    assert "認知範圍與誠實表達" in prompt
    assert "不要假裝專家" in prompt
    assert "主要是因為對方可能在意" in prompt
    assert "Cloudflare" in prompt


@pytest.mark.asyncio
async def test_prompt_includes_world_event_source_locale_and_user_location() -> None:
    model = _StubModel(
        '{"should_send": false, "reason": "checked", "message": null}',
    )
    decider = LLMProactiveDecider(model=model)

    await decider.decide(
        _context(
            world_event_seed_title="NCDR 颱風示警",
            world_event_seed_summary="台灣發布強風豪雨警戒。",
            world_event_seed_locale="zh-TW",
            operator_location_context="使用者所在地：San Francisco / US",
        ),
    )

    prompt = model.captured_prompt or ""
    assert "來源地區：zh-TW" in prompt
    assert "使用者所在地：San Francisco / US" in prompt
    assert "NCDR 颱風示警" in prompt


@pytest.mark.asyncio
async def test_prompt_surfaces_recent_sent_messages_and_reply_state() -> None:
    """The decider must see its own recent sends verbatim — without this
    it paraphrases the same opener every cooldown. Prompt should also
    flag whether the user replied to each, so the model can back off
    when its last message went unanswered."""
    from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
    from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome

    now = datetime(2026, 4, 21, 0, 5, tzinfo=timezone.utc)
    # User's last message was 45 minutes ago (idle_minutes=45 below).
    #
    # Proactive A (recent, 30 min ago): user spoke BEFORE this proactive,
    # so they haven't replied to it yet → "對方還沒回".
    thirty_min_ago = ProactiveAttempt.record(
        character_id="c",
        trigger=ProactiveTrigger.TICK,
        outcome=ProactiveOutcome.SENT,
        reason="",
        message="今天練琴練到手快斷了 QQ",
        now=now - timedelta(minutes=30),
    )
    # Proactive B (older, 61 min ago): user spoke AFTER this proactive
    # (at 45 min ago > 61 min ago in reverse-time sense), so they did
    # reply to it → "對方已回".
    one_hour_ago = ProactiveAttempt.record(
        character_id="c",
        trigger=ProactiveTrigger.TICK,
        outcome=ProactiveOutcome.SENT,
        reason="",
        message="剛剛下班路過咖啡店，看到一個穿哥德蘿莉裝的客人",
        now=now - timedelta(minutes=61),
    )

    model = _StubModel('{"should_send": false, "reason": "cooldown vibe"}')
    decider = LLMProactiveDecider(model=model)
    ctx = _context(
        idle_minutes=45.0,
        last_proactive_at=now - timedelta(minutes=30),
        recent_sent_attempts=(thirty_min_ago, one_hour_ago),  # newest first
        now=now,
    )
    await decider.decide(ctx)
    prompt = model.captured_prompt or ""

    # Both messages must appear verbatim so the LLM can avoid repeating.
    assert "咖啡店" in prompt
    assert "練琴" in prompt
    # Most recent one the user already replied to — tag must say so.
    assert "（對方已回）" in prompt
    # Older one the user never replied to — tag must say so.
    assert "（對方還沒回）" in prompt
    # Hard rule 2 about not re-using story event across the same day
    # should be in the instructions block.
    assert "不要再為同一題材發第二則" in prompt


@pytest.mark.asyncio
async def test_prompt_surfaces_unanswered_streak_when_high() -> None:
    """A run of ignored pushes must surface as its own fact so the
    character can *evolve* (worry / sulk / give space) instead of
    re-deriving the same opener every day (the 跳針 bug)."""
    model = _StubModel('{"should_send": false, "reason": "give space"}')
    decider = LLMProactiveDecider(model=model)
    await decider.decide(_context(unanswered_streak=3))
    prompt = model.captured_prompt or ""
    assert "連續主動傳了 3 則" in prompt
    # Licence to let it land emotionally, plus the anti-parrot guard.
    assert "賭氣" in prompt or "受傷" in prompt
    assert "換句話重講" in prompt


@pytest.mark.asyncio
async def test_prompt_omits_streak_block_when_not_a_run() -> None:
    model = _StubModel('{"should_send": false, "reason": "ok"}')
    decider = LLMProactiveDecider(model=model)
    await decider.decide(_context(unanswered_streak=1))
    prompt = model.captured_prompt or ""
    assert "連續未獲回應" not in prompt


@pytest.mark.asyncio
async def test_instructions_allow_evolution_not_just_silence() -> None:
    """Rule 3 was rewritten: being ignored no longer means 'just stay
    silent' — it permits a persona-driven emotional progression while
    still forbidding 跳針."""
    model = _StubModel('{"should_send": false, "reason": "ok"}')
    decider = LLMProactiveDecider(model=model)
    await decider.decide(_context())
    prompt = model.captured_prompt or ""
    assert "跳針" in prompt
