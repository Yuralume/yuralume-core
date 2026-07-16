from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from kokoro_link.domain.entities.arc_series import (
    SERIES_STATUS_CONCLUDED,
    ArcSeries,
    CharacterSeriesProgress,
)
from kokoro_link.domain.entities.arc_template import ArcTemplateBinding
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.persistence.sa_arc_series_repository import (
    SAArcSeriesRepository,
)
from kokoro_link.infrastructure.persistence.sa_character_repository import (
    SACharacterRepository,
)


def _series(
    series_id: str,
    *,
    title: str = "連載篇",
    template_ids: list[str] | None = None,
    user_id: str | None = "default",
) -> ArcSeries:
    return ArcSeries.create(
        id=series_id,
        title=title,
        premise="兩本劇本依序展開。",
        theme="growth",
        tone="dramatic",
        binding=ArcTemplateBinding(
            world_frames=("modern",),
            required_traits=("student",),
        ),
        template_ids=template_ids or ["book-one", "book-two"],
        user_id=user_id,
    )


def _character(*, arc_series_id: str | None = None) -> Character:
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
        user_id="default",
        arc_series_id=arc_series_id,
    )


@pytest.mark.asyncio
async def test_sa_arc_series_round_trip_and_overwrite(
    session_factory: sessionmaker,
) -> None:
    repo = SAArcSeriesRepository(session_factory)

    await repo.save_for_user(_series("series-a"), user_id="default")
    saved = await repo.get_for_user("series-a", user_id="default")
    assert saved is not None
    assert saved.member_template_ids == ("book-one", "book-two")
    assert saved.binding.world_frames == ("modern",)

    await repo.save_for_user(
        _series(
            "series-a",
            title="重排後",
            template_ids=["book-two", "book-one"],
        ),
        user_id="default",
        overwrite=True,
    )
    overwritten = await repo.get_for_user("series-a", user_id="default")
    assert overwritten is not None
    assert overwritten.title == "重排後"
    assert overwritten.member_template_ids == ("book-two", "book-one")
    assert [member.position for member in overwritten.members] == [0, 1]


@pytest.mark.asyncio
async def test_sa_arc_series_pack_visibility_and_user_collision(
    session_factory: sessionmaker,
) -> None:
    repo = SAArcSeriesRepository(session_factory)

    await repo.upsert_pack(
        _series("pack-series", user_id=None),
        pack_id="starter",
        external_id="starter/pack-series",
    )

    assert await repo.get_for_user("pack-series", user_id="default") is not None
    assert [item.id for item in await repo.list_for_user("default")] == [
        "pack-series",
    ]
    with pytest.raises(ValueError, match="reserved"):
        await repo.save_for_user(
            _series("pack-series"),
            user_id="default",
        )


@pytest.mark.asyncio
async def test_sa_character_arc_series_id_and_progress_round_trip(
    session_factory: sessionmaker,
) -> None:
    series_repo = SAArcSeriesRepository(session_factory)
    character_repo = SACharacterRepository(session_factory)
    await series_repo.save_for_user(_series("series-a"), user_id="default")
    character = _character(arc_series_id="series-a")
    await character_repo.save(character)

    saved_character = await character_repo.get(character.id)
    assert saved_character is not None
    assert saved_character.arc_series_id == "series-a"

    progress = CharacterSeriesProgress.start(
        character_id=character.id,
        series_id="series-a",
    ).with_started_member(index=1, arc_id="arc-two").concluded()
    await series_repo.save_progress(progress)

    saved_progress = await series_repo.get_progress(character.id, "series-a")
    assert saved_progress is not None
    assert saved_progress.status == SERIES_STATUS_CONCLUDED
    assert saved_progress.current_index == 1
    assert saved_progress.last_arc_id == "arc-two"

    assert await series_repo.clear_progress_for_character(character.id) == 1
    assert await series_repo.get_progress(character.id, "series-a") is None


@pytest.mark.asyncio
async def test_sa_clear_progress_for_series(
    session_factory: sessionmaker,
) -> None:
    series_repo = SAArcSeriesRepository(session_factory)
    character_repo = SACharacterRepository(session_factory)
    await series_repo.save_for_user(_series("series-a"), user_id="default")
    await series_repo.save_for_user(_series("series-b"), user_id="default")
    character = _character(arc_series_id="series-a")
    await character_repo.save(character)
    await series_repo.save_progress(
        CharacterSeriesProgress.start(
            character_id=character.id,
            series_id="series-a",
        )
    )
    await series_repo.save_progress(
        CharacterSeriesProgress.start(
            character_id=character.id,
            series_id="series-b",
        )
    )

    assert await series_repo.clear_progress_for_series("series-a") == 1

    assert await series_repo.get_progress(character.id, "series-a") is None
    assert await series_repo.get_progress(character.id, "series-b") is not None
