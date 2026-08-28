"""DIGEST_OFFPATH — the material digest is budgeted after the turn, in a table.

Four claims are pinned here:

* the store round-trips a digest losslessly, on SQLite via the very
  repository production runs on PostgreSQL;
* the precomputer's read rules — hit, miss, tolerance mismatch, **max-age**
  — and the "a failed recompute deletes the row" bound that keeps
  staleness at one turn rather than at "however long it stays broken";
* the handoff crosses a process boundary: a service that only writes and a
  service that only reads, sharing nothing but the store, still hand the
  digest over (this is the whole reason it is a table);
* the chat path reads and **never** computes, the post-turn budgets after
  its extraction and after its writes, and the turn undo deletes what a
  reversed turn budgeted.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.application.services.material_digest_precompute import (
    MaterialDigestPrecomputer,
    digest_operator_id,
)
from kokoro_link.application.services.turn_undo.result import UndoTally
from kokoro_link.application.services.turn_undo.step import UndoContext
from kokoro_link.application.services.turn_undo.steps import (
    MaterialDigestCacheInvalidateStep,
)
from kokoro_link.contracts.post_turn import PostTurnResult
from kokoro_link.contracts.prompt_material_digest import (
    PromptMaterialDigest,
    StoredPromptMaterialDigest,
)
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.persistence.engine import build_session_factory
from kokoro_link.infrastructure.persistence.models import (
    PromptMaterialDigestRow,
)
from kokoro_link.infrastructure.persistence.sa_prompt_material_digest_repository import (
    SAPromptMaterialDigestRepository,
    decode_digest,
    encode_digest,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_prompt_material_digests import (
    InMemoryPromptMaterialDigestRepository,
)
from kokoro_link.infrastructure.state.simple import SimpleStateEngine

_NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
_TODAY = date(2026, 8, 27)
_TIMEOUT = 5.0


def _digest(*bullets: str, **metadata) -> PromptMaterialDigest:  # noqa: ANN003
    return PromptMaterialDigest(
        bullets=tuple(bullets), digest_metadata=dict(metadata),
    )


def _character(character_id: str = "char-1", user_id: str = "user-1"):
    return SimpleNamespace(id=character_id, user_id=user_id, name="Mio")


def _operator(operator_id: str = "user-1"):
    return SimpleNamespace(id=operator_id, primary_language="zh-TW")


class _StubLoaders:
    """A ``MaterialDigestLoaders`` stand-in that records what it was asked."""

    def __init__(
        self,
        *,
        digest: PromptMaterialDigest | None = None,
        raises: Exception | None = None,
        story_events: list | None = None,
    ) -> None:
        self.digest = digest
        self.raises = raises
        self._story_events = story_events or []
        self.calls: list[str] = []
        self.seen: dict = {}

    async def _load_recent_emotion_events(self, *, character_id, operator, now):  # noqa: ANN001
        self.calls.append("emotion_events")
        self.seen["now"] = now
        return ["emotion-event"]

    async def _load_self_reflections(self, *, character_id, operator):  # noqa: ANN001
        self.calls.append("self_reflections")
        return ["reflection"]

    async def _load_recent_feed_posts(self, character_id):  # noqa: ANN001
        self.calls.append("feed_posts")
        return ("feed-post",)

    async def _load_material_digest_story_inputs(self, *, character, today):  # noqa: ANN001
        self.calls.append("story_inputs")
        self.seen["today"] = today
        return list(self._story_events), None, []

    async def _load_prompt_material_digest(self, **kwargs):  # noqa: ANN003
        self.calls.append("digest")
        self.seen["digest_kwargs"] = kwargs
        if self.raises is not None:
            raise self.raises
        return self.digest


# --------------------------------------------------------------------------
# The store — the same repository production runs, on SQLite
# --------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def sa_store():  # noqa: ANN201
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync: PromptMaterialDigestRow.__table__.create(sync),
        )
    try:
        yield SAPromptMaterialDigestRepository(build_session_factory(engine))
    finally:
        await engine.dispose()


def _stored(
    digest: PromptMaterialDigest,
    *,
    character_id: str = "char-1",
    operator_id: str = "user-1",
    content_tolerance: str = "frontier",
    updated_at: datetime = _NOW,
) -> StoredPromptMaterialDigest:
    return StoredPromptMaterialDigest(
        character_id=character_id,
        operator_id=operator_id,
        content_tolerance=content_tolerance,
        digest=digest,
        updated_at=updated_at,
    )


@pytest.mark.asyncio
async def test_sa_store_round_trips_bullets_and_metadata(sa_store) -> None:  # noqa: ANN001
    digest = _digest("今天的重點", "她提到了雨", provider_id="unit", bullet_count=2)

    await sa_store.upsert(_stored(digest))
    read = await sa_store.get(character_id="char-1", operator_id="user-1")

    assert read is not None
    assert read.digest.bullets == ("今天的重點", "她提到了雨")
    assert read.digest.digest_metadata["provider_id"] == "unit"
    assert read.content_tolerance == "frontier"
    assert read.updated_at == _NOW


@pytest.mark.asyncio
async def test_sa_store_upsert_replaces_rather_than_duplicates(sa_store) -> None:  # noqa: ANN001
    await sa_store.upsert(_stored(_digest("舊的")))
    await sa_store.upsert(
        _stored(
            _digest("新的"),
            content_tolerance="community",
            updated_at=_NOW + timedelta(minutes=5),
        ),
    )

    read = await sa_store.get(character_id="char-1", operator_id="user-1")

    assert read is not None
    assert read.digest.bullets == ("新的",)
    assert read.content_tolerance == "community"
    assert read.updated_at == _NOW + timedelta(minutes=5)
    # One row, not two: the pair is the primary key.
    assert await sa_store.delete(character_id="char-1") == 1


@pytest.mark.asyncio
async def test_sa_store_deletes_one_pair_or_every_operator(sa_store) -> None:  # noqa: ANN001
    await sa_store.upsert(_stored(_digest("a"), operator_id="user-1"))
    await sa_store.upsert(_stored(_digest("b"), operator_id="user-2"))
    await sa_store.upsert(
        _stored(_digest("c"), character_id="char-2", operator_id="user-1"),
    )

    assert await sa_store.delete(
        character_id="char-1", operator_id="user-1",
    ) == 1
    assert await sa_store.get(
        character_id="char-1", operator_id="user-2",
    ) is not None

    assert await sa_store.delete(character_id="char-1") == 1
    assert await sa_store.get(
        character_id="char-1", operator_id="user-2",
    ) is None
    # A sibling character is untouched by either delete.
    assert await sa_store.get(
        character_id="char-2", operator_id="user-1",
    ) is not None


@pytest.mark.asyncio
async def test_sa_store_reads_an_absent_pair_as_none(sa_store) -> None:  # noqa: ANN001
    assert await sa_store.get(
        character_id="char-1", operator_id="user-1",
    ) is None
    assert await sa_store.delete(character_id="char-1") == 0


def test_an_undecodable_payload_reads_as_absent() -> None:
    """A corrupt row is a miss, never a dead turn."""
    assert decode_digest("not json at all") is None
    assert decode_digest("") is None
    assert decode_digest('{"bullets": []}') is None
    assert decode_digest('{"bullets": "not a list"}') is None
    assert decode_digest(encode_digest(_digest("好"))) == _digest("好")


# --------------------------------------------------------------------------
# The precomputer's read rules
# --------------------------------------------------------------------------


def _precomputer(store=None):  # noqa: ANN001
    return MaterialDigestPrecomputer(
        store or InMemoryPromptMaterialDigestRepository(),
    )


@pytest.mark.asyncio
async def test_cold_store_is_a_miss() -> None:
    cache = _precomputer()

    assert await cache.cached(
        character_id="char-1",
        operator_id="user-1",
        content_tolerance="frontier",
        now=_NOW,
    ) is None


@pytest.mark.asyncio
async def test_recompute_makes_the_next_read_a_hit() -> None:
    cache = _precomputer()
    loaders = _StubLoaders(digest=_digest("今天的重點"))

    produced = await asyncio.wait_for(
        cache.recompute(
            loaders,
            character=_character(),
            operator=_operator(),
            content_tolerance="frontier",
            now=_NOW,
            today=_TODAY,
        ),
        timeout=_TIMEOUT,
    )

    assert produced is loaders.digest
    assert await cache.cached(
        character_id="char-1",
        operator_id="user-1",
        content_tolerance="frontier",
        now=_NOW,
    ) == loaders.digest
    # The inputs are re-read, not carried over from the turn.
    assert set(loaders.calls) == {
        "emotion_events", "self_reflections", "feed_posts", "story_inputs", "digest",
    }
    assert loaders.seen["now"] == _NOW
    assert loaders.seen["today"] == _TODAY
    assert loaders.seen["digest_kwargs"]["emotion_events"] == ["emotion-event"]
    assert loaders.seen["digest_kwargs"]["self_reflections"] == ["reflection"]
    assert loaders.seen["digest_kwargs"]["recent_feed_posts"] == ("feed-post",)


@pytest.mark.asyncio
async def test_a_different_content_tolerance_is_a_miss_not_a_stale_hit() -> None:
    """An NSFW-mode digest must not be fed to a normal-mode prompt."""
    cache = _precomputer()
    loaders = _StubLoaders(digest=_digest("露骨的重點"))

    await cache.recompute(
        loaders,
        character=_character(),
        operator=_operator(),
        content_tolerance="community",
        now=_NOW,
    )

    assert await cache.cached(
        character_id="char-1",
        operator_id="user-1",
        content_tolerance="community",
        now=_NOW,
    ) == loaders.digest
    assert await cache.cached(
        character_id="char-1",
        operator_id="user-1",
        content_tolerance="frontier",
        now=_NOW,
    ) is None


@pytest.mark.asyncio
async def test_a_row_past_the_max_age_is_a_miss() -> None:
    """A row never expires on its own; the reader is what bounds its age.

    The player who comes back after a month must get the source blocks,
    not a month-old summary of "recent" material rendered as current.
    """
    cache = _precomputer()
    await cache.recompute(
        _StubLoaders(digest=_digest("一個月前的重點")),
        character=_character(),
        operator=_operator(),
        content_tolerance="frontier",
        now=_NOW,
    )

    def _read(at: datetime):  # noqa: ANN202
        return cache.cached(
            character_id="char-1",
            operator_id="user-1",
            content_tolerance="frontier",
            now=at,
        )

    assert await _read(_NOW + timedelta(hours=23, minutes=59)) is not None
    assert await _read(_NOW + timedelta(hours=24, minutes=1)) is None
    assert await _read(_NOW + timedelta(days=30)) is None


@pytest.mark.asyncio
async def test_a_clock_that_went_backwards_is_not_treated_as_expiry() -> None:
    cache = _precomputer()
    await cache.recompute(
        _StubLoaders(digest=_digest("剛蒸好")),
        character=_character(),
        operator=_operator(),
        content_tolerance="frontier",
        now=_NOW,
    )

    assert await cache.cached(
        character_id="char-1",
        operator_id="user-1",
        content_tolerance="frontier",
        now=_NOW - timedelta(minutes=5),
    ) is not None


@pytest.mark.asyncio
async def test_entries_are_isolated_per_character_and_operator() -> None:
    cache = _precomputer()
    mine = _StubLoaders(digest=_digest("我的"))
    theirs = _StubLoaders(digest=_digest("他的"))

    await cache.recompute(
        mine,
        character=_character("char-1", "user-1"),
        operator=_operator("user-1"),
        content_tolerance="frontier",
        now=_NOW,
    )
    await cache.recompute(
        theirs,
        character=_character("char-1", "user-2"),
        operator=_operator("user-2"),
        content_tolerance="frontier",
        now=_NOW,
    )

    async def _read(operator_id: str, character_id: str = "char-1"):
        return await cache.cached(
            character_id=character_id,
            operator_id=operator_id,
            content_tolerance="frontier",
            now=_NOW,
        )

    assert await _read("user-1") == mine.digest
    assert await _read("user-2") == theirs.digest
    assert await _read("user-1", character_id="char-2") is None


@pytest.mark.asyncio
async def test_invalidate_forgets_every_operator_of_one_character() -> None:
    cache = _precomputer()
    for operator_id in ("user-1", "user-2"):
        await cache.recompute(
            _StubLoaders(digest=_digest(operator_id)),
            character=_character("char-1", operator_id),
            operator=_operator(operator_id),
            content_tolerance="frontier",
            now=_NOW,
        )
    await cache.recompute(
        _StubLoaders(digest=_digest("別的角色")),
        character=_character("char-2", "user-1"),
        operator=_operator("user-1"),
        content_tolerance="frontier",
        now=_NOW,
    )

    assert await cache.invalidate("char-1") == 2

    async def _read(character_id: str, operator_id: str):
        return await cache.cached(
            character_id=character_id,
            operator_id=operator_id,
            content_tolerance="frontier",
            now=_NOW,
        )

    assert await _read("char-1", "user-1") is None
    assert await _read("char-1", "user-2") is None
    # Untouched: the undo reversed a turn of one character only.
    assert await _read("char-2", "user-1") is not None


@pytest.mark.asyncio
async def test_a_raising_recompute_never_escapes_the_post_turn() -> None:
    cache = _precomputer()
    loaders = _StubLoaders(raises=RuntimeError("digester exploded"))

    produced = await asyncio.wait_for(
        cache.recompute(
            loaders,
            character=_character(),
            operator=_operator(),
            content_tolerance="frontier",
            now=_NOW,
        ),
        timeout=_TIMEOUT,
    )

    assert produced is None
    assert await cache.cached(
        character_id="char-1",
        operator_id="user-1",
        content_tolerance="frontier",
        now=_NOW,
    ) is None


@pytest.mark.asyncio
async def test_a_failed_recompute_deletes_the_row_it_could_not_refresh() -> None:
    """Staleness is bounded at one turn, not "however long it breaks"."""
    cache = _precomputer()
    good = _StubLoaders(digest=_digest("上一輪的重點"))
    await cache.recompute(
        good,
        character=_character(),
        operator=_operator(),
        content_tolerance="frontier",
        now=_NOW,
    )

    async def _read():
        return await cache.cached(
            character_id="char-1",
            operator_id="user-1",
            content_tolerance="frontier",
            now=_NOW,
        )

    assert await _read() == good.digest

    await cache.recompute(
        _StubLoaders(digest=None),
        character=_character(),
        operator=_operator(),
        content_tolerance="frontier",
        now=_NOW,
    )

    assert await _read() is None


# --------------------------------------------------------------------------
# F1 — undo landing while the digester is in flight
# --------------------------------------------------------------------------


class _GateStub:
    """``UndoneTurnGate``'s reading half, flipped by the test."""

    def __init__(self, undone: bool = False) -> None:
        self.undone = undone
        self.asked: list[str | None] = []

    async def is_undone(self, turn_record_id):  # noqa: ANN001
        self.asked.append(turn_record_id)
        return self.undone


