"""「同角色同時只能有一場 open」 on both storage backends (SC1-A).

The rule cannot live in the service: two taps of 起幕 land on two api
replicas and no read-then-write closes that window. So it is a partial
unique index on the SQL side and a real check on the in-memory side, and
the *same* test body runs against both — a divergence between them would
mean self-host and hosted disagree about whether a player can be in two
scenes at once.

SQLite (in-memory, ``sqlite+aiosqlite``) stands in for PostgreSQL exactly
as it does for ``uq_story_arcs_active_character``: it supports partial
indexes with the same semantics and needs no container. Only this one
table is created — the FKs to ``characters`` / ``conversations`` are not
enforced by SQLite unless asked.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from kokoro_link.contracts.story_scene import SceneSessionConflict
from kokoro_link.domain.entities.story_scene_session import (
    SCENE_CLOSE_MANUAL,
    SCENE_CLOSE_TIMEOUT,
    SCENE_CLOSED,
    SCENE_LAYER_BEAT,
    SCENE_LAYER_SIDE_STORY,
    StorySceneSession,
)
from kokoro_link.infrastructure.persistence.engine import build_session_factory
from kokoro_link.infrastructure.persistence.models import StorySceneSessionRow
from kokoro_link.infrastructure.persistence.sa_story_scene_session_repository import (
    SAStorySceneSessionRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_story_scene_sessions import (
    InMemoryStorySceneSessionRepository,
)

pytestmark = pytest.mark.asyncio

CHARACTER = "char-1"
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture(params=["sql", "memory"])
async def repo(request):  # noqa: ANN001, ANN201
    """The same port, once per backend — behaviour must not diverge."""
    if request.param == "memory":
        yield InMemoryStorySceneSessionRepository()
        return
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync: StorySceneSessionRow.__table__.create(sync),
        )
    try:
        yield SAStorySceneSessionRepository(build_session_factory(engine))
    finally:
        await engine.dispose()


def _scene(
    *,
    character_id: str = CHARACTER,
    conversation_id: str = "conv-1",
    layer: str = SCENE_LAYER_BEAT,
    opened_at: datetime = NOW,
) -> StorySceneSession:
    return StorySceneSession.open_scene(
        character_id=character_id,
        conversation_id=conversation_id,
        source_layer=layer,
        arc_id="arc-1",
        beat_id="beat-1",
        title="頂樓的獨奏",
        location="頂樓天台",
        mood="欲言又止",
        scene_type="encounter",
        dramatic_question="她會說出口嗎？",
        operator_position="central",
        operator_note="她要向你坦白",
        opened_at=opened_at,
    )


async def test_a_second_open_scene_is_rejected(repo) -> None:  # noqa: ANN001
    winner = _scene()
    await repo.add(winner)

    with pytest.raises(SceneSessionConflict) as caught:
        await repo.add(_scene(conversation_id="conv-2"))

    assert caught.value.character_id == CHARACTER
    live = await repo.get_open_for_character(CHARACTER)
    assert live is not None and live.id == winner.id
    # the loser wrote nothing at all
    assert live.conversation_id == "conv-1"


async def test_reopening_a_closed_scene_cannot_produce_two(repo) -> None:  # noqa: ANN001
    """``save`` is fenced too, not just ``add``."""
    live = _scene()
    await repo.add(live)
    retired = _scene(conversation_id="conv-2").closed(
        reason=SCENE_CLOSE_MANUAL, at=NOW,
    )
    await repo.add(retired)

    reopened = StorySceneSession(
        id=retired.id,
        character_id=retired.character_id,
        conversation_id=retired.conversation_id,
        source_layer=retired.source_layer,
        opened_at=retired.opened_at,
    )
    with pytest.raises(SceneSessionConflict):
        await repo.save(reopened)

    stored = await repo.get(retired.id)
    assert stored.status == SCENE_CLOSED
    assert (await repo.get_open_for_character(CHARACTER)).id == live.id


async def test_closed_scenes_are_unconstrained(repo) -> None:  # noqa: ANN001
    """Played-scene history is unlimited — only the live slot is unique."""
    for index in range(4):
        scene = _scene(
            conversation_id=f"conv-{index}",
            opened_at=NOW - timedelta(days=index + 1),
        ).closed(reason=SCENE_CLOSE_TIMEOUT, at=NOW)
        await repo.add(scene)
    await repo.add(_scene())

    live = await repo.get_open_for_character(CHARACTER)
    assert live is not None and live.conversation_id == "conv-1"


async def test_closing_then_opening_again_is_allowed(repo) -> None:  # noqa: ANN001
    first = _scene()
    await repo.add(first)

    await repo.save(first.closed(reason=SCENE_CLOSE_MANUAL, at=NOW))
    second = _scene(conversation_id="conv-2")
    await repo.add(second)

    assert (await repo.get_open_for_character(CHARACTER)).id == second.id


async def test_other_characters_are_independent(repo) -> None:  # noqa: ANN001
    await repo.add(_scene(character_id="char-a"))
    await repo.add(_scene(character_id="char-b", conversation_id="conv-b"))

    assert await repo.get_open_for_character("char-a") is not None
    assert await repo.get_open_for_character("char-b") is not None


async def test_delete_frees_the_slot(repo) -> None:  # noqa: ANN001
    """The opening path's rollback: a scene that never reached the thread
    must not lock the player out of the button."""
    stuck = _scene()
    await repo.add(stuck)

    await repo.delete(stuck.id)

    assert await repo.get(stuck.id) is None
    assert await repo.get_open_for_character(CHARACTER) is None
    await repo.add(_scene(conversation_id="conv-2"))


async def test_delete_is_idempotent(repo) -> None:  # noqa: ANN001
    await repo.delete("never-existed")


async def test_every_field_round_trips(repo) -> None:  # noqa: ANN001
    scene = StorySceneSession.open_scene(
        character_id=CHARACTER,
        conversation_id="conv-1",
        source_layer=SCENE_LAYER_SIDE_STORY,
        title="沒有弧線的一夜",
        location="深夜的便利商店",
        mood="溫吞的暖",
        scene_type="quiet",
        dramatic_question="她今晚會說實話嗎？",
        operator_position="present",
        operator_note="她想找人說說話",
        opened_at=NOW,
    )
    await repo.add(scene)

    stored = await repo.get(scene.id)

    assert stored == scene
    # layer 3 carries no arc — the columns must accept that
    assert stored.arc_id is None and stored.beat_id is None


async def test_an_unjudged_player_position_round_trips_as_none(repo) -> None:  # noqa: ANN001
    """Layer 3 (side story) never sets a position — ``None`` must survive
    the round trip on both backends, not be coerced into a guess."""
    scene = StorySceneSession.open_scene(
        character_id=CHARACTER,
        conversation_id="conv-1",
        source_layer=SCENE_LAYER_SIDE_STORY,
        opened_at=NOW,
    )
    await repo.add(scene)

    stored = await repo.get(scene.id)

    assert stored.operator_position is None
    assert stored.operator_note is None


async def test_activity_and_close_transitions_persist(repo) -> None:  # noqa: ANN001
    scene = _scene()
    await repo.add(scene)
    later = NOW + timedelta(minutes=30)

    await repo.save(scene.touched(later))
    bumped = await repo.get(scene.id)
    assert bumped.last_activity_at == later

    closed_at = NOW + timedelta(hours=25)
    await repo.save(bumped.closed(reason=SCENE_CLOSE_TIMEOUT, at=closed_at))

    stored = await repo.get(scene.id)
    assert stored.status == SCENE_CLOSED
    assert stored.closed_reason == SCENE_CLOSE_TIMEOUT
    assert stored.closed_at == closed_at
    assert await repo.get_open_for_character(CHARACTER) is None


# --- count_opened_since (SC3-B daily ceiling) ------------------------------


async def test_count_opened_since_counts_closed_scenes_too(repo) -> None:  # noqa: ANN001
    """A scene the player abandoned still spent its slot (STORY_SCENE_PLAN
    §4.1 SC3-B) -- a closed row must count exactly like an open one."""
    await repo.add(_scene(conversation_id="conv-1").closed(
        reason=SCENE_CLOSE_MANUAL, at=NOW,
    ))
    await repo.add(_scene(conversation_id="conv-2"))

    assert await repo.count_opened_since([CHARACTER], since=NOW - timedelta(hours=24)) == 2


async def test_count_opened_since_excludes_scenes_before_the_cutoff(repo) -> None:  # noqa: ANN001
    older = _scene(
        conversation_id="conv-old", opened_at=NOW - timedelta(hours=25),
    ).closed(reason=SCENE_CLOSE_TIMEOUT, at=NOW)
    await repo.add(older)
    await repo.add(_scene(conversation_id="conv-new"))

    assert await repo.count_opened_since([CHARACTER], since=NOW - timedelta(hours=24)) == 1
    # A wider window picks the older one back up.
    assert await repo.count_opened_since([CHARACTER], since=NOW - timedelta(hours=26)) == 2


async def test_count_opened_since_sums_across_a_players_characters(repo) -> None:  # noqa: ANN001
    """The ceiling is per-player, not per-character (§4.1 SC3-B): a caller
    passes every character id the operator owns and gets one combined count."""
    await repo.add(_scene(character_id="char-a", conversation_id="conv-a"))
    await repo.add(_scene(character_id="char-b", conversation_id="conv-b"))
    await repo.add(_scene(character_id="char-other", conversation_id="conv-other"))

    total = await repo.count_opened_since(
        ["char-a", "char-b"], since=NOW - timedelta(hours=24),
    )
    assert total == 2


async def test_count_opened_since_empty_character_list_is_zero(repo) -> None:  # noqa: ANN001
    await repo.add(_scene())

    assert await repo.count_opened_since([], since=NOW - timedelta(hours=24)) == 0


# --- touch_activity (SC1-C in-scene turns) --------------------------------


async def test_touch_activity_moves_the_idle_clock(repo) -> None:  # noqa: ANN001
    scene = _scene()
    await repo.add(scene)
    later = NOW + timedelta(minutes=12)

    assert await repo.touch_activity(scene.id, at=later) is True
    assert (await repo.get(scene.id)).last_activity_at == later


async def test_touch_activity_never_drags_the_clock_backwards(repo) -> None:  # noqa: ANN001
    """Two replicas finishing turns out of order must not fake staleness.

    The later instant wins whichever write lands second, and the loser
    still answers ``True`` — the scene *is* live, it just did not need
    this particular stamp."""
    scene = _scene()
    await repo.add(scene)
    later = NOW + timedelta(minutes=30)
    await repo.touch_activity(scene.id, at=later)

    assert await repo.touch_activity(
        scene.id, at=NOW + timedelta(minutes=5),
    ) is True
    assert (await repo.get(scene.id)).last_activity_at == later


async def test_touch_activity_reports_a_closed_scene_as_gone(repo) -> None:  # noqa: ANN001
    """The caller's cue that the chips it was about to write are moot."""
    scene = _scene()
    await repo.add(scene)
    await repo.save(scene.closed(reason=SCENE_CLOSE_MANUAL, at=NOW))

    assert await repo.touch_activity(
        scene.id, at=NOW + timedelta(minutes=5),
    ) is False
    # …and a closed scene's clock is not rewritten by a late turn.
    assert (await repo.get(scene.id)).last_activity_at == NOW


