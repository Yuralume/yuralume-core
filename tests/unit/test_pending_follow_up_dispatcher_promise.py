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

from kokoro_link.application.services.pending_follow_up_dispatcher import (
    PendingFollowUpDispatcher,
)
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
from kokoro_link.infrastructure.repositories.in_memory_pending_follow_ups import (
    InMemoryPendingFollowUpRepository,
)


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
