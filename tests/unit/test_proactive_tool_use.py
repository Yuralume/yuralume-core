"""BDD: proactive dispatcher + decider tool-use integration.

Covers:

- Decider JSON that includes ``tool_calls`` → parsed into ``ToolCall``
  VOs (disallowed tool names filtered out)
- Proactive prompt includes the tool clause when context has tools
- Dispatcher runs tool_calls → attachments show up on the outbound
  push; absolute URL rewriting works when ``public_base_url`` is set
- Tool failure: message still delivered, just without attachments
- No tool_calls: outbound has no attachments (status quo behaviour)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import pytest

from kokoro_link.application.services.proactive_dispatcher import (
    ProactiveDispatcher,
)
from kokoro_link.application.services.tool_orchestrator import ToolOrchestrator
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.proactive import (
    ProactiveContext,
    ProactiveDecision,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.domain.value_objects.tool_call import ToolCall
from kokoro_link.infrastructure.proactive.heuristic_gate import (
    HeuristicProactiveGate,
)
from kokoro_link.infrastructure.proactive.llm_decider import LLMProactiveDecider
from kokoro_link.infrastructure.repositories.in_memory_proactive_attempts import (
    InMemoryProactiveAttemptRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_tool_invocations import (
    InMemoryToolInvocationRepository,
)
from kokoro_link.infrastructure.tools.fake_tools import EchoTool, FakeImageTool
from kokoro_link.infrastructure.tools.registry import InMemoryToolRegistry
from tests.unit._messaging_harness import (
    build_messaging_harness,
    create_telegram_account,
)


_FAKE_IMAGE_URL = "/uploads/stub/fake.png"


class _StubModel(ChatModelPort):
    def __init__(self, response: str) -> None:
        self._response = response
        self.provider_id = "fake"
        self.captured_prompt: str | None = None

    async def generate(self, prompt: str) -> str:
        self.captured_prompt = prompt
        return self._response

    async def generate_stream(self, prompt: str) -> AsyncIterator[str]:  # pragma: no cover
        yield self._response


def _minimal_context(**overrides: Any) -> ProactiveContext:
    defaults = dict(
        character=Character.create(
            name="Mio", summary="", personality=[], interests=[],
            speaking_style="", boundaries=[],
            state=CharacterState(
                emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
            ),
            allowed_tools=["fake_image"],
        ),
        trigger=ProactiveTrigger.TICK,
        now=datetime(2026, 4, 19, 9, 0, tzinfo=timezone.utc),
        current_activity=None, upcoming_activities=[], schedule=None,
        idle_minutes=600.0, sent_today=0, last_proactive_at=None,
        recent_memories_text="", active_goals_text="",
    )
    defaults.update(overrides)
    return ProactiveContext(**defaults)  # type: ignore[arg-type]


# ---- decider-level --------------------------------------------------

@pytest.mark.asyncio
async def test_decider_parses_tool_calls() -> None:
    from kokoro_link.contracts.prompt import PromptToolDescriptor

    model = _StubModel(
        '{"should_send": true, "reason": "morning greet with selfie", '
        '"message": "早安～這是剛起床的我 🌅", '
        '"tool_calls": [{"tool": "fake_image", "args": {"scene": "剛起床"}}]}'
    )
    decider = LLMProactiveDecider(model=model)
    ctx = _minimal_context(
        available_tools=(
            PromptToolDescriptor(
                name="fake_image", description="產圖用",
                parameters_schema={"type": "object"},
            ),
        ),
    )

    decision = await decider.decide(ctx)

    assert decision.should_send
    assert decision.message == "早安～這是剛起床的我 🌅"
    assert len(decision.tool_calls) == 1
    assert decision.tool_calls[0].name == "fake_image"
    assert decision.tool_calls[0].arguments == {"scene": "剛起床"}
    # Prompt should mention the tool so the model can choose it.
    assert model.captured_prompt is not None
    assert "可用工具" in model.captured_prompt
    assert "fake_image" in model.captured_prompt


@pytest.mark.asyncio
async def test_decider_drops_disallowed_tool_name() -> None:
    from kokoro_link.contracts.prompt import PromptToolDescriptor

    model = _StubModel(
        '{"should_send": true, "reason": "x", "message": "hi", '
        '"tool_calls": [{"tool": "unknown_tool", "args": {}}]}'
    )
    decider = LLMProactiveDecider(model=model)
    ctx = _minimal_context(
        available_tools=(
            PromptToolDescriptor(
                name="fake_image", description="產圖",
                parameters_schema={"type": "object"},
            ),
        ),
    )

    decision = await decider.decide(ctx)

    assert decision.should_send
    assert decision.tool_calls == ()


@pytest.mark.asyncio
async def test_decider_prompt_asks_for_scene_aware_length() -> None:
    """Proactive message length should flex with context.

    When the user was mid-conversation or the character is continuing a thread
    they opened earlier, a cold 40-char LINE-style ping feels truncated. When
    it's a cold open, long paragraphs feel desperate. The prompt must give the
    LLM both modes explicitly.

    Note: the 「手機文體優化 打字感」 redesign (commit 45b6af7) intentionally
    dropped the old 100–200 字 continuation target — continuation mode now says
    to stay in mobile-message feel and split longer thoughts into several short
    bursts rather than write one long block. So we assert the *current* markers
    of each mode (that two distinct scene-aware modes exist) instead of the
    retired numeric range; the 300-char hard cap survival is covered separately
    by test_decider_continuation_reply_not_truncated_at_160.
    """
    model = _StubModel(
        '{"should_send": false, "reason": "inspection only", "message": null}'
    )
    decider = LLMProactiveDecider(model=model)

    await decider.decide(_minimal_context())

    assert model.captured_prompt is not None
    # Continuation mode: fuller, but still mobile-message feel / split into
    # several short bursts (the intentional post-redesign guidance).
    assert "延續" in model.captured_prompt
    assert "稍微完整" in model.captured_prompt
    assert "手機訊息感" in model.captured_prompt
    # New-topic mode: LINE-style short, explicit 40–80 字 target.
    assert "開新話題" in model.captured_prompt or "新話題" in model.captured_prompt
    assert "40" in model.captured_prompt and "80" in model.captured_prompt


@pytest.mark.asyncio
async def test_decider_continuation_reply_not_truncated_at_160() -> None:
    """Hard cap raised to 300 so natural continuation survives intact."""
    long_msg = "那個你說的咖啡店我昨天也去了一下，排隊其實沒想像中久，老闆娘還記得我上次點的口味，" * 4
    assert 160 < len(long_msg) <= 300  # fits in new cap, would've been cut by old
    model = _StubModel(
        '{"should_send": true, "reason": "continuing earlier thread",'
        f'"message": "{long_msg}"}}'
    )
    decider = LLMProactiveDecider(model=model)

    decision = await decider.decide(_minimal_context())

    assert decision.should_send
    assert decision.message == long_msg
    assert "…" not in decision.message


@pytest.mark.asyncio
async def test_decider_caps_tool_calls_to_one_and_dedups() -> None:
    """LLM may hallucinate a list of tool calls; only the first valid one survives.

    Otherwise a single decision can fan out into N adapter.send() image pushes
    and the user gets spammed.
    """
    from kokoro_link.contracts.prompt import PromptToolDescriptor

    model = _StubModel(
        '{"should_send": true, "reason": "x", "message": "早安",'
        '"tool_calls": ['
        '{"tool": "fake_image", "args": {"scene": "a"}},'
        '{"tool": "fake_image", "args": {"scene": "b"}},'
        '{"tool": "fake_image", "args": {"scene": "c"}}'
        ']}'
    )
    decider = LLMProactiveDecider(model=model)
    ctx = _minimal_context(
        available_tools=(
            PromptToolDescriptor(
                name="fake_image", description="產圖",
                parameters_schema={"type": "object"},
            ),
        ),
    )

    decision = await decider.decide(ctx)

    assert decision.should_send
    assert len(decision.tool_calls) == 1
    assert decision.tool_calls[0].arguments == {"scene": "a"}
    # Prompt must tell the model about the cap.
    assert model.captured_prompt is not None
    assert "最多 1 筆" in model.captured_prompt


@pytest.mark.asyncio
async def test_decider_empty_tool_calls_defaults_to_tuple() -> None:
    model = _StubModel(
        '{"should_send": true, "reason": "x", "message": "hi"}'
    )
    decider = LLMProactiveDecider(model=model)

    decision = await decider.decide(_minimal_context())

    assert decision.tool_calls == ()


# ---- dispatcher-level -----------------------------------------------

async def _build_dispatched(
    *,
    decision: ProactiveDecision,
    allowed_tools: list[str],
    public_base_url: str = "",
    public_base_url_provider=None,  # noqa: ANN001 - concise test hook
    tool_crashes: bool = False,
):
    harness = build_messaging_harness()
    created = await harness.character_service.create_character(
        _req("Mio", allowed_tools=allowed_tools),
    )
    entity = await harness.character_repository.get(created.id)
    assert entity is not None
    # Proactive needs to be enabled.
    await harness.character_repository.save(
        entity.update(
            name=None, summary=None, personality=None, interests=None,
            speaking_style=None, boundaries=None,
            state=CharacterState(
                emotion="neutral",
                affection=50,
                fatigue=0,
                trust=50,
                energy=100,
                last_active_at=datetime(2026, 4, 19, 8, 0, tzinfo=timezone.utc),
            ),
            proactive_enabled=True,
        ),
    )

    account = await create_telegram_account(
        harness, character_id=created.id,
    )
    binding = await harness.binding_service.create(
        account_id=account.id, chat_ref="chat-42",
    )
    from dataclasses import replace

    await harness.binding_repository.save(
        replace(binding, accepts_proactive=True),
    )

    image_url = "/uploads/characters/x/tools/img.png"
    fake = FakeImageTool(url=image_url)
    if tool_crashes:
        class _Boom:
            name = "fake_image"
            description = ""
            parameters_schema: dict[str, object] = {}

            async def invoke(self, ctx):  # noqa: ANN001
                raise RuntimeError("boom")

        tool_registry = InMemoryToolRegistry([EchoTool(), _Boom()])
    else:
        tool_registry = InMemoryToolRegistry([EchoTool(), fake])

    invocations = InMemoryToolInvocationRepository()
    orchestrator = ToolOrchestrator(
        registry=tool_registry, invocation_repository=invocations,
    )

    class _CannedDecider:
        async def decide(self, context):  # noqa: ANN001
            return decision

    attempts = InMemoryProactiveAttemptRepository()
    dispatcher = ProactiveDispatcher(
        character_repository=harness.character_repository,
        conversation_repository=harness.conversation_repository,
        account_repository=harness.account_repository,
        binding_repository=harness.binding_repository,
        attempt_repository=attempts,
        gate=HeuristicProactiveGate(local_tz=timezone.utc, quiet_hour_start=0, quiet_hour_end=0),
        decider=_CannedDecider(),
        adapters={
            Platform.TELEGRAM: harness.telegram_adapter,
            Platform.LINE: harness.line_adapter,
        },
        tool_registry=tool_registry,
        tool_orchestrator=orchestrator,
        public_base_url=public_base_url,
        public_base_url_provider=public_base_url_provider,
    )
    return harness, dispatcher, created.id, image_url


def _req(name: str, *, allowed_tools: list[str]):
    from kokoro_link.application.dto.character import CreateCharacterRequest
    return CreateCharacterRequest(name=name, allowed_tools=allowed_tools)


@pytest.mark.asyncio
async def test_decider_tool_calls_executed_and_attachments_delivered() -> None:
    decision = ProactiveDecision(
        should_send=True, reason="test",
        message="早安～這是今天的我",
        tool_calls=(
            ToolCall(name="fake_image", arguments={"scene": "morning"}),
        ),
    )
    harness, dispatcher, character_id, image_url = await _build_dispatched(
        decision=decision, allowed_tools=["fake_image"],
        public_base_url="https://example.test",
    )

    attempt = await dispatcher.evaluate(
        character_id=character_id,
        trigger=ProactiveTrigger.TICK,
        now=datetime(2026, 4, 19, 9, 0, tzinfo=timezone.utc),
    )

    assert attempt.outcome == ProactiveOutcome.SENT, attempt.reason
    # The fake telegram adapter records sent messages.
    sent = harness.telegram_adapter.sent
    assert len(sent) == 1
    assert sent[0].text == "早安～這是今天的我"
    assert len(sent[0].attachments) == 1
    # The stub URL is relative; dispatcher should have absolutised it.
    assert sent[0].attachments[0].url == f"https://example.test{image_url}"
    assert sent[0].attachments[0].kind == "image"


@pytest.mark.asyncio
async def test_decider_tool_attachment_uses_dynamic_messaging_public_url() -> None:
    async def public_url_provider() -> str:
        return "https://public.example.test"

    decision = ProactiveDecision(
        should_send=True, reason="test",
        message="早安～這是今天的我",
        tool_calls=(
            ToolCall(name="fake_image", arguments={"scene": "morning"}),
        ),
    )
    harness, dispatcher, character_id, image_url = await _build_dispatched(
        decision=decision,
        allowed_tools=["fake_image"],
        public_base_url="http://127.0.0.1:8012",
        public_base_url_provider=public_url_provider,
    )

    attempt = await dispatcher.evaluate(
        character_id=character_id,
        trigger=ProactiveTrigger.TICK,
        now=datetime(2026, 4, 19, 9, 0, tzinfo=timezone.utc),
    )

    assert attempt.outcome == ProactiveOutcome.SENT, attempt.reason
    assert harness.telegram_adapter.sent[0].attachments[0].url == (
        f"https://public.example.test{image_url}"
    )


@pytest.mark.asyncio
async def test_web_persists_relative_urls_while_tg_keeps_absolute() -> None:
    """Regression: when ``KOKORO_PUBLIC_BASE_URL`` points at the
    external DDNS host (so TG/LINE servers can fetch generated images),
    the SAME attachment tuple was being persisted into the web
    conversation — so an internal-LAN browser visit got pinned to the
    external host and timed out via hairpin NAT. Web delivery must
    demote URLs back to relative form before persisting; TG/LINE
    outbound keeps the absolute URL it actually needs."""
    decision = ProactiveDecision(
        should_send=True, reason="test",
        message="早安～這是今天的我",
        tool_calls=(
            ToolCall(name="fake_image", arguments={"scene": "morning"}),
        ),
    )
    harness, dispatcher, character_id, image_url = await _build_dispatched(
        decision=decision, allowed_tools=["fake_image"],
        public_base_url="https://kokoro.public.example",
    )

    attempt = await dispatcher.evaluate(
        character_id=character_id,
        trigger=ProactiveTrigger.TICK,
        now=datetime(2026, 4, 19, 9, 0, tzinfo=timezone.utc),
    )
    assert attempt.outcome == ProactiveOutcome.SENT

    # TG side: absolute URL (TG servers fetch by URL, must be reachable
    # from the public internet).
    sent = harness.telegram_adapter.sent
    assert len(sent) == 1
    assert sent[0].attachments[0].url == (
        f"https://kokoro.public.example{image_url}"
    )

    # Web side: persisted into the source="web" conversation as a
    # *relative* URL so whichever origin the operator's browser opens
    # (LAN domain, dev localhost, anything) the <img> resolves
    # against that origin.
    web_conv = await harness.conversation_repository.latest_for_character(
        character_id, source="web",
    )
    assert web_conv is not None
    assistant_messages = [
        m for m in web_conv.messages if m.role.value == "assistant"
    ]
    assert assistant_messages, "expected proactive message persisted to web"
    last = assistant_messages[-1]
    assert len(last.attachments) == 1
    assert last.attachments[0].url == image_url
    assert not last.attachments[0].url.startswith("https://")


@pytest.mark.asyncio
async def test_tool_crash_still_delivers_text_without_attachments() -> None:
    decision = ProactiveDecision(
        should_send=True, reason="test",
        message="嗨，本來想附圖的",
        tool_calls=(ToolCall(name="fake_image", arguments={}),),
    )
    harness, dispatcher, character_id, _ = await _build_dispatched(
        decision=decision, allowed_tools=["fake_image"],
        tool_crashes=True,
    )

    attempt = await dispatcher.evaluate(
        character_id=character_id,
        trigger=ProactiveTrigger.TICK,
        now=datetime(2026, 4, 19, 9, 0, tzinfo=timezone.utc),
    )

    assert attempt.outcome == ProactiveOutcome.SENT
    sent = harness.telegram_adapter.sent
    assert len(sent) == 1
    assert sent[0].text == "嗨，本來想附圖的"
    assert sent[0].attachments == ()


@pytest.mark.asyncio
async def test_decision_without_tool_calls_has_no_attachments() -> None:
    decision = ProactiveDecision(
        should_send=True, reason="test", message="只是想跟你說聲嗨",
    )
    harness, dispatcher, character_id, _ = await _build_dispatched(
        decision=decision, allowed_tools=["fake_image"],
    )

    attempt = await dispatcher.evaluate(
        character_id=character_id,
        trigger=ProactiveTrigger.TICK,
        now=datetime(2026, 4, 19, 9, 0, tzinfo=timezone.utc),
    )

    assert attempt.outcome == ProactiveOutcome.SENT
    sent = harness.telegram_adapter.sent
    assert len(sent) == 1
    assert sent[0].attachments == ()
