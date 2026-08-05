"""IV8 — ``GET /characters/{id}/memories`` pagination + projection.

Two halves, deliberately in one file because they guard each other:

**Characterization** (``test_char_*``) pins the semantics that must survive
the rewrite — kind filtering, newest-first ordering, world-scope breadth,
and what ``has_embedding`` actually means. These were written and run green
against the pre-IV8 implementation (bare ``list[MemoryResponse]``, no
pagination, ``SELECT *``); the only thing IV8 is allowed to change about
them is the envelope the items arrive in.

**Behaviour** (``test_page_*`` / ``test_select_*``) covers the two defects
IV8 fixes, which are independent (plan §1.6 / D8 / D9):

1. the route declared only ``kind``, so ``?limit=50`` was silently dropped
   by FastAPI and every caller got the whole table;
2. the listing query was ``select(MemoryItemRow)`` — two ``Vector(1024)``
   columns hauled out of Postgres and ``float()``-coerced element by
   element, purely to end up as one boolean. Paginating without fixing
   this just makes each page drag its own slice of the blob.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.routes.characters import router as character_router
from kokoro_link.api.routes.memory import router as memory_admin_router
from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.memory_admin_service import (
    MemoryAdminService,
)
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)

_BASE = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def _at(minutes: int) -> datetime:
    """Deterministic ``created_at`` so ordering assertions are not racy."""
    return _BASE + timedelta(minutes=minutes)


def _build() -> tuple[
    MemoryAdminService, InMemoryMemoryRepository, CharacterService,
]:
    memory_repo = InMemoryMemoryRepository()
    character_repo = InMemoryCharacterRepository()
    character_service = CharacterService(
        character_repo, memory_repository=memory_repo,
    )
    admin = MemoryAdminService(memory_repository=memory_repo, embedder=None)
    return admin, memory_repo, character_service


def _client(
    admin: MemoryAdminService, character_service: CharacterService,
) -> TestClient:
    class _Container:
        pass

    container = _Container()
    container.memory_admin_service = admin
    container.character_service = character_service
    app = FastAPI()
    app.state.container = container
    app.include_router(character_router, prefix="/api/v1")
    app.include_router(memory_admin_router, prefix="/api/v1")
    return TestClient(app)


async def _seed_character(character_service: CharacterService) -> str:
    created = await character_service.create_character(
        CreateCharacterRequest(name="Yui"),
    )
    return created.id


def _items(payload) -> list[dict]:
    """The item list, whatever envelope the endpoint currently uses.

    Kept as a seam so the characterization assertions below say exactly
    one thing — *the semantics did not move* — and the envelope change
    shows up only in the dedicated shape tests.
    """
    return payload if isinstance(payload, list) else payload["items"]


def _contents(payload) -> list[str]:
    return [row["content"] for row in _items(payload)]


# --------------------------------------------------------------------------
# Characterization — held green before and after IV8
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_char_kind_filter_returns_only_that_kind() -> None:
    admin, repo, characters = _build()
    character_id = await _seed_character(characters)
    for kind, content in (
        (MemoryKind.SEMANTIC, "事實"),
        (MemoryKind.RELATIONSHIP, "關係"),
        (MemoryKind.EPISODIC, "事件"),
        (MemoryKind.REFLECTION, "反思"),
    ):
        await repo.add(MemoryItem.create(
            character_id=character_id, kind=kind, content=content,
            created_at=_at(0),
        ))
    client = _client(admin, characters)

    for kind_value, expected in (
        ("semantic", "事實"),
        ("relationship", "關係"),
        ("episodic", "事件"),
        ("reflection", "反思"),
    ):
        resp = client.get(
            f"/api/v1/characters/{character_id}/memories",
            params={"kind": kind_value},
        )
        assert resp.status_code == 200
        assert _contents(resp.json()) == [expected]


@pytest.mark.asyncio
async def test_char_no_kind_filter_returns_every_kind() -> None:
    admin, repo, characters = _build()
    character_id = await _seed_character(characters)
    for index, kind in enumerate(
        (MemoryKind.SEMANTIC, MemoryKind.RELATIONSHIP, MemoryKind.HEARSAY),
    ):
        await repo.add(MemoryItem.create(
            character_id=character_id, kind=kind, content=f"m{index}",
            created_at=_at(index),
        ))
    client = _client(admin, characters)

    resp = client.get(f"/api/v1/characters/{character_id}/memories")

    assert resp.status_code == 200
    assert {row["kind"] for row in _items(resp.json())} == {
        "semantic", "relationship", "hearsay",
    }


@pytest.mark.asyncio
async def test_char_unknown_kind_yields_an_empty_list_not_an_error() -> None:
    """``MemoryKind`` is an open value object, not an enum.

    An unrecognised filter is therefore a legitimate query that simply
    matches nothing — it must not 400. Pinned because the route's
    ``ValueError -> 400`` branch invites the opposite reading.
    """
    admin, repo, characters = _build()
    character_id = await _seed_character(characters)
    await repo.add(MemoryItem.create(
        character_id=character_id, kind=MemoryKind.SEMANTIC, content="事實",
        created_at=_at(0),
    ))
    client = _client(admin, characters)

    resp = client.get(
        f"/api/v1/characters/{character_id}/memories",
        params={"kind": "not-a-kind"},
    )

    assert resp.status_code == 200
    assert _items(resp.json()) == []


@pytest.mark.asyncio
async def test_char_order_is_newest_first() -> None:
    admin, repo, characters = _build()
    character_id = await _seed_character(characters)
    for index in range(5):
        await repo.add(MemoryItem.create(
            character_id=character_id, kind=MemoryKind.SEMANTIC,
            content=f"m{index}", created_at=_at(index),
        ))
    client = _client(admin, characters)

    resp = client.get(f"/api/v1/characters/{character_id}/memories")

    assert _contents(resp.json()) == ["m4", "m3", "m2", "m1", "m0"]


@pytest.mark.asyncio
async def test_char_world_scoped_rows_are_not_filtered_out() -> None:
    """The browse path lists everything the character owns.

    ``world_scope`` defaults to ``"all"`` here, unlike the chat retrieval
    path which pins ``None``. An operator inspecting what the system
    learned must see legacy world-scoped rows too.
    """
    admin, repo, characters = _build()
    character_id = await _seed_character(characters)
    await repo.add(MemoryItem.create(
        character_id=character_id, kind=MemoryKind.SEMANTIC,
        content="worldless", created_at=_at(1),
    ))
    await repo.add(MemoryItem.create(
        character_id=character_id, kind=MemoryKind.SEMANTIC,
        content="scoped", created_at=_at(0), world_id="w-legacy",
    ))
    client = _client(admin, characters)

    resp = client.get(f"/api/v1/characters/{character_id}/memories")

    assert _contents(resp.json()) == ["worldless", "scoped"]


@pytest.mark.asyncio
async def test_char_has_embedding_tracks_the_content_vector_only() -> None:
    """A tags-only vector does **not** count as "has embedding".

    The flag mirrors ``MemoryItem.embedding is not None``; the tag vector
    is an auxiliary recall booster, and the UI badge ("no vector —
    semantic search skips this") is about the content vector. The
    SQL-side rewrite must compute exactly this predicate.
    """
    admin, repo, characters = _build()
    character_id = await _seed_character(characters)
    await repo.add(MemoryItem.create(
        character_id=character_id, kind=MemoryKind.SEMANTIC,
        content="with-content-vector", created_at=_at(2),
        embedding=(0.1, 0.2),
    ))
    await repo.add(MemoryItem.create(
        character_id=character_id, kind=MemoryKind.SEMANTIC,
        content="tags-vector-only", created_at=_at(1),
        tags_embedding=(0.3, 0.4),
    ))
    await repo.add(MemoryItem.create(
        character_id=character_id, kind=MemoryKind.SEMANTIC,
        content="no-vector", created_at=_at(0),
    ))
    client = _client(admin, characters)

    resp = client.get(f"/api/v1/characters/{character_id}/memories")

    flags = {row["content"]: row["has_embedding"] for row in _items(resp.json())}
    assert flags == {
        "with-content-vector": True,
        "tags-vector-only": False,
        "no-vector": False,
    }


@pytest.mark.asyncio
async def test_char_body_carries_the_browse_fields_but_never_a_vector() -> None:
    admin, repo, characters = _build()
    character_id = await _seed_character(characters)
    await repo.add(MemoryItem.create(
        character_id=character_id, kind=MemoryKind.SEMANTIC,
        content="fields", created_at=_at(0), salience=0.25,
        tags=["a", "b"], embedding=(0.1, 0.2), tags_embedding=(0.3, 0.4),
    ))
    client = _client(admin, characters)

    resp = client.get(f"/api/v1/characters/{character_id}/memories")

    row = _items(resp.json())[0]
    assert set(row) == {
        "id", "character_id", "conversation_id", "kind", "content",
        "salience", "tags", "created_at", "last_accessed_at",
        "access_count", "has_embedding",
    }
    assert row["tags"] == ["a", "b"]
    assert row["salience"] == 0.25


@pytest.mark.asyncio
async def test_char_other_characters_rows_never_leak() -> None:
    admin, repo, characters = _build()
    mine = await _seed_character(characters)
    other = (await characters.create_character(
        CreateCharacterRequest(name="Other"),
    )).id
    await repo.add(MemoryItem.create(
        character_id=mine, kind=MemoryKind.SEMANTIC, content="mine",
        created_at=_at(0),
    ))
    await repo.add(MemoryItem.create(
        character_id=other, kind=MemoryKind.SEMANTIC, content="theirs",
        created_at=_at(1),
    ))
    client = _client(admin, characters)

    resp = client.get(f"/api/v1/characters/{mine}/memories")

    assert _contents(resp.json()) == ["mine"]


# --------------------------------------------------------------------------
# Defect 1 — pagination (plan D8: feed's keyset shape, no page/page_size)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_page_limit_truncates_and_reports_the_cursor() -> None:
    admin, repo, characters = _build()
    character_id = await _seed_character(characters)
    for index in range(7):
        await repo.add(MemoryItem.create(
            character_id=character_id, kind=MemoryKind.SEMANTIC,
            content=f"m{index}", created_at=_at(index),
        ))
    client = _client(admin, characters)

    resp = client.get(
        f"/api/v1/characters/{character_id}/memories", params={"limit": 3},
    )

    body = resp.json()
    assert _contents(body) == ["m6", "m5", "m4"]
    assert body["has_more"] is True
    assert body["next_before"] is not None


@pytest.mark.asyncio
async def test_page_before_cursor_walks_the_whole_pool_without_gaps() -> None:
    admin, repo, characters = _build()
    character_id = await _seed_character(characters)
    for index in range(7):
        await repo.add(MemoryItem.create(
            character_id=character_id, kind=MemoryKind.SEMANTIC,
            content=f"m{index}", created_at=_at(index),
        ))
    client = _client(admin, characters)

    seen: list[str] = []
    before: str | None = None
    for _ in range(10):  # generous bound; the loop must exit on has_more
        params: dict[str, object] = {"limit": 3}
        if before:
            params["before"] = before
        body = client.get(
            f"/api/v1/characters/{character_id}/memories", params=params,
        ).json()
        seen.extend(_contents(body))
        if not body["has_more"]:
            assert body["next_before"] is None
            break
        before = body["next_before"]
    else:  # pragma: no cover - only reached if pagination never terminates
        pytest.fail("pagination did not terminate")

    assert seen == ["m6", "m5", "m4", "m3", "m2", "m1", "m0"]


@pytest.mark.asyncio
async def test_page_exhausted_page_reports_no_more() -> None:
    admin, repo, characters = _build()
    character_id = await _seed_character(characters)
    for index in range(2):
        await repo.add(MemoryItem.create(
            character_id=character_id, kind=MemoryKind.SEMANTIC,
            content=f"m{index}", created_at=_at(index),
        ))
    client = _client(admin, characters)

    body = client.get(
        f"/api/v1/characters/{character_id}/memories", params={"limit": 50},
    ).json()

    assert len(_items(body)) == 2
    assert body["has_more"] is False
    assert body["next_before"] is None


@pytest.mark.asyncio
async def test_page_kind_filter_survives_pagination() -> None:
    admin, repo, characters = _build()
    character_id = await _seed_character(characters)
    for index in range(6):
        await repo.add(MemoryItem.create(
            character_id=character_id,
            kind=MemoryKind.SEMANTIC if index % 2 == 0 else MemoryKind.EPISODIC,
            content=f"m{index}", created_at=_at(index),
        ))
    client = _client(admin, characters)

    first = client.get(
        f"/api/v1/characters/{character_id}/memories",
        params={"kind": "semantic", "limit": 2},
    ).json()
    second = client.get(
        f"/api/v1/characters/{character_id}/memories",
        params={"kind": "semantic", "limit": 2, "before": first["next_before"]},
    ).json()

    assert _contents(first) == ["m4", "m2"]
    assert _contents(second) == ["m0"]
    assert second["has_more"] is False


@pytest.mark.asyncio
async def test_page_rejects_out_of_range_limits() -> None:
    """A bounded ``limit`` is the whole point — an unbounded one would
    reintroduce the 1567-row response the ticket exists to kill."""
    admin, _, characters = _build()
    character_id = await _seed_character(characters)
    client = _client(admin, characters)

    assert client.get(
        f"/api/v1/characters/{character_id}/memories", params={"limit": 0},
    ).status_code == 422
    assert client.get(
        f"/api/v1/characters/{character_id}/memories", params={"limit": 100000},
    ).status_code == 422


# --------------------------------------------------------------------------
# Defect 2 — the SELECT (plan D9)
# --------------------------------------------------------------------------


def test_select_page_statement_never_reads_a_vector_column() -> None:
    """Guards the actual regression: ``select(MemoryItemRow)``.

    Compiling the statement is enough — no database needed — and it fails
    loudly the moment somebody swaps the column list back for the ORM
    entity, which is exactly how 3.2M floats ended up crossing the wire
    for a response body that contains none of them.
    """
    from kokoro_link.infrastructure.persistence.sa_memory_repository import (
        build_memory_page_stmt,
    )

    sql = str(build_memory_page_stmt("char-1", limit=50))

    assert "memory_items.tags_embedding" not in sql
    # The content vector appears only inside the server-side predicate
    # that produces ``has_embedding`` — never as a selected column.
    assert "memory_items.embedding IS NOT NULL AS has_embedding" in sql
    assert sql.count("memory_items.embedding") == 1


def test_select_page_statement_keeps_filter_and_order_semantics() -> None:
    from kokoro_link.infrastructure.persistence.sa_memory_repository import (
        build_memory_page_stmt,
    )

    sql = str(build_memory_page_stmt(
        "char-1",
        kinds=[MemoryKind.SEMANTIC],
        world_scope=None,
        limit=25,
        before=_at(3),
    ))

    assert "memory_items.character_id = " in sql
    assert "memory_items.kind IN " in sql
    assert "memory_items.world_id IS NULL" in sql
    assert "memory_items.created_at < " in sql
    assert "ORDER BY memory_items.created_at DESC" in sql


@pytest.mark.asyncio
async def test_select_repository_page_returns_a_vectorless_summary() -> None:
    """The port's browse projection must not carry vectors at all.

    Not "carries them but nobody looks" — the type itself has no vector
    field, so the in-memory and SQL implementations cannot drift into
    hydrating one.
    """
    _, repo, characters = _build()
    character_id = await _seed_character(characters)
    await repo.add(MemoryItem.create(
        character_id=character_id, kind=MemoryKind.SEMANTIC, content="x",
        created_at=_at(0), embedding=(0.1, 0.2), tags_embedding=(0.3, 0.4),
    ))

    page = await repo.list_page_for_character(character_id, limit=10)

    assert len(page) == 1
    summary = page[0]
    assert summary.has_embedding is True
    assert not hasattr(summary, "embedding")
    assert not hasattr(summary, "tags_embedding")
