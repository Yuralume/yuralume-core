"""The foreground-interaction anchor (NF4).

``CharacterState.last_active_at`` answers "is this player still here" for
dormancy, the idle down-shift, the freeze reaper and the feed's silence
anchor. It used to be written only at the end of a chat turn, which quietly
made "the player is here" mean "the player used the chat box" — so a player
who spends every evening in 分歧劇場 or 起幕 read as never having interacted
at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from kokoro_link.application.services.character_activity_anchor import (
    CharacterActivityAnchor,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.persistence.engine import build_session_factory
from kokoro_link.infrastructure.persistence.models import CharacterRow
from kokoro_link.infrastructure.persistence.sa_character_repository import (
    SACharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)

pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


def _character(name: str = "Yuki") -> Character:
    return Character.create(
        name=name,
        summary="",
        personality=[], interests=[], speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


async def _repo_with(*characters: Character) -> InMemoryCharacterRepository:
    repo = InMemoryCharacterRepository()
    for character in characters:
        await repo.save(character)
    return repo


async def test_touch_moves_the_anchor() -> None:
    character = _character()
    repo = await _repo_with(character)
    assert (await repo.get(character.id)).state.last_active_at is None

    await CharacterActivityAnchor(repo).touch(character, now=BASE)

    assert (await repo.get(character.id)).state.last_active_at == BASE


async def test_touch_accepts_an_id() -> None:
    character = _character()
    repo = await _repo_with(character)
    await CharacterActivityAnchor(repo).touch(character.id, now=BASE)
    assert (await repo.get(character.id)).state.last_active_at == BASE


async def test_touch_all_stamps_one_instant_on_the_whole_cast() -> None:
    a, b = _character("A"), _character("B")
    repo = await _repo_with(a, b)

    await CharacterActivityAnchor(repo).touch_all([a, b], now=BASE)

    assert (await repo.get(a.id)).state.last_active_at == BASE
    assert (await repo.get(b.id)).state.last_active_at == BASE


async def test_the_anchor_never_moves_backwards() -> None:
    """A slow paid action can start before a chat turn and finish after it;
    writing its own start instant would un-do the newer turn's anchor."""
    character = _character()
    repo = await _repo_with(character)
    anchor = CharacterActivityAnchor(repo)

    await anchor.touch(character, now=BASE)
    await anchor.touch(character, now=BASE - timedelta(hours=1))

    assert (await repo.get(character.id)).state.last_active_at == BASE


async def test_touch_does_not_write_back_stale_aggregate_state() -> None:
    """The callers hold entities loaded *before* a long model call. The touch
    is a targeted single-column update, so a concurrent write to the rest of
    the aggregate survives it."""
    character = _character()
    repo = await _repo_with(character)
    stale = character  # what the drama service is still holding

    # …meanwhile a chat turn advances the character's mood.
    moved = character.with_state(character.state.adjust(affection_delta=10))
    await repo.save(moved)

    await CharacterActivityAnchor(repo).touch(stale, now=BASE)

    stored = await repo.get(character.id)
    assert stored.state.affection == 60  # not clobbered back to 50
    assert stored.state.last_active_at == BASE


async def test_a_failing_repository_never_breaks_the_interaction() -> None:
    """The player already got (and paid for) what they pressed; bookkeeping
    that fails must be logged, not raised."""

    class _Broken:
        async def touch_last_active(self, character_id: str, now: datetime) -> bool:
            raise RuntimeError("db down")

    await CharacterActivityAnchor(_Broken()).touch("c1", now=BASE)


async def test_unknown_character_is_a_no_op() -> None:
    repo = InMemoryCharacterRepository()
    await CharacterActivityAnchor(repo).touch("nope", now=BASE)


async def test_blank_id_is_a_no_op() -> None:
    class _Recording:
        def __init__(self) -> None:
            self.calls = 0

        async def touch_last_active(self, character_id: str, now: datetime) -> bool:
            self.calls += 1
            return True

    repo = _Recording()
    await CharacterActivityAnchor(repo).touch("", now=BASE)
    assert repo.calls == 0


# -- the real conditional UPDATE (SQLite twin of the production adapter) ----- #


@pytest_asyncio.fixture()
async def sa_characters():  # noqa: ANN201
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: CharacterRow.__table__.create(sync))
    try:
        yield SACharacterRepository(build_session_factory(engine))
    finally:
        await engine.dispose()


async def test_sql_touch_sets_the_anchor(sa_characters) -> None:  # noqa: ANN001
    character = _character()
    await sa_characters.save(character)

    assert await sa_characters.touch_last_active(character.id, BASE) is True

    stored = await sa_characters.get(character.id)
    assert stored.state.last_active_at == BASE


async def test_sql_touch_is_monotonic(sa_characters) -> None:  # noqa: ANN001
    """The ``IS NULL OR < :now`` fence, proven against real SQL — a rewrite
    that drops it would let a slow action rewind a newer chat turn's anchor."""
    character = _character()
    await sa_characters.save(character)
    await sa_characters.touch_last_active(character.id, BASE)

    older = await sa_characters.touch_last_active(
        character.id, BASE - timedelta(hours=1),
    )

    assert older is False
    stored = await sa_characters.get(character.id)
    assert stored.state.last_active_at == BASE


async def test_sql_touch_writes_nothing_else(sa_characters) -> None:  # noqa: ANN001
    """Targeted: the rest of the aggregate is untouched, so this can never
    race an unrelated ``save()``."""
    character = _character()
    await sa_characters.save(character)
    moved = character.with_state(character.state.adjust(affection_delta=10))
    await sa_characters.save(moved)

    await sa_characters.touch_last_active(character.id, BASE)

    stored = await sa_characters.get(character.id)
    assert stored.state.affection == 60
    assert stored.state.last_active_at == BASE


async def test_sql_touch_reports_unknown_character(sa_characters) -> None:  # noqa: ANN001
    assert await sa_characters.touch_last_active("nope", BASE) is False
