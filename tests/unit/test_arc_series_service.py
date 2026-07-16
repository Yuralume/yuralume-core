from __future__ import annotations

from dataclasses import replace

import pytest

from kokoro_link.application.services.arc_series_service import (
    ArcSeriesNotFoundError,
    ArcSeriesService,
    ArcSeriesValidationError,
)
from kokoro_link.domain.entities.arc_series import (
    CharacterSeriesProgress,
    SERIES_STATUS_CONCLUDED,
)
from kokoro_link.domain.entities.arc_template import ArcTemplate, ArcTemplateBeat
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_arc_series import (
    InMemoryArcSeriesRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_arc_templates import (
    InMemoryArcTemplateRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)


def _template(template_id: str) -> ArcTemplate:
    return ArcTemplate.create(
        id=template_id,
        title=f"Template {template_id}",
        premise="一段可接續的劇情。",
        theme="growth",
        duration_days=7,
        beats=[
            ArcTemplateBeat.create(
                sequence=0,
                day_offset=0,
                title="Opening",
                summary="故事開始。",
            ),
        ],
    )


def _character(*, user_id: str = "user-a") -> Character:
    return Character.create(
        name="Aki",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral",
            affection=50,
            fatigue=0,
            trust=50,
            energy=100,
        ),
        user_id=user_id,
    )


async def _service() -> tuple[
    ArcSeriesService,
    InMemoryArcSeriesRepository,
    InMemoryArcTemplateRepository,
    InMemoryCharacterRepository,
]:
    series_repo = InMemoryArcSeriesRepository()
    template_repo = InMemoryArcTemplateRepository()
    character_repo = InMemoryCharacterRepository()
    service = ArcSeriesService(
        series_repository=series_repo,
        template_repository=template_repo,
        character_repository=character_repo,
    )
    return service, series_repo, template_repo, character_repo


@pytest.mark.asyncio
async def test_create_series_requires_two_visible_unique_templates() -> None:
    service, _, template_repo, _ = await _service()
    await template_repo.save_for_user(_template("a"), user_id="user-a")
    await template_repo.save_for_user(_template("b"), user_id="user-a")

    series = await service.create_for_user(
        id="series-a",
        user_id="user-a",
        title="第一季",
        premise="她依序面對三件事。",
        template_ids=[" a ", "b"],
    )

    assert series.member_template_ids == ("a", "b")
    assert series.user_id == "user-a"

    with pytest.raises(ArcSeriesValidationError, match="duplicate"):
        await service.create_for_user(
            user_id="user-a",
            title="重複",
            premise="x",
            template_ids=["a", "a"],
        )

    with pytest.raises(ArcSeriesValidationError, match="not visible"):
        await service.create_for_user(
            user_id="user-b",
            title="跨使用者",
            premise="x",
            template_ids=["a", "b"],
        )


@pytest.mark.asyncio
async def test_reorder_series_updates_member_positions() -> None:
    service, _, template_repo, _ = await _service()
    for template_id in ("a", "b", "c"):
        await template_repo.save_for_user(_template(template_id), user_id="user-a")
    await service.create_for_user(
        id="series-a",
        user_id="user-a",
        title="第一季",
        premise="x",
        template_ids=["a", "b", "c"],
    )

    reordered = await service.reorder_for_user(
        "series-a",
        user_id="user-a",
        template_ids=["c", "a", "b"],
    )

    assert reordered.member_template_ids == ("c", "a", "b")
    assert [member.position for member in reordered.members] == [0, 1, 2]


@pytest.mark.asyncio
async def test_bind_series_to_character_initialises_progress() -> None:
    service, series_repo, template_repo, character_repo = await _service()
    await template_repo.save_for_user(_template("a"), user_id="user-a")
    await template_repo.save_for_user(_template("b"), user_id="user-a")
    character = _character(user_id="user-a")
    await character_repo.save(character)
    await service.create_for_user(
        id="series-a",
        user_id="user-a",
        title="第一季",
        premise="x",
        template_ids=["a", "b"],
    )

    series = await service.bind_to_character(
        character_id=character.id,
        series_id="series-a",
        user_id="user-a",
    )

    saved_character = await character_repo.get(character.id)
    progress = await series_repo.get_progress(character.id, "series-a")
    assert series is not None
    assert saved_character is not None
    assert saved_character.arc_series_id == "series-a"
    assert progress is not None
    assert progress.current_index == 0
    assert progress.last_arc_id is None


