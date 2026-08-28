"""TD — the dialogue summariser's transcript carries a time anchor per turn.

Production incident, 2026-08-26: a proactive push composed at 07:56
described a question the player had asked at 07:46 as 「昨天」. The
current-time injection and the anti-fabrication rules in the proactive
prompts were both already in place — the fact they needed was gone
before they ran. ``LLMDialogueSummarizer._format_line`` rendered only
``角色名：內容`` and dropped ``Message.created_at`` on the floor, so the
summary handed to the decider had no clock in it at all and a small
model filled the hole itself.

These tests pin the anchor at the data layer: the *prompt the
summarising model actually receives*, not the summary it returns.
A ten-minute-old turn has to read as minutes, the rendering has to
survive a missing timestamp, and a caller with no instant in hand has
to still get an anchor rather than none.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Message, MessageRole
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.dialogue.llm_summarizer import (
    LLMDialogueSummarizer,
)
from kokoro_link.infrastructure.dialogue.null_summarizer import (
    NullDialogueSummarizer,
)

_TAIPEI = timezone(timedelta(hours=8))
_INCIDENT_NOW = datetime(2026, 8, 26, 7, 56, tzinfo=timezone.utc)

#: Every relative-day word the incident produced or could produce. None
#: of them may appear anywhere in a transcript whose turns are minutes
#: old — the anchor is what makes them unavailable to the model.
_DAY_WORDS = ("昨天", "前天", "上禮拜", "上週", "之前那天")


class _RecordingModel:
    """Captures the prompt so the transcript itself can be asserted on."""

    supports_vision = False

    def __init__(self, response: str = "他們聊到明天的安排。") -> None:
        self._response = response
        self.last_prompt: str | None = None

    async def generate(self, prompt: str, **_kwargs) -> str:  # noqa: ANN003
        self.last_prompt = prompt
        return self._response

    def generate_stream(self, prompt: str, **_kwargs):  # noqa: ANN003, ARG002
        async def _empty():
            if False:
                yield ""
        return _empty()


def _character() -> Character:
    return Character.create(
        name="小藍",
        summary="插畫家",
        personality=["內向"],
        interests=["音樂"],
        speaking_style="溫柔",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


def _incident_messages() -> list[Message]:
    """The shape of the real incident: two turns, ten minutes old."""
    return [
        Message(
            role=MessageRole.USER,
            content="妳明天有空嗎",
            created_at=_INCIDENT_NOW - timedelta(minutes=10),
        ),
        Message(
            role=MessageRole.ASSISTANT,
            content="我看一下行事曆喔",
            created_at=_INCIDENT_NOW - timedelta(minutes=9),
        ),
    ]


def _transcript_of(prompt: str) -> str:
    """Just the rendered turns.

    Day words are *legal* above this line — the template's prohibition
    has to name 「昨天」 to forbid it. What must not contain one is the
    data the model reads the events off.
    """
    _, _, tail = prompt.partition("對話紀錄（")
    assert tail, "summarizer template lost its transcript section"
    return tail.split("\n", 1)[1]


async def _prompt_for(
    messages: list[Message],
    *,
    now: datetime | None = _INCIDENT_NOW,
    local_tz=_TAIPEI,
) -> str:
    model = _RecordingModel()
    summarizer = LLMDialogueSummarizer(model=model)
    await summarizer.summarize(
        character=_character(),
        messages=messages,
        now=now,
        local_tz=local_tz,
    )
    assert model.last_prompt is not None
    return model.last_prompt


@pytest.mark.asyncio
async def test_every_turn_is_rendered_with_its_own_time_anchor() -> None:
    prompt = await _prompt_for(_incident_messages())

    # 07:46 UTC == 15:46 in the operator's zone. Both halves of the
    # anchor are present: the absolute civil clock the model can do
    # calendar arithmetic against, and the relative reading it can copy.
    assert (
        "[2026-08-26 15:46（下午）｜約 10 分鐘前] 使用者：妳明天有空嗎"
    ) in prompt
    assert (
        "[2026-08-26 15:47（下午）｜約 9 分鐘前] 小藍：我看一下行事曆喔"
    ) in prompt


@pytest.mark.asyncio
async def test_ten_minute_old_turn_reads_as_minutes_not_a_date() -> None:
    """The incident, stated as an assertion.

    A turn ten minutes old must surface as a minute-level reading and a
    same-day clock time. Nothing in the transcript may offer the model a
    previous-day word to reach for.
    """
    transcript = _transcript_of(await _prompt_for(_incident_messages()))

    assert "分鐘前" in transcript
    for word in _DAY_WORDS:
        assert word not in transcript, f"transcript leaked a day word: {word}"


@pytest.mark.asyncio
async def test_template_forbids_backdating_recent_events() -> None:
    prompt = await _prompt_for(_incident_messages())

    # The rendered instruction, not just the data: a transcript full of
    # anchors is still summarisable into 「昨天」 unless the model is told
    # the anchors are binding.
    assert "時間錨必須保留" in prompt
    assert "「昨天」" in prompt


@pytest.mark.asyncio
async def test_missing_created_at_renders_without_an_anchor() -> None:
    """Defensive: a message with no timestamp must not take the call down."""
    messages = [
        Message(
            role=MessageRole.USER, content="沒有時戳的一句", created_at=None,
        ),
        Message(
            role=MessageRole.ASSISTANT,
            content="我看一下行事曆喔",
            created_at=_INCIDENT_NOW - timedelta(minutes=9),
        ),
    ]

    prompt = await _prompt_for(messages)

    assert "使用者：沒有時戳的一句" in prompt
    # The neighbouring turn keeps its anchor — one bad row does not
    # de-anchor the whole transcript.
    assert "約 9 分鐘前] 小藍：我看一下行事曆喔" in prompt


@pytest.mark.asyncio
async def test_absent_now_and_tz_fail_soft_to_an_anchor_anyway() -> None:
    """No instant from the caller still beats no anchor at all.

    Some stations (the side-story scene builder) only ever hold a civil
    date. They fall back to the UTC wall clock: the absolute half is then
    unlocalised, but the relative half — the half the incident got wrong
    — stays exact.
    """
    real_now = datetime.now(timezone.utc)
    messages = [
        Message(
            role=MessageRole.USER,
            content="妳明天有空嗎",
            created_at=real_now - timedelta(minutes=10),
        ),
        Message(
            role=MessageRole.ASSISTANT,
            content="我看一下行事曆喔",
            created_at=real_now - timedelta(minutes=9),
        ),
    ]

    transcript = _transcript_of(await _prompt_for(messages, now=None, local_tz=None))

    assert "約 10 分鐘前] 使用者：妳明天有空嗎" in transcript
    for word in _DAY_WORDS:
        assert word not in transcript


@pytest.mark.asyncio
async def test_naive_created_at_is_read_as_utc_not_crashed_on() -> None:
    messages = [
        Message(
            role=MessageRole.USER,
            content="妳明天有空嗎",
            created_at=_INCIDENT_NOW.replace(tzinfo=None) - timedelta(minutes=10),
        ),
        Message(
            role=MessageRole.ASSISTANT,
            content="我看一下行事曆喔",
            created_at=_INCIDENT_NOW.replace(tzinfo=None) - timedelta(minutes=9),
        ),
    ]

    prompt = await _prompt_for(messages)

    assert "約 10 分鐘前] 使用者：妳明天有空嗎" in prompt


@pytest.mark.asyncio
async def test_null_summarizer_accepts_the_widened_signature() -> None:
    """The port grew two keywords; the no-op implementation has to match."""
    result = await NullDialogueSummarizer().summarize(
        character=_character(),
        messages=_incident_messages(),
        now=_INCIDENT_NOW,
        local_tz=_TAIPEI,
    )
    assert result == ""
