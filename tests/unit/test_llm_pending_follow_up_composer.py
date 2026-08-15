"""Parser-level tests for :class:`LLMPendingFollowUpComposer`.

Output is plain prose (single string), so the tests focus on:

* Empty queued list → fail-soft empty body.
* LLM exception → fail-soft empty body.
* Length cap trims rather than rejecting.
* Whitespace / fence normalisation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.pending_follow_up_composer import (
    PendingFollowUpComposeInput,
)
from kokoro_link.contracts.prompt import PromptToolDescriptor, ToolOutcomeMessage
from kokoro_link.domain.value_objects.content_flow import CONTENT_TOLERANCE_COMMUNITY
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.pending_follow_up import (
    PendingFollowUpMessage,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.disposition import CharacterDisposition
from kokoro_link.domain.value_objects.personality_type import (
    CharacterPersonalityType,
)
from kokoro_link.infrastructure.busy.llm_follow_up_composer import (
    LLMPendingFollowUpComposer,
    NullPendingFollowUpComposer,
    _MAX_REPLY_CHARS,
    _build_prompt,
    _normalize,
)


def _character(
    *,
    disposition: CharacterDisposition | None = None,
    personality_type: CharacterPersonalityType | None = None,
) -> Character:
    return Character.create(
        name="Airi",
        summary="社畜 OL",
        personality=["責任感重", "怕對方等"],
        interests=[],
        speaking_style="平淡",
        boundaries=[],
        state=CharacterState(
            emotion="放鬆", affection=60, fatigue=20, trust=55, energy=70,
        ),
        disposition=disposition,
        personality_type=personality_type,
    )


def _now() -> datetime:
    return datetime(2026, 5, 16, 15, 30, tzinfo=timezone.utc)


def _queued_messages() -> tuple[PendingFollowUpMessage, ...]:
    base = _now() - timedelta(hours=1)
    return (
        PendingFollowUpMessage.new(content="你在嗎", queued_at=base),
        PendingFollowUpMessage.new(content="晚餐吃什麼", queued_at=base + timedelta(minutes=5)),
    )


def _input(queued: tuple[PendingFollowUpMessage, ...] | None = None) -> PendingFollowUpComposeInput:
    return PendingFollowUpComposeInput(
        character=_character(),
        queued_messages=queued if queued is not None else _queued_messages(),
        brief_reply="先回，會議結束再好好回你",
        defer_reason="會議中",
        queued_at=_now() - timedelta(hours=1),
        just_finished_activity=None,
        current_activity=None,
        recent_dialogue_summary=None,
        now=_now(),
    )


def test_prompt_includes_operator_persona_lines() -> None:
    payload = _input()
    payload = PendingFollowUpComposeInput(
        character=payload.character,
        queued_messages=payload.queued_messages,
        brief_reply=payload.brief_reply,
        defer_reason=payload.defer_reason,
        queued_at=payload.queued_at,
        just_finished_activity=payload.just_finished_activity,
        current_activity=payload.current_activity,
        recent_dialogue_summary=payload.recent_dialogue_summary,
        now=payload.now,
        operator_persona_lines=("- 對方資料：職業是後端工程師。",),
    )

    prompt = _build_prompt(payload)

    assert "職業是後端工程師" in prompt
    assert "不要裝熟" in prompt


def test_prompt_includes_disposition_and_personality_type_lines() -> None:
    payload = _input()
    character = _character(
        disposition=CharacterDisposition(sharing_drive="high"),
        personality_type=CharacterPersonalityType(
            code="ENFP",
            rationale="外放、容易被新鮮事點燃。",
        ),
    )
    payload = PendingFollowUpComposeInput(
        character=character,
        queued_messages=payload.queued_messages,
        brief_reply=payload.brief_reply,
        defer_reason=payload.defer_reason,
        queued_at=payload.queued_at,
        just_finished_activity=payload.just_finished_activity,
        current_activity=payload.current_activity,
        recent_dialogue_summary=payload.recent_dialogue_summary,
        now=payload.now,
    )

    prompt = _build_prompt(payload)

    assert "你的內在表達傾向" in prompt
    assert "連珠炮一樣連發幾則" in prompt
    assert "16 型性格參考" in prompt
    assert "ENFP" in prompt


def test_prompt_injects_operator_local_current_time() -> None:
    payload = _input()
    payload = PendingFollowUpComposeInput(
        character=payload.character,
        queued_messages=payload.queued_messages,
        brief_reply=payload.brief_reply,
        defer_reason=payload.defer_reason,
        queued_at=payload.queued_at,
        just_finished_activity=payload.just_finished_activity,
        current_activity=payload.current_activity,
        recent_dialogue_summary=payload.recent_dialogue_summary,
        now=datetime(2026, 6, 19, 23, 30, tzinfo=timezone.utc),
        local_tz=ZoneInfo("Asia/Taipei"),
    )

    prompt = _build_prompt(payload)

    assert "現在時間：2026-06-20 07:30" in prompt
    assert "清晨" in prompt


class _StubModel(ChatModelPort):
    supports_vision = False

    def __init__(self, response: str, *, provider_id: str = "fake") -> None:
        self.response = response
        self.provider_id = provider_id
        self.calls = 0
        self.prompts: list[str] = []

    async def generate(self, prompt: str, **kwargs: object) -> str:
        self.calls += 1
        self.prompts.append(prompt)
        return self.response

    async def generate_stream(  # pragma: no cover - unused
        self, prompt: str, **kwargs: object,
    ) -> AsyncIterator[str]:
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


class TestNormalize:
    def test_strips_code_fence(self) -> None:
        assert _normalize("```\n剛開完會，晚餐我想吃義大利麵\n```") == (
            "剛開完會，晚餐我想吃義大利麵"
        )

    def test_blank_returns_empty(self) -> None:
        assert _normalize("") == ""
        assert _normalize("   \n\n  ") == ""

    def test_caps_length_with_clean_sentence_break(self) -> None:
        long = "我剛剛在開會。" + ("補充說明很多很長的字。" * 100)
        out = _normalize(long)
        assert len(out) <= _MAX_REPLY_CHARS
        # Should end at a sentence-ish boundary, not mid-word.
        assert out.endswith("。") or out.endswith("？") or out.endswith("！")


class TestCompose:
    @pytest.mark.asyncio
    async def test_empty_queue_short_circuits(self) -> None:
        model = _StubModel("會議結束了…")
        composer = LLMPendingFollowUpComposer(model=model)
        out = await composer.compose(_input(queued=()))
        assert out.content_text == ""
        assert model.calls == 0

    @pytest.mark.asyncio
    async def test_happy_path_returns_normalised_body(self) -> None:
        model = _StubModel("```\n剛剛會議很長，抱歉。晚餐我想吃義大利麵欸。\n```")
        composer = LLMPendingFollowUpComposer(model=model)
        out = await composer.compose(_input())
        assert "義大利麵" in out.content_text
        assert "```" not in out.content_text

    @pytest.mark.asyncio
    async def test_llm_crash_returns_empty(self) -> None:
        class _Boom(ChatModelPort):
            supports_vision = False

            async def generate(self, prompt: str, **kwargs: object) -> str:
                raise RuntimeError("backend down")

            async def generate_stream(  # pragma: no cover - unused
                self, prompt: str, **kwargs: object,
            ) -> AsyncIterator[str]:
                yield ""

        composer = LLMPendingFollowUpComposer(model=_Boom())
        out = await composer.compose(_input())
        assert out.content_text == ""

    @pytest.mark.asyncio
    async def test_null_composer_always_empty(self) -> None:
        composer = NullPendingFollowUpComposer()
        out = await composer.compose(_input())
        assert out.content_text == ""

    @pytest.mark.asyncio
    async def test_frontier_provider_omits_nsfw_queued_message(self) -> None:
        from kokoro_link.domain.entities.conversation import MessageContentMode

        model = _StubModel("我回來了", provider_id="openai")
        composer = LLMPendingFollowUpComposer(model=model)
        queued = (
            PendingFollowUpMessage.new(
                content="NSFW queued raw",
                queued_at=_now(),
                content_mode=MessageContentMode.NSFW,
            ),
        )

        out = await composer.compose(_input(queued=queued))

        assert out.content_text == "我回來了"
        assert "NSFW queued raw" not in model.prompts[0]
        assert "目前模型容忍度下不可直接提供" in model.prompts[0]

    @pytest.mark.asyncio
    async def test_frontier_provider_uses_safe_summary_for_nsfw_queued_message(self) -> None:
        from kokoro_link.domain.entities.conversation import MessageContentMode

        model = _StubModel("我回來了", provider_id="openai")
        composer = LLMPendingFollowUpComposer(model=model)
        queued = (
            PendingFollowUpMessage.new(
                content="NSFW queued raw",
                queued_at=_now(),
                content_mode=MessageContentMode.NSFW,
                safe_summary="對方延續私密但不露骨的情緒需求",
            ),
        )

        await composer.compose(_input(queued=queued))

        assert "對方延續私密但不露骨的情緒需求" in model.prompts[0]
        assert "NSFW queued raw" not in model.prompts[0]
        assert "目前模型容忍度下不可直接提供" not in model.prompts[0]

    @pytest.mark.asyncio
    async def test_community_provider_keeps_nsfw_queued_message(self) -> None:
        from kokoro_link.domain.entities.conversation import MessageContentMode

        model = _StubModel("我回來了", provider_id="local_openai_compatible")
        composer = LLMPendingFollowUpComposer(model=model)
        queued = (
            PendingFollowUpMessage.new(
                content="NSFW queued raw",
                queued_at=_now(),
                content_mode=MessageContentMode.NSFW,
            ),
        )

        await composer.compose(_input(queued=queued))

        assert "NSFW queued raw" in model.prompts[0]

    @pytest.mark.asyncio
    async def test_unreplaceable_nsfw_queue_requests_community_routing_hint(self) -> None:
        from kokoro_link.domain.entities.conversation import MessageContentMode

        model = _StubModel("我回來了", provider_id="local_openai_compatible")
        provider = _RecordingActiveProvider(model)
        composer = LLMPendingFollowUpComposer(provider=provider)
        queued = (
            PendingFollowUpMessage.new(
                content="NSFW queued raw",
                queued_at=_now(),
                content_mode=MessageContentMode.NSFW,
            ),
        )

        await composer.compose(_input(queued=queued))

        assert provider.fake_tolerances == [CONTENT_TOLERANCE_COMMUNITY]
        assert provider.resolve_tolerances == [CONTENT_TOLERANCE_COMMUNITY]
        assert provider.model_id_tolerances == [CONTENT_TOLERANCE_COMMUNITY]
        assert model.calls == 1
        assert "NSFW queued raw" in model.prompts[0]


# --- PF2: two-pass tool loop (busy-defer follow-up) ---------------------


_WEB_SEARCH = PromptToolDescriptor(
    name="web_search",
    description="上網搜尋最新資訊。",
    parameters_schema={
        "type": "object",
        "properties": {"query": {"type": "string"}},
    },
)


def _input_with(**overrides) -> PendingFollowUpComposeInput:
    payload = _input()
    base = dict(
        character=payload.character,
        queued_messages=payload.queued_messages,
        brief_reply=payload.brief_reply,
        defer_reason=payload.defer_reason,
        queued_at=payload.queued_at,
        just_finished_activity=payload.just_finished_activity,
        current_activity=payload.current_activity,
        recent_dialogue_summary=payload.recent_dialogue_summary,
        now=payload.now,
    )
    base.update(overrides)
    return PendingFollowUpComposeInput(**base)


def test_prompt_without_tools_keeps_the_pre_pf2_shape() -> None:
    """Characterization: a payload with no tools renders no tool
    sections at all and still closes with the plain write-the-reply
    instruction — the single-pass call every existing caller makes."""
    prompt = _build_prompt(_input_with())

    assert "可用工具" not in prompt
    assert "工具回傳結果" not in prompt
    assert "${" not in prompt
    assert prompt.endswith(
        "請直接寫出你要傳給對方的訊息內容，不要加任何前綴或標籤：",
    )


def test_pass_one_prompt_offers_tools_and_the_json_contract() -> None:
    prompt = _build_prompt(_input_with(available_tools=(_WEB_SEARCH,)))

    assert "可用工具" in prompt
    assert "web_search" in prompt
    assert '{"tool": "工具名稱", "args": {...}}' in prompt
    # The closing line alone would tell the model to write prose no
    # matter what; the first pass has to say "or call a tool instead".
    assert "這一輪就只輸出工具 JSON" in prompt


def test_pass_two_prompt_carries_the_tool_failure_as_a_fact() -> None:
    prompt = _build_prompt(
        _input_with(
            tool_results=(
                ToolOutcomeMessage(
                    tool_name="web_search",
                    ok=False,
                    output_text="",
                    error="search backend unreachable",
                ),
            ),
        ),
    )

    assert "工具回傳結果" in prompt
    assert "search backend unreachable" in prompt
    assert "請以角色語氣向使用者簡短致歉" in prompt
    # Second pass must not be offered the tool again — the loop only
    # runs one round of calls.
    assert "可用工具" not in prompt


@pytest.mark.asyncio
async def test_first_pass_tool_json_becomes_a_tool_call() -> None:
    model = _StubModel(
        '```json\n{"tool": "web_search", "args": {"query": "晚餐推薦"}}\n```',
        provider_id="local_openai_compatible",
    )
    composer = LLMPendingFollowUpComposer(model=model)

    output = await composer.compose(_input_with(available_tools=(_WEB_SEARCH,)))

    assert output.content_text == ""
    assert len(output.tool_calls) == 1
    assert output.tool_calls[0].name == "web_search"
    assert output.tool_calls[0].arguments == {"query": "晚餐推薦"}


@pytest.mark.asyncio
async def test_second_pass_returns_prose_not_a_tool_call() -> None:
    model = _StubModel(
        "查到了！附近那家義大利麵今天有開，我幫你留意到了。",
        provider_id="local_openai_compatible",
    )
    composer = LLMPendingFollowUpComposer(model=model)

    output = await composer.compose(
        _input_with(
            tool_results=(
                ToolOutcomeMessage(
                    tool_name="web_search",
                    ok=True,
                    output_text="義大利麵店今天有開",
                ),
            ),
        ),
    )

    assert output.tool_calls == ()
    assert "義大利麵" in output.content_text


@pytest.mark.asyncio
async def test_call_to_an_unoffered_tool_is_suppressed() -> None:
    """The model may name a tool the character isn't allowed. Returning
    it would waste a pass on a call the orchestrator will deny; the
    fail-soft answer is "no output, retry next tick"."""
    model = _StubModel(
        '{"tool": "generate_image", "args": {"positive": "selfie"}}',
        provider_id="local_openai_compatible",
    )
    composer = LLMPendingFollowUpComposer(model=model)

    output = await composer.compose(_input_with(available_tools=(_WEB_SEARCH,)))

    assert output.content_text == ""
    assert output.tool_calls == ()


@pytest.mark.asyncio
async def test_tool_json_when_no_tools_offered_never_reaches_the_player() -> None:
    model = _StubModel(
        '{"tool": "web_search", "args": {"query": "晚餐推薦"}}',
        provider_id="local_openai_compatible",
    )
    composer = LLMPendingFollowUpComposer(model=model)

    output = await composer.compose(_input_with())

    assert output.content_text == ""
    assert output.tool_calls == ()


# --- S4: an object literal is never the follow-up reply ----------------

_FOREIGN_TOOL_SHAPES = [
    # OpenAI function-call habit: name/arguments instead of tool/args.
    '{"name": "generate_image", "arguments": {"positive": "自拍"}}',
    # Single quotes — json.loads never even gets off the ground.
    "{'tool': 'generate_image', 'args': {}}",
    # Nested under a wrapper key, another common upstream convention.
    '{"function_call": {"name": "web_search", "args": {"query": "晚餐"}}}',
    # Fenced, because models fence whatever they emit.
    '```json\n{"name": "web_search", "arguments": {"query": "晚餐"}}\n```',
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
    literal — not the follow-up reply. Shipping it would put raw JSON in
    the player's chat; empty means "retry next tick"."""
    model = _StubModel(raw, provider_id="local_openai_compatible")
    composer = LLMPendingFollowUpComposer(model=model)

    output = await composer.compose(_input_with(available_tools=(_WEB_SEARCH,)))

    assert output.content_text == ""
    assert output.tool_calls == ()


