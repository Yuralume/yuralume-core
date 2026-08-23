"""FX3 — a drama created under an older, higher segment cap stays usable.

``MAX_TOTAL_SEGMENTS`` is a policy number, and policy numbers move: the
create form offered up to 15 segments before BD4 lowered the ceiling to 12,
so rows above today's cap exist in real databases.

The trap is that the entity constructor is not only the create path — every
repository mapper builds a ``BranchingDrama`` from a row, and ``list_recent``
maps *before* any caller can filter. Enforcing the ceiling there does not
stop a single over-cap drama from existing; it turns one of them into a
500 on the whole drama list, and the delete endpoint that is the only way
to get rid of it shares that path. The player cannot even clean up after
the cap change.

So: the ceiling is enforced where a drama is *made* (``create_pending`` and
the request DTO), and the read path is tolerant. Both halves are pinned
here, on both repositories.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from kokoro_link.application.dto.branching_drama import (
    CreateBranchingDramaRequest,
)
from kokoro_link.domain.entities.branching_drama import (
    MAX_TOTAL_SEGMENTS,
    STATUS_READY,
    BranchingDrama,
)
from kokoro_link.infrastructure.persistence.branching_drama_models import (
    BranchingDramaNodeRow,
    BranchingDramaRow,
    BranchingDramaSessionRow,
)
from kokoro_link.infrastructure.persistence.engine import build_session_factory
from kokoro_link.infrastructure.persistence.sa_branching_drama_repository import (
    SABranchingDramaRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_branching_drama import (
    InMemoryBranchingDramaRepository,
)


_LEGACY_SEGMENTS = 15
"""What the create form used to allow — comfortably above today's cap."""


@pytest_asyncio.fixture
async def sa_repo():  # noqa: ANN201
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        for row in (
            BranchingDramaRow,
            BranchingDramaNodeRow,
            BranchingDramaSessionRow,
        ):
            await conn.run_sync(lambda sync, r=row: r.__table__.create(sync))
    factory = build_session_factory(engine)
    try:
        yield SABranchingDramaRepository(factory), factory
    finally:
        await engine.dispose()


async def _insert_legacy_row(factory, drama_id: str = "legacy-1") -> None:  # noqa: ANN001
    """A row exactly as a pre-BD4 create would have left it."""
    now = datetime.now(timezone.utc)
    async with factory() as session:
        row = BranchingDramaRow()
        row.id = drama_id
        row.character_ids_json = '["c-a", "c-b"]'
        row.prompt = "十五段的舊劇場"
        row.title = "舊標題"
        row.total_segments = _LEGACY_SEGMENTS
        row.status = STATUS_READY
        row.operator_position = None
        row.operator_note = None
        row.created_at = now
        row.updated_at = now
        session.add(row)
        await session.commit()


# ── the read path stays open ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_over_cap_row_does_not_break_the_whole_list(sa_repo) -> None:  # noqa: ANN001
    """One bad row must not take every other drama down with it."""
    repo, factory = sa_repo
    await repo.add(
        BranchingDrama.create_pending(
            id="current-1", character_ids=["c-a", "c-b"], prompt="新的劇場",
        ),
    )
    await _insert_legacy_row(factory)

    listed = await repo.list_recent()

    assert {d.id for d in listed} == {"current-1", "legacy-1"}
    legacy = next(d for d in listed if d.id == "legacy-1")
    assert legacy.total_segments == _LEGACY_SEGMENTS


@pytest.mark.asyncio
async def test_an_over_cap_drama_can_still_be_fetched(sa_repo) -> None:  # noqa: ANN001
    repo, factory = sa_repo
    await _insert_legacy_row(factory)

    stored = await repo.get("legacy-1")

    assert stored is not None
    assert stored.total_segments == _LEGACY_SEGMENTS


@pytest.mark.asyncio
async def test_an_over_cap_drama_can_still_be_deleted(sa_repo) -> None:  # noqa: ANN001
    """Delete is the player's only way out, and it reads before it writes."""
    repo, factory = sa_repo
    await _insert_legacy_row(factory)

    await repo.delete("legacy-1")

    assert await repo.get("legacy-1") is None
    assert await repo.list_recent() == []


@pytest.mark.asyncio
async def test_the_in_memory_repository_agrees() -> None:
    repo = InMemoryBranchingDramaRepository()
    legacy = BranchingDrama(
        id="legacy-1",
        character_ids=("c-a", "c-b"),
        prompt="十五段的舊劇場",
        title="舊標題",
        total_segments=_LEGACY_SEGMENTS,
        status=STATUS_READY,
    )
    await repo.add(legacy)

    assert [d.id for d in await repo.list_recent()] == ["legacy-1"]
    assert (await repo.get("legacy-1")).total_segments == _LEGACY_SEGMENTS

    await repo.delete("legacy-1")
    assert await repo.get("legacy-1") is None


@pytest.mark.asyncio
async def test_an_over_cap_drama_still_saves(sa_repo) -> None:  # noqa: ANN001
    """Playing one writes it back — a title, a status, an error message."""
    repo, factory = sa_repo
    await _insert_legacy_row(factory)

    stored = await repo.get("legacy-1")
    await repo.save(stored.with_title("改過名字了"))

    assert (await repo.get("legacy-1")).title == "改過名字了"


# ── the create path stays shut ────────────────────────────────────────


def test_creating_over_the_cap_is_still_refused() -> None:
    with pytest.raises(ValueError, match="total_segments"):
        BranchingDrama.create_pending(
            character_ids=["c-a", "c-b"],
            prompt="p",
            total_segments=MAX_TOTAL_SEGMENTS + 1,
        )


def test_the_request_dto_still_refuses_over_the_cap() -> None:
    with pytest.raises(ValueError):
        CreateBranchingDramaRequest(
            character_ids=["c-a", "c-b"],
            prompt="p",
            total_segments=MAX_TOTAL_SEGMENTS + 1,
        )

    ok = CreateBranchingDramaRequest(
        character_ids=["c-a", "c-b"],
        prompt="p",
        total_segments=MAX_TOTAL_SEGMENTS,
    )
    assert ok.total_segments == MAX_TOTAL_SEGMENTS
