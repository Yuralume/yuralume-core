from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.scheduled_promise_composer import (
    ScheduledPromiseComposeInput,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import MessageContentMode
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
