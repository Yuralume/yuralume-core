"""Dispatcher routes scheduled-promise rows to the promise composer.

Mirrors the existing ``test_pending_follow_up_dispatcher`` harness but
exercises the ``kind=SCHEDULED_PROMISE`` branch: busy_score check is
skipped, ScheduledPromiseComposerPort is invoked, trigger is
``SCHEDULED_PROMISE``, and the bypass path covers high-busy + zero
limit cases.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from kokoro_link.application.services.composer_tool_loop import (
    DELIVERED_WITHOUT_TEXT_FALLBACK_KEY,
    UNDELIVERABLE_ARTIFACT_FALLBACK_KEY,
    ComposerToolLoop,
)
from kokoro_link.application.services.pending_follow_up_dispatcher import (
    PendingFollowUpDispatcher,
)
from kokoro_link.application.services.tool_orchestrator import ToolOrchestrator
from kokoro_link.contracts.pending_follow_up_composer import (
    PendingFollowUpComposeOutput,
    PendingFollowUpComposerPort,
)
from kokoro_link.contracts.scheduled_promise_composer import (
    ScheduledPromiseComposeInput,
    ScheduledPromiseComposeOutput,
    ScheduledPromiseComposerPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.pending_follow_up import (
    PendingFollowUp,
    PendingFollowUpKind,
    PendingFollowUpStatus,
)
from kokoro_link.domain.entities.conversation import MessageContentMode
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.entities.schedule import ScheduleActivity
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.localization.fallback_texts import (
    localized_fallback_text,
)
from kokoro_link.infrastructure.repositories.in_memory_pending_follow_ups import (
    InMemoryPendingFollowUpRepository,
)
from kokoro_link.contracts.tool import ToolPort, ToolRegistryPort
from kokoro_link.domain.value_objects.tool_call import ToolCall, ToolResult
from kokoro_link.infrastructure.repositories.in_memory_tool_invocations import (
    InMemoryToolInvocationRepository,
)
from kokoro_link.infrastructure.tools.fake_tools import EchoTool, FakeImageTool
from kokoro_link.infrastructure.tools.registry import InMemoryToolRegistry


def _now() -> datetime:
    return datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc)


def _character(cid: str = "char-1") -> Character:
    return replace(
        Character.create(
            name="Aki", summary="", personality=[], interests=[],
            speaking_style="", boundaries=[],
            state=CharacterState(
                emotion="neutral", affection=50, fatigue=20, trust=50, energy=70,
            ),
        ),
        id=cid,
    )


def _promise_row(
    *,
    character_id: str = "char-1",
    conversation_id: str = "conv-1",
    offset_min: int = -1,
    source_content_mode: MessageContentMode = MessageContentMode.NORMAL,
    source_safe_summary: str = "",
) -> PendingFollowUp:
    return PendingFollowUp.new_promise(
        character_id=character_id,
        conversation_id=conversation_id,
        promise_intent="叫使用者起床",
        scheduled_for=_now() + timedelta(minutes=offset_min),
        source_message_content="明天 10 點叫我起床",
        source_content_mode=source_content_mode,
        source_safe_summary=source_safe_summary,
        now=_now() - timedelta(hours=8),
    )


@dataclass
class _StubCharacterRepo:
    characters: dict[str, Character]

    async def get(self, character_id: str) -> Character | None:
        return self.characters.get(character_id)

    async def list(self) -> list[Character]:  # pragma: no cover
        return list(self.characters.values())

    async def save(self, c: Character) -> None:  # pragma: no cover
        self.characters[c.id] = c

    async def delete(self, c: str) -> bool:  # pragma: no cover
        return self.characters.pop(c, None) is not None


class _StubScheduleService:
    def __init__(self, current_activity: Any = None) -> None:
        self.current_activity = current_activity

    async def ensure_schedule(self, character):
        return object()

    def resolve_current(self, schedule, *, now):
        return self.current_activity, [], None


@dataclass
class _StubOperatorProfile:
    primary_language: str
    timezone_id: str = "UTC"


class _StubOperatorProfileService:
    """Pins the owner's UI language, the way the real service does."""

    def __init__(self, primary_language: str) -> None:
        self._profile = _StubOperatorProfile(primary_language)

    async def get_for_user(self, user_id: str) -> _StubOperatorProfile:
        return self._profile