class _BlockingLoaders(_StubLoaders):
    """Parks inside the digester call so a test can interleave an undo.

    This is the window the bug lives in: between the post-turn's second
    undo gate and the row landing there are two or three upstream round
    trips, and the digester's own call is the last of them.
    """

    def __init__(self, *, digest: PromptMaterialDigest, released) -> None:  # noqa: ANN001
        super().__init__(digest=digest)
        self.entered = asyncio.Event()
        self._released = released

    async def _load_prompt_material_digest(self, **kwargs):  # noqa: ANN003
        self.entered.set()
        await self._released.wait()
        return await super()._load_prompt_material_digest(**kwargs)


@pytest.mark.asyncio
async def test_an_undo_during_the_digester_call_does_not_survive_the_write(
    sa_store,  # noqa: ANN001
) -> None:
    """The race F1 names, run on the production store.

    The undo — tombstone *and* its real invalidate step — completes while
    the digester is still talking to the upstream. Without the post-write
    re-ask, the late ``upsert`` puts the reversed turn's material back and
    the next prompt reads it.
    """
    cache = _precomputer(sa_store)
    gate = _GateStub()
    released = asyncio.Event()
    loaders = _BlockingLoaders(digest=_digest("被撤回那輪的素材"), released=released)

    budget = asyncio.create_task(
        cache.recompute(
            loaders,
            character=_character(),
            operator=_operator(),
            content_tolerance="frontier",
            now=_NOW,
            turn_record_id="turn-1",
            undone_turn_gate=gate,
        ),
    )
    await asyncio.wait_for(loaders.entered.wait(), timeout=_TIMEOUT)

    # The undo runs to completion inside the window: tombstone first
    # (which is what the registry order guarantees), then the step that
    # drops the row.
    gate.undone = True
    await MaterialDigestCacheInvalidateStep().apply(
        UndoContext(
            journal=SimpleNamespace(
                character_id="char-1", conversation_id="conv-1",
            ),
            deps=SimpleNamespace(material_digest_cache=cache),
            now=_NOW,
        ),
        UndoTally(),
    )

    released.set()
    await asyncio.wait_for(budget, timeout=_TIMEOUT)

    assert gate.asked == ["turn-1"]
    assert await sa_store.get(
        character_id="char-1", operator_id="user-1",
    ) is None