_PROSE_THAT_MUST_STILL_SHIP = [
    "查到了！附近那家義大利麵今天有開，我幫你留意到了。",
    "調べたよ。近くのパスタ屋さん、今日は開いてるって！",
    "Looked it up — the pasta place is open today.",
    "查到囉～ 🍝 那家今天有開，要去嗎？",
    "我查完了。\n\n那家今天有開，\n晚點一起去吧。",
    # Braces inside prose (roleplay action markers, quoted fragments).
    "{笑}我查到了，那家今天有開。",
    "營業到 {21:00}，我幫你記著了。",
    '網站上寫著 {"open": true}，應該是有開。',
]


@pytest.mark.parametrize("raw", _PROSE_THAT_MUST_STILL_SHIP)
@pytest.mark.asyncio
async def test_ordinary_replies_still_ship_on_the_tool_pass(raw: str) -> None:
    """The object-literal guard must not eat real replies — including
    ones that merely contain braces or emoji."""
    model = _StubModel(raw, provider_id="local_openai_compatible")
    composer = LLMPendingFollowUpComposer(model=model)

    output = await composer.compose(_input_with(available_tools=(_WEB_SEARCH,)))

    assert output.tool_calls == ()
    assert output.content_text == raw.strip()


# --- S4 (second round): the guard belongs to the *output*, not the pass -

