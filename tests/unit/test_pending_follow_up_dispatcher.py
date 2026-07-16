"""Tick-flow tests for :class:`PendingFollowUpDispatcher`.

Verifies the release contract:

* Due rows whose owner is still mid high-busy activity stay queued.
* Due rows whose owner is now idle (or below the busy ceiling) are
  released: composer fires, fan-out fires, row is marked ``resolved``.
* At-cap rows force-release regardless of current busy_score.
* Composer / delivery failure flips the row back to ``queued`` with
  ``last_error`` set so the next tick retries.
* Missing character cancels the row (won't requeue forever).

The harness wires a fake proactive dispatcher and a stub composer so
nothing touches the real LLM / DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from kokoro_link.application.services.pending_follow_up_dispatcher import (
    PendingFollowUpDispatcher,
)
from kokoro_link.contracts.pending_follow_up_composer import (
    PendingFollowUpComposeInput,
    PendingFollowUpComposeOutput,
    PendingFollowUpComposerPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.pending_follow_up import (
    MAX_QUEUED_MESSAGES,
    PendingFollowUp,
    PendingFollowUpMessage,
    PendingFollowUpStatus,
)
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.repositories.in_memory_pending_follow_ups import (
    InMemoryPendingFollowUpRepository,
)


def _now() -> datetime:
    return datetime(2026, 5, 16, 15, 0, tzinfo=timezone.utc)


def _character(cid: str = "char-1") -> Character:
    return Character.create(
        name="Airi", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=20, trust=50, energy=70,
        ),
    ).with_id(cid) if hasattr(Character, "with_id") else _force_id(
        Character.create(
            name="Airi", summary="", personality=[], interests=[],
            speaking_style="", boundaries=[],
            state=CharacterState(
                emotion="neutral", affection=50, fatigue=20, trust=50, energy=70,
            ),
        ),
        cid,
    )


def _force_id(character: Character, cid: str) -> Character:
    from dataclasses import replace
    return replace(character, id=cid)


def _row(
    *,
    character_id: str = "char-1",
    conversation_id: str = "conv-1",
    offset_min: int = -5,
    n_messages: int = 1,
) -> PendingFollowUp:
    base = PendingFollowUp.new(
        character_id=character_id,
        conversation_id=conversation_id,
        first_message=PendingFollowUpMessage.new(
            content="晚餐吃什麼", queued_at=_now() - timedelta(minutes=30),
        ),
        brief_reply="先回，會議結束我再好好回你",
        defer_reason="會議中",
        scheduled_for=_now() + timedelta(minutes=offset_min),
        activity_id="act-1",
        now=_now() - timedelta(minutes=30),
    )
    row = base
    for i in range(1, n_messages):
        row = row.appended(
            PendingFollowUpMessage.new(
                content=f"再補一句 {i}", queued_at=_now() - timedelta(minutes=29 - i),
            ),
        )
    return row


@dataclass
class _StubCharacterRepo:
    characters: dict[str, Character]

    async def get(self, character_id: str) -> Character | None:
        return self.characters.get(character_id)

    async def list(self) -> list[Character]:  # pragma: no cover - unused
        return list(self.characters.values())

    async def save(self, character: Character) -> None:  # pragma: no cover
        self.characters[character.id] = character

    async def delete(self, character_id: str) -> bool:  # pragma: no cover
        return self.characters.pop(character_id, None) is not None


class _StubScheduleService:
    def __init__(self, *, current_activity: Any = None, just_finished: Any = None) -> None:
        self.current_activity = current_activity
        self.just_finished = just_finished

    async def ensure_schedule(self, character: Character) -> object:
        return object()  # truthy placeholder

    def resolve_current(self, schedule, *, now):
        return self.current_activity, [], self.just_finished


class _StubComposer(PendingFollowUpComposerPort):
    def __init__(self, response: str = "會議終於結束了，剛剛你問的事…") -> None:
        self.response = response
        self.calls: list[PendingFollowUpComposeInput] = []
        self.crash = False

    async def compose(self, payload):
        self.calls.append(payload)
        if self.crash:
            raise RuntimeError("boom")
        return PendingFollowUpComposeOutput(content_text=self.response)


class _StubProactiveDispatcher:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.outcome = ProactiveOutcome.SENT

    async def deliver_pre_composed(
        self,
        *,
        character_id: str,
        text: str,
        trigger: ProactiveTrigger,
        reason: str = "",
        attachments: tuple = (),
        now: datetime | None = None,
    ) -> ProactiveAttempt:
        self.calls.append({
            "character_id": character_id, "text": text,
            "trigger": trigger, "reason": reason,
        })
        return ProactiveAttempt.record(
            character_id=character_id, trigger=trigger,
            outcome=self.outcome, reason=reason or "stub",
            message=text, now=now or _now(),
        )


def _busy_activity():
    from kokoro_link.domain.entities.schedule import ScheduleActivity
    start = _now() - timedelta(minutes=30)
    end = _now() + timedelta(minutes=30)
    return ScheduleActivity.create(
        start_at=start, end_at=end, description="會議", category="meeting",
        busy_score=0.9,
    )


@pytest.mark.asyncio
async def test_releases_when_no_longer_busy() -> None:
    repo = InMemoryPendingFollowUpRepository()
    row = _row()
    await repo.add(row)
    char = _character()
    proactive = _StubProactiveDispatcher()
    composer = _StubComposer()
    dispatcher = PendingFollowUpDispatcher(
        repository=repo,
        composer=composer,
        proactive_dispatcher=proactive,
        character_repository=_StubCharacterRepo({char.id: char}),
        schedule_service=_StubScheduleService(current_activity=None),
    )
    resolved = await dispatcher.tick(now=_now())
    assert resolved == 1
    assert len(proactive.calls) == 1
    assert proactive.calls[0]["trigger"] == ProactiveTrigger.PENDING_FOLLOW_UP
    row_after = await repo.get(row.id)
    assert row_after is not None
    assert row_after.status == PendingFollowUpStatus.RESOLVED


@pytest.mark.asyncio
async def test_keeps_queued_while_still_busy() -> None:
    repo = InMemoryPendingFollowUpRepository()
    row = _row()
    await repo.add(row)
    char = _character()
    proactive = _StubProactiveDispatcher()
    composer = _StubComposer()
    dispatcher = PendingFollowUpDispatcher(
        repository=repo,
        composer=composer,
        proactive_dispatcher=proactive,
        character_repository=_StubCharacterRepo({char.id: char}),
        schedule_service=_StubScheduleService(current_activity=_busy_activity()),
    )
    resolved = await dispatcher.tick(now=_now())
    assert resolved == 0
    assert proactive.calls == []
    row_after = await repo.get(row.id)
    assert row_after is not None
    assert row_after.status == PendingFollowUpStatus.QUEUED


@pytest.mark.asyncio
async def test_force_release_when_at_cap() -> None:
    """Capped rows release even when the owner is still mid high-busy."""
    repo = InMemoryPendingFollowUpRepository()
    row = _row(n_messages=MAX_QUEUED_MESSAGES)
    assert row.is_at_cap
    await repo.add(row)
    char = _character()
    proactive = _StubProactiveDispatcher()
    composer = _StubComposer()
    dispatcher = PendingFollowUpDispatcher(
        repository=repo,
        composer=composer,
        proactive_dispatcher=proactive,
        character_repository=_StubCharacterRepo({char.id: char}),
        schedule_service=_StubScheduleService(current_activity=_busy_activity()),
    )
    resolved = await dispatcher.tick(now=_now())
    assert resolved == 1
    assert len(proactive.calls) == 1


@pytest.mark.asyncio
async def test_composer_crash_requeues_with_error() -> None:
    repo = InMemoryPendingFollowUpRepository()
    row = _row()
    await repo.add(row)
    char = _character()
    proactive = _StubProactiveDispatcher()
    composer = _StubComposer()
    composer.crash = True
    dispatcher = PendingFollowUpDispatcher(
        repository=repo, composer=composer,
        proactive_dispatcher=proactive,
        character_repository=_StubCharacterRepo({char.id: char}),
        schedule_service=_StubScheduleService(current_activity=None),
    )
    resolved = await dispatcher.tick(now=_now())
    assert resolved == 0
    row_after = await repo.get(row.id)
    assert row_after is not None
    assert row_after.status == PendingFollowUpStatus.QUEUED
    assert row_after.last_error and "crashed" in row_after.last_error


@pytest.mark.asyncio
async def test_empty_compose_requeues() -> None:
    repo = InMemoryPendingFollowUpRepository()
    row = _row()
    await repo.add(row)
    char = _character()
    proactive = _StubProactiveDispatcher()
    composer = _StubComposer(response="   ")
    dispatcher = PendingFollowUpDispatcher(
        repository=repo, composer=composer,
        proactive_dispatcher=proactive,
        character_repository=_StubCharacterRepo({char.id: char}),
        schedule_service=_StubScheduleService(current_activity=None),
    )
    resolved = await dispatcher.tick(now=_now())
    assert resolved == 0
    row_after = await repo.get(row.id)
    assert row_after is not None
    assert row_after.status == PendingFollowUpStatus.QUEUED


@pytest.mark.asyncio
async def test_delivery_errored_requeues() -> None:
    repo = InMemoryPendingFollowUpRepository()
    row = _row()
    await repo.add(row)
    char = _character()
    proactive = _StubProactiveDispatcher()
    proactive.outcome = ProactiveOutcome.ERRORED
    composer = _StubComposer()
    dispatcher = PendingFollowUpDispatcher(
        repository=repo, composer=composer,
        proactive_dispatcher=proactive,
        character_repository=_StubCharacterRepo({char.id: char}),
        schedule_service=_StubScheduleService(current_activity=None),
    )
    resolved = await dispatcher.tick(now=_now())
    assert resolved == 0
    row_after = await repo.get(row.id)
    assert row_after is not None
    assert row_after.status == PendingFollowUpStatus.QUEUED
    assert row_after.last_error is not None


@pytest.mark.asyncio
async def test_missing_character_cancels_row() -> None:
    repo = InMemoryPendingFollowUpRepository()
    row = _row()
    await repo.add(row)
    proactive = _StubProactiveDispatcher()
    composer = _StubComposer()
    dispatcher = PendingFollowUpDispatcher(
        repository=repo, composer=composer,
        proactive_dispatcher=proactive,
        character_repository=_StubCharacterRepo({}),
        schedule_service=_StubScheduleService(current_activity=None),
    )
    resolved = await dispatcher.tick(now=_now())
    assert resolved == 0
    row_after = await repo.get(row.id)
    assert row_after is not None
    assert row_after.status == PendingFollowUpStatus.CANCELLED