@pytest.mark.asyncio
async def test_a_turn_that_was_not_undone_keeps_its_budget() -> None:
    """The re-ask must not cost every healthy turn its digest."""
    cache = _precomputer()
    gate = _GateStub(undone=False)
    loaders = _StubLoaders(digest=_digest("正常那輪"))

    await cache.recompute(
        loaders,
        character=_character(),
        operator=_operator(),
        content_tolerance="frontier",
        now=_NOW,
        turn_record_id="turn-1",
        undone_turn_gate=gate,
    )

    assert gate.asked == ["turn-1"]
    assert await cache.cached(
        character_id="char-1",
        operator_id="user-1",
        content_tolerance="frontier",
        now=_NOW,
    ) == loaders.digest


@pytest.mark.asyncio
async def test_a_gate_that_raises_withdraws_rather_than_serves() -> None:
    """Unknown undo state resolves toward the player's retraction."""

    class _BrokenGate:
        async def is_undone(self, turn_record_id):  # noqa: ANN001
            raise RuntimeError("undone-turn lookup down")

    cache = _precomputer()
    await cache.recompute(
        _StubLoaders(digest=_digest("不確定狀態")),
        character=_character(),
        operator=_operator(),
        content_tolerance="frontier",
        now=_NOW,
        turn_record_id="turn-1",
        undone_turn_gate=_BrokenGate(),
    )

    assert await cache.cached(
        character_id="char-1",
        operator_id="user-1",
        content_tolerance="frontier",
        now=_NOW,
    ) is None


