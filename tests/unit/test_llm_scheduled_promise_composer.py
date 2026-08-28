from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.prompt import PromptToolDescriptor, ToolOutcomeMessage
from kokoro_link.contracts.scheduled_promise_composer import (
    ScheduledPromiseComposeInput,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import MessageContentMode
from kokoro_link.domain.entities.schedule import ScheduleActivity
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.content_flow import CONTENT_TOLERANCE_COMMUNITY
from kokoro_link.domain.value_objects.disposition import CharacterDisposition
from kokoro_link.domain.value_objects.personality_type import (
    CharacterPersonalityType,
)
from kokoro_link.infrastructure.busy.llm_scheduled_promise_composer import (
    LLMScheduledPromiseComposer,
    _build_prompt,
)


class _StubModel(ChatModelPort):
    supports_vision = False

    def __init__(self, response: str, *, provider_id: str) -> None:
        self.response = response
        self.provider_id = provider_id
        self.calls = 0
        self.prompts: list[str] = []

    async def generate(self, prompt: str, **kwargs: object) -> str:
        self.calls += 1
        self.prompts.append(prompt)
        return self.response

    async def generate_stream(
        self, prompt: str, **kwargs: object,
    ) -> AsyncIterator[str]:  # pragma: no cover - unused
        yield self.response


class _RecordingActiveProvider:
    def __init__(self, model: _StubModel) -> None:
        self.model = model
        self.resolve_tolerances: list[str | None] = []
        self.model_id_tolerances: list[str | None] = []
        self.fake_tolerances: list[str | None] = []

    async def resolve(
        self,
        feature_key=None,
        *,
        character=None,
        content_tolerance=None,
    ):
        self.resolve_tolerances.append(content_tolerance)
        return self.model

    async def resolve_model_id(
        self,
        feature_key=None,
        *,
        character=None,
        content_tolerance=None,
    ):
        self.model_id_tolerances.append(content_tolerance)
        return "community-model" if content_tolerance else None

    async def is_fake(
        self,
        feature_key=None,
        *,
        character=None,
        content_tolerance=None,
    ) -> bool:
        self.fake_tolerances.append(content_tolerance)
        return False


def _character() -> Character:
    return Character.create(
        name="Mio",
        summary="咖啡店打工的大學生",
        personality=["溫柔"],
        interests=[],
        speaking_style="輕柔",
        boundaries=[],
        state=CharacterState(
            emotion="平靜", affection=60, fatigue=10, trust=55, energy=80,
        ),
    )


def test_prompt_includes_operator_persona_lines() -> None:
    character = Character.create(
        name="Mio",
        summary="咖啡店打工的大學生",
        personality=["溫柔"],
        interests=[],
        speaking_style="輕柔",
        boundaries=[],
        state=CharacterState(
            emotion="平靜", affection=60, fatigue=10, trust=55, energy=80,
        ),
    )
    payload = ScheduledPromiseComposeInput(
        character=character,
        promise_intent="叫對方起床",
        promise_text="明天十點叫我起床",
        scheduled_for=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        current_activity=None,
        just_finished_activity=None,
        recent_dialogue_summary=None,
        now=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        operator_persona_lines=("- 對方資料：小名 小丹。",),
    )

    prompt = _build_prompt(payload)

    assert "小名 小丹" in prompt
    assert "不要把畫像內容硬塞進提醒" in prompt


def test_prompt_includes_schedule_activity_knowledge_boundary() -> None:
    """KB9: an activity description can name a companion/place the player
    has never heard of — pin the schedule-block rider that covers it."""
    from kokoro_link.infrastructure.prompt.player_knowledge_lines import (
        render_schedule_activity_knowledge_line,
    )

    now = datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc)
    payload = ScheduledPromiseComposeInput(
        character=_character(),
        promise_intent="叫對方起床",
        promise_text="明天十點叫我起床",
        scheduled_for=now,
        current_activity=ScheduleActivity(
            id="act-1",
            start_at=now,
            end_at=now,
            description="跟阿凱討論山區那次的事",
            category="social",
            companion_names=("阿凱",),
        ),
        just_finished_activity=None,
        recent_dialogue_summary=None,
        now=now,
    )

    prompt = _build_prompt(payload)

    assert render_schedule_activity_knowledge_line() in prompt


