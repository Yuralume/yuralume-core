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
from datetime import date, datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.feature_keys import FEATURE_PROACTIVE_MESSAGE
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


class _CapturingProvider:
    def __init__(self, model: ChatModelPort) -> None:
        self.model = model
        self.feature_keys: list[str | None] = []

    async def resolve(self, feature_key=None, **kwargs):
        self.feature_keys.append(feature_key)
        return self.model

    async def resolve_model_id(self, feature_key=None, **kwargs):
        return None

    async def is_fake(self, feature_key=None, **kwargs):
        return False


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
    weather_context: str = "",
    upcoming_day_schedules: tuple = (),
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
        weather_context=weather_context,
        upcoming_day_schedules=upcoming_day_schedules,
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
async def test_proactive_message_uses_dedicated_feature_key() -> None:
    provider = _CapturingProvider(
        _StubModel(
            '{"should_send": false, "reason": "先不打擾", "message": null}',
        ),
    )

    await LLMProactiveDecider(provider=provider).decide(_context())

    assert provider.feature_keys == [FEATURE_PROACTIVE_MESSAGE]


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
async def test_weather_fact_carries_freshness_authority() -> None:
    """The decider used to receive the weather fact bare, so a schedule
    written at midnight ("下雨改室內") kept steering proactive messages
    long after the sky cleared. The fact now ships with the same
    freshness-authority directive the chat prompt uses."""
    model = _StubModel('{"should_send": false, "reason": "ok"}')
    decider = LLMProactiveDecider(model=model)
    await decider.decide(
        _context(weather_context="台北目前天氣（事實層）：\n- 現在：晴朗，氣溫 26°C"),
    )
    prompt = model.captured_prompt or ""
    assert "晴朗，氣溫 26°C" in prompt
    assert "以此刻天氣事實為準" in prompt
    assert "不要延續已經過時的天氣狀態" in prompt


@pytest.mark.asyncio
async def test_no_weather_fact_means_no_freshness_directive() -> None:
    model = _StubModel('{"should_send": false, "reason": "ok"}')
    decider = LLMProactiveDecider(model=model)
    await decider.decide(_context())
    prompt = model.captured_prompt or ""
    assert "以此刻天氣事實為準" not in prompt


@pytest.mark.asyncio
async def test_schedule_authority_excludes_weather_implied_by_description() -> None:
    """行程 stays the sole truth for 地點/正在做的事, but its description is
    a *pre-planned* narrative — any weather baked into it must yield to
    the live weather fact layer, otherwise the decider re-broadcasts a
    rainy day that ended hours ago."""
    model = _StubModel('{"should_send": false, "reason": "ok"}')
    decider = LLMProactiveDecider(model=model)
    await decider.decide(_context())
    prompt = model.captured_prompt or ""
    assert "唯一真實來源" in prompt
    assert "預排" in prompt
    assert "實際天氣一律以天氣事實層為準" in prompt


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


@pytest.mark.asyncio
async def test_decision_carries_the_prompt_actually_sent_to_the_model() -> None:
    """The dispatcher persists ``prompt_assembled`` on SENT turns, so the
    decision has to hand back exactly what the model saw."""
    sending = _StubModel(
        '{"should_send": true, "reason": "想分享", "message": "剛練完那首歌"}',
    )
    decision = await LLMProactiveDecider(model=sending).decide(_context())
    assert sending.captured_prompt
    assert decision.prompt_assembled == sending.captured_prompt

    skipping = _StubModel('{"should_send": false, "reason": "先不打擾"}')
    skipped = await LLMProactiveDecider(model=skipping).decide(_context())
    assert skipped.prompt_assembled == skipping.captured_prompt


@pytest.mark.asyncio
async def test_fake_provider_short_circuit_has_no_prompt() -> None:
    """No LLM call means no assembled prompt to audit."""

    class _FakeProvider:
        async def resolve(self, feature_key=None, **kwargs):  # pragma: no cover
            raise AssertionError("fake path must not resolve a model")

        async def resolve_model_id(self, feature_key=None, **kwargs):
            return None

        async def is_fake(self, feature_key=None, **kwargs):
            return True

    decision = await LLMProactiveDecider(provider=_FakeProvider()).decide(
        _context(),
    )
    assert decision.should_send is False
    assert decision.prompt_assembled is None


# ------------------------------------------------------------------
# Injection-side time discipline
# (COMMITMENT_LIFECYCLE_AND_FRESHNESS_PLAN §2 P6a / P6b)
# ------------------------------------------------------------------


def _upcoming_schedule(day: date, descriptions: list[str]) -> DailySchedule:
    base = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    activities = [
        ScheduleActivity.create(
            start_at=base.replace(hour=9 + index),
            end_at=base.replace(hour=10 + index),
            description=description,
            category="leisure",
        )
        for index, description in enumerate(descriptions)
    ]
    return DailySchedule.create(
        character_id="char-x", date_=day, activities=activities,
    )


