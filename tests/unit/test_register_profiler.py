from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import pytest

from kokoro_link.contracts.register_profile import RegisterProfileContext
from kokoro_link.infrastructure.register.llm_register_profiler import (
    LLMRegisterProfiler,
    _build_prompt,
)
from kokoro_link.infrastructure.register.null_register_profiler import (
    NullRegisterProfiler,
)


class _Model:
    provider_id = "unit-provider"
    supports_vision = False

    def __init__(self, response: str | Exception) -> None:
        self.response = response

    async def generate(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> str:
        del prompt, image_urls, model
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    async def generate_stream(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> AsyncIterator[str]:
        del prompt, image_urls, model
        if False:
            yield ""

    async def list_models(self) -> list[str]:
        return []


def _context() -> RegisterProfileContext:
    return RegisterProfileContext(
        character_id="c1",
        operator_id="u1",
        latest_user_message="今天只是想隨便聊聊午餐",
        recent_dialogue_summary="最近都在聊日常和工作。",
        relationship_context=("互動還很少。",),
        content_tolerance="frontier",
    )


def test_register_profiler_prompt_contains_scaffolding_context() -> None:
    prompt = _build_prompt(_context())

    assert "軟性多軸剖面" in prompt
    assert "latest" not in prompt.lower()
    assert "今天只是想隨便聊聊午餐" in prompt
    assert "互動還很少" in prompt
    assert "frontier" in prompt


@pytest.mark.asyncio
async def test_llm_register_profiler_parses_axes_and_vulnerability_metadata() -> None:
    profiler = LLMRegisterProfiler(
        model=_Model(
            '{"axes":{"emotional_intensity":0.8,"seriousness":0.7,'
            '"intimacy":0.4,"humor_latitude":0.1,"help_seeking":0.6},'
            '"confidence":0.35,"vulnerable_disclosure":true,'
            '"note":"可能在低信心下揭露脆弱"}',
        ),
    )

    profile = await profiler.profile(_context())

    assert profile is not None
    assert profile.emotional_intensity == 0.8
    assert profile.vulnerable_disclosure is True
    assert profile.confidence == 0.35
    assert profile.metadata["provider_id"] == "unit-provider"
    assert profile.metadata["model_id"] == "unit-provider"


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["not json", '{"axes":[]}', '{"confidence":"x"}'])
async def test_llm_register_profiler_fails_soft_for_bad_output(response: str) -> None:
    profile = await LLMRegisterProfiler(model=_Model(response)).profile(_context())

    assert profile is None


@pytest.mark.asyncio
async def test_llm_register_profiler_clamps_bad_confidence_with_valid_axes() -> None:
    profile = await LLMRegisterProfiler(
        model=_Model(
            '{"axes":{"emotional_intensity":0.2,"seriousness":0.3,'
            '"intimacy":0.1,"humor_latitude":0.7,"help_seeking":0.0},'
            '"confidence":"x","vulnerable_disclosure":false,"note":"日常"}',
        ),
    ).profile(_context())

    assert profile is not None
    assert profile.confidence == 0.0


@pytest.mark.asyncio
async def test_llm_register_profiler_fails_soft_on_provider_error() -> None:
    profile = await LLMRegisterProfiler(model=_Model(RuntimeError("boom"))).profile(
        _context(),
    )

    assert profile is None


@pytest.mark.asyncio
async def test_null_register_profiler_returns_none() -> None:
    profile = await NullRegisterProfiler().profile(_context())

    assert profile is None