def test_schedule_block_omits_activity_knowledge_line_when_free() -> None:
    """No activity in either slot → the placeholder line only, no rider
    (nothing was rendered that could name someone unheard-of)."""
    from kokoro_link.infrastructure.prompt.player_knowledge_lines import (
        render_schedule_activity_knowledge_line,
    )

    now = datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc)
    payload = ScheduledPromiseComposeInput(
        character=_character(),
        promise_intent="叫對方起床",
        promise_text="明天十點叫我起床",
        scheduled_for=now,
        current_activity=None,
        just_finished_activity=None,
        recent_dialogue_summary=None,
        now=now,
    )

    prompt = _build_prompt(payload)

    assert render_schedule_activity_knowledge_line() not in prompt


def test_prompt_includes_disposition_and_personality_type_lines() -> None:
    character = Character.create(
        name="Mio",
        summary="咖啡店打工的大學生",
        personality=["溫柔"],
        interests=[],
        speaking_style="輕柔",
        boundaries=[],
        state=CharacterState(
            emotion="平靜", affection=60, fatigue=10, trust=55, energy=80,
        ),
        disposition=CharacterDisposition(sharing_drive="low"),
        personality_type=CharacterPersonalityType(
            code="ISFJ",
            rationale="重視安定，表達偏克制。",
        ),
    )
    payload = ScheduledPromiseComposeInput(
        character=character,
        promise_intent="叫對方起床",
        promise_text="明天十點叫我起床",
        scheduled_for=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        current_activity=None,
        just_finished_activity=None,
        recent_dialogue_summary=None,
        now=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
    )

    prompt = _build_prompt(payload)

    assert "你的內在表達傾向" in prompt
    assert "一兩則短訊" in prompt
    assert "16 型性格參考" in prompt
    assert "ISFJ" in prompt


def test_prompt_covers_rendezvous_and_report_promise_shapes() -> None:
    # The composer used to frame every promise as a reminder ("叫對方起床／
    # 提醒對方吃飯"). A rendezvous intent ("一起上線") written in that frame
    # reads as a bystander nudging the user instead of the character
    # actually showing up, so the shapes are named explicitly.
    payload = ScheduledPromiseComposeInput(
        character=_character(),
        promise_intent="到約定時間主動找使用者，開始一起核對夏祭任務",
        promise_text="那我們七點半一起上線核對任務",
        scheduled_for=datetime(2026, 8, 12, 11, 30, tzinfo=timezone.utc),
        current_activity=None,
        just_finished_activity=None,
        recent_dialogue_summary=None,
        now=datetime(2026, 8, 12, 11, 30, tzinfo=timezone.utc),
    )

    prompt = _build_prompt(payload)

    assert "赴約型" in prompt
    assert "回報型" in prompt
    # A completion promise must never invent the result it was asked to fetch.
    assert "不要憑空編造具體結果" in prompt


def test_prompt_injects_operator_local_current_time() -> None:
    payload = ScheduledPromiseComposeInput(
        character=_character(),
        promise_intent="叫對方起床",
        promise_text="明天早上叫我起床",
        scheduled_for=datetime(2026, 6, 19, 23, 35, tzinfo=timezone.utc),
        current_activity=None,
        just_finished_activity=None,
        recent_dialogue_summary=None,
        now=datetime(2026, 6, 19, 23, 30, tzinfo=timezone.utc),
        local_tz=ZoneInfo("Asia/Taipei"),
    )

    prompt = _build_prompt(payload)

    assert "現在時間：2026-06-20 07:30" in prompt
    assert "約定時間：2026-06-20 07:35" in prompt
    assert "清晨" in prompt


# --- SP1: promise_made_at anchors "你之前答應的事" ----------------------