class _StubBusyComposer(PendingFollowUpComposerPort):
    """Should NEVER be called for scheduled-promise rows."""

    def __init__(self) -> None:
        self.calls = 0

    async def compose(self, payload):  # pragma: no cover - asserted via calls
        self.calls += 1
        return PendingFollowUpComposeOutput(content_text="should not run")


class _StubPromiseComposer(ScheduledPromiseComposerPort):
    def __init__(self, response: str = "早安!該起床囉~") -> None:
        self.response = response
        self.calls: list[ScheduledPromiseComposeInput] = []

    async def compose(self, payload):
        self.calls.append(payload)
        return ScheduledPromiseComposeOutput(content_text=self.response)


class _StubProactiveDispatcher:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.outcome = ProactiveOutcome.SENT

    async def deliver_pre_composed(
        self, *, character_id, text, trigger, reason="", attachments=(), now=None,
    ):
        self.calls.append({
            "character_id": character_id, "text": text,
            "trigger": trigger, "reason": reason,
            "attachments": tuple(attachments),
        })
        return ProactiveAttempt.record(
            character_id=character_id, trigger=trigger,
            outcome=self.outcome, reason=reason or "stub",
            message=text, now=now or _now(),
        )


def _busy_meeting() -> ScheduleActivity:
    return ScheduleActivity.create(
        start_at=_now() - timedelta(minutes=30),
        end_at=_now() + timedelta(minutes=30),
        description="會議", category="meeting", busy_score=0.95,
    )


@pytest.mark.asyncio
async def test_scheduled_promise_releases_through_promise_composer() -> None:
    repo = InMemoryPendingFollowUpRepository()
    row = _promise_row(
        source_content_mode=MessageContentMode.NSFW,
        source_safe_summary="安全約定摘要",
    )
    await repo.add(row)
    char = _character()
    proactive = _StubProactiveDispatcher()
    busy_composer = _StubBusyComposer()
    promise_composer = _StubPromiseComposer()
    dispatcher = PendingFollowUpDispatcher(
        repository=repo,
        composer=busy_composer,
        proactive_dispatcher=proactive,
        character_repository=_StubCharacterRepo({char.id: char}),
        schedule_service=_StubScheduleService(current_activity=None),
        scheduled_promise_composer=promise_composer,
    )

    resolved = await dispatcher.tick(now=_now())
    assert resolved == 1
    assert busy_composer.calls == 0  # busy composer untouched
    assert len(promise_composer.calls) == 1
    assert promise_composer.calls[0].promise_intent == "叫使用者起床"
    assert (
        promise_composer.calls[0].promise_content_mode
        is MessageContentMode.NSFW
    )
    assert promise_composer.calls[0].promise_safe_summary == "安全約定摘要"
    assert proactive.calls[0]["trigger"] == ProactiveTrigger.SCHEDULED_PROMISE
    assert proactive.calls[0]["text"] == "早安!該起床囉~"

    stored = await repo.get(row.id)
    assert stored is not None
    assert stored.status == PendingFollowUpStatus.RESOLVED


@pytest.mark.asyncio
async def test_scheduled_promise_releases_even_when_busy() -> None:
    """The user asked for THIS time — busy_score must not gate."""
    repo = InMemoryPendingFollowUpRepository()
    await repo.add(_promise_row())
    char = _character()
    proactive = _StubProactiveDispatcher()
    dispatcher = PendingFollowUpDispatcher(
        repository=repo,
        composer=_StubBusyComposer(),
        proactive_dispatcher=proactive,
        character_repository=_StubCharacterRepo({char.id: char}),
        schedule_service=_StubScheduleService(current_activity=_busy_meeting()),
        scheduled_promise_composer=_StubPromiseComposer(),
    )

    resolved = await dispatcher.tick(now=_now())
    assert resolved == 1
    assert proactive.calls[0]["trigger"] == ProactiveTrigger.SCHEDULED_PROMISE


