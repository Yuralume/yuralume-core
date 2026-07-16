"""BDD: ChatService tool-use cycle.

Scenario: user asks Yuki for a picture of herself → model emits a
fenced JSON tool call → ChatService runs the fake image tool via the
orchestrator → second-hop prompt carries the tool outcome → model
produces a natural-language reply → assistant message carries the
attachment URL.

Also covers the guard-rails:
- character without the tool in ``allowed_tools`` cannot call it
- unparseable first reply falls through as the final answer
- tool-cycle exhausted (two hops) still returns a user-facing text
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.dto.chat import SendChatMessageRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.application.services.tool_orchestrator import ToolOrchestrator
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.value_objects.account_runtime_profile import (
    DEMO_ACCOUNT_RUNTIME_PROFILE,
)
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.post_turn.null_processor import NullPostTurnProcessor
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.repositories.in_memory_account_runtime_usage import (
    InMemoryAccountRuntimeUsageRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_tool_invocations import (
    InMemoryToolInvocationRepository,
)
from kokoro_link.infrastructure.state.simple import SimpleStateEngine
from kokoro_link.infrastructure.tools.fake_tools import EchoTool, FakeImageTool
from kokoro_link.infrastructure.tools.registry import InMemoryToolRegistry


_FAKE_IMAGE_URL = "/uploads/stub/fake.png"


class _StaticDemoRuntimeProfileResolver:
    async def resolve_for_operator(self, operator_id: str):
        return DEMO_ACCOUNT_RUNTIME_PROFILE


class _MutableClock:
    def __init__(self, initial: datetime) -> None:
        self.current = initial

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


class _ScriptedModel(ChatModelPort):
    """Yields a pre-programmed sequence of replies for ``generate``.

    Streaming isn't used by the tool-use path so the stream method
    just delegates to ``generate`` for symmetry.
    """

    def __init__(self, replies: list[str]) -> None:
        self.provider_id = "fake"
        self._replies = replies
        self.calls: list[str] = []

    supports_vision = False

    async def generate(self, prompt: str, **kwargs) -> str:
        self.calls.append(prompt)
        if self._replies:
            return self._replies.pop(0)
        return "（沒有更多腳本）"

    async def generate_stream(self, prompt: str, **kwargs) -> AsyncIterator[str]:
        text = await self.generate(prompt)

        async def _iter() -> AsyncIterator[str]:
            yield text

        return _iter()


def _build_chat_service(
    *,
    replies: list[str],
    allowed_tools: list[str] | None = None,
) -> tuple[ChatService, CharacterService, _ScriptedModel, InMemoryToolInvocationRepository]:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    invocation_repository = InMemoryToolInvocationRepository()

    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    scripted = _ScriptedModel(replies)
    registry.register(scripted)

    tool_registry = InMemoryToolRegistry([
        EchoTool(),
        FakeImageTool(url=_FAKE_IMAGE_URL),
    ])
    orchestrator = ToolOrchestrator(
        registry=tool_registry,
        invocation_repository=invocation_repository,
    )

    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        tool_registry=tool_registry,
        tool_orchestrator=orchestrator,
    )
    character_service = CharacterService(character_repository)
    return chat_service, character_service, scripted, invocation_repository


async def _seed_character(
    service: CharacterService, *, allowed_tools: list[str],
) -> str:
    created = await service.create_character(
        CreateCharacterRequest(name="Yuki", allowed_tools=allowed_tools),
    )
    return created.id


@pytest.mark.asyncio
async def test_tool_call_runs_and_second_hop_produces_user_reply() -> None:
    chat, chars, model, invocations = _build_chat_service(
        replies=[
            '```json\n{"tool": "fake_image", "args": {"scene": "窗邊", "caption": "現在的我"}}\n```',
            "我剛畫了一張現在的我，希望你喜歡～",
        ],
    )
    character_id = await _seed_character(chars, allowed_tools=["fake_image"])

    response = await chat.send_message(SendChatMessageRequest(
        character_id=character_id, message="傳一張你現在的照片給我",
    ))

    # Final assistant text is the second-hop reply, not the raw tool JSON.
    assert response.assistant_message.content == "我剛畫了一張現在的我，希望你喜歡～"
    # Attachment flowed through from tool → assistant message.
    assert len(response.assistant_message.attachments) == 1
    assert response.assistant_message.attachments[0].url == _FAKE_IMAGE_URL
    assert response.assistant_message.attachments[0].kind == "image"
    # Two model.generate calls happened: one for the tool call, one
    # for the natural reply with the outcome injected.
    assert len(model.calls) == 2
    # Audit log has exactly one invocation row, successful.
    logs = await invocations.list_for_character(character_id)
    assert len(logs) == 1
    assert logs[0].tool_name == "fake_image"
    assert logs[0].status == "success"


@pytest.mark.asyncio
async def test_disallowed_tool_call_falls_through_without_crash() -> None:
    chat, chars, model, invocations = _build_chat_service(
        replies=[
            '```json\n{"tool": "fake_image", "args": {"scene": "x"}}\n```',
            "抱歉我剛剛沒辦法做到那件事。",
        ],
    )
    # No allowed_tools → registry returns empty list → tool cycle skipped
    character_id = await _seed_character(chars, allowed_tools=[])

    response = await chat.send_message(SendChatMessageRequest(
        character_id=character_id, message="畫一張",
    ))

    # Since no tools are offered, the raw JSON is what the user sees.
    # This is by design: a character without tools shouldn't have its
    # replies filtered. The first (and only) model call produces the
    # visible reply verbatim.
    assert len(model.calls) == 1
    assert "fake_image" in response.assistant_message.content
    # And no tool ran.
    assert await invocations.list_for_character(character_id) == []


@pytest.mark.asyncio
async def test_plain_text_reply_skips_tool_cycle() -> None:
    chat, chars, model, invocations = _build_chat_service(
        replies=[
            "今天天氣很好呢，你那邊呢？",
            # Second reply exists but should never be fetched.
            "不該被用到的第二輪",
        ],
    )
    character_id = await _seed_character(chars, allowed_tools=["fake_image"])

    response = await chat.send_message(SendChatMessageRequest(
        character_id=character_id, message="天氣如何？",
    ))

    assert response.assistant_message.content == "今天天氣很好呢，你那邊呢？"
    assert response.assistant_message.attachments == []
    assert len(model.calls) == 1
    assert await invocations.list_for_character(character_id) == []


@pytest.mark.asyncio
async def test_character_without_tool_registry_behaves_as_before() -> None:
    """A ChatService built without tool plumbing still works — important
    for older tests and the fake-provider dev path."""
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    scripted = _ScriptedModel(["直接回覆，不碰工具"])
    registry.register(scripted)

    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        # tool_registry + tool_orchestrator intentionally omitted
    )
    character_service = CharacterService(character_repository)
    character_id = await _seed_character(character_service, allowed_tools=["fake_image"])

    response = await chat_service.send_message(SendChatMessageRequest(
        character_id=character_id, message="嗨",
    ))

    assert response.assistant_message.content == "直接回覆，不碰工具"
    assert len(scripted.calls) == 1


# ---------------------------------------------------------------------------
# Image-trigger command: force ``generate_image`` regardless of LLM decision
# ---------------------------------------------------------------------------


from dataclasses import dataclass  # noqa: E402

from kokoro_link.contracts.tool import ToolContext, ToolPort  # noqa: E402
from kokoro_link.domain.value_objects.tool_call import (  # noqa: E402
    ToolAttachment, ToolResult,
)


@dataclass
class _RealNameImageTool(ToolPort):
    """Tool registered under the production name ``generate_image`` so
    the command-forced path actually finds something allowed to run.
    We can't rename ``FakeImageTool`` — other tests depend on that name.
    """

    name: str = "generate_image"
    description: str = "Production-name stub for trigger-command tests."
    parameters_schema: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.parameters_schema is None:
            self.parameters_schema = {
                "type": "object",
                "properties": {"positive": {"type": "string"}},
                "required": ["positive"],
            }
        self.last_positive: str | None = None
        self.invoke_count = 0

    async def invoke(self, ctx: ToolContext) -> ToolResult:
        self.invoke_count += 1
        positive = str(ctx.arguments.get("positive") or "")
        self.last_positive = positive
        return ToolResult.success(
            output_text=f"已產生：{positive}",
            attachments=[
                ToolAttachment(
                    kind="image", url="/uploads/stub/forced.png",
                    mime_type="image/png", caption=positive[:60],
                ),
            ],
        )


def _build_forced_trigger_service(
    *, replies: list[str],
    account_runtime_profile_resolver=None,
    account_runtime_usage_repository=None,
    clock=None,
) -> tuple[ChatService, CharacterService, _ScriptedModel, _RealNameImageTool]:
    """Wire a ChatService with a tool registered under the production
    name ``generate_image`` — needed because the forced-trigger code path
    filters by that exact tool name."""
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    invocation_repository = InMemoryToolInvocationRepository()

    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    scripted = _ScriptedModel(replies)
    registry.register(scripted)

    image_tool = _RealNameImageTool()
    tool_registry = InMemoryToolRegistry([EchoTool(), image_tool])
    orchestrator = ToolOrchestrator(
        registry=tool_registry,
        invocation_repository=invocation_repository,
    )

    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        tool_registry=tool_registry,
        tool_orchestrator=orchestrator,
        account_runtime_profile_resolver=account_runtime_profile_resolver,
        account_runtime_usage_repository=account_runtime_usage_repository,
        clock=clock,
    )
    character_service = CharacterService(character_repository)
    return chat_service, character_service, scripted, image_tool


async def _seed_trigger_character(
    service: CharacterService,
    *,
    allowed_tools: list[str],
) -> str:
    created = await service.create_character(
        CreateCharacterRequest(
            name="Yuki",
            allowed_tools=allowed_tools,
        ),
    )
    return created.id


@pytest.mark.asyncio
async def test_pic_command_forces_llm_to_emit_tool_call_and_keeps_context() -> None:
    """Standalone ``/pic`` + allowed_tools → this turn *must* route through
    ``generate_image``, but the LLM still writes the JSON call and
    picks ``positive`` from the conversation, not from the raw /pic
    command text. Hop 0 emits the call; hop 1 writes the wrap-up.

    This is the post-refactor behaviour: the command is a forced-route
    signal, not an argument source. The image-tab workflow no longer
    duplicates the forced-path because the LLM's tool decision is now
    always involved.
    """
    chat, chars, model, image_tool = _build_forced_trigger_service(
        replies=[
            # Hop 0: LLM responds to the forced directive by emitting
            # a tool call whose ``positive`` reflects the scene being
            # discussed (its own choice, not the literal /pic suffix).
            '```json\n{"tool": "generate_image", "args": '
            '{"positive": "coffee shop window, side profile, warm light"}}\n```',
            # Hop 1: natural-language wrap-up after the tool result.
            "好～這是剛畫好的那張 ✨",
        ],
    )
    character_id = await _seed_trigger_character(
        chars,
        allowed_tools=["generate_image"],
    )

    response = await chat.send_message(SendChatMessageRequest(
        character_id=character_id,
        message="/pic 咖啡店窗邊的側臉",
    ))

    # LLM's chosen positive won — not the raw user-message substring.
    assert image_tool.last_positive == "coffee shop window, side profile, warm light"
    # Attachment reached the final assistant message.
    assert len(response.assistant_message.attachments) == 1
    assert response.assistant_message.attachments[0].url == "/uploads/stub/forced.png"
    # Wrap-up text is the model's hop-1 reply, not empty.
    assert response.assistant_message.content == "好～這是剛畫好的那張 ✨"
    # Two LLM calls: hop 0 (forced tool call) + hop 1 (wrap-up reply).
    assert len(model.calls) == 2
    # Hop 0 prompt contains the forced directive so the model knows
    # this turn is locked to ``generate_image``.
    assert "強制工具呼叫" in model.calls[0]
    assert "generate_image" in model.calls[0]


@pytest.mark.asyncio
async def test_pic_command_fallback_synthesises_call_when_llm_ignores_directive() -> None:
    """If the LLM replies with plain text despite the forced directive,
    ChatService synthesises a ``generate_image`` call using the raw
    user message as ``positive`` so the operator's trigger still
    produces an image. This is the safety net — models occasionally
    disobey strong directives, and silently dropping back to text
    would feel like the feature is broken.
    """
    chat, chars, model, image_tool = _build_forced_trigger_service(
        replies=[
            # Hop 0: LLM disobeys — writes natural-language reply.
            "現在還不太方便呢～",
            # Hop 1: wrap-up after the fallback call executes.
            "好啦還是畫了一張給你 ✨",
        ],
    )
    character_id = await _seed_trigger_character(
        chars,
        allowed_tools=["generate_image"],
    )

    response = await chat.send_message(SendChatMessageRequest(
        character_id=character_id,
        message="/pic 海邊夕陽",
    ))

    # Fallback fired. The trigger marker ``/pic`` is stripped from the
    # message before it reaches the fallback — the LLM's synthetic
    # positive uses the cleaned remainder, so next turn's history
    # doesn't carry the backend command into dialogue context.
    assert image_tool.last_positive == "海邊夕陽"
    assert len(response.assistant_message.attachments) == 1
    assert response.assistant_message.attachments[0].url == "/uploads/stub/forced.png"
    assert response.assistant_message.content == "好啦還是畫了一張給你 ✨"


@pytest.mark.asyncio
async def test_pic_command_without_allowed_tool_falls_through_to_llm() -> None:
    """``/pic`` is present but the character doesn't have ``generate_image``
    in ``allowed_tools`` → no forced call; behaviour collapses to the
    normal (no-tool) chat path. Avoids a confusing silent denial."""
    chat, chars, model, image_tool = _build_forced_trigger_service(
        replies=["抱歉我沒辦法畫圖呢"],
    )
    character_id = await _seed_trigger_character(
        chars,
        allowed_tools=[],  # tool not granted
    )

    response = await chat.send_message(SendChatMessageRequest(
        character_id=character_id,
        message="/pic 隨便什麼",
    ))

    assert image_tool.last_positive is None
    assert response.assistant_message.attachments == []
    assert response.assistant_message.content == "抱歉我沒辦法畫圖呢"


@pytest.mark.asyncio
async def test_forced_trigger_still_runs_post_turn_extraction() -> None:
    """Pattern-forced ``/pic`` turns used to skip post-turn extraction
    (the assumption was: mechanical command, no memory value). After
    the marker-stripping refactor the persisted user message is
    actual conversational content (``我想看你在咖啡廳`` not
    ``/pic 我想看你在咖啡廳``), so we now run post-turn like any
    other turn — let the extractor decide if there's anything worth
    remembering.

    Test asserts the post-turn processor's ``process`` method got
    called once on a forced-trigger turn.
    """
    # Build a chat service with a tracking post-turn processor
    # rather than the default ``NullPostTurnProcessor`` so we can see
    # that ``process`` actually fired.
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    invocation_repository = InMemoryToolInvocationRepository()

    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    scripted = _ScriptedModel(replies=[
        '```json\n{"tool": "generate_image", "args": '
        '{"positive": "coffee shop window"}}\n```',
        "好～這就是你想看的樣子",
    ])
    registry.register(scripted)

    image_tool = _RealNameImageTool()
    tool_registry = InMemoryToolRegistry([EchoTool(), image_tool])
    orchestrator = ToolOrchestrator(
        registry=tool_registry,
        invocation_repository=invocation_repository,
    )

    class _CountingPostTurn:
        def __init__(self) -> None:
            self.process_calls = 0

        async def process(self, **kwargs):
            self.process_calls += 1
            from kokoro_link.contracts.post_turn import PostTurnResult
            return PostTurnResult()

    post_turn = _CountingPostTurn()
    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=post_turn,
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        tool_registry=tool_registry,
        tool_orchestrator=orchestrator,
    )
    character_service = CharacterService(character_repository)
    created = await character_service.create_character(
        CreateCharacterRequest(
            name="Yuki",
            allowed_tools=["generate_image"],
        ),
    )

    await chat_service.send_message(SendChatMessageRequest(
        character_id=created.id,
        message="想看看你現在的樣子 /pic",
    ))

    # Post-turn should have fired exactly once — pre-refactor this
    # was 0 because the forced trigger gated the call.
    assert post_turn.process_calls == 1


@pytest.mark.asyncio
async def test_trigger_marker_stripped_from_persisted_user_message() -> None:
    """The ``/pic`` marker in the user's message is purely a backend
    trigger — it shouldn't leak into conversation history. Otherwise
    the next turn's LLM context reads ``使用者: 看你 /pic`` as prose,
    which is a bug surface leaking into roleplay.
    """
    chat, chars, model, image_tool = _build_forced_trigger_service(
        replies=[
            '```json\n{"tool": "generate_image", "args": '
            '{"positive": "portrait, warm light"}}\n```',
            "好～這就是你想看的樣子 ✨",
        ],
    )
    character_id = await _seed_trigger_character(
        chars,
        allowed_tools=["generate_image"],
    )

    response = await chat.send_message(SendChatMessageRequest(
        character_id=character_id,
        message="想看看你現在的樣子 /pic",
    ))

    # Persisted user message has the trigger stripped; cleaned text
    # is what surfaces in conversation history.
    assert response.user_message.content == "想看看你現在的樣子"
    assert "/pic" not in response.user_message.content
    # Hop 0 prompt surfaces the cleaned message on the "最新使用者訊息"
    # line. (The forced-directive block separately documents ``/pic``
    # as an example, so a blanket ``/pic not in prompt`` check would
    # false-positive — the doc-reference is fine, the goal is just to
    # keep the backend marker out of the user-facing dialogue slot.)
    assert "最新使用者訊息：想看看你現在的樣子" in model.calls[0]
    assert "最新使用者訊息：想看看你現在的樣子 /pic" not in model.calls[0]


@pytest.mark.asyncio
async def test_forced_trigger_passes_recent_dialogue_into_tool_context() -> None:
    """The rewriter needs prior-turn context to resolve scene pronouns
    ("那樣的感覺"). ChatService must format recent_messages and plumb
    them through orchestrator → ToolContext → tool.invoke so the
    downstream generator gets them even on the command-forced path.

    Post-refactor: the tool call now comes from the LLM hop 0 output,
    so we script a JSON reply on the trigger turn.
    """
    chat, chars, model, image_tool = _build_forced_trigger_service(
        replies=[
            # Turn 1: no trigger, so normal single-hop text reply.
            "我上次跟你講那間咖啡店在市民大道那邊",
            # Turn 2 hop 0: forced directive — LLM emits tool call.
            '```json\n{"tool": "generate_image", "args": '
            '{"positive": "coffee shop, citizen boulevard vibe"}}\n```',
            # Turn 2 hop 1: wrap-up reply.
            "好，這張應該就是那個感覺 ✨",
        ],
    )
    character_id = await _seed_trigger_character(
        chars,
        allowed_tools=["generate_image"],
    )

    # Monkey-patch the tool so we can capture the ToolContext recent_dialogue
    # without wiring a real ComfyUI generator.
    captured: dict[str, str] = {}
    original_invoke = image_tool.invoke

    async def capturing_invoke(ctx):
        captured["recent_dialogue"] = ctx.recent_dialogue
        return await original_invoke(ctx)

    image_tool.invoke = capturing_invoke  # type: ignore[method-assign]

    # First turn — establishes context; reuse its conversation_id so the
    # second turn lands in the same thread (otherwise ChatService would
    # start a fresh Conversation and recent_messages would be empty).
    turn1 = await chat.send_message(SendChatMessageRequest(
        character_id=character_id,
        message="你昨天跟我講的那間咖啡店在哪？",
    ))
    # Second turn — user triggers with a vague scene pointer.
    await chat.send_message(SendChatMessageRequest(
        character_id=character_id,
        conversation_id=turn1.conversation_id,
        message="/pic 那邊的感覺",
    ))

    # The rendered dialogue must include both the prior exchange and
    # the triggering message so the rewriter can see the referent.
    dlg = captured.get("recent_dialogue", "")
    assert "咖啡店" in dlg
    assert "那邊的感覺" in dlg
    # Lines formatted as role: text
    assert "使用者:" in dlg


@pytest.mark.asyncio
async def test_llm_can_chain_multiple_tools_in_one_turn() -> None:
    """Model emits two different tool calls back-to-back (e.g. search
    then fetch) before writing a final reply. The budget lifted to
    ``_MAX_TOOL_HOPS = 4`` keeps tools visible until the model stops
    calling them or the final hop forces a wrap-up."""
    chat, chars, model, invocations = _build_chat_service(
        replies=[
            '```json\n{"tool": "echo", "args": {"text": "first"}}\n```',
            '```json\n{"tool": "fake_image", "args": {"scene": "窗邊"}}\n```',
            "查好也畫好了～",
        ],
    )
    character_id = await _seed_character(
        chars, allowed_tools=["echo", "fake_image"],
    )

    response = await chat.send_message(SendChatMessageRequest(
        character_id=character_id, message="幫我查一下然後畫張圖",
    ))

    assert response.assistant_message.content == "查好也畫好了～"
    # Image attachment from the second tool call flowed through.
    assert len(response.assistant_message.attachments) == 1
    assert response.assistant_message.attachments[0].url == _FAKE_IMAGE_URL
    # Three generate calls: hop0 (echo), hop1 (fake_image), hop2 (reply).
    assert len(model.calls) == 3
    logs = await invocations.list_for_character(character_id)
    # Repository ordering is not guaranteed across backends; compare as a set.
    assert {row.tool_name for row in logs} == {"echo", "fake_image"}
    assert all(row.status == "success" for row in logs)


@pytest.mark.asyncio
async def test_no_pic_command_leaves_normal_tool_cycle_intact() -> None:
    """User message doesn't include the fixed command → LLM-gated tool cycle
    runs exactly as before (hop 0 can still emit a tool call). This
    pins the regression so the trigger feature doesn't silently break
    the existing LLM-driven path."""
    chat, chars, model, image_tool = _build_forced_trigger_service(
        replies=[
            '```json\n{"tool": "generate_image", "args": '
            '{"positive": "garden scene"}}\n```',
            "畫好了～",
        ],
    )
    character_id = await _seed_trigger_character(
        chars,
        allowed_tools=["generate_image"],
    )

    response = await chat.send_message(SendChatMessageRequest(
        character_id=character_id,
        message="隨便聊聊花園的事情",  # no /pic prefix
    ))

    # Tool still ran — but via LLM's decision (hop 0 emitted the JSON).
    assert image_tool.last_positive == "garden scene"
    # Two LLM calls: hop 0 (tool decision) + hop 1 (wrap-up).
    assert len(model.calls) == 2
    assert response.assistant_message.content == "畫好了～"


@pytest.mark.asyncio
async def test_chat_turn_blocks_repeated_generate_image_tool_calls() -> None:
    chat, chars, model, image_tool = _build_forced_trigger_service(
        replies=[
            '```json\n{"tool": "generate_image", "args": '
            '{"positive": "first portrait"}}\n```',
            '```json\n{"tool": "generate_image", "args": '
            '{"positive": "second portrait"}}\n```',
            '```json\n{"tool": "generate_image", "args": '
            '{"positive": "third portrait"}}\n```',
            "我只傳這一張給你。",
        ],
    )
    character_id = await _seed_trigger_character(
        chars,
        allowed_tools=["generate_image"],
    )

    response = await chat.send_message(SendChatMessageRequest(
        character_id=character_id,
        message="傳幾張你現在的照片給我",
    ))

    assert image_tool.invoke_count == 1
    assert image_tool.last_positive == "first portrait"
    assert len(response.assistant_message.attachments) == 1
    assert response.assistant_message.content == "我只傳這一張給你。"
    assert len(model.calls) == 4


@pytest.mark.asyncio
async def test_demo_runtime_profile_blocks_second_chat_image_within_24h() -> None:
    clock = _MutableClock(datetime(2026, 6, 23, 8, 0, tzinfo=timezone.utc))
    chat, chars, model, image_tool = _build_forced_trigger_service(
        replies=[
            '```json\n{"tool": "generate_image", "args": '
            '{"positive": "first portrait"}}\n```',
            "第一張好了。",
            '```json\n{"tool": "generate_image", "args": '
            '{"positive": "second portrait"}}\n```',
            "今天先用文字陪你。",
            '```json\n{"tool": "generate_image", "args": '
            '{"positive": "third portrait"}}\n```',
            "第二天的圖好了。",
        ],
        account_runtime_profile_resolver=_StaticDemoRuntimeProfileResolver(),
        account_runtime_usage_repository=InMemoryAccountRuntimeUsageRepository(),
        clock=clock,
    )
    character_id = await _seed_trigger_character(
        chars,
        allowed_tools=["generate_image"],
    )

    first = await chat.send_message(SendChatMessageRequest(
        character_id=character_id,
        message="傳一張你現在的照片",
    ))
    second = await chat.send_message(SendChatMessageRequest(
        character_id=character_id,
        message="再傳一張照片",
    ))
    clock.advance(timedelta(days=1, seconds=1))
    third = await chat.send_message(SendChatMessageRequest(
        character_id=character_id,
        message="今天再傳一張照片",
    ))

    assert image_tool.invoke_count == 2
    assert first.assistant_message.attachments
    assert second.assistant_message.attachments == []
    assert second.assistant_message.content == "今天先用文字陪你。"
    assert third.assistant_message.attachments


@pytest.mark.asyncio
async def test_demo_runtime_profile_blocks_chat_image_when_ledger_missing() -> None:
    chat, chars, model, image_tool = _build_forced_trigger_service(
        replies=[
            '```json\n{"tool": "generate_image", "args": '
            '{"positive": "blocked portrait"}}\n```',
            "我先不用圖片。",
        ],
        account_runtime_profile_resolver=_StaticDemoRuntimeProfileResolver(),
    )
    character_id = await _seed_trigger_character(
        chars,
        allowed_tools=["generate_image"],
    )

    response = await chat.send_message(SendChatMessageRequest(
        character_id=character_id,
        message="傳一張照片",
    ))

    assert image_tool.invoke_count == 0
    assert image_tool.last_positive is None
    assert response.assistant_message.attachments == []
    assert response.assistant_message.content == "我先不用圖片。"
    assert len(model.calls) == 2


@pytest.mark.asyncio
async def test_legacy_custom_image_commands_do_not_force_generation() -> None:
    """Only the fixed ``/pic`` command can force image generation now."""
    chat, chars, model, image_tool = _build_forced_trigger_service(
        replies=["我先用文字描述給你聽。"],
    )
    character_id = await _seed_trigger_character(
        chars,
        allowed_tools=["generate_image"],
    )

    response = await chat.send_message(SendChatMessageRequest(
        character_id=character_id,
        message="/selfie",
    ))

    assert image_tool.last_positive is None
    assert response.assistant_message.attachments == []
    assert response.assistant_message.content == "我先用文字描述給你聽。"
    assert len(model.calls) == 1