def test_prompt_omits_promise_made_line_when_promise_made_at_is_none() -> None:
    """Fail-soft default: rows written before this field existed (or any
    caller that hasn't been updated) render byte-identical to before."""
    payload = ScheduledPromiseComposeInput(
        character=_character(),
        promise_intent="叫對方起床",
        promise_text="明天十點叫我起床",
        scheduled_for=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        current_activity=None,
        just_finished_activity=None,
        recent_dialogue_summary=None,
        now=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
    )

    prompt = _build_prompt(payload)

    assert "向你提的" not in prompt
    assert "${" not in prompt


def test_prompt_includes_relative_minutes_for_a_recent_promise() -> None:
    payload = ScheduledPromiseComposeInput(
        character=_character(),
        promise_intent="叫對方起床",
        promise_text="等等叫我起床",
        promise_made_at=datetime(2026, 5, 18, 9, 30, tzinfo=timezone.utc),
        scheduled_for=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        current_activity=None,
        just_finished_activity=None,
        recent_dialogue_summary=None,
        now=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
    )

    prompt = _build_prompt(payload)

    assert "這個承諾是對方在 約 30 分鐘前 向你提的" in prompt


def test_prompt_tags_the_civil_day_when_the_promise_crosses_midnight() -> None:
    """23:50 last night, fulfilled the next morning: duration alone
    ("約 7 小時前") would still read as the same evening — the civil-day
    tag is what tells the model it was actually a different calendar
    day, so a character can say "昨晚" instead of guessing wrong."""
    payload = ScheduledPromiseComposeInput(
        character=_character(),
        promise_intent="叫對方起床",
        promise_text="明早叫我起床",
        promise_made_at=datetime(2026, 5, 17, 23, 50, tzinfo=timezone.utc),
        scheduled_for=datetime(2026, 5, 18, 7, 0, tzinfo=timezone.utc),
        current_activity=None,
        just_finished_activity=None,
        recent_dialogue_summary=None,
        now=datetime(2026, 5, 18, 7, 0, tzinfo=timezone.utc),
    )

    prompt = _build_prompt(payload)

    assert "1 天前" in prompt
    assert "這個承諾是對方在" in prompt
    assert "向你提的" in prompt


def test_frontier_prompt_omits_nsfw_original_promise_text() -> None:
    character = Character.create(
        name="Mio",
        summary="咖啡店打工的大學生",
        personality=["溫柔"],
        interests=[],
        speaking_style="輕柔",
        boundaries=[],
        state=CharacterState(
            emotion="平靜", affection=60, fatigue=10, trust=55, energy=80,
        ),
    )
    payload = ScheduledPromiseComposeInput(
        character=character,
        promise_intent="履行一個私密承諾",
        promise_text="NSFW scheduled raw",
        scheduled_for=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        current_activity=None,
        just_finished_activity=None,
        recent_dialogue_summary=None,
        now=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        promise_content_mode=MessageContentMode.NSFW,
    )

    prompt = _build_prompt(payload)

    assert "履行一個私密承諾" in prompt
    assert "NSFW scheduled raw" not in prompt


def test_frontier_prompt_uses_safe_summary_for_nsfw_promise_text() -> None:
    character = Character.create(
        name="Mio",
        summary="咖啡店打工的大學生",
        personality=["溫柔"],
        interests=[],
        speaking_style="輕柔",
        boundaries=[],
        state=CharacterState(
            emotion="平靜", affection=60, fatigue=10, trust=55, energy=80,
        ),
    )
    payload = ScheduledPromiseComposeInput(
        character=character,
        promise_intent="履行一個私密承諾",
        promise_text="NSFW scheduled raw",
        scheduled_for=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        current_activity=None,
        just_finished_activity=None,
        recent_dialogue_summary=None,
        now=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        promise_content_mode=MessageContentMode.NSFW,
        promise_safe_summary="對方希望角色在約定時間延續親密但不露骨的承諾",
    )

    prompt = _build_prompt(payload)

    assert "對方希望角色在約定時間延續親密但不露骨的承諾" in prompt
    assert "NSFW scheduled raw" not in prompt