@pytest.mark.asyncio
async def test_the_withdrawal_cannot_take_out_a_newer_digest() -> None:
    """A late withdrawal is bounded by its own stamp, like every write."""
    cache = _precomputer()
    newer = _digest("後來那輪")
    await cache.recompute(
        _StubLoaders(digest=newer),
        character=_character(),
        operator=_operator(),
        content_tolerance="frontier",
        now=_NOW + timedelta(minutes=5),
    )

    # The undone turn read its material five minutes earlier, so its
    # upsert is refused outright — and its withdrawal must not then delete
    # the row that beat it.
    await cache.recompute(
        _StubLoaders(digest=_digest("被撤回那輪")),
        character=_character(),
        operator=_operator(),
        content_tolerance="frontier",
        now=_NOW,
        turn_record_id="turn-1",
        undone_turn_gate=_GateStub(undone=True),
    )

    assert await cache.cached(
        character_id="char-1",
        operator_id="user-1",
        content_tolerance="frontier",
        now=_NOW + timedelta(minutes=5),
    ) == newer


# --------------------------------------------------------------------------
# F2 — two post-turns for one character, running at once
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("backed_by_sql", [False, True])
async def test_a_slower_older_read_cannot_overwrite_a_newer_one(
    backed_by_sql, sa_store,  # noqa: ANN001
) -> None:
    """``updated_at`` is the version, and the newer read wins.

    Two post-turn jobs really can run for one character at once, and the
    one that started earlier can finish later while holding the *older*
    material. Asserted against both store implementations so neither can
    drift from the rule.
    """
    cache = _precomputer(sa_store if backed_by_sql else None)
    fresh = _digest("較新的素材")
    stale = _digest("較舊的素材")

    await cache.recompute(
        _StubLoaders(digest=fresh),
        character=_character(),
        operator=_operator(),
        content_tolerance="frontier",
        now=_NOW,
    )
    produced = await cache.recompute(
        _StubLoaders(digest=stale),
        character=_character(),
        operator=_operator(),
        content_tolerance="frontier",
        now=_NOW - timedelta(minutes=1),
    )

    assert produced is None, "a refused write must not report success"
    assert await cache.cached(
        character_id="char-1",
        operator_id="user-1",
        content_tolerance="frontier",
        now=_NOW,
    ) == fresh


