"""Cloud-mode attribution requires every encounter LLM call to carry a
character, so ``CloudActiveLLMProvider`` can resolve the owning operator's
tenant/account. These regression tests pin that the encounter planner and
runner forward a character on every provider call — a bare
``resolve(FEATURE_X)`` would raise ``CloudIdentityUnavailable`` at generate
time in cloud mode.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from kokoro_link.application.services.character_encounter_service import (
    CharacterEncounterPlanner,
    CharacterEncounterRunner,
)


class _FakeModel:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def generate(self, prompt: str, *, model: str | None = None) -> str:
        self.prompts.append(prompt)
        return '{"should_plan": false, "summary_for_a": "a", "summary_for_b": "b"}'


class _RecordingProvider:
    """Records the ``character`` kwarg passed on every provider call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, object]] = []
        self._model = _FakeModel()

    async def is_fake(self, feature_key=None, *, character=None) -> bool:
        self.calls.append(("is_fake", feature_key, character))
        return False

    async def resolve(self, feature_key=None, *, character=None):
        self.calls.append(("resolve", feature_key, character))
        return self._model

    async def resolve_model_id(self, feature_key=None, *, character=None):
        self.calls.append(("resolve_model_id", feature_key, character))
        return None


def _char(cid: str, user_id: str = "cloud:acct_1") -> SimpleNamespace:
    return SimpleNamespace(
        id=cid, name=cid.upper(), summary=f"{cid} summary", user_id=user_id,
        personality=(), speaking_style="", interests=(), boundaries=(),
    )


def _relationship() -> SimpleNamespace:
    perspective = SimpleNamespace(
        affection_self_to_peer=50,
        trust_self_to_peer=50,
    )
    return SimpleNamespace(
        relationship_label=None,
        how_a_sees_b=None,
        how_b_sees_a=None,
        last_interaction_at=None,
        perspective_for=lambda _character_id: perspective,
    )


def _assert_all_calls_forward(provider: _RecordingProvider, expected: object) -> None:
    assert provider.calls, "provider was not invoked"
    for method, _feature, character in provider.calls:
        assert character is expected, f"{method} did not forward the character"


def _planner(provider: _RecordingProvider) -> CharacterEncounterPlanner:
    return CharacterEncounterPlanner(
        relationship_repository=MagicMock(),
        encounter_repository=MagicMock(),
        character_repository=MagicMock(),
        schedule_service=MagicMock(),
        schedule_repository=MagicMock(),
        provider=provider,
        local_tz=timezone.utc,
    )


def _runner(
    provider: _RecordingProvider,
    *,
    local_tz=timezone.utc,  # noqa: ANN001
) -> CharacterEncounterRunner:
    return CharacterEncounterRunner(
        encounter_repository=MagicMock(),
        character_repository=MagicMock(),
        memory_writer=MagicMock(),
        relationship_service=MagicMock(),
        provider=provider,
        local_tz=local_tz,
    )


@pytest.mark.asyncio
async def test_encounter_planner_forwards_character_for_cloud_attribution() -> None:
    provider = _RecordingProvider()
    planner = _planner(provider)
    char_a = _char("a")
    now = datetime(2026, 6, 8, tzinfo=timezone.utc)
    relationship = _relationship()

    await planner._ask_llm_for_plan(
        relationship=relationship,
        char_a=char_a,
        char_b=_char("b"),
        start_at=now,
        end_at=now,
        hint_location=None,
    )

    _assert_all_calls_forward(provider, char_a)


@pytest.mark.asyncio
async def test_encounter_runner_transcript_forwards_character() -> None:
    provider = _RecordingProvider()
    runner = _runner(provider)
    char_a = _char("a")
    encounter = SimpleNamespace(max_turns=2, location="街角", trigger_reason="巧遇")

    await runner._generate_transcript(encounter, char_a, _char("b"))

    _assert_all_calls_forward(provider, char_a)


@pytest.mark.asyncio
async def test_encounter_runner_reflect_forwards_character() -> None:
    provider = _RecordingProvider()
    runner = _runner(provider)
    char_a = _char("a")
    encounter = SimpleNamespace(
        id="enc1", max_turns=2, location="街角", trigger_reason="巧遇",
    )

    await runner._reflect(encounter, char_a, _char("b"), ())

    _assert_all_calls_forward(provider, char_a)


@pytest.mark.asyncio
async def test_encounter_runner_transcript_includes_local_scheduled_time() -> None:
    provider = _RecordingProvider()
    runner = _runner(provider, local_tz=ZoneInfo("Asia/Taipei"))
    char_a = _char("a")
    encounter = SimpleNamespace(
        max_turns=2,
        location="街角",
        trigger_reason="巧遇",
        scheduled_for=datetime(2026, 6, 19, 23, 30, tzinfo=timezone.utc),
    )

    await runner._generate_transcript(encounter, char_a, _char("b"))

    prompt = provider._model.prompts[0]
    assert "碰面時間：2026-06-20 07:30" in prompt
    assert "清晨" in prompt