_SEARCH_RESULT = ToolOutcomeMessage(
    tool_name="web_search",
    ok=True,
    output_text="義大利麵店今天有開",
)


@pytest.mark.parametrize(
    "raw",
    [
        *_FOREIGN_TOOL_SHAPES,
        '[{"name": "web_search", "arguments": {"query": "晚餐"}}]',
    ],
)
@pytest.mark.asyncio
async def test_object_literal_on_the_second_pass_never_reaches_the_player(
    raw: str,
) -> None:
    """The second pass carries ``tool_results`` and **no** tools, so a
    guard hung off "did this pass offer tools" skips it entirely — and
    the second pass is the one whose text goes straight to the player,
    attachments included. Every foreign shape must die here too."""
    model = _StubModel(raw, provider_id="local_openai_compatible")
    composer = LLMPendingFollowUpComposer(model=model)

    output = await composer.compose(_input_with(tool_results=(_SEARCH_RESULT,)))

    assert output.content_text == ""
    assert output.tool_calls == ()


_CALL_SHAPES_WITH_SOMETHING_AROUND_THEM = [
    # The model narrating the call it was told to emit alone.
    '好的，我幫你查一下：\n{"name": "generate_image", "arguments": {"positive": "自拍"}}',
    # Markdown formatting around the blob.
    '## 工具呼叫\n\n{"name": "web_search", "arguments": {"query": "晚餐"}}',
    # Batched-call habit with prose in front.
    '我先查一下喔\n[{"name": "web_search", "arguments": {"query": "晚餐"}}]',
    # Commentary trailing the blob.
    '{"name": "web_search", "arguments": {"query": "晚餐"}}\n查完再跟你說！',
]


