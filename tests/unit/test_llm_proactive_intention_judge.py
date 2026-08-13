"""Unit tests for the proactive intention judge."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, datetime, timezone

import pytest

from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.persona_curiosity import PersonaCuriosityPlan
from kokoro_link.contracts.proactive import ProactiveContext
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import (
    OPERATOR_CONFIRMED_LAPSED_ROLE,
    OPERATOR_INVITE_EXPIRED_ROLE,
    DailySchedule,
    ScheduleActivity,
)
from kokoro_link.domain.value_objects.actor import ParticipantRef
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.personality_type import CharacterPersonalityType
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.proactive.llm_intention_judge import (
    LLMProactiveIntentionJudge,
)


class _StubModel(ChatModelPort):
    def __init__(self, response: str) -> None:
        self._response = response
        self.captured_prompt: str | None = None
        self.calls = 0

    async def generate(self, prompt: str) -> str:
        self.calls += 1
        self.captured_prompt = prompt
        return self._response

    async def generate_stream(
        self, prompt: str,
    ) -> AsyncIterator[str]:  # pragma: no cover
        yield self._response


def _context(
    *,
    trigger: ProactiveTrigger = ProactiveTrigger.TICK,
    sent_today: int = 1,
    idle_minutes: float | None = 180.0,
    unanswered_streak: int = 0,
    operator_persona_lines: tuple[str, ...] = (),
    world_event_seed_title: str = "",
    persona_curiosity_plan: PersonaCuriosityPlan | None = None,
    initial_relationship_lines: tuple[str, ...] = (),
    personality_type: CharacterPersonalityType | None = None,
    weather_context: str = "天氣：台北陰天，23 度。",
    upcoming_day_schedules: tuple = (),
) -> ProactiveContext:
    character = Character.create(
        name="Mio",
        summary="咖啡店打工的大學生。",
        personality=["溫柔", "容易想太多"],
        interests=["吉他", "咖啡"],
        speaking_style="輕柔自然",
        boundaries=[],
        personality_type=personality_type or CharacterPersonalityType.DEFAULT,
        state=CharacterState(
            emotion="有點想念",
            affection=65,
            fatigue=25,
            trust=70,
            energy=80,
        ),
        proactive_enabled=True,
        proactive_daily_limit=3,
    )
    return ProactiveContext(
        character=character,
        trigger=trigger,
        now=datetime(2026, 4, 18, 14, 30, tzinfo=timezone.utc),
        current_activity=None,
        upcoming_activities=[],
        schedule=None,
        idle_minutes=idle_minutes,
        sent_today=sent_today,
        unanswered_streak=unanswered_streak,
        last_proactive_at=None,
        weather_context=weather_context,
        recent_dialogue_summary="昨天對方說今天要去面試。",
        operator_persona_lines=operator_persona_lines,
        initial_relationship_lines=initial_relationship_lines,
        world_event_seed_title=world_event_seed_title,
        world_event_seed_summary="多個網站與 API 服務異常。",
        persona_curiosity_plan=persona_curiosity_plan,
        upcoming_day_schedules=upcoming_day_schedules,
    )


class _RaisingModel(ChatModelPort):
    async def generate(self, prompt: str) -> str:
        raise RuntimeError("upstream 502")

    async def generate_stream(
        self, prompt: str,
    ) -> AsyncIterator[str]:  # pragma: no cover
        raise RuntimeError("upstream 502")
        yield ""


@pytest.mark.parametrize(
    "model",
    [
        _RaisingModel(),
        _StubModel("模型今天只想聊天，沒有 JSON"),
        _StubModel('{"should_consume_slot": false, '),
        _StubModel("[1, 2, 3]"),
    ],
    ids=["llm-raised", "no-json", "unparseable", "not-an-object"],
)
@pytest.mark.asyncio
async def test_fail_soft_skips_are_flagged_as_unavailable(model) -> None:
    """F2-3 — every fail-soft path returns the same
    ``should_consume_slot=False`` a real "not now" verdict does. The
    dispatcher must be able to tell them apart *structurally* (it decides
    whether a spent revisit alarm is given back), so the flag lives on the
    decision rather than being inferred from the reason text."""
    decision = await LLMProactiveIntentionJudge(model=model).judge(_context())

    assert decision.should_consume_slot is False
    assert decision.judge_unavailable is True


@pytest.mark.asyncio
async def test_a_real_verdict_is_never_flagged_unavailable() -> None:
    model = _StubModel(
        '{"should_consume_slot": false, '
        '"inner_motive": "想關心面試", "risk": "太黏", '
        '"reason": "剛聊完，等晚一點"}',
    )

    decision = await LLMProactiveIntentionJudge(model=model).judge(_context())

    assert decision.should_consume_slot is False
    assert decision.judge_unavailable is False


@pytest.mark.asyncio
async def test_parses_positive_intention_json() -> None:
    model = _StubModel(
        '{"should_consume_slot": true, '
        '"inner_motive": "想到對方面試可能緊張", '
        '"conversation_purpose": "自然關心面試狀況", '
        '"expected_reply": "對方可以回面試如何", '
        '"risk": "低", "best_timing": "now", '
        '"reason": "有明確延續話題"}',
    )
    judge = LLMProactiveIntentionJudge(model=model)

    decision = await judge.judge(_context())

    assert decision.should_consume_slot is True
    assert "面試" in decision.inner_motive
    assert decision.best_timing == "now"
    assert "延續話題" in decision.reason


@pytest.mark.asyncio
async def test_parses_negative_intention_json_and_prompt_has_self_questions() -> None:
    model = _StubModel(
        '{"should_consume_slot": false, '
        '"inner_motive": "", "conversation_purpose": "", '
        '"expected_reply": "", "risk": "像天氣推播", '
        '"best_timing": "evening", "reason": "只有天氣素材"}',
    )
    judge = LLMProactiveIntentionJudge(model=model)

    decision = await judge.judge(_context())

    assert decision.should_consume_slot is False
    assert decision.risk == "像天氣推播"
    assert decision.best_timing == "evening"
    prompt = model.captured_prompt or ""
    assert "素材不是動機" in prompt
    assert "我希望對方怎麼回" in prompt
    assert "今日剩餘額度：2" in prompt
    assert "昨天對方說今天要去面試" in prompt


@pytest.mark.asyncio
async def test_prompt_includes_initial_relationship_and_mbti_boundaries() -> None:
    model = _StubModel(
        '{"should_consume_slot": false, '
        '"inner_motive": "", "conversation_purpose": "", '
        '"expected_reply": "", "risk": "too early", '
        '"best_timing": "later", "reason": "boundary"}',
    )
    judge = LLMProactiveIntentionJudge(model=model)

    await judge.judge(
        _context(
            initial_relationship_lines=(
                "使用者創角時確認的起始關係設定：",
                "- 關係：朋友",
                "- 主動訊息頻率或時機：一天最多一次，下午比較好",
            ),
            personality_type=CharacterPersonalityType(
                code="ENFP",
                source="user_explicit",
                confidence=1.0,
                rationale="外向、好奇，容易被新鮮事打動。",
            ),
        ),
    )

    prompt = model.captured_prompt or ""
    assert "朋友" in prompt
    assert "一天最多一次" in prompt
    assert "不可當成已發生過的系統內記憶" in prompt
    assert "不可假設對方當下狀態" in prompt
    assert "16 型性格參考" in prompt
    assert "ENFP" in prompt
    assert "confidence" not in prompt
    assert "personality_type_json" not in prompt


@pytest.mark.asyncio
async def test_prompt_surfaces_persona_curiosity_as_candidate_motive_only() -> None:
    model = _StubModel(
        '{"should_consume_slot": false, '
        '"inner_motive": "", "conversation_purpose": "", '
        '"expected_reply": "", "risk": "too soon", '
        '"best_timing": "later", "reason": "not enough motive"}',
    )
    judge = LLMProactiveIntentionJudge(model=model)

    await judge.judge(
        _context(
            persona_curiosity_plan=PersonaCuriosityPlan(
                should_ask=True,
                target_layer=2,
                target_topic="routine",
                tone_strategy="輕、不要像盤問",
                question_intent="想知道對方平常什麼時候比較有空",
                safety_reason="低壓生活節奏",
                avoid=("不要表單化",),
            ),
        ),
    )

    prompt = model.captured_prompt or ""
    assert "自然認識對方的候選意圖" in prompt
    assert "routine" in prompt
    assert "只是一個候選動機" in prompt
    assert "值得消耗今日主動訊息額度" in prompt


@pytest.mark.asyncio
async def test_promise_fulfilment_bypasses_llm() -> None:
    model = _StubModel('{"should_consume_slot": false, "reason": "no"}')
    judge = LLMProactiveIntentionJudge(model=model)

    decision = await judge.judge(
        _context(trigger=ProactiveTrigger.SCHEDULED_PROMISE),
    )

    assert decision.should_consume_slot is True
    assert "promise fulfilment" in decision.reason
    assert model.calls == 0


@pytest.mark.asyncio
async def test_prompt_surfaces_deferred_intents_block_when_present() -> None:
    """HUMANIZATION_ROADMAP §3.4 — still-active deferred motives must be
    re-surfaced as a fact-layer block so the LLM can decide whether the
    timing is right *now* instead of forgetting an authentic urge."""
    from dataclasses import replace
    from datetime import timedelta

    from kokoro_link.domain.entities.deferred_intent import DeferredIntent

    model = _StubModel(
        '{"should_consume_slot": false, "inner_motive": "", '
        '"conversation_purpose": "", "expected_reply": "", '
        '"risk": "", "best_timing": "later", "reason": ""}',
    )
    judge = LLMProactiveIntentionJudge(model=model)

    ctx = _context()
    parked = DeferredIntent.new(
        character_id=ctx.character.id,
        operator_id="default",
        trigger="tick",
        inner_motive="想分享今天讀完的小說的後勁",
        conversation_purpose="延續上週的閱讀話題",
        expected_reply="對方可以接幾句感想",
        risk="可能讀到一半被打斷",
        best_timing="evening",
        reason="剛聊完工作不適合立刻切",
        ttl_minutes=180,
        now=ctx.now - timedelta(minutes=40),
    )
    ctx_with_intent = replace(ctx, deferred_intents=(parked,))

    await judge.judge(ctx_with_intent)
    prompt = model.captured_prompt or ""

    # The block header lands so the LLM knows this is a remembered urge.
    assert "先前你曾想過、但被自己壓下來的念頭" in prompt
    # The motive itself is quoted, not paraphrased.
    assert "想分享今天讀完的小說的後勁" in prompt
    # Supporting fields show up so the LLM can re-judge timing.
    assert "延續上週的閱讀話題" in prompt
    assert "剛聊完工作不適合立刻切" in prompt
    # Elapsed marker is present so the LLM can sense the half-life
    # without inferring from raw timestamps.
    assert "已等候" in prompt


@pytest.mark.asyncio
async def test_parses_revisit_at_iso_and_prompt_asks_for_it() -> None:
    """T2 — a skip whose real blocker is "the agreed time hasn't come
    yet" carries that time back as structured output, so the dispatcher
    can return at 19:30 instead of being fenced out by the cooldown."""
    model = _StubModel(
        '{"should_consume_slot": false, '
        '"inner_motive": "要一起上線核對任務", '
        '"conversation_purpose": "赴約", "expected_reply": "對方上線", '
        '"risk": "太早講會打斷", "best_timing": "evening", '
        '"revisit_at_iso": "2026-04-18T19:30", '
        '"reason": "已經約好七點半，等到時間再自然聯絡"}',
    )
    judge = LLMProactiveIntentionJudge(model=model)

    decision = await judge.judge(_context())

    assert decision.should_consume_slot is False
    assert decision.revisit_at_iso == "2026-04-18T19:30"
    prompt = model.captured_prompt or ""
    assert "revisit_at_iso" in prompt
    # The instruction must stay a semantic judgement, not a trigger list:
    # vague timing is explicitly told to leave the field blank.
    assert "留空字串，不要硬湊一個時間" in prompt


@pytest.mark.asyncio
async def test_revisit_at_iso_defaults_empty_when_model_omits_it() -> None:
    """Every response written before T2 — and every skip with only a
    vague "later" — must keep working with no alarm attached."""
    model = _StubModel(
        '{"should_consume_slot": false, "inner_motive": "想聊天", '
        '"conversation_purpose": "", "expected_reply": "", '
        '"risk": "", "best_timing": "later", "reason": "再等等"}',
    )
    judge = LLMProactiveIntentionJudge(model=model)

    decision = await judge.judge(_context())

    assert decision.revisit_at_iso == ""


@pytest.mark.asyncio
async def test_prompt_shows_whether_a_parked_alarm_has_arrived() -> None:
    """The judge must be able to tell "the agreed 19:30 is now" from
    "still waiting" — otherwise the re-tick has no way to know why it is
    being asked again."""
    from dataclasses import replace
    from datetime import timedelta

    from kokoro_link.domain.entities.deferred_intent import DeferredIntent

    model = _StubModel(
        '{"should_consume_slot": false, "inner_motive": "", '
        '"conversation_purpose": "", "expected_reply": "", '
        '"risk": "", "best_timing": "later", "reason": ""}',
    )
    judge = LLMProactiveIntentionJudge(model=model)

    ctx = _context()
    parked = DeferredIntent.new(
        character_id=ctx.character.id,
        operator_id="default",
        trigger="tick",
        inner_motive="說好七點半一起上線核對任務",
        revisit_at=ctx.now - timedelta(minutes=2),
        ttl_minutes=180,
        now=ctx.now - timedelta(minutes=40),
    )

    await judge.judge(replace(ctx, deferred_intents=(parked,)))
    prompt = model.captured_prompt or ""
    assert "當時記下的明確時點" in prompt
    assert "已經到了" in prompt

    future = replace(parked, revisit_at=ctx.now + timedelta(minutes=30))
    await judge.judge(replace(ctx, deferred_intents=(future,)))
    prompt_future = model.captured_prompt or ""
    assert "還沒到" in prompt_future


@pytest.mark.asyncio
async def test_prompt_omits_the_alarm_line_for_a_motive_without_one() -> None:
    from dataclasses import replace
    from datetime import timedelta

    from kokoro_link.domain.entities.deferred_intent import DeferredIntent

    model = _StubModel(
        '{"should_consume_slot": false, "inner_motive": "", '
        '"conversation_purpose": "", "expected_reply": "", '
        '"risk": "", "best_timing": "later", "reason": ""}',
    )
    judge = LLMProactiveIntentionJudge(model=model)
    ctx = _context()
    parked = DeferredIntent.new(
        character_id=ctx.character.id,
        operator_id="default",
        trigger="tick",
        inner_motive="想分享今天讀完的小說",
        ttl_minutes=180,
        now=ctx.now - timedelta(minutes=40),
    )

    await judge.judge(replace(ctx, deferred_intents=(parked,)))

    assert "當時記下的明確時點" not in (model.captured_prompt or "")


@pytest.mark.asyncio
async def test_prompt_surfaces_pace_preference_when_set() -> None:
    """HUMANIZATION_ROADMAP §3.6 — operator pace preference appears in
    a "對方期望" fact-layer block when set; absent when blank."""
    from dataclasses import replace as dc_replace

    model = _StubModel(
        '{"should_consume_slot": false, "inner_motive": "", '
        '"conversation_purpose": "", "expected_reply": "", '
        '"risk": "", "best_timing": "later", "reason": ""}',
    )
    judge = LLMProactiveIntentionJudge(model=model)

    ctx = _context()
    quiet_char = dc_replace(ctx.character, operator_pace_preference="more_quiet")
    ctx_quiet = dc_replace(ctx, character=quiet_char)

    await judge.judge(ctx_quiet)
    prompt_quiet = model.captured_prompt or ""
    assert "對方對這個角色的期望節奏" in prompt_quiet
    assert "安靜一點" in prompt_quiet


@pytest.mark.asyncio
async def test_prompt_omits_pace_preference_when_blank() -> None:
    model = _StubModel(
        '{"should_consume_slot": false, "inner_motive": "", '
        '"conversation_purpose": "", "expected_reply": "", '
        '"risk": "", "best_timing": "later", "reason": ""}',
    )
    judge = LLMProactiveIntentionJudge(model=model)
    await judge.judge(_context())
    prompt = model.captured_prompt or ""
    assert "對方對這個角色的期望節奏" not in prompt


@pytest.mark.asyncio
async def test_prompt_omits_deferred_intents_block_when_empty() -> None:
    model = _StubModel(
        '{"should_consume_slot": false, "inner_motive": "", '
        '"conversation_purpose": "", "expected_reply": "", '
        '"risk": "", "best_timing": "later", "reason": ""}',
    )
    judge = LLMProactiveIntentionJudge(model=model)
    await judge.judge(_context())

    prompt = model.captured_prompt or ""
    assert "先前你曾想過" not in prompt


@pytest.mark.asyncio
async def test_prompt_surfaces_unanswered_streak_block_when_high() -> None:
    """A run of ignored pushes must reach the judge so it can tell a
    cheap repeat apart from a genuine, evolving reaction to being
    ignored (the latter is a valid reason to spend a slot)."""
    model = _StubModel(
        '{"should_consume_slot": false, "inner_motive": "", '
        '"conversation_purpose": "", "expected_reply": "", '
        '"risk": "", "best_timing": "later", "reason": ""}',
    )
    judge = LLMProactiveIntentionJudge(model=model)
    await judge.judge(_context(unanswered_streak=4))
    prompt = model.captured_prompt or ""
    assert "連續主動傳了 4 則" in prompt
    # Self-question 4 was rewritten to separate 跳針 from real evolution.
    assert "跳針" in prompt


@pytest.mark.asyncio
async def test_prompt_omits_streak_block_when_not_a_run() -> None:
    model = _StubModel(
        '{"should_consume_slot": false, "inner_motive": "", '
        '"conversation_purpose": "", "expected_reply": "", '
        '"risk": "", "best_timing": "later", "reason": ""}',
    )
    judge = LLMProactiveIntentionJudge(model=model)
    await judge.judge(_context(unanswered_streak=1))
    prompt = model.captured_prompt or ""
    assert "連續未獲回應" not in prompt


@pytest.mark.asyncio
async def test_prompt_weather_fact_carries_freshness_authority() -> None:
    """Same directive the chat prompt and decider get: the live fact wins
    over any weather implied by memory / dialogue / schedule text."""
    model = _StubModel(
        '{"should_consume_slot": false, "inner_motive": "", '
        '"conversation_purpose": "", "expected_reply": "", '
        '"risk": "", "best_timing": "later", "reason": ""}',
    )
    judge = LLMProactiveIntentionJudge(model=model)
    await judge.judge(
        _context(weather_context="台北目前天氣（事實層）：\n- 現在：晴朗，氣溫 26°C"),
    )
    prompt = model.captured_prompt or ""
    assert "晴朗，氣溫 26°C" in prompt
    assert "以此刻天氣事實為準" in prompt
    assert "不要延續已經過時的天氣狀態" in prompt


@pytest.mark.asyncio
async def test_prompt_omits_weather_directive_when_no_weather_fact() -> None:
    model = _StubModel(
        '{"should_consume_slot": false, "inner_motive": "", '
        '"conversation_purpose": "", "expected_reply": "", '
        '"risk": "", "best_timing": "later", "reason": ""}',
    )
    judge = LLMProactiveIntentionJudge(model=model)
    await judge.judge(_context(weather_context=""))
    prompt = model.captured_prompt or ""
    assert "以此刻天氣事實為準" not in prompt


@pytest.mark.asyncio
async def test_prompt_judges_user_relevance_without_role_expertise() -> None:
    model = _StubModel(
        '{"should_consume_slot": false, "inner_motive": "", '
        '"conversation_purpose": "", "expected_reply": "", '
        '"risk": "角色不該裝懂", "best_timing": "later", '
        '"reason": "等更自然時機"}',
    )
    judge = LLMProactiveIntentionJudge(model=model)

    await judge.judge(
        _context(
            operator_persona_lines=("- 職業：後端工程師",),
            world_event_seed_title="Cloudflare 大規模故障",
        ),
    )

    prompt = model.captured_prompt or ""
    assert "這則訊息跟對方有什麼關係" in prompt
    assert "不要假裝專家" in prompt
    assert "角色能否用符合自身身份" in prompt
    assert "Cloudflare" in prompt


# ------------------------------------------------------------------
# Retired commitments must not reach the judge as future plans
# (COMMITMENT_LIFECYCLE_AND_FRESHNESS_PLAN §2 P1c, proactive half)
# ------------------------------------------------------------------

_TOMORROW = date(2026, 4, 19)


def _activity(
    description: str, *, hour: int, role: str | None = None,
) -> ScheduleActivity:
    base = datetime.combine(_TOMORROW, datetime.min.time(), tzinfo=timezone.utc)
    refs = (
        (
            ParticipantRef(
                actor_kind="operator",
                actor_id=None,
                display_name="使用者",
                role=role,
            ),
        )
        if role
        else ()
    )
    return ScheduleActivity.create(
        start_at=base.replace(hour=hour),
        end_at=base.replace(hour=hour + 1),
        description=description,
        category="social" if role else "work",
        participant_refs=refs,
    )


def _tomorrow(activities: list[ScheduleActivity]):
    return DailySchedule.create(
        character_id="char-x", date_=_TOMORROW, activities=activities,
    )


@pytest.mark.asyncio
async def test_upcoming_days_drop_expired_operator_commitments() -> None:
    """The judge weighs upcoming days as reasons to spend a slot ("明天要一起
    去…，先問幾點集合"), so a commitment the sweep retired must never appear
    there — same filter the chat renderer and the decider apply.

    The retired block sits at the head of the day, so the filter also has to
    run *before* the three-item slice, or it would silently evict a live
    block from the list.
    """
    model = _StubModel(
        '{"should_consume_slot": false, "inner_motive": "", '
        '"conversation_purpose": "", "expected_reply": "", '
        '"risk": "", "best_timing": "later", "reason": ""}',
    )
    judge = LLMProactiveIntentionJudge(model=model)

    await judge.judge(
        _context(
            upcoming_day_schedules=(
                _tomorrow([
                    _activity(
                        "和使用者一起去吃刨冰",
                        hour=9,
                        role=OPERATOR_INVITE_EXPIRED_ROLE,
                    ),
                    _activity("晨間散步", hour=10),
                    _activity("改稿", hour=11),
                    _activity("練吉他", hour=12),
                ]),
            ),
        ),
    )
    prompt = model.captured_prompt or ""

    assert "刨冰" not in prompt
    assert "晨間散步" in prompt
    assert "改稿" in prompt
    assert "練吉他" in prompt


@pytest.mark.asyncio
async def test_upcoming_day_row_disappears_when_every_block_expired() -> None:
    """A day left empty by the filter renders as no row at all — this
    renderer already omits days with no activities (``if snippets``), so the
    filtered-empty day simply takes the same path."""
    model = _StubModel(
        '{"should_consume_slot": false, "inner_motive": "", '
        '"conversation_purpose": "", "expected_reply": "", '
        '"risk": "", "best_timing": "later", "reason": ""}',
    )
    judge = LLMProactiveIntentionJudge(model=model)

    await judge.judge(
        _context(
            upcoming_day_schedules=(
                _tomorrow([
                    _activity(
                        "和使用者一起去共享工作室",
                        hour=15,
                        role=OPERATOR_CONFIRMED_LAPSED_ROLE,
                    ),
                ]),
            ),
        ),
    )
    prompt = model.captured_prompt or ""

    assert "共享工作室" not in prompt
    assert _TOMORROW.isoformat() not in prompt
