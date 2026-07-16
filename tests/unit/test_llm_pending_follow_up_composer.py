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
