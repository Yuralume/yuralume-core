"""Shared fixtures for the checkpoint suites.

Everything here is deterministic and every timestamp is derived from a
frozen ``NOW``. No test in this package reads the wall clock — a suite
about "how far back does the summary reach" would otherwise start
failing on a date nobody chose.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from kokoro_link.contracts.dialogue_checkpoint import (
    DialogueCheckpointMergeResult,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import (
    Message,
    MessageContentMode,
    MessageKind,
    MessageRole,
)
from kokoro_link.domain.value_objects.character_state import CharacterState

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
OPERATOR_ID = "op-1"
CHARACTER_ID = "char-1"


def at(minutes_before: int) -> datetime:
    return NOW - timedelta(minutes=minutes_before)


def character(
    *, character_id: str = CHARACTER_ID, operator_id: str = OPERATOR_ID,
) -> Character:
    built = Character.create(
        name="小悠",
        summary="測試角色",
        user_id=operator_id,
        personality=["溫和"],
        interests=["咖啡"],
        speaking_style="口語",
        boundaries=[],
        state=CharacterState(
            emotion="平靜", affection=50, fatigue=20, trust=50, energy=70,
        ),
    )
    # ``create`` mints a uuid; the suites want a stable id so a stored
    # checkpoint can be looked up by the constant.
    return replace(built, id=character_id)


def user_message(content: str, when: datetime) -> Message:
    return Message(role=MessageRole.USER, content=content, created_at=when)


def assistant_message(content: str, when: datetime) -> Message:
    return Message(
        role=MessageRole.ASSISTANT, content=content, created_at=when,
    )


def tool_only_message(when: datetime) -> Message:
    return Message(
        role=MessageRole.ASSISTANT,
        content="",
        kind=MessageKind.TOOL_ONLY,
        created_at=when,
    )


def restricted_message(
    content: str, when: datetime, *, safe_summary: str = "",
) -> Message:
    """An NSFW-marked message, with or without a safe replacement.

    Without one, the tolerance filter drops it entirely; with one, the
    filter substitutes the summary. Both directions matter to the
    checkpoint, which must never carry the original either way.
    """
    return Message(
        role=MessageRole.ASSISTANT,
        content=content,
        content_mode=MessageContentMode.NSFW,
        safe_summary=safe_summary,
        created_at=when,
    )


def conversation_of(count: int, *, oldest_minutes_before: int = 600) -> list[Message]:
    """``count`` alternating turns, oldest first, one minute apart.

    Each line is long enough that a handful of them clear a realistic
    token trigger, so a test can talk in turns rather than in characters.
    """
    messages: list[Message] = []
    for index in range(count):
        when = at(oldest_minutes_before - index)
        text = (
            f"第{index}輪：我們今天聊到了工作、家裡的事，還有週末大概"
            f"要做什麼，這是一段夠長的訊息內容用來累積 token。"
        )
        messages.append(
            user_message(text, when) if index % 2 == 0
            else assistant_message(text, when)
        )
    return messages


class FakeMerger:
    """A merger with no model behind it.

    ``summaries`` is consumed in order; running out repeats the last
    one, so a 60-turn script does not need 60 scripted answers.
    """

    def __init__(
        self, summaries: list[str] | None = None, *, label: str = "fake-model",
    ) -> None:
        self._summaries = list(summaries or ["累積摘要"])
        self._label = label
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def merge(self, *, character, previous_summary, messages):
        self.calls.append(
            (previous_summary, tuple(m.content for m in messages)),
        )
        summary = (
            self._summaries.pop(0) if len(self._summaries) > 1
            else self._summaries[0]
        )
        return DialogueCheckpointMergeResult(
            summary=summary, model=self._label,
        )


class FailingMerger:
    """Returns an empty result — the shape of every merge failure."""

    def __init__(self) -> None:
        self.calls = 0

    async def merge(self, *, character, previous_summary, messages):
        self.calls += 1
        return DialogueCheckpointMergeResult.failed()


class StubConversationRepository:
    """Just enough of ``ConversationRepositoryPort`` for the updater.

    The updater calls exactly one method; a full in-memory repository
    would need conversations, ids and a save path this suite never
    exercises, and would hide which call the updater actually makes.
    """

    def __init__(self, messages: list[Message]) -> None:
        self.messages = list(messages)
        self.requested_limits: list[int] = []

    async def recent_messages_for_character(
        self, character_id: str, *, limit: int, exclude_tool_only: bool = False,
    ) -> list[Message]:
        self.requested_limits.append(limit)
        rows = [
            m for m in self.messages
            if not (exclude_tool_only and m.kind is MessageKind.TOOL_ONLY)
        ]
        return rows[-limit:] if limit > 0 else []