@pytest.mark.asyncio
async def test_upcoming_days_block_demands_a_cross_check_before_promising() -> None:
    """The old guidance handed the model an unconditional worked example
    ("明天有約 X 想到就期待"), which is exactly the sentence a zombie goal
    ("陪使用者明早去吃刨冰", written three days ago) got filled into. The
    block now states the cross-check obligation instead."""
    model = _StubModel('{"should_send": false, "reason": "ok"}')
    decider = LLMProactiveDecider(model=model)
    now = datetime(2026, 7, 29, 6, 0, tzinfo=timezone.utc)
    await decider.decide(
        _context(
            now=now,
            upcoming_day_schedules=(
                _upcoming_schedule(date(2026, 7, 30), ["去書店挑禮物"]),
            ),
        ),
    )
    prompt = model.captured_prompt or ""

    # The factual layer is unchanged: real dates + real activities.
    assert "明天（2026-07-30 週四）" in prompt
    assert "去書店挑禮物" in prompt
    # The unconditional "just say 明天有約 X" demo is gone…
    assert "想到就期待" not in prompt
    # …replaced by an explicit look-it-up-first duty, and the pre-existing
    # anti-fabrication rail is kept.
    assert "唯一的日期權威" in prompt
    assert "只有清單上那天真的排著 X" in prompt
    assert "找不到對應" in prompt
    assert "不要憑空編造" in prompt


@pytest.mark.asyncio
async def test_instructions_bind_future_commitments_to_the_schedule_block() -> None:
    """Goals / story arcs / schedule seeds all froze relative words like
    「明天」 at write time. The decider instructions must name the schedule
    block as the only date authority and offer the past-tense way out,
    otherwise a three-day-old invite gets re-announced as tomorrow's plan."""
    model = _StubModel('{"should_send": false, "reason": "ok"}')
    decider = LLMProactiveDecider(model=model)
    await decider.decide(_context())
    prompt = model.captured_prompt or ""

    assert "未來的約定要先跟行程對帳" in prompt
    assert "唯一的日期權威" in prompt
    # The rule has to cover the three material sections that actually
    # carry frozen commitments, not just the memory recall block.
    assert "你目前在意的目標" in prompt
    assert "故事線" in prompt
    # An expired appointment may still be spoken about — as history.
    assert "過去式" in prompt