async def test_touch_activity_on_a_missing_scene_is_false_not_an_error(repo) -> None:  # noqa: ANN001
    assert await repo.touch_activity("no-such-scene", at=NOW) is False


# ── list_stale_open (SC1-E's whole-table idle sweep) ─────────────────


async def test_list_stale_open_finds_only_open_and_only_stale(repo) -> None:  # noqa: ANN001
    cutoff = NOW + timedelta(hours=1)
    stale = _scene(character_id="stale")
    await repo.add(stale)
    fresh = _scene(character_id="fresh", conversation_id="conv-2")
    await repo.add(fresh)
    await repo.touch_activity(fresh.id, at=cutoff + timedelta(minutes=1))
    closed = _scene(character_id="closed", conversation_id="conv-3")
    await repo.add(closed)
    await repo.save(closed.closed(reason=SCENE_CLOSE_MANUAL, at=NOW))

    found = await repo.list_stale_open(idle_before=cutoff)

    assert [scene.id for scene in found] == [stale.id]


async def test_list_stale_open_returns_oldest_first(repo) -> None:  # noqa: ANN001
    """So ``limit`` cannot starve the scene that has been idle longest."""
    older = _scene(character_id="older")
    await repo.add(older)
    newer = _scene(character_id="newer", conversation_id="conv-2")
    await repo.add(newer)
    await repo.touch_activity(newer.id, at=NOW + timedelta(minutes=10))

    found = await repo.list_stale_open(idle_before=NOW + timedelta(hours=1))

    assert [scene.id for scene in found] == [older.id, newer.id]
    assert [
        scene.id
        for scene in await repo.list_stale_open(
            idle_before=NOW + timedelta(hours=1), limit=1,
        )
    ] == [older.id]


async def test_list_stale_open_with_no_budget_reads_nothing(repo) -> None:  # noqa: ANN001
    await repo.add(_scene())

    assert await repo.list_stale_open(
        idle_before=NOW + timedelta(hours=1), limit=0,
    ) == ()