def test_community_prompt_keeps_nsfw_original_promise_text() -> None:
    character = Character.create(
        name="Mio",
        summary="咖啡店打工的大學生",
        personality=["溫柔"],
        interests=[],
        speaking_style="輕柔",
        boundaries=[],
        state=CharacterState(
            emotion="平靜", affection=60, fatigue=10, trust=55, energy=80,
        ),
    )
    payload = ScheduledPromiseComposeInput(
        character=character,
        promise_intent="履行一個私密承諾",
        promise_text="NSFW scheduled raw",
        scheduled_for=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        current_activity=None,
        just_finished_activity=None,
        recent_dialogue_summary=None,
        now=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        promise_content_mode=MessageContentMode.NSFW,
        content_tolerance=CONTENT_TOLERANCE_COMMUNITY,
    )

    prompt = _build_prompt(payload)

    assert "NSFW scheduled raw" in prompt


@pytest.mark.asyncio
async def test_unreplaceable_nsfw_promise_requests_community_routing_hint() -> None:
    model = _StubModel("我記得這件事", provider_id="local_openai_compatible")
    provider = _RecordingActiveProvider(model)
    composer = LLMScheduledPromiseComposer(provider=provider)
    payload = ScheduledPromiseComposeInput(
        character=_character(),
        promise_intent="履行一個私密承諾",
        promise_text="NSFW scheduled raw",
        scheduled_for=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        current_activity=None,
        just_finished_activity=None,
        recent_dialogue_summary=None,
        now=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        promise_content_mode=MessageContentMode.NSFW,
    )

    await composer.compose(payload)

    assert provider.fake_tolerances == [CONTENT_TOLERANCE_COMMUNITY]
    assert provider.resolve_tolerances == [CONTENT_TOLERANCE_COMMUNITY]
    assert provider.model_id_tolerances == [CONTENT_TOLERANCE_COMMUNITY]
    assert model.calls == 1
    assert "NSFW scheduled raw" in model.prompts[0]


# --- PF1: two-pass tool loop -------------------------------------------


_WEB_SEARCH = PromptToolDescriptor(
    name="web_search",
    description="上網搜尋最新資訊。",
    parameters_schema={
        "type": "object",
        "properties": {"query": {"type": "string"}},
    },
)


