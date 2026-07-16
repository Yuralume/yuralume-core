"""C0-2 progress payload: deterministic stage/beat bookkeeping.

The generation pipeline's stages are deterministic (planning →
per-beat writing → polishing), so progress is pure bookkeeping over
the persisted story — no LLM involvement. Both the full response and
the index summary expose the same ``progress`` block so the viewer
progress bar and the bookshelf in-progress badges read one contract.
"""

from __future__ import annotations

from kokoro_link.application.dto.fusion_story import (
    FusionStoryProgressResponse,
    FusionStoryResponse,
    FusionStorySummaryResponse,
)
from kokoro_link.domain.entities.fusion_story import (
    STATUS_POLISHING,
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


def _outline(beat_count: int = 4) -> FusionOutline:
    acts = (ACT_OPENING, ACT_RISING, ACT_TURN, ACT_RESOLUTION)
    beats = [
        FusionBeatPlan.create(
            sequence=i, act=acts[i % len(acts)], title=f"幕{i}",
            hook=f"hook{i}", dramatic_question="",
            target_chars=500, focus_character_ids=("c-a", "c-b"),
        )
        for i in range(beat_count)
    ]
    return FusionOutline.create(
        title="標題", premise="前提", theme="custom", beats=beats,
    )


def _pending() -> FusionStory:
    return FusionStory.create_pending(
        character_ids=["c-a", "c-b"], prompt="提示",
    )


class TestProgressStages:
    def test_planning_has_no_beats_and_low_percent(self) -> None:
        progress = FusionStoryProgressResponse.from_domain(_pending())
        assert progress.stage == "planning"
        assert progress.beats_total == 0
        assert progress.beats_done == 0
        assert progress.percent == 5

    def test_writing_percent_tracks_completed_beats(self) -> None:
        story = _pending().with_outline(_outline())
        p0 = FusionStoryProgressResponse.from_domain(story)
        assert p0.stage == "writing"
        assert (p0.beats_total, p0.beats_done) == (4, 0)
        assert p0.percent == 10

        for beat in story.beats[:2]:
            story = story.with_beat_content(
                beat_id=beat.id, content=f"寫完 {beat.sequence}",
            )
        p2 = FusionStoryProgressResponse.from_domain(story)
        assert (p2.beats_total, p2.beats_done) == (4, 2)
        assert p2.percent == 10 + 37  # 10 + int(75 * 2/4)

    def test_polishing_and_ready(self) -> None:
        story = _pending().with_outline(_outline())
        for beat in story.beats:
            story = story.with_beat_content(
                beat_id=beat.id, content=f"寫完 {beat.sequence}",
            )
        polishing = story.with_status(STATUS_POLISHING)
        assert (
            FusionStoryProgressResponse.from_domain(polishing).percent == 90
        )
        ready = story.with_full_text("完稿。")
        p = FusionStoryProgressResponse.from_domain(ready)
        assert p.percent == 100
        assert p.beats_done == 4

    def test_failed_has_no_percent(self) -> None:
        story = _pending().with_status("failed", error_message="炸了")
        p = FusionStoryProgressResponse.from_domain(story)
        assert p.stage == "failed"
        assert p.percent is None


class TestProgressEmbedding:
    def test_full_response_and_summary_share_progress(self) -> None:
        story = _pending().with_outline(_outline())
        story = story.with_beat_content(
            beat_id=story.beats[0].id, content="寫完 0",
        )
        full = FusionStoryResponse.from_domain(story)
        summary = FusionStorySummaryResponse.from_domain(story)
        assert full.progress == summary.progress
        assert full.progress.beats_done == 1
