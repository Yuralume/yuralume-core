"""ScheduleService forwards recent-dialogue summary into the planner.

Guards the wiring from ``ScheduleService.ensure_schedule`` →
``DialogueSummarizerPort`` → ``SchedulePlannerPort.plan_day``. A planner
that ignores the new kwarg is fine, but the service must still fetch the
latest web conversation, strip tool-only turns, summarise it, and pass
the result down. No conversation / no summarizer → empty string, no
exception — the planner must still run.
"""

from __future__ import annotations

from datetime import date, datetime, timezone, tzinfo

import pytest

from kokoro_link.application.services.schedule_service import ScheduleService
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageKind,
    MessageRole,
)
from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_schedules import (
    InMemoryScheduleRepository,
)


UTC = timezone.utc


class _RecordingPlanner:
    def __init__(self) -> None:
        self.last_summary: str | None = None

    async def plan_day(
        self,
        *,
        character: Character,
        date_: date,
        local_tz: tzinfo,
        recent_dialogue_summary: str = "",
        **_: object,
    ) -> DailySchedule:
        self.last_summary = recent_dialogue_summary
        activity = ScheduleActivity.create(
            start_at=datetime.combine(date_, datetime.min.time(), tzinfo=local_tz).replace(hour=9),
            end_at=datetime.combine(date_, datetime.min.time(), tzinfo=local_tz).replace(hour=10),
            description="rec",
            category="work",
        )
        return DailySchedule.create(
            character_id=character.id, date_=date_, activities=[activity],
        )


class _RecordingSummarizer:
    def __init__(self, output: str = "近期聊了什麼") -> None:
        self.output = output
        self.calls: list[list[Message]] = []

    async def summarize(
        self, *, character: Character, messages: list[Message],
    ) -> str:
        self.calls.append(list(messages))
        return self.output


class _ExplodingSummarizer:
    async def summarize(self, *, character, messages):  # noqa: ANN001
        raise RuntimeError("LLM is down")


def _character() -> Character:
    return Character.create(
        name="Mio", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


async def _seed_web_conversation(
    repo: InMemoryConversationRepository, character_id: str,
) -> None:
    convo = Conversation.start(character_id=character_id)
    convo = convo.append(Message(role=MessageRole.USER, content="今天好累"))
    convo = convo.append(Message(
        role=MessageRole.ASSISTANT, content="辛苦了，要不要早點休息？",
    ))
    # Tool-only turn must be filtered before summarisation.
    convo = convo.append(Message(
        role=MessageRole.ASSISTANT, content="",
        kind=MessageKind.TOOL_ONLY,
    ))
    await repo.save(convo)


@pytest.mark.asyncio
async def test_summarizer_output_flows_into_planner() -> None:
    schedules = InMemoryScheduleRepository()
    convos = InMemoryConversationRepository()
    planner = _RecordingPlanner()
    summarizer = _RecordingSummarizer(output="剛剛在聊累不累、要早點睡")
    service = ScheduleService(
        repository=schedules,
        planner=planner,
        local_tz=UTC,
        conversation_repository=convos,
        dialogue_summarizer=summarizer,
    )
    character = _character()
    await _seed_web_conversation(convos, character.id)

    await service.ensure_schedule(character, date_=date(2026, 4, 18))

    assert planner.last_summary == "剛剛在聊累不累、要早點睡"
    # Summarizer got the chat turns without the TOOL_ONLY entry.
    assert len(summarizer.calls) == 1
    fed = summarizer.calls[0]
    assert all(m.kind is MessageKind.CHAT for m in fed)
    assert [m.content for m in fed] == ["今天好累", "辛苦了，要不要早點休息？"]


@pytest.mark.asyncio
async def test_missing_conversation_yields_empty_summary() -> None:
    schedules = InMemoryScheduleRepository()
    convos = InMemoryConversationRepository()
    planner = _RecordingPlanner()
    summarizer = _RecordingSummarizer(output="should not be called")
    service = ScheduleService(
        repository=schedules,
        planner=planner,
        local_tz=UTC,
        conversation_repository=convos,
        dialogue_summarizer=summarizer,
    )

    await service.ensure_schedule(_character(), date_=date(2026, 4, 18))

    assert planner.last_summary == ""
    assert summarizer.calls == []


@pytest.mark.asyncio
async def test_summarizer_failure_degrades_to_empty_summary() -> None:
    schedules = InMemoryScheduleRepository()
    convos = InMemoryConversationRepository()
    planner = _RecordingPlanner()
    service = ScheduleService(
        repository=schedules,
        planner=planner,
        local_tz=UTC,
        conversation_repository=convos,
        dialogue_summarizer=_ExplodingSummarizer(),
    )
    character = _character()
    await _seed_web_conversation(convos, character.id)

    schedule = await service.ensure_schedule(character, date_=date(2026, 4, 18))

    assert planner.last_summary == ""
    # Planner still ran and produced the (stub) activity, so the day
    # isn't broken just because summarisation blew up.
    assert len(schedule.activities) == 1


@pytest.mark.asyncio
async def test_service_without_summarizer_wiring_passes_empty() -> None:
    """Backward-compat: existing call-sites that construct ScheduleService
    without the dialogue kwargs should still work and pass empty."""
    schedules = InMemoryScheduleRepository()
    planner = _RecordingPlanner()
    service = ScheduleService(
        repository=schedules, planner=planner, local_tz=UTC,
    )

    await service.ensure_schedule(_character(), date_=date(2026, 4, 18))

    assert planner.last_summary == ""