@pytest.mark.asyncio
@pytest.mark.parametrize("backed_by_sql", [False, True])
async def test_a_late_empty_recompute_cannot_delete_a_newer_digest(
    backed_by_sql, sa_store,  # noqa: ANN001
) -> None:
    """The delete branch is version-bounded too.

    Arriving late with nothing to say is not a licence to clear the row a
    fresher read just produced — that turned a slow sibling into a wipe.
    """
    cache = _precomputer(sa_store if backed_by_sql else None)
    fresh = _digest("較新的素材")
    await cache.recompute(
        _StubLoaders(digest=fresh),
        character=_character(),
        operator=_operator(),
        content_tolerance="frontier",
        now=_NOW,
    )

    await cache.recompute(
        _StubLoaders(digest=None),
        character=_character(),
        operator=_operator(),
        content_tolerance="frontier",
        now=_NOW - timedelta(minutes=1),
    )

    assert await cache.cached(
        character_id="char-1",
        operator_id="user-1",
        content_tolerance="frontier",
        now=_NOW,
    ) == fresh


@pytest.mark.asyncio
async def test_an_empty_recompute_still_clears_its_own_generation() -> None:
    """The version bound must not disarm the staleness bound (F2 vs the
    "stale by one turn" promise): a digester that goes quiet still drops
    the row *it* is responsible for."""
    cache = _precomputer()
    await cache.recompute(
        _StubLoaders(digest=_digest("上一輪")),
        character=_character(),
        operator=_operator(),
        content_tolerance="frontier",
        now=_NOW,
    )

    await cache.recompute(
        _StubLoaders(digest=None),
        character=_character(),
        operator=_operator(),
        content_tolerance="frontier",
        now=_NOW + timedelta(minutes=1),
    )

    assert await cache.cached(
        character_id="char-1",
        operator_id="user-1",
        content_tolerance="frontier",
        now=_NOW + timedelta(minutes=1),
    ) is None


