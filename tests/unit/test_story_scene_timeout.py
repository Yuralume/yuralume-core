"""BDD for the idle 起幕 wrap-up (SC1-E).

The sweep's whole job is to decide *whether* to close and *for whom*; the
close itself is SC1-D's shared core, already covered by
``test_story_scene_close.py``. So these tests are about the decision:

* a scene past its window closes, one still inside it does not;
* the close runs through ``StorySceneService.close_scene`` with
  ``mode=timeout`` — the shared path that carries the "never invent player
  actions" red line — and not through some second implementation;
* every way the pass can fail leaves the scene for the next one (the
  player came back mid-turn, the model died, the row was closed by
  someone else);
* a session whose character no longer exists is retired straight through
  the repository, with no wrap-up and no canon — SQLite does not enforce
  the table's ``ON DELETE CASCADE``, so those rows are real on self-host.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.chat_turn_lease import (
    ConversationBusyError,
)
from kokoro_link.application.services.story_scene_closing import SceneClosing
from kokoro_link.application.services.story_scene_timeout import (
    DEFAULT_STORY_SCENE_IDLE_TIMEOUT_SECONDS,
    StorySceneTimeoutCloser,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_scene_session import (
    SCENE_CLOSE_TIMEOUT,
    SCENE_CLOSED,
    SCENE_LAYER_BEAT,
    StorySceneSession,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_story_scene_sessions import (
    InMemoryStorySceneSessionRepository,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
WINDOW = timedelta(seconds=DEFAULT_STORY_SCENE_IDLE_TIMEOUT_SECONDS)


# ── doubles ──────────────────────────────────────────────────────────


class _SpyScenes:
    """Stands in for ``StorySceneService`` at the one seam this uses."""

    def __init__(
        self, *, raises: Exception | None = None, already_closed: bool = False,
    ) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._raises = raises
        self._already_closed = already_closed

    async def close_scene(  # noqa: ANN201
        self, character, *, session, mode, now=None,  # noqa: ANN001
    ):
        self.calls.append((character.id, session.id, mode))
        if self._raises is not None:
            raise self._raises
        return SceneClosing(
            session=session.closed(reason=mode, at=now or NOW),
            already_closed=self._already_closed,
        )


class _Characters:
    def __init__(
        self, *characters: Character, raises: Exception | None = None,
    ) -> None:
        self._by_id = {c.id: c for c in characters}
        self._raises = raises
        self.lookups: list[str] = []

    async def get(self, character_id: str) -> Character | None:
        self.lookups.append(character_id)
        if self._raises is not None:
            raise self._raises
        return self._by_id.get(character_id)


def _character(character_id: str = "c1") -> Character:
    return Character(
        id=character_id,
        name="Mio",
        summary="a violinist",
        personality=(),
        interests=(),
        speaking_style="soft",
        boundaries=(),
        aspirations=(),
        appearance="",
        world_frame="modern",
        user_id="u1",
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


def _session(
    *, character_id: str = "c1", last_activity_at: datetime,
) -> StorySceneSession:
    session = StorySceneSession.open_scene(
        character_id=character_id,
        conversation_id=f"conv-{character_id}",
        source_layer=SCENE_LAYER_BEAT,
        beat_id="b1",
        title="頂樓的那次相遇",
        opened_at=last_activity_at,
    )
    return session.touched(last_activity_at)


async def _fixture(
    *sessions: StorySceneSession,
    characters: _Characters | None = None,
    scenes: _SpyScenes | None = None,
    **kwargs,  # noqa: ANN003
) -> tuple[StorySceneTimeoutCloser, InMemoryStorySceneSessionRepository, _SpyScenes]:
    repository = InMemoryStorySceneSessionRepository()
    for session in sessions:
        await repository.add(session)
    spy = scenes or _SpyScenes()
    closer = StorySceneTimeoutCloser(
        sessions=repository,
        scenes=spy,
        characters=characters or _Characters(_character()),
        **kwargs,
    )
    return closer, repository, spy


# ── the decision ─────────────────────────────────────────────────────


async def test_scene_idle_past_the_window_is_closed_as_a_timeout() -> None:
    stale = _session(last_activity_at=NOW - WINDOW - timedelta(minutes=1))
    closer, _, scenes = await _fixture(stale)

    assert await closer.close_if_idle(_character(), now=NOW) is True
    # Through the SHARED close, with the timeout vocabulary — the red line
    # about never inventing player actions lives on the other side of it.
    assert scenes.calls == [("c1", stale.id, SCENE_CLOSE_TIMEOUT)]


async def test_scene_still_inside_the_window_is_left_alone() -> None:
    fresh = _session(last_activity_at=NOW - WINDOW + timedelta(minutes=1))
    closer, _, scenes = await _fixture(fresh)

    assert await closer.close_if_idle(_character(), now=NOW) is False
    assert scenes.calls == []


async def test_character_with_no_live_scene_costs_one_read() -> None:
    closer, _, scenes = await _fixture()

    assert await closer.close_if_idle(_character(), now=NOW) is False
    assert scenes.calls == []


async def test_window_is_configurable() -> None:
    idle = _session(last_activity_at=NOW - timedelta(hours=2))
    closer, _, scenes = await _fixture(idle, idle_timeout_seconds=3600)

    assert closer.idle_timeout == timedelta(hours=1)
    assert await closer.close_if_idle(_character(), now=NOW) is True
    assert scenes.calls


async def test_a_non_positive_window_is_clamped_not_obeyed() -> None:
    """A zero window would close the scene the player is reading."""
    fresh = _session(last_activity_at=NOW)
    closer, _, scenes = await _fixture(fresh, idle_timeout_seconds=0)

    assert closer.idle_timeout >= timedelta(minutes=1)
    assert await closer.close_if_idle(_character(), now=NOW) is False
    assert scenes.calls == []


# ── failure directions ───────────────────────────────────────────────


async def test_a_busy_conversation_leaves_the_scene_for_the_next_pass() -> None:
    """The player came back mid-turn — the best reason not to end a scene."""
    stale = _session(last_activity_at=NOW - WINDOW - timedelta(minutes=1))
    scenes = _SpyScenes(raises=ConversationBusyError("busy"))
    closer, repository, _ = await _fixture(stale, scenes=scenes)

    assert await closer.close_if_idle(_character(), now=NOW) is False
    live = await repository.get_open_for_character("c1")
    assert live is not None and live.is_open


async def test_a_crashing_close_never_escapes_into_the_tick() -> None:
    stale = _session(last_activity_at=NOW - WINDOW - timedelta(minutes=1))
    scenes = _SpyScenes(raises=RuntimeError("model down"))
    closer, repository, _ = await _fixture(stale, scenes=scenes)

    assert await closer.close_if_idle(_character(), now=NOW) is False
    assert (await repository.get_open_for_character("c1")) is not None


async def test_losing_the_close_race_is_a_quiet_no() -> None:
    stale = _session(last_activity_at=NOW - WINDOW - timedelta(minutes=1))
    scenes = _SpyScenes(already_closed=True)
    closer, _, _ = await _fixture(stale, scenes=scenes)

    assert await closer.close_if_idle(_character(), now=NOW) is False


async def test_closing_twice_is_idempotent() -> None:
    """The second pass finds nothing open and does not re-close it."""
    stale = _session(last_activity_at=NOW - WINDOW - timedelta(minutes=1))
    repository = InMemoryStorySceneSessionRepository()
    await repository.add(stale)
    scenes = _SpyScenes()

    class _RealClose(_SpyScenes):
        async def close_scene(self, character, *, session, mode, now=None):  # noqa: ANN001, ANN201
            self.calls.append((character.id, session.id, mode))
            closed = await repository.close(
                session.id, reason=mode, at=now or NOW,
            )
            return SceneClosing(
                session=closed or session,
                already_closed=closed is None,
            )

    real = _RealClose()
    closer = StorySceneTimeoutCloser(
        sessions=repository, scenes=real, characters=_Characters(_character()),
    )

    assert await closer.close_if_idle(_character(), now=NOW) is True
    assert await closer.close_if_idle(_character(), now=NOW) is False
    assert len(real.calls) == 1
    assert scenes.calls == []
    stored = await repository.get(stale.id)
    assert stored is not None
    assert stored.status == SCENE_CLOSED
    assert stored.closed_reason == SCENE_CLOSE_TIMEOUT


# ── the chain's explicit due time ────────────────────────────────────


async def test_next_timeout_at_is_the_scenes_own_deadline() -> None:
    live = _session(last_activity_at=NOW - timedelta(hours=1))
    closer, _, _ = await _fixture(live)

    assert await closer.next_timeout_at(_character(), NOW) == (
        NOW - timedelta(hours=1) + WINDOW
    )


async def test_no_scene_means_no_explicit_due() -> None:
    closer, _, _ = await _fixture()

    assert await closer.next_timeout_at(_character(), NOW) is None


async def test_an_elapsed_deadline_falls_back_to_the_base_cadence() -> None:
    """An instant already in the past says nothing about when to retry —
    and answering with it would mint a next-due inside the window the
    current occurrence already owns."""
    stale = _session(last_activity_at=NOW - WINDOW - timedelta(hours=3))
    closer, _, _ = await _fixture(stale)

    assert await closer.next_timeout_at(_character(), NOW) is None


# ── rows no chain covers ─────────────────────────────────────────────


async def test_a_session_whose_character_is_gone_is_retired_silently() -> None:
    orphan = _session(
        character_id="deleted",
        last_activity_at=NOW - WINDOW - timedelta(minutes=1),
    )
    closer, repository, scenes = await _fixture(
        orphan, characters=_Characters(),  # nobody
    )

    assert await closer.sweep_unowned(now=NOW) == 1
    # No wrap-up writer, no canon: canon is what a character remembers and
    # this one no longer exists.
    assert scenes.calls == []
    stored = await repository.get(orphan.id)
    assert stored is not None
    assert stored.status == SCENE_CLOSED
    assert stored.closed_reason == SCENE_CLOSE_TIMEOUT


async def test_the_sweep_leaves_owned_sessions_to_their_own_chain() -> None:
    """Closing them here too would race the wrap-up that writes canon."""
    owned = _session(last_activity_at=NOW - WINDOW - timedelta(minutes=1))
    closer, repository, scenes = await _fixture(owned)

    assert await closer.sweep_unowned(now=NOW) == 0
    assert scenes.calls == []
    assert (await repository.get_open_for_character("c1")) is not None


async def test_the_sweep_ignores_sessions_still_inside_the_window() -> None:
    fresh = _session(
        character_id="deleted", last_activity_at=NOW - timedelta(hours=1),
    )
    closer, repository, _ = await _fixture(fresh, characters=_Characters())

    assert await closer.sweep_unowned(now=NOW) == 0
    assert (await repository.get_open_for_character("deleted")) is not None


async def test_an_unreadable_character_is_not_treated_as_a_deleted_one() -> None:
    """One flaky query must not retire live scenes without narrating them."""
    stale = _session(last_activity_at=NOW - WINDOW - timedelta(minutes=1))
    characters = _Characters(raises=RuntimeError("db down"))
    closer, repository, scenes = await _fixture(stale, characters=characters)

    assert await closer.sweep_unowned(now=NOW) == 0
    # Same rule on the per-character path, reached with no character in hand.
    assert await closer._close(stale, character=None, now=NOW) is False  # noqa: SLF001
    assert scenes.calls == []
    assert (await repository.get_open_for_character("c1")) is not None


async def test_the_sweep_is_bounded_and_drains_oldest_first() -> None:
    older = _session(
        character_id="gone-a",
        last_activity_at=NOW - WINDOW - timedelta(hours=5),
    )
    newer = _session(
        character_id="gone-b",
        last_activity_at=NOW - WINDOW - timedelta(hours=1),
    )
    closer, repository, _ = await _fixture(
        newer, older, characters=_Characters(),
    )

    assert await closer.sweep_unowned(now=NOW, limit=1) == 1
    assert (await repository.get(older.id)).status == SCENE_CLOSED
    assert (await repository.get(newer.id)).status != SCENE_CLOSED