@pytest.mark.asyncio
async def test_missing_promise_composer_cancels_row() -> None:
    """Operator forgot to wire scheduled_promise_composer → don't loop;
    cancel the row so it's visible in audit and stops retrying."""
    repo = InMemoryPendingFollowUpRepository()
    row = _promise_row()
    await repo.add(row)
    char = _character()
    dispatcher = PendingFollowUpDispatcher(
        repository=repo,
        composer=_StubBusyComposer(),
        proactive_dispatcher=_StubProactiveDispatcher(),
        character_repository=_StubCharacterRepo({char.id: char}),
        schedule_service=_StubScheduleService(),
        scheduled_promise_composer=None,
    )

    await dispatcher.tick(now=_now())
    stored = await repo.get(row.id)
    assert stored is not None
    assert stored.status == PendingFollowUpStatus.CANCELLED


@pytest.mark.asyncio
async def test_busy_defer_still_uses_busy_composer() -> None:
    """Adding the scheduled-promise path must not break the legacy
    busy-defer flow."""
    from kokoro_link.domain.entities.pending_follow_up import (
        PendingFollowUpMessage,
    )

    repo = InMemoryPendingFollowUpRepository()
    row = PendingFollowUp.new(
        character_id="char-1",
        conversation_id="conv-1",
        first_message=PendingFollowUpMessage.new(content="嗨"),
        brief_reply="先回，等我忙完",
        defer_reason="會議中",
        scheduled_for=_now() - timedelta(minutes=5),
    )
    await repo.add(row)
    char = _character()
    busy_composer = _StubBusyComposer()

    # Have busy composer actually return something so the row resolves.
    async def _real_compose(payload):  # noqa: ANN001
        return PendingFollowUpComposeOutput(content_text="會議結束了!")
    busy_composer.compose = _real_compose  # type: ignore[assignment]

    promise_composer = _StubPromiseComposer()
    proactive = _StubProactiveDispatcher()
    dispatcher = PendingFollowUpDispatcher(
        repository=repo,
        composer=busy_composer,
        proactive_dispatcher=proactive,
        character_repository=_StubCharacterRepo({char.id: char}),
        schedule_service=_StubScheduleService(current_activity=None),
        scheduled_promise_composer=promise_composer,
    )

    resolved = await dispatcher.tick(now=_now())
    assert resolved == 1
    assert promise_composer.calls == []  # promise composer untouched
    assert proactive.calls[0]["trigger"] == ProactiveTrigger.PENDING_FOLLOW_UP


# --- PF1: two-pass compose -> tool -> compose ---------------------------


class _ScriptedPromiseComposer(ScheduledPromiseComposerPort):
    """Answers the first pass with tool calls, the second with prose.

    Which pass it is is read off the payload itself (``tool_results``
    present = second), so the test asserts the loop's data shape rather
    than a call counter the composer could fake."""

    def __init__(
        self,
        *,
        tool_calls: tuple[ToolCall, ...],
        final_text: str = "查到了，抽選七月一號開始。",
    ) -> None:
        self._tool_calls = tool_calls
        self._final_text = final_text
        self.calls: list[ScheduledPromiseComposeInput] = []

    async def compose(self, payload):
        self.calls.append(payload)
        if payload.tool_results:
            return ScheduledPromiseComposeOutput(content_text=self._final_text)
        if payload.available_tools:
            return ScheduledPromiseComposeOutput(
                content_text="", tool_calls=self._tool_calls,
            )
        return ScheduledPromiseComposeOutput(content_text="沒有工具就直接寫")


