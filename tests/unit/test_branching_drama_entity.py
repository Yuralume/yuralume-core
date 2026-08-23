"""Unit tests for branching drama domain entities."""

from __future__ import annotations

import pytest

from kokoro_link.domain.entities.branching_drama import (
    DEFAULT_TOTAL_SEGMENTS,
    MAX_TOTAL_SEGMENTS,
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

    def test_create_pending_accepts_max_total_segments(self):
        # BD4: the ceiling itself must still succeed — off-by-one errors
        # in a `>` vs `>=` check would reject the legal boundary.
        drama = BranchingDrama.create_pending(
            character_ids=["a", "b"],
            prompt="test",
            total_segments=MAX_TOTAL_SEGMENTS,
        )
        assert drama.total_segments == MAX_TOTAL_SEGMENTS

    def test_create_pending_rejects_over_max_total_segments(self):
        # BD4: total_segments has a hard ceiling — the tree's node count
        # is 3^N, so one turn past the cap is already unbounded fan-out.
        with pytest.raises(ValueError, match="total_segments"):
            BranchingDrama.create_pending(
                character_ids=["a", "b"],
                prompt="test",
                total_segments=MAX_TOTAL_SEGMENTS + 1,
            )

    def test_constructor_tolerates_over_max_total_segments(self):
        # FX3: the constructor is also the *read* path — every repository
        # mapper builds an entity from a row — and the create form allowed
        # 15 before BD4 lowered the cap to 12. Enforcing the ceiling here
        # would not stop those dramas, it would take the whole list down
        # with them (``list_recent`` maps before anything can filter), and
        # the delete endpoint that is the only way to remove them shares
        # that path. So a stored over-cap drama constructs.
        drama = BranchingDrama(
            id="d1",
            character_ids=("a",),
            prompt="test",
            title="t",
            total_segments=MAX_TOTAL_SEGMENTS + 3,
            status=STATUS_GENERATING_OUTLINES,
        )

        assert drama.total_segments == MAX_TOTAL_SEGMENTS + 3

    def test_constructor_still_rejects_an_unplayably_shallow_tree(self):
        # The floor is not policy: ``total_segments - 1`` is the ending
        # layer's index all through the service, so a 1-segment tree has no
        # beat to end on. No writer has ever produced one.
        with pytest.raises(ValueError, match="total_segments"):
            BranchingDrama(
                id="d1",
                character_ids=("a",),
                prompt="test",
                title="t",
                total_segments=1,
                status=STATUS_GENERATING_OUTLINES,
            )

    def test_expected_node_count(self):
        drama = BranchingDrama.create_pending(
            character_ids=["a", "b"],
            prompt="test",
            total_segments=6,
        )
        # (3^6 - 1) / 2 = 364
        assert drama.expected_node_count() == 364

    def test_initial_node_target_is_prefetch_layers_not_full_tree(self):
        drama = BranchingDrama.create_pending(
            character_ids=["a", "b"],
            prompt="test",
            total_segments=6,
        )
        # root + 3 tonal children = the two layers _generate_tree actually
        # produces synchronously (OUTLINE_PREFETCH_DEPTH=2); nowhere near
        # the 364-node full-tree total from expected_node_count().
        assert drama.initial_node_target() == 4
        assert drama.initial_node_target() < drama.expected_node_count()

    def test_initial_node_target_bounded_by_small_tree(self):
        # A 2-segment tree's full size already equals what the prefetch
        # loop would produce for a deeper tree, so the two must agree.
        drama = BranchingDrama.create_pending(
            character_ids=["a", "b"],
            prompt="test",
            total_segments=2,
        )
        assert drama.initial_node_target() == drama.expected_node_count() == 4

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


class TestCreateBranchingDramaRequestTotalSegments:
    """BD4 — the DTO's edge must agree with the entity's edge, or a
    request that clears validation still blows up inside the domain."""

    def test_accepts_max_total_segments(self):
        from kokoro_link.application.dto.branching_drama import (
            CreateBranchingDramaRequest,
        )

        request = CreateBranchingDramaRequest(
            character_ids=["a", "b"],
            prompt="test",
            total_segments=MAX_TOTAL_SEGMENTS,
        )
        assert request.total_segments == MAX_TOTAL_SEGMENTS

    def test_rejects_over_max_total_segments(self):
        import pydantic

        from kokoro_link.application.dto.branching_drama import (
            CreateBranchingDramaRequest,
        )

        with pytest.raises(pydantic.ValidationError):
            CreateBranchingDramaRequest(
                character_ids=["a", "b"],
                prompt="test",
                total_segments=MAX_TOTAL_SEGMENTS + 1,
            )
