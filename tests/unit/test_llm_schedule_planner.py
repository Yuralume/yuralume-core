"""LLMSchedulePlanner tests — output coercion & robustness."""

from __future__ import annotations

from datetime import date, timezone

import pytest

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import (
    MeetingAffordance,
    OPERATOR_INVITE_PENDING_ROLE,
    ScenePrivacy,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.personality_type import CharacterPersonalityType
from kokoro_link.infrastructure.schedule.llm_planner import LLMSchedulePlanner

UTC = timezone.utc


class FakeModel:
    def __init__(self, response: str) -> None:
        self._response = response

    async def generate(self, prompt: str) -> str:  # noqa: ARG002
        return self._response

    def generate_stream(self, prompt: str):  # noqa: ARG002
        async def _empty():
            if False:
                yield ""
        return _empty()


def _character(**overrides) -> Character:  # noqa: ANN003
    base = dict(
        name="Aki",
        summary="插畫家",
        personality=["內向"],
        interests=["咖啡"],
        speaking_style="溫柔",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )
    base.update(overrides)
    return Character.create(**base)


@pytest.mark.asyncio
async def test_parses_valid_json_array() -> None:
    payload = (
        '[{"start":"09:00","end":"12:00","description":"畫草稿",'
        '"category":"work","location":"工作室"},'
        '{"start":"12:00","end":"13:00","description":"午餐",'
        '"category":"meal","location":"家中"}]'
    )
    planner = LLMSchedulePlanner(model=FakeModel(payload))
    schedule = await planner.plan_day(
        character=_character(), date_=date(2026, 4, 18), local_tz=UTC,
    )
    assert len(schedule.activities) == 2
    assert schedule.activities[0].description == "畫草稿"
    assert schedule.activities[0].category == "work"
    assert schedule.activities[0].scene_privacy is None
    assert schedule.activities[0].meeting_affordance is None


@pytest.mark.asyncio
async def test_parses_scene_access_affordance_without_location_keyword_override() -> None:
    payload = (
        '[{"start":"15:00","end":"16:00","description":"開放工作坊",'
        '"category":"workshop","location":"Aki的家",'
        '"scene_privacy":"public","meeting_affordance":"open_to_encounter"}]'
    )
    planner = LLMSchedulePlanner(model=FakeModel(payload))

    schedule = await planner.plan_day(
        character=_character(), date_=date(2026, 4, 18), local_tz=UTC,
    )

    assert schedule.activities[0].scene_privacy is ScenePrivacy.PUBLIC
    assert (
        schedule.activities[0].meeting_affordance
        is MeetingAffordance.OPEN_TO_ENCOUNTER
    )


@pytest.mark.asyncio
async def test_parses_operator_involvement_as_participant_ref() -> None:
    payload = (
        '[{"start":"19:00","end":"20:00","description":"想邀請對方看電影",'
        '"category":"social","operator_involvement":"invite_pending",'
        '"companion_names":["同事"]}]'
    )
    planner = LLMSchedulePlanner(model=FakeModel(payload))

    schedule = await planner.plan_day(
        character=_character(), date_=date(2026, 4, 18), local_tz=UTC,
    )

    activity = schedule.activities[0]
    assert activity.companion_names == ("同事",)
    operator_refs = [
        ref for ref in activity.participant_refs if ref.actor_kind == "operator"
    ]
    assert len(operator_refs) == 1
    assert operator_refs[0].role == OPERATOR_INVITE_PENDING_ROLE


@pytest.mark.asyncio
async def test_tolerates_code_fence_and_preamble() -> None:
    payload = (
        "以下是為角色設計的一天：\n"
        "```json\n"
        '[{"start":"07:00","end":"08:00","description":"早餐",'
        '"category":"meal"}]\n'
        "```\n"
    )
    planner = LLMSchedulePlanner(model=FakeModel(payload))
    schedule = await planner.plan_day(
        character=_character(), date_=date(2026, 4, 18), local_tz=UTC,
    )
    assert len(schedule.activities) == 1
    assert schedule.activities[0].description == "早餐"


@pytest.mark.asyncio
async def test_drops_invalid_entries() -> None:
    payload = (
        '[{"start":"09:00","end":"10:00","description":"ok","category":"work"},'
        '{"start":"bad","end":"10:00","description":"x","category":"y"},'
        '{"start":"11:00","end":"10:00","description":"backwards","category":"z"},'
        '{"start":"","end":"","description":"","category":""}]'
    )
    planner = LLMSchedulePlanner(model=FakeModel(payload))
    schedule = await planner.plan_day(
        character=_character(), date_=date(2026, 4, 18), local_tz=UTC,
    )
    assert len(schedule.activities) == 1


@pytest.mark.asyncio
async def test_trims_overlaps() -> None:
    payload = (
        '[{"start":"09:00","end":"12:00","description":"first","category":"work"},'
        '{"start":"11:00","end":"13:00","description":"second","category":"work"}]'
    )
    planner = LLMSchedulePlanner(model=FakeModel(payload))
    schedule = await planner.plan_day(
        character=_character(), date_=date(2026, 4, 18), local_tz=UTC,
    )
    # both should survive; second one gets its start pushed to 12:00
    assert len(schedule.activities) == 2
    assert schedule.activities[1].start_at.hour == 12
    assert schedule.activities[1].end_at.hour == 13


@pytest.mark.asyncio
async def test_empty_on_unparseable_response() -> None:
    planner = LLMSchedulePlanner(model=FakeModel("這不是 JSON"))
    schedule = await planner.plan_day(
        character=_character(), date_=date(2026, 4, 18), local_tz=UTC,
    )
    assert schedule.activities == ()


@pytest.mark.asyncio
async def test_empty_on_model_exception() -> None:
    class BrokenModel:
        async def generate(self, prompt: str) -> str:  # noqa: ARG002
            raise RuntimeError("boom")
        def generate_stream(self, prompt: str):  # noqa: ARG002
            async def _e():
                if False:
                    yield ""
            return _e()

    planner = LLMSchedulePlanner(model=BrokenModel())
    schedule = await planner.plan_day(
        character=_character(), date_=date(2026, 4, 18), local_tz=UTC,
    )
    assert schedule.activities == ()


@pytest.mark.asyncio
async def test_accepts_free_form_category() -> None:
    payload = (
        '[{"start":"20:00","end":"21:30","description":"在陽台觀星",'
        '"category":"觀星","location":"家"}]'
    )
    planner = LLMSchedulePlanner(model=FakeModel(payload))
    schedule = await planner.plan_day(
        character=_character(), date_=date(2026, 4, 18), local_tz=UTC,
    )
    assert schedule.activities[0].category == "觀星"


@pytest.mark.asyncio
async def test_today_beat_appears_in_prompt() -> None:
    """When a today_beat is supplied, the planner prompt must contain
    its location, NPCs and dramatic question — without those signals
    the LLM has no way to embed the scene into the day."""
    from datetime import timedelta
    from kokoro_link.domain.entities.story_arc import StoryArcBeat

    captured: dict[str, str] = {}

    class _CapturingModel:
        async def generate(self, prompt: str) -> str:
            captured["prompt"] = prompt
            return "[]"
        def generate_stream(self, prompt: str):  # noqa: ARG002
            async def _e():
                if False:
                    yield ""
            return _e()

    today = date(2026, 4, 18)
    arc_id = "arc-prompt-test"
    beat = StoryArcBeat.create(
        arc_id=arc_id,
        sequence=1, scheduled_date=today,
        title="公告欄發現試鏡海報", summary="今天她注意到一張新海報",
        tension="rising", scene_type="encounter",
        location="學校公告欄",
        scene_characters=("室友 美咲",),
        dramatic_question="她敢報名試鏡嗎？",
    )
    upcoming = StoryArcBeat.create(
        arc_id=arc_id,
        sequence=2, scheduled_date=today + timedelta(days=2),
        title="試鏡前夜", summary="緊張到睡不著", tension="climax",
    )
    planner = LLMSchedulePlanner(model=_CapturingModel())
    await planner.plan_day(
        character=_character(), date_=today, local_tz=UTC,
        today_beat=beat, upcoming_beats=(upcoming,),
    )
    prompt = captured["prompt"]
    assert "本日劇情骨架" in prompt
    assert "公告欄發現試鏡海報" in prompt
    assert "學校公告欄" in prompt
    assert "室友 美咲" in prompt
    assert "她敢報名試鏡嗎？" in prompt
    assert "試鏡前夜" in prompt  # upcoming beat label


@pytest.mark.asyncio
async def test_today_beat_in_future_renders_preparation_block() -> None:
    """When today_beat is actually scheduled for a future date (gap-day
    fallback from ScheduleService), the planner prompt must NOT order
    the scene played today — instead it asks for prep / anticipation.
    This keeps the prompt honest about timing while still anchoring the
    day in the arc."""
    from datetime import timedelta
    from kokoro_link.domain.entities.story_arc import StoryArcBeat

    captured: dict[str, str] = {}

    class _CapturingModel:
        async def generate(self, prompt: str) -> str:
            captured["prompt"] = prompt
            return "[]"
        def generate_stream(self, prompt: str):  # noqa: ARG002
            async def _e():
                if False:
                    yield ""
            return _e()

    today = date(2026, 4, 18)
    future_beat = StoryArcBeat.create(
        arc_id="arc-future-fallback",
        sequence=1, scheduled_date=today + timedelta(days=3),
        title="試鏡當天", summary="走上舞台",
        tension="climax", scene_type="conflict",
        location="劇場",
        scene_characters=("評審 田中",),
        dramatic_question="她能撐住嗎？",
    )
    planner = LLMSchedulePlanner(model=_CapturingModel())
    await planner.plan_day(
        character=_character(), date_=today, local_tz=UTC,
        today_beat=future_beat, upcoming_beats=(),
    )
    prompt = captured["prompt"]
    # No "必須 today" directive — that would lie about timing.
    assert "本日劇情骨架" not in prompt
    # Preparation framing present, with the correct future date signal.
    assert "鋪陳" in prompt or "準備" in prompt
    assert "再 3 天" in prompt
    assert "試鏡當天" in prompt
    # Explicit "do NOT play this scene today" guard.
    assert "不要" in prompt


@pytest.mark.asyncio
async def test_no_arc_block_when_today_beat_absent() -> None:
    """Without a today_beat the prompt skips the arc directive entirely
    so old characters (no arc) keep behaving as before."""
    captured: dict[str, str] = {}

    class _CapturingModel:
        async def generate(self, prompt: str) -> str:
            captured["prompt"] = prompt
            return "[]"
        def generate_stream(self, prompt: str):  # noqa: ARG002
            async def _e():
                if False:
                    yield ""
            return _e()

    planner = LLMSchedulePlanner(model=_CapturingModel())
    await planner.plan_day(
        character=_character(), date_=date(2026, 4, 18), local_tz=UTC,
    )
    assert "本日劇情骨架" not in captured["prompt"]


@pytest.mark.asyncio
async def test_prompt_discourages_multi_hour_daytime_unknown_gaps() -> None:
    captured: dict[str, str] = {}

    class _CapturingModel:
        async def generate(self, prompt: str) -> str:
            captured["prompt"] = prompt
            return "[]"
        def generate_stream(self, prompt: str):  # noqa: ARG002
            async def _e():
                if False:
                    yield ""
            return _e()

    planner = LLMSchedulePlanner(model=_CapturingModel())
    await planner.plan_day(
        character=_character(), date_=date(2026, 4, 18), local_tz=UTC,
    )

    prompt = captured["prompt"]
    assert "不要留下多小時的白天 unknown gap" in prompt
    assert "長時間留白請安排成具體低強度活動" in prompt
    assert "照樣填 scene_privacy / meeting_affordance" in prompt


@pytest.mark.asyncio
async def test_prompt_includes_role_knowledge_boundary() -> None:
    captured: dict[str, str] = {}

    class _CapturingModel:
        async def generate(self, prompt: str) -> str:
            captured["prompt"] = prompt
            return "[]"
        def generate_stream(self, prompt: str):  # noqa: ARG002
            async def _e():
                if False:
                    yield ""
            return _e()

    planner = LLMSchedulePlanner(model=_CapturingModel())
    await planner.plan_day(
        character=_character(), date_=date(2026, 4, 18), local_tz=UTC,
    )

    prompt = captured["prompt"]
    assert "認知範圍與誠實表達" in prompt
    assert "不要假裝專家" in prompt
    assert "年齡與生活閱歷" in prompt


@pytest.mark.asyncio
async def test_prompt_includes_operator_relationship_policy_and_persona() -> None:
    captured: dict[str, str] = {}

    class _CapturingModel:
        async def generate(self, prompt: str) -> str:
            captured["prompt"] = prompt
            return "[]"

        def generate_stream(self, prompt: str):  # noqa: ARG002
            async def _e():
                if False:
                    yield ""
            return _e()

    planner = LLMSchedulePlanner(model=_CapturingModel())
    await planner.plan_day(
        character=_character(),
        date_=date(2026, 4, 18),
        local_tz=UTC,
        operator_relationship_context=(
            "使用者創角時確認的起始關係設定：\n"
            "- 關係：朋友\n"
            "- 未提供的共同經歷不得補完"
        ),
        operator_persona_lines=("- 使用者興趣：爵士樂",),
        schedule_involvement_policy="invite_required",
    )

    prompt = captured["prompt"]
    assert "使用者相關事實" in prompt
    assert "關係：朋友" in prompt
    assert "使用者興趣：爵士樂" in prompt
    assert "想邀請使用者" in prompt
    assert "不可杜撰已約好的時段" in prompt


@pytest.mark.asyncio
async def test_prompt_includes_cohabitation_home_location_rule() -> None:
    captured: dict[str, str] = {}

    class _CapturingModel:
        async def generate(self, prompt: str) -> str:
            captured["prompt"] = prompt
            return "[]"

        def generate_stream(self, prompt: str):  # noqa: ARG002
            async def _e():
                if False:
                    yield ""
            return _e()

    planner = LLMSchedulePlanner(model=_CapturingModel())
    await planner.plan_day(
        character=_character(name="露露"),
        date_=date(2026, 4, 18),
        local_tz=UTC,
        operator_relationship_context=(
            "使用者創角時確認的起始關係設定：\n"
            "- 關係：貼身小精靈\n"
            "- 居住安排：住在使用者家裡"
        ),
        schedule_involvement_policy="mention_only",
    )

    prompt = captured["prompt"]
    assert "居家時段的 location 請命名為共同住所" in prompt
    assert "不要另造一間「露露的家」" in prompt
    assert "使用者自然可能在同一生活空間是環境事實" in prompt
    assert "不要把使用者放進 companion_names" in prompt
    assert "不可寫成已約定見面或具體共同活動" in prompt


@pytest.mark.asyncio
async def test_prompt_includes_personality_type_without_engineering_fields() -> None:
    captured: dict[str, str] = {}

    class _CapturingModel:
        async def generate(self, prompt: str) -> str:
            captured["prompt"] = prompt
            return "[]"

        def generate_stream(self, prompt: str):  # noqa: ARG002
            async def _e():
                if False:
                    yield ""
            return _e()

    planner = LLMSchedulePlanner(model=_CapturingModel())
    await planner.plan_day(
        character=_character(
            personality_type=CharacterPersonalityType(
                code="ISTJ",
                source="user_explicit",
                confidence=0.9,
                rationale="偏重秩序與可預期流程。",
                consistency_notes=("具體人設優先。",),
            ),
        ),
        date_=date(2026, 4, 18),
        local_tz=UTC,
    )

    prompt = captured["prompt"]
    assert "16 型性格參考" in prompt
    assert "ISTJ" in prompt
    assert "偏重秩序與可預期流程" in prompt
    assert "具體人設優先" in prompt
    assert "confidence" not in prompt
    assert "personality_type_json" not in prompt


@pytest.mark.asyncio
async def test_prompt_reanchors_busy_score_as_reply_cost_and_personality_density() -> None:
    captured: dict[str, str] = {}

    class _CapturingModel:
        async def generate(self, prompt: str) -> str:
            captured["prompt"] = prompt
            return "[]"

        def generate_stream(self, prompt: str):  # noqa: ARG002
            async def _e():
                if False:
                    yield ""
            return _e()

    planner = LLMSchedulePlanner(model=_CapturingModel())
    await planner.plan_day(
        character=_character(
            personality_type=CharacterPersonalityType(
                code="INFP",
                source="user_explicit",
                confidence=0.9,
                rationale="重視彈性與臨時起意。",
            ),
        ),
        date_=date(2026, 4, 18),
        local_tz=UTC,
    )

    prompt = captured["prompt"]
    assert "回訊息的成本" in prompt
    assert "不是單純投入程度" in prompt
    assert "0.4–0.6 在做事但看得到手機" in prompt
    assert "0.9+ 真的碰不了手機" in prompt
    assert "不要把一整天排成高 busy_score" in prompt
    assert "行程的密度、規律性、留白多寡要反映角色性格" in prompt
    assert "隨興自由、重視彈性的角色應排鬆一些" in prompt
    assert '"category": "work"' in prompt
    assert '"busy_score": 0.55' in prompt
    assert '"category": "important meeting"' in prompt
    assert '"busy_score": 0.9' in prompt
    assert '"busy_score": 0.85' not in prompt


@pytest.mark.asyncio
async def test_prompt_softens_sleep_privacy_and_adds_cohabitation_co_presence() -> None:
    """睡眠時段不再被硬性設成最封閉；有居住安排時補上親密同住共眠的環境
    指引，讓 Scene Access 不會把親密伴侶在角色睡覺時擋成不可同場。"""
    captured: dict[str, str] = {}

    class _CapturingModel:
        async def generate(self, prompt: str) -> str:
            captured["prompt"] = prompt
            return "[]"

        def generate_stream(self, prompt: str):  # noqa: ARG002
            async def _e():
                if False:
                    yield ""
            return _e()

    planner = LLMSchedulePlanner(model=_CapturingModel())
    await planner.plan_day(
        character=_character(name="露露"),
        date_=date(2026, 4, 18),
        local_tz=UTC,
        operator_relationship_context=(
            "使用者創角時確認的起始關係設定：\n"
            "- 關係：伴侶\n"
            "- 居住安排：住在一起、同床共枕"
        ),
        schedule_involvement_policy="shared_allowed",
    )

    prompt = captured["prompt"]
    # 通用：睡眠 privacy 不再一律設成最封閉
    assert "不要一律設成最封閉" in prompt
    # 同住分支：親密伴侶共眠的環境指引
    assert "夜間休息／睡眠屬共同生活" in prompt
    assert "不必另切成不可打擾的獨處硬段" in prompt
    assert "室友／家人／寵物不適用此放寬" in prompt


@pytest.mark.asyncio
async def test_prompt_omits_cohabitation_sleep_guidance_without_living_arrangement() -> (
    None
):
    """沒有居住安排的角色不出現同住共眠指引（行為回歸不變），
    但通用的睡眠 privacy 軟化指引仍在。"""
    captured: dict[str, str] = {}

    class _CapturingModel:
        async def generate(self, prompt: str) -> str:
            captured["prompt"] = prompt
            return "[]"

        def generate_stream(self, prompt: str):  # noqa: ARG002
            async def _e():
                if False:
                    yield ""
            return _e()

    planner = LLMSchedulePlanner(model=_CapturingModel())
    await planner.plan_day(
        character=_character(),
        date_=date(2026, 4, 18),
        local_tz=UTC,
    )

    prompt = captured["prompt"]
    assert "不要一律設成最封閉" in prompt
    assert "夜間休息／睡眠屬共同生活" not in prompt