class _AlwaysListingRegistry(ToolRegistryPort):
    """Offers its tools to every character, whatever ``allowed_tools``
    says. Used to prove the orchestrator — not the prompt — is what
    enforces permission."""

    def __init__(self, tools: list) -> None:
        self._tools = {t.name: t for t in tools}

    def all(self) -> list:
        return list(self._tools.values())

    def get(self, name: str):
        return self._tools.get(name)

    def list_for_character(self, character) -> list:
        return list(self._tools.values())


class _FailingTool(ToolPort):
    name: str = "web_search"
    description: str = "上網搜尋（測試用一定失敗）。"
    parameters_schema: dict[str, Any] = {"type": "object"}

    async def invoke(self, ctx):
        return ToolResult.failure("upstream timeout")


def _tooled_character(cid: str = "char-1", *, allowed: list[str]) -> Character:
    return replace(_character(cid), allowed_tools=tuple(allowed))


def _loop(
    tools: list,
    *,
    registry: ToolRegistryPort | None = None,
    public_base_url: str = "https://yura.example",
) -> tuple[ComposerToolLoop, InMemoryToolInvocationRepository]:
    invocations = InMemoryToolInvocationRepository()
    shared_registry = registry or InMemoryToolRegistry(tools)
    return (
        ComposerToolLoop(
            tool_registry=shared_registry,
            tool_orchestrator=ToolOrchestrator(
                registry=shared_registry,
                invocation_repository=invocations,
            ),
            public_base_url=public_base_url,
        ),
        invocations,
    )


def _dispatcher(
    repo: InMemoryPendingFollowUpRepository,
    character: Character,
    promise_composer: ScheduledPromiseComposerPort,
    proactive: _StubProactiveDispatcher,
    *,
    tool_loop=None,
) -> PendingFollowUpDispatcher:
    return PendingFollowUpDispatcher(
        repository=repo,
        composer=_StubBusyComposer(),
        proactive_dispatcher=proactive,
        character_repository=_StubCharacterRepo({character.id: character}),
        schedule_service=_StubScheduleService(current_activity=None),
        scheduled_promise_composer=promise_composer,
        tool_loop=tool_loop,
    )


@pytest.mark.asyncio
async def test_without_tool_loop_promise_stays_a_single_plain_compose() -> None:
    """Characterization of the pre-PF1 behaviour: exactly one compose,
    payload untouched, no attachments on the delivery."""
    repo = InMemoryPendingFollowUpRepository()
    await repo.add(_promise_row())
    char = _character()
    proactive = _StubProactiveDispatcher()
    composer = _StubPromiseComposer()
    dispatcher = _dispatcher(repo, char, composer, proactive, tool_loop=None)

    assert await dispatcher.tick(now=_now()) == 1
    assert len(composer.calls) == 1
    assert composer.calls[0].available_tools == ()
    assert composer.calls[0].tool_results == ()
    assert proactive.calls[0]["attachments"] == ()


@pytest.mark.asyncio
async def test_tool_loop_without_permitted_tools_stays_single_pass() -> None:
    """Loop wired, but this character may call nothing -> the registry
    offers nothing and the call is identical to the line above."""
    repo = InMemoryPendingFollowUpRepository()
    await repo.add(_promise_row())
    char = _tooled_character(allowed=[])
    proactive = _StubProactiveDispatcher()
    composer = _StubPromiseComposer()
    loop, _ = _loop([EchoTool()])
    dispatcher = _dispatcher(repo, char, composer, proactive, tool_loop=loop)

    assert await dispatcher.tick(now=_now()) == 1
    assert len(composer.calls) == 1
    assert composer.calls[0].available_tools == ()
    assert proactive.calls[0]["attachments"] == ()


