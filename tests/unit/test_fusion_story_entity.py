"""Unit tests for the FusionStory domain entity + outline value objects.

Locks in the version-snapshot semantics so a regression in the iterate
flow can't silently drop earlier drafts. Pure entity tests — no LLM, no
repository.
"""

from __future__ import annotations

import pytest

from kokoro_link.domain.entities.fusion_story import (
    STATUS_PLANNING,
    STATUS_READY,
    STATUS_WRITING,
    FusionStory,
)
from kokoro_link.domain.value_objects.fusion_outline import (
    ACT_OPENING,
    ACT_RESOLUTION,
    ACT_RISING,
    ACT_TURN,
    FusionBeatPlan,
    FusionOutline,
)


def _outline() -> FusionOutline:
    beats = [
        FusionBeatPlan.create(
            sequence=i, act=act, title=f"幕{i}", hook="hook" + str(i),
            target_chars=600, focus_character_ids=("a",),
        )
        for i, act in enumerate(
            (ACT_OPENING, ACT_RISING, ACT_TURN, ACT_RESOLUTION),
        )
    ]
    return FusionOutline.create(
        title="標題", premise="前提", theme="custom", beats=beats,
    )


class TestCreatePending:
    def test_dedupes_and_strips_character_ids(self) -> None:
        story = FusionStory.create_pending(
            character_ids=[" a ", "b", "a", ""],
            prompt="提示",
        )
        assert story.character_ids == ("a", "b")
        assert story.status == STATUS_PLANNING
        assert story.head_version == 1

    def test_rejects_empty_prompt(self) -> None:
        with pytest.raises(ValueError):
            FusionStory.create_pending(
                character_ids=["a", "b"], prompt="   ",
            )

    def test_rejects_empty_character_ids(self) -> None:
        with pytest.raises(ValueError):
            FusionStory.create_pending(character_ids=[], prompt="p")


class TestWithOutlineAndBeatContent:
    def test_apply_outline_creates_beat_shells(self) -> None:
        story = FusionStory.create_pending(
            character_ids=["a", "b"], prompt="p",
        ).with_outline(_outline())
        assert story.status == STATUS_WRITING
        assert len(story.beats) == 4
        assert all(beat.content == "" for beat in story.beats)
        assert story.title == "標題"
        assert story.outline is not None

    def test_with_beat_content_updates_targeted_beat(self) -> None:
        story = FusionStory.create_pending(
            character_ids=["a", "b"], prompt="p",
        ).with_outline(_outline())
        target_id = story.beats[1].id
        updated = story.with_beat_content(beat_id=target_id, content="第二幕內文")
        assert updated.beats[1].content == "第二幕內文"
        assert updated.beats[1].actual_chars == len("第二幕內文")
        # Other beats untouched.
        assert updated.beats[0].content == ""


class TestSnapshotVersion:
    def test_snapshot_appends_history_and_bumps_head(self) -> None:
        story = (
            FusionStory.create_pending(character_ids=["a", "b"], prompt="p")
            .with_outline(_outline())
            .with_full_text("first version full text")
        )
        assert story.head_version == 1
        snap = story.snapshot_version(label="iterate-1")
        assert snap.head_version == 2
        assert len(snap.versions) == 1
        v = snap.versions[0]
        assert v.version_number == 1
        assert v.full_text == "first version full text"
        assert v.iteration_label == "iterate-1"


class TestJoinedText:
    def test_falls_back_to_beat_join_when_polish_missing(self) -> None:
        story = FusionStory.create_pending(
            character_ids=["a", "b"], prompt="p",
        ).with_outline(_outline())
        story = story.with_beat_content(
            beat_id=story.beats[0].id, content="幕一"
        ).with_beat_content(
            beat_id=story.beats[1].id, content="幕二"
        )
        # No full_text yet — joined_text concatenates beats.
        assert "幕一" in story.joined_text()
        assert "幕二" in story.joined_text()
        # After polish, joined_text returns the polished string verbatim.
        polished = story.with_full_text("整稿後的文字")
        assert polished.joined_text() == "整稿後的文字"
        assert polished.status == STATUS_READY


class TestOutlineValidation:
    def test_rejects_duplicate_sequence(self) -> None:
        with pytest.raises(ValueError):
            FusionOutline.create(
                title="t", premise="p", theme="custom",
                beats=[
                    FusionBeatPlan.create(
                        sequence=0, act=ACT_OPENING, title="a", hook="h",
                        target_chars=400,
                    ),
                    FusionBeatPlan.create(
                        sequence=0, act=ACT_RISING, title="b", hook="h",
                        target_chars=400,
                    ),
                ],
            )

    def test_beat_target_chars_clamps_to_floor(self) -> None:
        # ``create`` clamps low values to the 100-char floor so a planner
        # outputting tiny targets doesn't crash the pipeline.
        plan = FusionBeatPlan.create(
            sequence=0, act=ACT_OPENING, title="t", hook="h",
            target_chars=10,
        )
        assert plan.target_chars == 100