@pytest.mark.asyncio
async def test_rebinding_same_active_series_preserves_progress() -> None:
    service, series_repo, template_repo, character_repo = await _service()
    await template_repo.save_for_user(_template("a"), user_id="user-a")
    await template_repo.save_for_user(_template("b"), user_id="user-a")
    character = _character(user_id="user-a")
    await character_repo.save(character)
    await service.create_for_user(
        id="series-a",
        user_id="user-a",
        title="第一季",
        premise="x",
        template_ids=["a", "b"],
    )
    progress = CharacterSeriesProgress.start(
        character_id=character.id,
        series_id="series-a",
    ).with_started_member(index=1, arc_id="arc-two")
    await series_repo.save_progress(progress)
    await character_repo.save(replace(character, arc_series_id="series-a"))

    await service.bind_to_character(
        character_id=character.id,
        series_id="series-a",
        user_id="user-a",
    )

    saved_progress = await series_repo.get_progress(character.id, "series-a")
    assert saved_progress is not None
    assert saved_progress.current_index == 1
    assert saved_progress.last_arc_id == "arc-two"


@pytest.mark.asyncio
async def test_rebinding_concluded_series_restarts_progress() -> None:
    service, series_repo, template_repo, character_repo = await _service()
    await template_repo.save_for_user(_template("a"), user_id="user-a")
    await template_repo.save_for_user(_template("b"), user_id="user-a")
    character = _character(user_id="user-a")
    await character_repo.save(character)
    await service.create_for_user(
        id="series-a",
        user_id="user-a",
        title="第一季",
        premise="x",
        template_ids=["a", "b"],
    )
    await series_repo.save_progress(
        CharacterSeriesProgress.start(
            character_id=character.id,
            series_id="series-a",
        ).with_started_member(index=1, arc_id="arc-two").concluded()
    )
    await character_repo.save(replace(character, arc_series_id="series-a"))

    await service.bind_to_character(
        character_id=character.id,
        series_id="series-a",
        user_id="user-a",
    )

    saved_progress = await series_repo.get_progress(character.id, "series-a")
    assert saved_progress is not None
    assert saved_progress.status != SERIES_STATUS_CONCLUDED
    assert saved_progress.current_index == 0
    assert saved_progress.last_arc_id is None


@pytest.mark.asyncio
async def test_delete_series_clears_bound_characters_and_progress() -> None:
    service, series_repo, template_repo, character_repo = await _service()
    await template_repo.save_for_user(_template("a"), user_id="user-a")
    await template_repo.save_for_user(_template("b"), user_id="user-a")
    character = _character(user_id="user-a")
    await character_repo.save(character)
    await service.create_for_user(
        id="series-a",
        user_id="user-a",
        title="第一季",
        premise="x",
        template_ids=["a", "b"],
    )
    await service.bind_to_character(
        character_id=character.id,
        series_id="series-a",
        user_id="user-a",
    )

    await service.delete_for_user("series-a", user_id="user-a")

    saved_character = await character_repo.get(character.id)
    assert saved_character is not None
    assert saved_character.arc_series_id is None
    assert await series_repo.get_progress(character.id, "series-a") is None


@pytest.mark.asyncio
async def test_bind_cross_user_character_is_not_found() -> None:
    service, _, template_repo, character_repo = await _service()
    await template_repo.save_for_user(_template("a"), user_id="user-a")
    await template_repo.save_for_user(_template("b"), user_id="user-a")
    character = _character(user_id="user-b")
    await character_repo.save(character)
    await service.create_for_user(
        id="series-a",
        user_id="user-a",
        title="第一季",
        premise="x",
        template_ids=["a", "b"],
    )

    with pytest.raises(ArcSeriesNotFoundError):
        await service.bind_to_character(
            character_id=character.id,
            series_id="series-a",
            user_id="user-a",
        )