@pytest.mark.asyncio
async def test_undo_deletes_regardless_of_version(sa_store) -> None:  # noqa: ANN001
    """The version bound is for writers withdrawing their own work.

    An undo is not a writer: a reversed turn's material has to go whoever
    wrote last, so ``invalidate`` passes no ceiling.
    """
    cache = _precomputer(sa_store)
    await cache.recompute(
        _StubLoaders(digest=_digest("很新的素材")),
        character=_character(),
        operator=_operator(),
        content_tolerance="frontier",
        now=_NOW + timedelta(days=1),
    )

    assert await cache.invalidate("char-1") == 1
    assert await sa_store.get(
        character_id="char-1", operator_id="user-1",
    ) is None


class _BrokenStore:
    """Every method raises — the store is down."""

    async def get(self, **kwargs):  # noqa: ANN003
        raise RuntimeError("db down")

    async def upsert(self, stored):  # noqa: ANN001
        raise RuntimeError("db down")

    async def delete(self, **kwargs):  # noqa: ANN003
        raise RuntimeError("db down")


@pytest.mark.asyncio
async def test_a_broken_store_degrades_to_source_blocks() -> None:
    """A store outage costs the digest and nothing else — on all three paths."""
    cache = _precomputer(_BrokenStore())

    assert await cache.cached(
        character_id="char-1",
        operator_id="user-1",
        content_tolerance="frontier",
        now=_NOW,
    ) is None
    assert await cache.recompute(
        _StubLoaders(digest=_digest("寫不進去")),
        character=_character(),
        operator=_operator(),
        content_tolerance="frontier",
        now=_NOW,
    ) is None
    assert await cache.invalidate("char-1") == 0


def test_operator_id_falls_back_to_the_character_owner() -> None:
    assert digest_operator_id(_character(), _operator("user-9")) == "user-9"
    assert digest_operator_id(_character(user_id="owner-1"), None) == "owner-1"
    assert digest_operator_id(
        SimpleNamespace(id="char-1"), None,
    ) == DEFAULT_OPERATOR_ID


# --------------------------------------------------------------------------
# The undo interlock
# --------------------------------------------------------------------------


class _RecordingInvalidator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def invalidate(
        self, character_id: str, operator_id: str | None = None,
    ) -> int:
        self.calls.append((character_id, operator_id))
        return 1


@pytest.mark.asyncio
async def test_undo_forgets_what_the_reversed_turn_budgeted() -> None:
    invalidator = _RecordingInvalidator()
    context = UndoContext(
        journal=SimpleNamespace(character_id="char-1", conversation_id="conv-1"),
        deps=SimpleNamespace(material_digest_cache=invalidator),
        now=_NOW,
    )

    await MaterialDigestCacheInvalidateStep().apply(context, UndoTally())

    assert invalidator.calls == [("char-1", None)]


