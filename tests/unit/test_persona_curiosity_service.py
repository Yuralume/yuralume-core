from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from kokoro_link.application.services.persona_curiosity_service import (
    PersonaCuriosityService,
)
from kokoro_link.domain.entities.operator_persona import OperatorPersona


@pytest.mark.asyncio
async def test_build_context_keeps_initial_relationship_above_empty_interaction() -> None:
    repository = AsyncMock()
    repository.list_recent = AsyncMock(return_value=[])
    service = PersonaCuriosityService(repository=repository)
    persona = OperatorPersona.empty("char-A", "default")

    context = await service.build_context(
        persona=persona,
        surface="chat",
        initial_relationship_lines=(
            "使用者創角時確認的起始關係設定：",
            "- 關係：老朋友",
        ),
    )

    assert "互動紀錄" in context.interaction_strength
    assert "剛認識" not in context.interaction_strength
    assert context.initial_relationship_summary[0].startswith("起始關係設定")
    assert "- 關係：老朋友" in context.initial_relationship_summary


@pytest.mark.asyncio
async def test_build_context_carries_operator_language() -> None:
    repository = AsyncMock()
    repository.list_recent = AsyncMock(return_value=[])
    service = PersonaCuriosityService(repository=repository)
    persona = OperatorPersona.empty("char-A", "default")

    context = await service.build_context(
        persona=persona,
        surface="chat",
        operator_primary_language="en-US",
    )

    assert context.operator_primary_language == "en-US"


@pytest.mark.asyncio
async def test_build_context_defaults_blank_operator_language_to_zh_tw() -> None:
    repository = AsyncMock()
    repository.list_recent = AsyncMock(return_value=[])
    service = PersonaCuriosityService(repository=repository)
    persona = OperatorPersona.empty("char-A", "default")

    context = await service.build_context(
        persona=persona,
        surface="chat",
        operator_primary_language="",
    )

    assert context.operator_primary_language == "zh-TW"