@pytest.mark.asyncio
async def test_promise_runs_the_tool_and_sends_the_second_pass_text() -> None:
    repo = InMemoryPendingFollowUpRepository()
    await repo.add(_promise_row())
    char = _tooled_character(allowed=["echo"])
    proactive = _StubProactiveDispatcher()
    composer = _ScriptedPromiseComposer(
        tool_calls=(ToolCall(name="echo", arguments={"text": "夏祭抽選"}),),
    )
    loop, invocations = _loop([EchoTool()])
    dispatcher = _dispatcher(repo, char, composer, proactive, tool_loop=loop)

    assert await dispatcher.tick(now=_now()) == 1

    assert len(composer.calls) == 2
    first, second = composer.calls
    assert [t.name for t in first.available_tools] == ["echo"]
    assert first.tool_results == ()
    # Second pass sees the real result and is no longer offered tools.
    assert second.available_tools == ()
    assert len(second.tool_results) == 1
    assert second.tool_results[0].ok is True
    assert second.tool_results[0].output_text == "echo: 夏祭抽選"
    # The rest of the payload is carried over untouched.
    assert second.promise_intent == first.promise_intent

    assert proactive.calls[0]["text"] == "查到了，抽選七月一號開始。"
    assert len(await invocations.list_for_character(char.id)) == 1


@pytest.mark.asyncio
async def test_tool_failure_is_reported_to_the_second_pass() -> None:
    """A broken tool must not become a silent broken promise — the
    failure is a fact the character gets to speak about."""
    repo = InMemoryPendingFollowUpRepository()
    await repo.add(_promise_row())
    char = _tooled_character(allowed=["web_search"])
    proactive = _StubProactiveDispatcher()
    composer = _ScriptedPromiseComposer(
        tool_calls=(ToolCall(name="web_search", arguments={"query": "夏祭"}),),
        final_text="欸我查了但查不到，晚點再幫你看。",
    )
    loop, _ = _loop([_FailingTool()])
    dispatcher = _dispatcher(repo, char, composer, proactive, tool_loop=loop)

    assert await dispatcher.tick(now=_now()) == 1

    assert len(composer.calls) == 2
    results = composer.calls[1].tool_results
    assert len(results) == 1
    assert results[0].ok is False
    assert results[0].error == "upstream timeout"
    assert proactive.calls[0]["text"] == "欸我查了但查不到，晚點再幫你看。"
    assert proactive.calls[0]["attachments"] == ()


@pytest.mark.asyncio
async def test_denied_tool_reaches_the_second_pass_as_a_failure() -> None:
    """Permission is enforced by the orchestrator against
    ``character.allowed_tools``, even if a registry hands the tool over
    anyway — and the denial still reaches pass 2 as a fact."""
    repo = InMemoryPendingFollowUpRepository()
    await repo.add(_promise_row())
    char = _tooled_character(allowed=[])
    proactive = _StubProactiveDispatcher()
    composer = _ScriptedPromiseComposer(
        tool_calls=(ToolCall(name="echo", arguments={"text": "hi"}),),
        final_text="抱歉，我沒辦法查。",
    )
    loop, _ = _loop([], registry=_AlwaysListingRegistry([EchoTool()]))
    dispatcher = _dispatcher(repo, char, composer, proactive, tool_loop=loop)

    assert await dispatcher.tick(now=_now()) == 1

    results = composer.calls[1].tool_results
    assert results[0].ok is False
    assert "allowed_tools" in (results[0].error or "")


@pytest.mark.asyncio
async def test_generated_image_ships_with_the_promised_message() -> None:
    repo = InMemoryPendingFollowUpRepository()
    await repo.add(_promise_row())
    char = _tooled_character(allowed=["fake_image"])
    proactive = _StubProactiveDispatcher()
    composer = _ScriptedPromiseComposer(
        tool_calls=(ToolCall(name="fake_image", arguments={"scene": "廚房"}),),
        final_text="拍好了，你看~",
    )
    loop, _ = _loop([FakeImageTool()])
    dispatcher = _dispatcher(repo, char, composer, proactive, tool_loop=loop)

    assert await dispatcher.tick(now=_now()) == 1

    attachments = proactive.calls[0]["attachments"]
    assert len(attachments) == 1
    # Relative tool URL absolutised for external platforms.
    assert attachments[0].url == "https://yura.example/uploads/stub/fake.png"
    assert composer.calls[1].tool_results[0].attachment_urls == (
        "https://yura.example/uploads/stub/fake.png",
    )