@pytest.mark.asyncio
async def test_undo_deletes_the_row_through_the_real_store(sa_store) -> None:  # noqa: ANN001
    """End to end on SQL: the row a post-turn wrote is gone after the undo."""
    cache = _precomputer(sa_store)
    await cache.recompute(
        _StubLoaders(digest=_digest("被撤回的那輪")),
        character=_character(),
        operator=_operator(),
        content_tolerance="frontier",
        now=_NOW,
    )
    context = UndoContext(
        journal=SimpleNamespace(character_id="char-1", conversation_id="conv-1"),
        deps=SimpleNamespace(material_digest_cache=cache),
        now=_NOW,
    )

    await MaterialDigestCacheInvalidateStep().apply(context, UndoTally())

    assert await sa_store.get(
        character_id="char-1", operator_id="user-1",
    ) is None


@pytest.mark.asyncio
async def test_undo_step_is_a_no_op_when_no_store_is_wired() -> None:
    context = UndoContext(
        journal=SimpleNamespace(character_id="char-1", conversation_id="conv-1"),
        deps=SimpleNamespace(material_digest_cache=None),
        now=_NOW,
    )

    await MaterialDigestCacheInvalidateStep().apply(context, UndoTally())


# --------------------------------------------------------------------------
# Wired into ChatService: read on the turn, write on the post-turn
# --------------------------------------------------------------------------


class _RecordingDigester:
    """Stands in for ``LLMPromptMaterialDigester``."""

    def __init__(self, digest: PromptMaterialDigest | None, log: list[str]) -> None:
        self.result = digest
        self.calls = 0
        self._log = log

    async def digest(self, context, *, character=None):  # noqa: ANN001
        self.calls += 1
        self._log.append("digest")
        return self.result


class _RecordingPostTurnProcessor:
    def __init__(self, log: list[str]) -> None:
        self.calls = 0
        self._log = log

    async def process(self, **kwargs):  # noqa: ANN003
        self.calls += 1
        self._log.append("extract")
        return PostTurnResult()


class _OperatorProfileService:
    async def get_for_user(self, user_id: str):  # noqa: ANN001
        return SimpleNamespace(id=user_id, timezone_id="Asia/Taipei")

    async def get_current(self):
        return SimpleNamespace(id=DEFAULT_OPERATOR_ID, timezone_id="UTC")


class _AlwaysUndoneGate:
    def __init__(self) -> None:
        self.asked = 0

    async def is_undone(self, turn_record_id):  # noqa: ANN001
        self.asked += 1
        return True


class _UndoneAfterExtractionGate:
    """Not undone on entry; undone by the time the extraction returns."""

    def __init__(self) -> None:
        self.asked = 0

    async def is_undone(self, turn_record_id):  # noqa: ANN001
        self.asked += 1
        return self.asked > 1


def _build_chat_service(
    *,
    digest: PromptMaterialDigest | None,
    enabled: bool = True,
    store=None,  # noqa: ANN001
):
    log: list[str] = []
    digester = _RecordingDigester(digest, log)
    processor = _RecordingPostTurnProcessor(log)
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    service = ChatService(
        character_repository=InMemoryCharacterRepository(),
        conversation_repository=InMemoryConversationRepository(),
        memory_repository=InMemoryMemoryRepository(),
        post_turn_processor=processor,
        prompt_context_builder=SimpleNamespace(build=lambda **kwargs: ""),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        operator_profile_service=_OperatorProfileService(),
        prompt_material_digester=digester,
        prompt_material_digest_enabled=enabled,
        prompt_material_digest_store=store,
        # Fixed, so the row's ``updated_at`` is a known instant and the
        # max-age assertions below are about the rule rather than about
        # how long the test took to run.
        clock=SimpleNamespace(now=lambda: _NOW),
    )
    return SimpleNamespace(
        service=service, digester=digester, processor=processor, log=log,
    )


async def _run_post_turn(rig, *, character=None) -> dict:
    return await rig.service._do_post_turn(
        character=character or _character(),
        conversation_id="conv-1",
        turn_record_id="turn-1",
        user_text="今天想聊天",
        assistant_text="好啊",
        prior_messages=[],
    )


async def _read_budget(service, *, now: datetime = _NOW):  # noqa: ANN001
    return await service._load_cached_prompt_material_digest(
        character=_character(),
        operator=_operator(),
        content_tolerance="community",
        now=now,
    )


@pytest.mark.asyncio
async def test_post_turn_budgets_the_next_turn_digest() -> None:
    rig = _build_chat_service(digest=_digest("今天的重點"))

    await asyncio.wait_for(_run_post_turn(rig), timeout=_TIMEOUT)

    assert rig.digester.calls == 1
    # After the extraction — this turn's own facts are in the digest.
    assert rig.log == ["extract", "digest"]
    assert await _read_budget(rig.service) == rig.digester.result