@pytest.mark.parametrize("raw", _CALL_SHAPES_WITH_SOMETHING_AROUND_THEM)
@pytest.mark.asyncio
async def test_wrapped_call_shapes_never_reach_the_player_on_the_tool_pass(
    raw: str,
) -> None:
    """A failed call doesn't stop looking like a failed call because the
    model wrapped a sentence around it. This pass told the model to emit
    the JSON alone, so a call-shaped structure anywhere in the answer is
    a call it fumbled — not the follow-up reply."""
    model = _StubModel(raw, provider_id="local_openai_compatible")
    composer = LLMPendingFollowUpComposer(model=model)

    output = await composer.compose(_input_with(available_tools=(_WEB_SEARCH,)))

    assert output.content_text == ""
    assert output.tool_calls == ()


@pytest.mark.parametrize(
    "raw",
    [
        *_PROSE_THAT_MUST_STILL_SHIP,
        # Written on the pass that reports what the search returned.
        "久等了，那家今天有開。\n\n晚點我們一起去吧？",
        "你要的片段：\n```python\ndef greet(name):\n    return f\"哈囉 {name}\"\n```",
    ],
)
@pytest.mark.asyncio
async def test_ordinary_replies_still_ship_on_the_second_pass(raw: str) -> None:
    """The narrow width is the one the reply-delivering pass runs, and it
    has to stay narrow: a reply suppressed here is a follow-up the player
    never receives, with the row retrying forever behind it."""
    model = _StubModel(raw, provider_id="local_openai_compatible")
    composer = LLMPendingFollowUpComposer(model=model)

    output = await composer.compose(_input_with(tool_results=(_SEARCH_RESULT,)))

    assert output.tool_calls == ()
    assert output.content_text != ""
