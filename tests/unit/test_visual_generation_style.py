"""BDD for player visual-generation style preferences."""

from __future__ import annotations

import pytest

from kokoro_link.application.services.visual_generation_style import (
    VISUAL_GENERATION_STYLE_DEFAULT,
    VisualGenerationStyleService,
    apply_visual_generation_style,
    normalise_visual_generation_style,
)
from kokoro_link.infrastructure.repositories.in_memory_preferences import (
    InMemoryPreferencesRepository,
)


def test_normalise_visual_generation_style_defaults_unknown_values() -> None:
    assert normalise_visual_generation_style(None) == VISUAL_GENERATION_STYLE_DEFAULT
    assert normalise_visual_generation_style({"style": "realistic"}) == "realistic"
    assert normalise_visual_generation_style({"style": "noir"}) == "anime"


def test_apply_visual_generation_style_appends_prompt_guidance() -> None:
    prompt = apply_visual_generation_style("cafe at dusk", "realistic")

    assert "cafe at dusk" in prompt
    assert "realistic live-action" in prompt
    assert "Avoid anime" in prompt


@pytest.mark.asyncio
async def test_visual_generation_style_service_uses_user_override() -> None:
    service = VisualGenerationStyleService(
        preferences=InMemoryPreferencesRepository(),
    )

    await service.set_style("realistic", user_id="alice")
    styled = await service.styled_prompt("rainy street", user_id="alice")

    assert "rainy street" in styled
    assert "realistic live-action" in styled
    assert await service.get_style(user_id="bob") == "anime"