@pytest.mark.asyncio
async def test_only_one_tool_call_runs_per_fulfilment() -> None:
    repo = InMemoryPendingFollowUpRepository()
    await repo.add(_promise_row())
    char = _tooled_character(allowed=["echo", "fake_image"])
    proactive = _StubProactiveDispatcher()
    composer = _ScriptedPromiseComposer(
        tool_calls=(
            ToolCall(name="echo", arguments={"text": "one"}),
            ToolCall(name="fake_image", arguments={"scene": "two"}),
        ),
    )
    loop, invocations = _loop([EchoTool(), FakeImageTool()])
    dispatcher = _dispatcher(repo, char, composer, proactive, tool_loop=loop)

    assert await dispatcher.tick(now=_now()) == 1

    assert len(composer.calls) == 2  # never a third pass
    assert [r.tool_name for r in composer.calls[1].tool_results] == ["echo"]
    assert len(await invocations.list_for_character(char.id)) == 1


@pytest.mark.asyncio
async def test_empty_second_pass_with_nothing_produced_leaves_the_row_queued() -> None:
    """Nothing was rendered, so repeating the round costs a lookup —
    fail-soft, retry next tick, exactly as the composers' contract says."""
    repo = InMemoryPendingFollowUpRepository()
    row = _promise_row()
    await repo.add(row)
    char = _tooled_character(allowed=["echo"])
    proactive = _StubProactiveDispatcher()
    composer = _ScriptedPromiseComposer(
        tool_calls=(ToolCall(name="echo", arguments={"text": "夏祭"}),),
        final_text="",
    )
    loop, _ = _loop([EchoTool()])
    dispatcher = _dispatcher(repo, char, composer, proactive, tool_loop=loop)

    assert await dispatcher.tick(now=_now()) == 0
    assert proactive.calls == []
    stored = await repo.get(row.id)
    assert stored is not None
    assert stored.status == PendingFollowUpStatus.QUEUED


@pytest.mark.asyncio
async def test_empty_second_pass_still_ships_an_already_rendered_photo() -> None:
    """The picture exists — GPU spent, credits charged, file written. Dropping
    it to "retry" would re-render it every tick for as long as the model keeps
    fumbling the last hop, and the promise would never arrive. Ship it with the
    same localized line the chat loop uses for this exact situation."""
    repo = InMemoryPendingFollowUpRepository()
    row = _promise_row()
    await repo.add(row)
    char = _tooled_character(allowed=["fake_image"])
    proactive = _StubProactiveDispatcher()
    composer = _ScriptedPromiseComposer(
        tool_calls=(ToolCall(name="fake_image", arguments={"scene": "廚房"}),),
        final_text="",
    )
    loop, _ = _loop([FakeImageTool()])
    dispatcher = _dispatcher(repo, char, composer, proactive, tool_loop=loop)

    assert await dispatcher.tick(now=_now()) == 1

    sent = proactive.calls[0]
    assert sent["text"] == localized_fallback_text(
        DELIVERED_WITHOUT_TEXT_FALLBACK_KEY, "zh-TW",
    )
    assert sent["attachments"][0].url == (
        "https://yura.example/uploads/stub/fake.png"
    )
    stored = await repo.get(row.id)
    assert stored is not None
    assert stored.status == PendingFollowUpStatus.RESOLVED


