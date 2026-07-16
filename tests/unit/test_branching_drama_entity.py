"""Unit tests for branching drama domain entities."""

from __future__ import annotations

import pytest

from kokoro_link.domain.entities.branching_drama import (
    DEFAULT_TOTAL_SEGMENTS,
    SESSION_ENDED,
    SESSION_PLAYING,
    SEGMENTS_WARNING_THRESHOLD,
    STATUS_FAILED,
    STATUS_GENERATING_OUTLINES,
    STATUS_READY,
    TONE_DARK,
    TONE_NEUTRAL,
    TONE_SUNNY,
    BranchingDrama,
    DramaNode,
    DramaSession,
    Exchange,
)


class TestBranchingDrama:
    def test_create_pending_defaults(self):
        drama = BranchingDrama.create_pending(
            character_ids=["a", "b"],
            prompt="test prompt",
        )
        assert drama.total_segments == DEFAULT_TOTAL_SEGMENTS
        assert drama.status == STATUS_GENERATING_OUTLINES
        assert drama.character_ids == ("a", "b")
        assert drama.title == "(generating…)"

    def test_create_pending_dedupes(self):
        drama = BranchingDrama.create_pending(
            character_ids=["a", "b", "a", "c"],
            prompt="test",
        )
        assert drama.character_ids == ("a", "b", "c")

    def test_create_pending_rejects_empty(self):
        with pytest.raises(ValueError):
            BranchingDrama.create_pending(
                character_ids=[], prompt="test",
            )

    def test_expected_node_count(self):
        drama = BranchingDrama.create_pending(
            character_ids=["a", "b"],
            prompt="test",
            total_segments=6,
        )
        # (3^6 - 1) / 2 = 364
        assert drama.expected_node_count() == 364

    def test_status_transitions(self):
        drama = BranchingDrama.create_pending(
            character_ids=["a", "b"], prompt="test",
        )
        assert not drama.is_terminal()
        ready = drama.with_status(STATUS_READY)
        assert ready.is_terminal()
        failed = drama.with_status(STATUS_FAILED, error_message="boom")
        assert failed.is_terminal()
        assert failed.error_message == "boom"


class TestDramaNode:
    def test_create_root(self):
        node = DramaNode.create_root(
            drama_id="d1",
            title="Opening",
            summary="The beginning",
            appearing_character_ids=("a", "b"),
        )
        assert node.depth == 0
        assert node.tone is None
        assert node.parent_node_id is None
        assert node.is_root

    def test_create_child(self):
        node = DramaNode.create_child(
            drama_id="d1",
            parent_node_id="p1",
            depth=1,
            tone=TONE_DARK,
            title="Dark path",
            summary="Things go wrong",
            appearing_character_ids=("a",),
        )
        assert node.depth == 1
        assert node.tone == TONE_DARK
        assert not node.is_root

    def test_root_cannot_have_tone(self):
        with pytest.raises(ValueError, match="tone=None"):
            DramaNode(
                id="n1", drama_id="d1", parent_node_id=None,
                depth=0, tone=TONE_DARK,
                title="t", summary="s",
                appearing_character_ids=(),
            )

    def test_non_root_must_have_tone(self):
        with pytest.raises(ValueError, match="must have a tone"):
            DramaNode(
                id="n1", drama_id="d1", parent_node_id="p1",
                depth=1, tone=None,
                title="t", summary="s",
                appearing_character_ids=(),
            )

    def test_with_image_path(self):
        node = DramaNode.create_root(
            drama_id="d1", title="t", summary="s",
            appearing_character_ids=(),
        )
        assert node.image_path is None
        updated = node.with_image_path("/img/scene.png")
        assert updated.image_path == "/img/scene.png"


class TestDramaSession:
    def test_start_session(self):
        session = DramaSession.start(
            drama_id="d1", root_node_id="n1",
        )
        assert session.status == SESSION_PLAYING
        assert session.current_node_id == "n1"
        assert len(session.turns) == 0

    def test_with_turn(self):
        session = DramaSession.start(
            drama_id="d1", root_node_id="n1",
        )
        session = session.with_turn(
            node_id="n1",
            narration="Opening scene",
        )
        assert len(session.turns) == 1
        assert session.turns[0].node_id == "n1"
        assert session.turns[0].narration == "Opening scene"

        session = session.with_turn(
            node_id="n2",
            narration="Next scene",
            player_input="go forward",
            chosen_tone=TONE_SUNNY,
        )
        assert len(session.turns) == 2
        assert session.current_node_id == "n2"

    def test_with_exchange(self):
        session = DramaSession.start(
            drama_id="d1", root_node_id="n1",
        )
        session = session.with_turn(
            node_id="n1", narration="Opening",
        )
        session = session.with_exchange(
            player_input="hello", response="hi there",
        )
        assert len(session.turns[-1].exchanges) == 1
        assert session.turns[-1].exchanges[0].player_input == "hello"
        assert session.turns[-1].exchanges[0].response == "hi there"

        session = session.with_exchange(
            player_input="how are you", response="fine",
        )
        assert len(session.turns[-1].exchanges) == 2

    def test_with_exchange_no_turns_raises(self):
        session = DramaSession.start(
            drama_id="d1", root_node_id="n1",
        )
        with pytest.raises(ValueError, match="no turns"):
            session.with_exchange(
                player_input="hello", response="hi",
            )

    def test_end_session(self):
        session = DramaSession.start(
            drama_id="d1", root_node_id="n1",
        )
        ended = session.end()
        assert ended.status == SESSION_ENDED
        assert ended.is_ended
