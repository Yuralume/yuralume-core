"""Contract tests for the album candidate-batch repository.

Two backends run the *same* assertions:

- ``memory`` — the DB-less dev / unit twin.
- ``sqlite`` — the real SQLAlchemy adapter against ``sqlite+aiosqlite``.

The SQLite leg is not decoration: the container swaps implementations purely
on "is a database wired", so a self-host on SQLite gets the SQL adapter too.
A PostgreSQL-only statement construct (``ON CONFLICT`` from the ``postgresql``
dialect, say) compiles fine on Postgres and blows up here — which is exactly
the regression this parametrisation exists to catch. The Postgres leg lives in
``tests/integration/test_sa_character_image_candidate_batches.py``.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from kokoro_link.infrastructure.persistence.engine import build_session_factory
from kokoro_link.infrastructure.persistence.models import (
    CharacterImageCandidateBatchRow,
)
from kokoro_link.infrastructure.persistence.sa_character_image_candidate_batch_repository import (  # noqa: E501
    SACharacterImageCandidateBatchRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_character_image_candidate_batches import (  # noqa: E501
    InMemoryCharacterImageCandidateBatchRepository,
)


@pytest_asyncio.fixture(params=["memory", "sqlite"])
async def repository(request):  # noqa: ANN001, ANN201
    """One candidate-batch store per backend; SQLite gets a shared memory DB."""
    if request.param == "memory":
        yield InMemoryCharacterImageCandidateBatchRepository()
        return
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(CharacterImageCandidateBatchRow.__table__.create)
    try:
        yield SACharacterImageCandidateBatchRepository(build_session_factory(engine))
    finally:
        await engine.dispose()


# --- base semantics -------------------------------------------------------


@pytest.mark.asyncio
async def test_get_returns_empty_tuple_for_unknown_character(repository) -> None:  # noqa: ANN001
    assert await repository.get("char-1") == ()


@pytest.mark.asyncio
async def test_replace_overwrites_the_single_active_batch(repository) -> None:  # noqa: ANN001
    await repository.replace("char-1", ["a.png", "b.png"])
    # A second generate must supersede the first without a unique violation.
    await repository.replace("char-1", ["c.png"])

    assert await repository.get("char-1") == ("c.png",)


@pytest.mark.asyncio
async def test_replace_preserves_order_and_isolates_characters(repository) -> None:  # noqa: ANN001
    await repository.replace("char-1", ["b.png", "a.png", "c.png"])
    await repository.replace("char-2", ["z.png"])

    assert await repository.get("char-1") == ("b.png", "a.png", "c.png")
    assert await repository.get("char-2") == ("z.png",)


@pytest.mark.asyncio
async def test_replace_with_no_keys_drops_the_batch(repository) -> None:  # noqa: ANN001
    await repository.replace("char-1", ["a.png"])

    await repository.replace("char-1", [])

    assert await repository.get("char-1") == ()


@pytest.mark.asyncio
async def test_clear_removes_the_batch_and_is_idempotent(repository) -> None:  # noqa: ANN001
    await repository.replace("char-1", ["a.png"])

    assert await repository.clear("char-1") is True
    # Idempotent: "no batch" is not an error, and the unconditional form
    # always reports success because it has nothing to lose a race against.
    assert await repository.clear("char-1") is True
    assert await repository.get("char-1") == ()


# --- compare-and-swap: a stale writer must not clobber a newer batch ------
#
# The race is ordinary under the hosted topology: replica A commits a batch it
# read a moment ago while replica B's generate installs a fresh one. Without a
# CAS guard A's closing ``clear`` deletes B's row and B's candidates become
# uncommittable orphans.


@pytest.mark.asyncio
async def test_conditional_clear_drops_the_batch_it_expected(repository) -> None:  # noqa: ANN001
    await repository.replace("char-1", ["a.png", "b.png"])

    assert await repository.clear(
        "char-1", expected_keys=("a.png", "b.png"),
    ) is True
    assert await repository.get("char-1") == ()


@pytest.mark.asyncio
async def test_conditional_clear_spares_a_superseded_batch(repository) -> None:  # noqa: ANN001
    await repository.replace("char-1", ["old.png"])
    # Another replica's generate landed between our get and our clear.
    await repository.replace("char-1", ["new.png"])

    assert await repository.clear("char-1", expected_keys=("old.png",)) is False
    assert await repository.get("char-1") == ("new.png",)


@pytest.mark.asyncio
async def test_conditional_clear_on_a_missing_batch_reports_no_match(
    repository,  # noqa: ANN001
) -> None:
    assert await repository.clear("char-1", expected_keys=("old.png",)) is False


@pytest.mark.asyncio
async def test_conditional_clear_requires_the_exact_key_order(repository) -> None:  # noqa: ANN001
    """Order is part of the identity — the batch list is ordered."""
    await repository.replace("char-1", ["a.png", "b.png"])

    assert await repository.clear(
        "char-1", expected_keys=("b.png", "a.png"),
    ) is False
    assert await repository.get("char-1") == ("a.png", "b.png")


@pytest.mark.asyncio
async def test_conditional_replace_rewrites_the_batch_it_expected(repository) -> None:  # noqa: ANN001
    await repository.replace("char-1", ["a.png", "b.png"])

    applied = await repository.replace(
        "char-1", ["b.png"], expected_keys=("a.png", "b.png"),
    )

    assert applied is True
    assert await repository.get("char-1") == ("b.png",)


@pytest.mark.asyncio
async def test_conditional_replace_spares_a_superseded_batch(repository) -> None:  # noqa: ANN001
    await repository.replace("char-1", ["old-1.png", "old-2.png"])
    await repository.replace("char-1", ["new.png"])

    applied = await repository.replace(
        "char-1", ["old-2.png"], expected_keys=("old-1.png", "old-2.png"),
    )

    assert applied is False
    assert await repository.get("char-1") == ("new.png",)


@pytest.mark.asyncio
async def test_conditional_replace_with_no_keys_drops_the_expected_batch(
    repository,  # noqa: ANN001
) -> None:
    await repository.replace("char-1", ["a.png"])

    assert await repository.replace(
        "char-1", [], expected_keys=("a.png",),
    ) is True
    assert await repository.get("char-1") == ()


@pytest.mark.asyncio
async def test_conditional_replace_with_no_keys_spares_a_superseded_batch(
    repository,  # noqa: ANN001
) -> None:
    await repository.replace("char-1", ["old.png"])
    await repository.replace("char-1", ["new.png"])

    assert await repository.replace(
        "char-1", [], expected_keys=("old.png",),
    ) is False
    assert await repository.get("char-1") == ("new.png",)


@pytest.mark.asyncio
async def test_unconditional_writes_report_success(repository) -> None:  # noqa: ANN001
    """``expected_keys=None`` keeps the old last-writer-wins semantics."""
    assert await repository.replace("char-1", ["a.png"]) is True
    assert await repository.replace("char-1", ["b.png"]) is True
    assert await repository.get("char-1") == ("b.png",)