@pytest.mark.asyncio
async def test_the_chat_read_never_computes_a_digest() -> None:
    rig = _build_chat_service(digest=_digest("今天的重點"))

    for _ in range(3):
        assert await _read_budget(rig.service) is None

    assert rig.digester.calls == 0


@pytest.mark.asyncio
async def test_the_chat_read_honours_the_max_age() -> None:
    rig = _build_chat_service(digest=_digest("今天的重點"))
    await _run_post_turn(rig)

    assert await _read_budget(rig.service) is not None
    assert await _read_budget(
        rig.service, now=_NOW + timedelta(days=2),
    ) is None


@pytest.mark.asyncio
async def test_disabled_switch_neither_budgets_nor_reads() -> None:
    rig = _build_chat_service(digest=_digest("今天的重點"), enabled=False)

    await asyncio.wait_for(_run_post_turn(rig), timeout=_TIMEOUT)

    assert rig.digester.calls == 0
    assert await _read_budget(rig.service) is None


@pytest.mark.asyncio
async def test_a_turn_undone_before_the_post_turn_budgets_nothing() -> None:
    rig = _build_chat_service(digest=_digest("今天的重點"))
    rig.service.set_undone_turn_gate(_AlwaysUndoneGate())

    await asyncio.wait_for(_run_post_turn(rig), timeout=_TIMEOUT)

    assert rig.processor.calls == 0
    assert rig.digester.calls == 0


@pytest.mark.asyncio
async def test_a_turn_undone_while_in_flight_budgets_nothing() -> None:
    """The budget sits on the far side of the second gate (TU2)."""
    rig = _build_chat_service(digest=_digest("今天的重點"))
    rig.service.set_undone_turn_gate(_UndoneAfterExtractionGate())

    await asyncio.wait_for(_run_post_turn(rig), timeout=_TIMEOUT)

    assert rig.processor.calls == 1
    assert rig.digester.calls == 0
    assert await _read_budget(rig.service) is None


@pytest.mark.asyncio
async def test_a_broken_digester_does_not_break_the_post_turn() -> None:
    rig = _build_chat_service(digest=None)

    async def _boom(context, *, character=None):  # noqa: ANN001
        raise RuntimeError("upstream down")

    rig.digester.digest = _boom

    result = await asyncio.wait_for(_run_post_turn(rig), timeout=_TIMEOUT)

    assert "post_turn_error" not in result
    assert await _read_budget(rig.service) is None


@pytest.mark.asyncio
async def test_the_digest_crosses_a_process_boundary(sa_store) -> None:  # noqa: ANN001
    """The whole reason this is a table rather than a dict.

    On hosted the post-turn body runs on a worker process and the chat
    turn is served from an api replica. Two ``ChatService`` instances
    sharing nothing but the store stand in for that: one only ever runs
    the post-turn, the other only ever reads, and the digest still makes
    the trip. Against a process-local cache the reader sees ``None``
    forever.
    """
    worker = _build_chat_service(digest=_digest("worker 蒸的"), store=sa_store)
    api = _build_chat_service(digest=_digest("api 永遠不該呼叫"), store=sa_store)

    await asyncio.wait_for(_run_post_turn(worker), timeout=_TIMEOUT)
    read = await _read_budget(api.service)

    assert read is not None
    assert read.bullets == ("worker 蒸的",)
    # The reading replica never called its own digester.
    assert api.digester.calls == 0


@pytest.mark.asyncio
async def test_an_undo_on_one_replica_clears_what_another_budgeted(sa_store) -> None:  # noqa: ANN001
    """Same boundary, the other direction — the undo runs where chat does."""
    worker = _build_chat_service(digest=_digest("被撤回的那輪"), store=sa_store)
    api = _build_chat_service(digest=None, store=sa_store)
    await asyncio.wait_for(_run_post_turn(worker), timeout=_TIMEOUT)
    assert await _read_budget(api.service) is not None

    context = UndoContext(
        journal=SimpleNamespace(character_id="char-1", conversation_id="conv-1"),
        deps=SimpleNamespace(
            material_digest_cache=api.service.material_digest_precomputer,
        ),
        now=_NOW,
    )
    await MaterialDigestCacheInvalidateStep().apply(context, UndoTally())

    assert await _read_budget(api.service) is None