def _operator_activity(
    day: date, description: str, *, role: str, hour: int = 15,
) -> ScheduleActivity:
    base = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    return ScheduleActivity.create(
        start_at=base.replace(hour=hour),
        end_at=base.replace(hour=hour + 1),
        description=description,
        category="social",
        participant_refs=(
            ParticipantRef(
                actor_kind="operator",
                actor_id=None,
                display_name="使用者",
                role=role,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_upcoming_days_block_drops_expired_operator_commitments() -> None:
    """The decider's day list is declared the only date authority, so it has
    to be clean of commitments the sweep already retired — otherwise naming
    it as the authority merely launders the zombie 刨冰 invite (plan §2 P1c,
    proactive half; chat had this filter, proactive did not)."""
    model = _StubModel('{"should_send": false, "reason": "ok"}')
    decider = LLMProactiveDecider(model=model)
    tomorrow = _upcoming_schedule(date(2026, 7, 30), ["去書店挑禮物"])
    tomorrow = tomorrow.with_activities([
        *tomorrow.activities,
        _operator_activity(
            date(2026, 7, 30),
            "和使用者一起去吃刨冰",
            role=OPERATOR_INVITE_EXPIRED_ROLE,
        ),
    ])

    await decider.decide(
        _context(
            now=datetime(2026, 7, 29, 6, 0, tzinfo=timezone.utc),
            upcoming_day_schedules=(tomorrow,),
        ),
    )
    prompt = model.captured_prompt or ""

    assert "去書店挑禮物" in prompt
    assert "刨冰" not in prompt


@pytest.mark.asyncio
async def test_upcoming_days_overflow_count_excludes_expired_blocks() -> None:
    """「另外還有 N 段」 is arithmetic on the day list; computing it before the
    filter would leak a dropped block back in as a remainder."""
    model = _StubModel('{"should_send": false, "reason": "ok"}')
    decider = LLMProactiveDecider(model=model)
    tomorrow = _upcoming_schedule(
        date(2026, 7, 30),
        ["晨跑", "改稿", "採買", "回信", "練吉他"],
    )
    tomorrow = tomorrow.with_activities([
        *tomorrow.activities,
        _operator_activity(
            date(2026, 7, 30),
            "和使用者一起去共享工作室",
            role=OPERATOR_CONFIRMED_LAPSED_ROLE,
            hour=20,
        ),
    ])

    await decider.decide(
        _context(
            now=datetime(2026, 7, 29, 6, 0, tzinfo=timezone.utc),
            upcoming_day_schedules=(tomorrow,),
        ),
    )
    prompt = model.captured_prompt or ""

    assert "共享工作室" not in prompt
    # 5 live blocks, 4 shown → the remainder is 1, not 2.
    assert "另外還有 1 段" in prompt


@pytest.mark.asyncio
async def test_upcoming_day_emptied_by_filter_renders_as_unplanned() -> None:
    """A day whose every block was retired collapses to the same wording a
    genuinely unplanned day gets — matching the chat renderer, which also
    keeps the day row and says 「尚未安排具體時段」."""
    model = _StubModel('{"should_send": false, "reason": "ok"}')
    decider = LLMProactiveDecider(model=model)
    tomorrow = DailySchedule.create(
        character_id="char-x",
        date_=date(2026, 7, 30),
        activities=[
            _operator_activity(
                date(2026, 7, 30),
                "和使用者一起去吃刨冰",
                role=OPERATOR_INVITE_EXPIRED_ROLE,
            ),
        ],
    )

    await decider.decide(
        _context(
            now=datetime(2026, 7, 29, 6, 0, tzinfo=timezone.utc),
            upcoming_day_schedules=(tomorrow,),
        ),
    )
    prompt = model.captured_prompt or ""

    assert "刨冰" not in prompt
    assert "明天（2026-07-30 週四）：尚未安排具體時段" in prompt


@pytest.mark.asyncio
async def test_instructions_carry_the_memory_time_anchor_rule() -> None:
    """The shipped baseline pack was missing the recall-anchor discipline
    entirely (only the tuned overlay had it), so a self-hosted deployment
    got none of it."""
    model = _StubModel('{"should_send": false, "reason": "ok"}')
    decider = LLMProactiveDecider(model=model)
    await decider.decide(_context())
    prompt = model.captured_prompt or ""

    assert "回憶的時間別亂編" in prompt
    assert "錨點" in prompt


@pytest.mark.asyncio
async def test_instructions_separate_own_story_from_user_reality() -> None:
    """A player said he had a contract to sign; the arc turned it into a
    meeting with an invented agent at an invented company and the decider
    referenced it as shared knowledge. The instructions must state that the
    character's own story material is news to the user, that the user's real
    life has exactly one source (what he said), and that no detail may be
    invented for it — while still allowing quoting what he actually said."""
    model = _StubModel('{"should_send": false, "reason": "ok"}')
    decider = LLMProactiveDecider(model=model)
    await decider.decide(_context())
    prompt = model.captured_prompt or ""

    # The two material blocks that carry the character's own world.
    assert "你目前在進行的故事線" in prompt
    assert "今天你身上發生的小事" in prompt
    # First mention must be introduced, not name-dropped.
    assert "沒聽過" in prompt
    assert "你也知道那個" in prompt
    # The user's reality has a single source of truth.
    assert "只能引用他真的說出口的部分" in prompt
    # No invented details attached to the user's real-life affairs.
    assert "公司名" in prompt
    # …and the legitimate quote-back hook survives the rule.
    assert "你之前不是說有個工作要簽約" in prompt


# --- TR3: health follow-up hint (code-side addendum, not a pack edit) --


@pytest.mark.asyncio
async def test_health_follow_up_hint_appears_when_memory_material_exists() -> None:
    model = _StubModel('{"should_send": false, "reason": "ok"}')
    decider = LLMProactiveDecider(model=model)
    await decider.decide(
        _context(recent_memories_text="- [semantic] 使用者提到這幾天一直胸悶"),
    )
    prompt = model.captured_prompt or ""

    assert "健康關懷加分項" in prompt
    assert "上次說的胸悶後來好點了嗎" in prompt  # positive example survives
    # Same discipline as the chat-side health_care section, restated for
    # the decider's own voice.
    assert "不要切成衛教口吻" in prompt
    assert "不要條列式問診" in prompt
    assert "不要診斷或建議吃什麼藥" in prompt
    assert "不要貼求助專線" in prompt


@pytest.mark.asyncio
async def test_health_follow_up_hint_is_absent_with_no_material_at_all() -> None:
    model = _StubModel('{"should_send": false, "reason": "ok"}')
    decider = LLMProactiveDecider(model=model)
    # Default context: empty recent_memories_text and (via the
    # ProactiveContext default) empty recent_dialogue_summary.
    await decider.decide(_context())
    prompt = model.captured_prompt or ""

    # Nothing to gate the hint on, so it must not print an empty
    # "check the material above" instruction into every ordinary tick.
    assert "健康關懷加分項" not in prompt