def _promise_payload(**overrides) -> ScheduledPromiseComposeInput:
    base = dict(
        character=_character(),
        promise_intent="回家幫對方查夏祭的抽選規則",
        promise_text="你晚點回家幫我查一下好不好",
        scheduled_for=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        current_activity=None,
        just_finished_activity=None,
        recent_dialogue_summary=None,
        now=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return ScheduledPromiseComposeInput(**base)


def test_prompt_without_tools_keeps_the_pre_pf1_shape() -> None:
    """Characterization: a payload with no tools renders no tool
    sections at all and still closes with the plain write-the-message
    instruction — the single-pass call every existing caller makes."""
    prompt = _build_prompt(_promise_payload())

    assert "可用工具" not in prompt
    assert "工具回傳結果" not in prompt
    assert "${" not in prompt
    assert prompt.endswith(
        "請直接寫出你現在要傳給對方的訊息內容，不要加任何前綴或標籤：",
    )


def test_pass_one_prompt_offers_tools_and_the_json_contract() -> None:
    prompt = _build_prompt(_promise_payload(available_tools=(_WEB_SEARCH,)))

    assert "可用工具" in prompt
    assert "web_search" in prompt
    assert '{"tool": "工具名稱", "args": {...}}' in prompt
    # The closing line alone would tell the model to write prose no
    # matter what; the first pass has to say "or call a tool instead".
    assert "這一輪就只輸出工具 JSON" in prompt


def test_pass_two_prompt_carries_the_tool_failure_as_a_fact() -> None:
    prompt = _build_prompt(
        _promise_payload(
            tool_results=(
                ToolOutcomeMessage(
                    tool_name="generate_image",
                    ok=False,
                    output_text="",
                    error="ComfyUI unreachable",
                ),
            ),
        ),
    )

    assert "工具回傳結果" in prompt
    assert "ComfyUI unreachable" in prompt
    assert "請以角色語氣向使用者簡短致歉" in prompt
    # Second pass must not be offered the tool again — the loop only
    # runs one round of calls.
    assert "可用工具" not in prompt


@pytest.mark.asyncio
async def test_first_pass_tool_json_becomes_a_tool_call() -> None:
    model = _StubModel(
        '```json\n{"tool": "web_search", "args": {"query": "夏祭 抽選"}}\n```',
        provider_id="local_openai_compatible",
    )
    composer = LLMScheduledPromiseComposer(provider=_RecordingActiveProvider(model))

    output = await composer.compose(_promise_payload(available_tools=(_WEB_SEARCH,)))

    assert output.content_text == ""
    assert len(output.tool_calls) == 1
    assert output.tool_calls[0].name == "web_search"
    assert output.tool_calls[0].arguments == {"query": "夏祭 抽選"}


@pytest.mark.asyncio
async def test_second_pass_returns_prose_not_a_tool_call() -> None:
    model = _StubModel(
        "查到了！今年抽選是七月一號開始，我幫你記著。",
        provider_id="local_openai_compatible",
    )
    composer = LLMScheduledPromiseComposer(provider=_RecordingActiveProvider(model))

    output = await composer.compose(
        _promise_payload(
            tool_results=(
                ToolOutcomeMessage(
                    tool_name="web_search",
                    ok=True,
                    output_text="抽選 7/1 開始",
                ),
            ),
        ),
    )

    assert output.tool_calls == ()
    assert "七月一號" in output.content_text


@pytest.mark.asyncio
async def test_call_to_an_unoffered_tool_is_suppressed() -> None:
    """The model may name a tool the character isn't allowed. Returning
    it would waste a pass on a call the orchestrator will deny; the
    fail-soft answer is "no output, retry next tick"."""
    model = _StubModel(
        '{"tool": "generate_image", "args": {"positive": "selfie"}}',
        provider_id="local_openai_compatible",
    )
    composer = LLMScheduledPromiseComposer(provider=_RecordingActiveProvider(model))

    output = await composer.compose(_promise_payload(available_tools=(_WEB_SEARCH,)))

    assert output.content_text == ""
    assert output.tool_calls == ()


@pytest.mark.asyncio
async def test_tool_json_when_no_tools_offered_never_reaches_the_player() -> None:
    model = _StubModel(
        '{"tool": "web_search", "args": {"query": "夏祭"}}',
        provider_id="local_openai_compatible",
    )
    composer = LLMScheduledPromiseComposer(provider=_RecordingActiveProvider(model))

    output = await composer.compose(_promise_payload())

    assert output.content_text == ""
    assert output.tool_calls == ()


# --- S4: an object literal is never the promised message ---------------

_FOREIGN_TOOL_SHAPES = [
    # OpenAI function-call habit: name/arguments instead of tool/args.
    '{"name": "generate_image", "arguments": {"positive": "自拍"}}',
    # Single quotes — json.loads never even gets off the ground.
    "{'tool': 'generate_image', 'args': {}}",
    # Nested under a wrapper key, another common upstream convention.
    '{"function_call": {"name": "web_search", "args": {"query": "夏祭"}}}',
    # Fenced, because models fence whatever they emit.
    '```json\n{"name": "web_search", "arguments": {"query": "夏祭"}}\n```',
    # Trailing whitespace/newlines around the blob.
    '\n\n  {"tool_name": "web_search", "parameters": {}}  \n',
]


@pytest.mark.parametrize("raw", _FOREIGN_TOOL_SHAPES)
@pytest.mark.asyncio
async def test_unparseable_object_on_the_tool_pass_never_reaches_the_player(
    raw: str,
) -> None:
    """The prompt asked for *only* the tool JSON this turn. Whatever key
    names or quoting the model reached for, the answer is an object
    literal — not the promised message. Shipping it would put raw JSON
    in the player's chat; empty means "retry next tick"."""
    model = _StubModel(raw, provider_id="local_openai_compatible")
    composer = LLMScheduledPromiseComposer(provider=_RecordingActiveProvider(model))

    output = await composer.compose(_promise_payload(available_tools=(_WEB_SEARCH,)))

    assert output.content_text == ""
    assert output.tool_calls == ()


_PROSE_THAT_MUST_STILL_SHIP = [
    "查到了！今年抽選是七月一號開始，我幫你記著。",
    "調べておいたよ。抽選は 7/1 からだって！",
    "Looked it up — the lottery opens on July 1st.",
    "查到囉～ 🎆 抽選 7/1 開始，別忘了喔！",
    "我查完了。\n\n抽選是七月一號開始，\n記得提早報名。",
    # Braces inside prose (roleplay action markers, quoted fragments).
    "{微笑}我查到了，抽選七月一號開始。",
    "抽選在 {7/1} 開始，我幫你標起來了。",
    '網站上寫著 {"date": "7/1"}，我猜是抽選開始日。',
]


@pytest.mark.parametrize("raw", _PROSE_THAT_MUST_STILL_SHIP)
@pytest.mark.asyncio
async def test_ordinary_messages_still_ship_on_the_tool_pass(raw: str) -> None:
    """The object-literal guard must not eat real messages — including
    ones that merely contain braces or emoji."""
    model = _StubModel(raw, provider_id="local_openai_compatible")
    composer = LLMScheduledPromiseComposer(provider=_RecordingActiveProvider(model))

    output = await composer.compose(_promise_payload(available_tools=(_WEB_SEARCH,)))

    assert output.tool_calls == ()
    assert output.content_text == raw.strip()


# --- S4 (second round): the guard belongs to the *output*, not the pass -

_SEARCH_RESULT = ToolOutcomeMessage(
    tool_name="web_search",
    ok=True,
    output_text="抽選 7/1 開始",
)


@pytest.mark.parametrize(
    "raw",
    [
        *_FOREIGN_TOOL_SHAPES,
        '[{"name": "web_search", "arguments": {"query": "夏祭"}}]',
    ],
)
@pytest.mark.asyncio
async def test_object_literal_on_the_second_pass_never_reaches_the_player(
    raw: str,
) -> None:
    """The second pass carries ``tool_results`` and **no** tools, so a
    guard hung off "did this pass offer tools" skips it entirely — and
    the second pass is the one whose text goes straight to the player,
    image attached. Every foreign shape must die here too."""
    model = _StubModel(raw, provider_id="local_openai_compatible")
    composer = LLMScheduledPromiseComposer(provider=_RecordingActiveProvider(model))

    output = await composer.compose(
        _promise_payload(tool_results=(_SEARCH_RESULT,)),
    )

    assert output.content_text == ""
    assert output.tool_calls == ()


_CALL_SHAPES_WITH_SOMETHING_AROUND_THEM = [
    # The model narrating the call it was told to emit alone.
    '好的，我幫你查一下：\n{"name": "generate_image", "arguments": {"positive": "自拍"}}',
    # Markdown formatting around the blob.
    '## 工具呼叫\n\n{"name": "web_search", "arguments": {"query": "夏祭"}}',
    # Batched-call habit with prose in front.
    '我先查一下喔\n[{"name": "web_search", "arguments": {"query": "夏祭"}}]',
    # Commentary trailing the blob.
    '{"name": "web_search", "arguments": {"query": "夏祭"}}\n查完再跟你說！',
]


@pytest.mark.parametrize("raw", _CALL_SHAPES_WITH_SOMETHING_AROUND_THEM)
@pytest.mark.asyncio
async def test_wrapped_call_shapes_never_reach_the_player_on_the_tool_pass(
    raw: str,
) -> None:
    """A failed call doesn't stop looking like a failed call because the
    model wrapped a sentence around it. This pass told the model to emit
    the JSON alone, so a call-shaped structure anywhere in the answer is
    a call it fumbled — not the promised message."""
    model = _StubModel(raw, provider_id="local_openai_compatible")
    composer = LLMScheduledPromiseComposer(provider=_RecordingActiveProvider(model))

    output = await composer.compose(_promise_payload(available_tools=(_WEB_SEARCH,)))

    assert output.content_text == ""
    assert output.tool_calls == ()


@pytest.mark.parametrize(
    "raw",
    [
        *_PROSE_THAT_MUST_STILL_SHIP,
        # Written on the pass that reports what the search returned.
        "查到了，7/1 開始抽選。\n\n我幫你把日期記下來了，別忘了喔。",
        "你要的片段：\n```python\ndef greet(name):\n    return f\"哈囉 {name}\"\n```",
    ],
)
@pytest.mark.asyncio
async def test_ordinary_messages_still_ship_on_the_second_pass(raw: str) -> None:
    """The narrow width is the one the promise-delivering pass runs, and
    it has to stay narrow: a message suppressed here is a promise the
    player never receives, with the row retrying forever behind it."""
    model = _StubModel(raw, provider_id="local_openai_compatible")
    composer = LLMScheduledPromiseComposer(provider=_RecordingActiveProvider(model))

    output = await composer.compose(
        _promise_payload(tool_results=(_SEARCH_RESULT,)),
    )

    assert output.tool_calls == ()
    assert output.content_text != ""


# --------------------------------------------------------------------- #
# TC — the promised moment's freshness.
#
# The template hard-coded 「（剛到）」 next to the promised time. That is a
# lie on every late release, and this path has four ways to be late
# (honesty park +300s, judge outage +900s, quality park +900s, any tick
# outage or leader handover) — after which the model was shown
# 「約定時間：…（剛到）」 directly above a 現在時間 line reading two days
# later. Two contradictory facts, and nothing saying which to believe.
# --------------------------------------------------------------------- #

_FRESHNESS_NOW = datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc)


