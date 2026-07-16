from __future__ import annotations

import pytest

from kokoro_link.domain.entities.arc_series import (
    SERIES_STATUS_ACTIVE,
    SERIES_STATUS_CONCLUDED,
    ArcSeries,
    CharacterSeriesProgress,
)


def test_arc_series_normalises_member_order_and_dedupes() -> None:
    series = ArcSeries.create(
        id=" summer-series ",
        title=" 夏日篇 ",
        premise="一段連續劇情。",
        template_ids=[" first ", "second", "first"],
        user_id="user-a",
    )

    assert series.id == "summer-series"
    assert series.title == "夏日篇"
    assert series.member_template_ids == ("first", "second")
    assert [member.position for member in series.members] == [0, 1]
    assert series.user_id == "user-a"


def test_arc_series_rejects_blank_title_or_empty_members() -> None:
    with pytest.raises(ValueError, match="title"):
        ArcSeries.create(
            id="series",
            title=" ",
            premise="x",
            template_ids=["a", "b"],
        )

    with pytest.raises(ValueError, match="at least one"):
        ArcSeries.create(
            id="series",
            title="Series",
            premise="x",
            template_ids=[],
        )


def test_character_series_progress_tracks_member_and_conclusion() -> None:
    progress = CharacterSeriesProgress.start(
        character_id="character-a",
        series_id="series-a",
    )

    started = progress.with_started_member(index=1, arc_id="arc-2")
    concluded = started.concluded()

    assert progress.status == SERIES_STATUS_ACTIVE
    assert started.current_index == 1
    assert started.last_arc_id == "arc-2"
    assert concluded.status == SERIES_STATUS_CONCLUDED
    assert concluded.current_index == 1