@pytest.mark.asyncio
async def test_a_render_that_cannot_be_delivered_still_answers_the_promise() -> None:
    """No public base URL: the render happened, and every attachment was
    dropped on the way out because external platforms cannot fetch
    ``/uploads/...``.

    The cost is identical to the test above — GPU, credits, file — so the
    round must NOT report itself as free to repeat. Judging by the delivery
    list (empty) instead of by what the tool produced would flip the row
    back to ``queued`` and re-render the same picture on every reconcile,
    forever, on exactly the deployment that can never ship it."""
    repo = InMemoryPendingFollowUpRepository()
    row = _promise_row()
    await repo.add(row)
    char = _tooled_character(allowed=["fake_image"])
    proactive = _StubProactiveDispatcher()
    composer = _ScriptedPromiseComposer(
        tool_calls=(ToolCall(name="fake_image", arguments={"scene": "廚房"}),),
        final_text="",
    )
    loop, invocations = _loop([FakeImageTool()], public_base_url="")
    dispatcher = _dispatcher(repo, char, composer, proactive, tool_loop=loop)

    assert await dispatcher.tick(now=_now()) == 1

    sent = proactive.calls[0]
    assert sent["attachments"] == ()  # nothing shippable existed
    assert sent["text"] == localized_fallback_text(
        UNDELIVERABLE_ARTIFACT_FALLBACK_KEY, "zh-TW",
    )
    # NOT the "the picture was sent" line — it provably was not.
    assert sent["text"] != localized_fallback_text(
        DELIVERED_WITHOUT_TEXT_FALLBACK_KEY, "zh-TW",
    )
    stored = await repo.get(row.id)
    assert stored is not None
    assert stored.status == PendingFollowUpStatus.RESOLVED
    assert len(await invocations.list_for_character(char.id)) == 1


@pytest.mark.asyncio
async def test_an_undeliverable_render_is_not_retried_on_the_next_tick() -> None:
    """The consequence of the line above, stated as the loop it prevents:
    a second tick must find nothing to do and must not run the tool again."""
    repo = InMemoryPendingFollowUpRepository()
    await repo.add(_promise_row())
    char = _tooled_character(allowed=["fake_image"])
    proactive = _StubProactiveDispatcher()
    composer = _ScriptedPromiseComposer(
        tool_calls=(ToolCall(name="fake_image", arguments={"scene": "廚房"}),),
        final_text="",
    )
    loop, invocations = _loop([FakeImageTool()], public_base_url="")
    dispatcher = _dispatcher(repo, char, composer, proactive, tool_loop=loop)

    assert await dispatcher.tick(now=_now()) == 1
    assert await dispatcher.tick(now=_now() + timedelta(minutes=15)) == 0

    assert len(await invocations.list_for_character(char.id)) == 1
    assert len(proactive.calls) == 1


@pytest.mark.asyncio
async def test_the_photo_only_fallback_speaks_the_operator_language() -> None:
    """It is player-visible text, so it obeys the same pinned operator
    language every other deterministic line does — not a hardcoded zh-TW."""
    repo = InMemoryPendingFollowUpRepository()
    await repo.add(_promise_row())
    char = _tooled_character(allowed=["fake_image"])
    proactive = _StubProactiveDispatcher()
    composer = _ScriptedPromiseComposer(
        tool_calls=(ToolCall(name="fake_image", arguments={"scene": "廚房"}),),
        final_text="",
    )
    loop, _ = _loop([FakeImageTool()])
    dispatcher = PendingFollowUpDispatcher(
        repository=repo,
        composer=_StubBusyComposer(),
        proactive_dispatcher=proactive,
        character_repository=_StubCharacterRepo({char.id: char}),
        schedule_service=_StubScheduleService(current_activity=None),
        scheduled_promise_composer=composer,
        tool_loop=loop,
        operator_profile_service=_StubOperatorProfileService("en-US"),
    )

    assert await dispatcher.tick(now=_now()) == 1

    assert proactive.calls[0]["text"] == localized_fallback_text(
        DELIVERED_WITHOUT_TEXT_FALLBACK_KEY, "en-US",
    )
    assert proactive.calls[0]["text"] != localized_fallback_text(
        DELIVERED_WITHOUT_TEXT_FALLBACK_KEY, "zh-TW",
    )