def _freshness_payload(*, late: timedelta) -> ScheduledPromiseComposeInput:
    return ScheduledPromiseComposeInput(
        character=_character(),
        promise_intent="叫對方起床",
        promise_text="明天十點叫我起床",
        scheduled_for=_FRESHNESS_NOW - late,
        current_activity=None,
        just_finished_activity=None,
        recent_dialogue_summary=None,
        now=_FRESHNESS_NOW,
    )


def _scheduled_line(prompt: str) -> str:
    """The 約定時間 bullet alone.

    Asserted on in isolation because the discipline lines added beside it
    *quote* both renderings — a whole-prompt substring check for 「剛到」
    passes no matter which branch actually ran.
    """
    return next(
        line for line in prompt.splitlines() if line.startswith("- 約定時間：")
    )


def test_promise_prompt_says_just_now_when_the_slot_really_just_came_due() -> None:
    line = _scheduled_line(
        _build_prompt(_freshness_payload(late=timedelta(minutes=2))),
    )

    assert "（剛到）" in line
    assert "你晚了" not in line


def test_promise_prompt_says_just_now_within_ordinary_tick_jitter() -> None:
    """The dispatcher ticks about every five minutes; a character must not
    apologise for the scheduler's own granularity."""
    line = _scheduled_line(
        _build_prompt(_freshness_payload(late=timedelta(minutes=5))),
    )

    assert "（剛到）" in line


def test_promise_prompt_states_how_late_it_actually_is() -> None:
    line = _scheduled_line(
        _build_prompt(_freshness_payload(late=timedelta(days=1, hours=2))),
    )

    assert "（剛到）" not in line
    assert "你晚了" in line
    assert "已經過了 約 1 天" in line


def test_promise_prompt_marks_a_quality_park_retry_as_late() -> None:
    """The 900-second parks (quality, honesty-outage) are the common way
    this path runs late, and the one that used to read as punctual."""
    line = _scheduled_line(
        _build_prompt(_freshness_payload(late=timedelta(seconds=900))),
    )

    assert "你晚了" in line
    assert "已經過了 約 15 分鐘" in line


def test_promise_prompt_teaches_what_to_do_when_late() -> None:
    """Stating the fact is half the fix; without the discipline line the
    model has a number and no instruction — the exact shape of the
    follow-up composer's own defect."""
    prompt = _build_prompt(_freshness_payload(late=timedelta(hours=20)))

    assert "時效紀律" in prompt
    assert "不要假裝準時" in prompt
