"""Contract tests for the LR campaign ledger (LR T1).

Two backends run the *same* assertions:

- ``memory`` — the DB-less unit twin.
- ``sqlite`` — the real SQLAlchemy adapter against ``sqlite+aiosqlite``.

The SQLite leg carries the three guards that are only guards if the
database says so: a duplicate ``campaign_id`` must be refused (the
in-memory twin checks a dict; the adapter relies on the primary key),
``record_outcome`` must be fenced on ``outcome IS NULL`` so a second
runner cannot overwrite a recorded attempt, and ``claim_item`` must hand
leased ownership of a pending row to exactly one caller so a second
runner never *sends* in the first place.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from kokoro_link.contracts.line_reactivation import (
    CAMPAIGN_STATUS_COMPLETED,
    CAMPAIGN_STATUS_RUNNING,
    LineReactivationCampaign,
    LineReactivationCampaignConflictError,
)
from kokoro_link.infrastructure.persistence.engine import build_session_factory
from kokoro_link.infrastructure.persistence.models import (
    CharacterRow,
    LineReactivationCampaignItemRow,
    LineReactivationCampaignRow,
)
from kokoro_link.infrastructure.persistence.sa_line_reactivation_repository import (  # noqa: E501
    SALineReactivationCampaignRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_line_reactivation import (  # noqa: E501
    InMemoryLineReactivationCampaignRepository,
)

pytestmark = pytest.mark.asyncio

UTC = timezone.utc
_NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
_CAMPAIGN_ID = "3f1c0b6a-0000-4000-8000-000000000001"
_LEASE = timedelta(minutes=15)


@pytest_asyncio.fixture(params=["memory", "sqlite"])
async def repository(request):  # noqa: ANN001, ANN201
    if request.param == "memory":
        yield InMemoryLineReactivationCampaignRepository()
        return
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(LineReactivationCampaignRow.__table__.create)
        await conn.run_sync(LineReactivationCampaignItemRow.__table__.create)
    try:
        yield SALineReactivationCampaignRepository(build_session_factory(engine))
    finally:
        await engine.dispose()


def _campaign(total: int = 2) -> LineReactivationCampaign:
    return LineReactivationCampaign(
        campaign_id=_CAMPAIGN_ID,
        actor="admin@example.com",
        status=CAMPAIGN_STATUS_RUNNING,
        created_at=_NOW,
        total=total,
    )


async def test_create_lands_campaign_and_one_pending_item_each(
    repository,
) -> None:
    await repository.create(_campaign(), ["c1", "c2"])

    stored = await repository.get(_CAMPAIGN_ID)
    assert stored is not None
    assert stored.actor == "admin@example.com"
    assert stored.status == CAMPAIGN_STATUS_RUNNING
    assert stored.total == 2
    assert stored.completed_at is None

    items = await repository.list_items(_CAMPAIGN_ID)
    assert [item.character_id for item in items] == ["c1", "c2"]
    assert all(item.pending for item in items)


async def test_duplicate_character_ids_collapse(repository) -> None:
    """A request listing the same character twice is one attempt slot."""
    await repository.create(_campaign(total=1), ["c1", "c1"])

    items = await repository.list_items(_CAMPAIGN_ID)
    assert [item.character_id for item in items] == ["c1"]


async def test_reusing_a_campaign_id_is_refused(repository) -> None:
    await repository.create(_campaign(), ["c1", "c2"])

    with pytest.raises(LineReactivationCampaignConflictError):
        await repository.create(_campaign(), ["c3"])


async def test_unknown_campaign_reads_as_none(repository) -> None:
    assert await repository.get("nope") is None
    assert await repository.list_items("nope") == []
    assert await repository.list_pending_items("nope") == []


async def test_recording_an_outcome_removes_it_from_pending(
    repository,
) -> None:
    await repository.create(_campaign(), ["c1", "c2"])

    recorded = await repository.record_outcome(
        _CAMPAIGN_ID,
        "c1",
        outcome="sent",
        detail=None,
        attempted_at=_NOW + timedelta(minutes=1),
    )

    assert recorded is True
    pending = await repository.list_pending_items(_CAMPAIGN_ID)
    assert [item.character_id for item in pending] == ["c2"]
    done = next(
        item
        for item in await repository.list_items(_CAMPAIGN_ID)
        if item.character_id == "c1"
    )
    assert done.outcome == "sent"
    assert done.attempted_at == _NOW + timedelta(minutes=1)


async def test_outcome_detail_round_trips(repository) -> None:
    await repository.create(_campaign(total=1), ["c1"])

    await repository.record_outcome(
        _CAMPAIGN_ID,
        "c1",
        outcome="gate_blocked",
        detail="quiet hours",
        attempted_at=_NOW,
    )

    item = (await repository.list_items(_CAMPAIGN_ID))[0]
    assert (item.outcome, item.detail) == ("gate_blocked", "quiet hours")
    # Nothing was sent, so there is no body to review.
    assert item.message_text is None


async def test_the_sent_body_round_trips_unclipped(repository) -> None:
    """The report's payload column.

    Asserted at a length no bounded string column would survive: the
    operator reads this to judge whether the recall lands, and a body
    truncated by storage would read as a message that ends badly rather
    than as a column that is too small.
    """
    await repository.create(_campaign(total=1), ["c1"])
    body = "好久不見，" * 400

    await repository.record_outcome(
        _CAMPAIGN_ID,
        "c1",
        outcome="sent",
        detail="admin reactivation",
        message_text=body,
        attempted_at=_NOW,
    )

    item = (await repository.list_items(_CAMPAIGN_ID))[0]
    assert item.message_text == body


async def test_a_message_text_left_unsaid_stores_null(repository) -> None:
    """``message_text`` is a keyword with a default, so every existing
    caller that records a skip or a block keeps compiling — and must land
    an honest ``NULL`` rather than an empty string the console would have
    to distinguish from a genuinely blank message."""
    await repository.create(_campaign(total=1), ["c1"])

    await repository.record_outcome(
        _CAMPAIGN_ID,
        "c1",
        outcome="skipped_not_dormant",
        detail="no_longer_dormant",
        attempted_at=_NOW,
    )

    assert (await repository.list_items(_CAMPAIGN_ID))[0].message_text is None


async def test_second_outcome_write_is_refused(repository) -> None:
    """Per-item idempotency: the first attempt's answer is the answer."""
    await repository.create(_campaign(total=1), ["c1"])
    await repository.record_outcome(
        _CAMPAIGN_ID, "c1", outcome="sent", detail=None, attempted_at=_NOW,
    )

    again = await repository.record_outcome(
        _CAMPAIGN_ID,
        "c1",
        outcome="errored",
        detail="late loser",
        attempted_at=_NOW + timedelta(minutes=5),
    )

    assert again is False
    item = (await repository.list_items(_CAMPAIGN_ID))[0]
    assert item.outcome == "sent"
    assert item.detail is None


async def test_outcome_for_an_unselected_character_is_refused(
    repository,
) -> None:
    await repository.create(_campaign(total=1), ["c1"])

    assert (
        await repository.record_outcome(
            _CAMPAIGN_ID,
            "not-selected",
            outcome="sent",
            detail=None,
            attempted_at=_NOW,
        )
        is False
    )


async def test_only_one_caller_wins_a_claim(repository) -> None:
    """The fence that sits *before* the send.

    ``record_outcome``'s ``outcome IS NULL`` fence is crossed after the
    message has gone out; it makes the loser's bookkeeping fail, not its
    message disappear. This is the one that decides who may dispatch.
    """
    await repository.create(_campaign(total=1), ["c1"])

    first = await repository.claim_item(
        _CAMPAIGN_ID, "c1", now=_NOW, lease=_LEASE,
    )
    second = await repository.claim_item(
        _CAMPAIGN_ID, "c1", now=_NOW + timedelta(minutes=1), lease=_LEASE,
    )

    assert (first, second) == (True, False)


async def test_a_claim_lapses_after_its_lease(repository) -> None:
    """A replica that dies mid-send must not strand the row forever."""
    await repository.create(_campaign(total=1), ["c1"])
    await repository.claim_item(_CAMPAIGN_ID, "c1", now=_NOW, lease=_LEASE)

    just_inside = await repository.claim_item(
        _CAMPAIGN_ID, "c1", now=_NOW + _LEASE - timedelta(seconds=1),
        lease=_LEASE,
    )
    past_it = await repository.claim_item(
        _CAMPAIGN_ID, "c1", now=_NOW + _LEASE + timedelta(seconds=1),
        lease=_LEASE,
    )

    assert (just_inside, past_it) == (False, True)


async def test_an_answered_item_can_never_be_claimed(repository) -> None:
    """Even a lapsed lease must not re-open a row that has an outcome —
    that would be a second send for a character already dealt with."""
    await repository.create(_campaign(total=1), ["c1"])
    await repository.record_outcome(
        _CAMPAIGN_ID, "c1", outcome="sent", detail=None, attempted_at=_NOW,
    )

    assert (
        await repository.claim_item(
            _CAMPAIGN_ID, "c1", now=_NOW + timedelta(days=1), lease=_LEASE,
        )
        is False
    )


async def test_claiming_an_unselected_character_is_refused(repository) -> None:
    await repository.create(_campaign(total=1), ["c1"])

    assert (
        await repository.claim_item(
            _CAMPAIGN_ID, "not-selected", now=_NOW, lease=_LEASE,
        )
        is False
    )


async def test_a_claimed_item_is_still_pending(repository) -> None:
    """"Someone is working on it" and "it has been dealt with" are
    different questions; only the second one may end a campaign."""
    await repository.create(_campaign(total=1), ["c1"])
    await repository.claim_item(_CAMPAIGN_ID, "c1", now=_NOW, lease=_LEASE)

    pending = await repository.list_pending_items(_CAMPAIGN_ID)

    assert [item.character_id for item in pending] == ["c1"]
    assert pending[0].claimed_at == _NOW


async def test_completion_is_a_one_way_transition(repository) -> None:
    await repository.create(_campaign(total=1), ["c1"])

    first = await repository.mark_completed(
        _CAMPAIGN_ID, completed_at=_NOW + timedelta(minutes=10),
    )
    second = await repository.mark_completed(
        _CAMPAIGN_ID, completed_at=_NOW + timedelta(hours=1),
    )

    assert (first, second) == (True, False)
    stored = await repository.get(_CAMPAIGN_ID)
    assert stored is not None
    assert stored.status == CAMPAIGN_STATUS_COMPLETED
    assert stored.completed_at == _NOW + timedelta(minutes=10)


async def test_completing_an_unknown_campaign_reports_false(
    repository,
) -> None:
    assert await repository.mark_completed("nope", completed_at=_NOW) is False


async def test_a_foreign_key_failure_is_not_reported_as_a_conflict() -> None:
    """SQLite-only, and with the FK pragma actually on.

    The adapter maps ``IntegrityError`` to a 409 ``campaign_conflict``,
    and a conflict tells the console one thing: mint a new
    ``campaign_id``. A selection naming a deleted character produces the
    same exception from the item rows' foreign key, and no new id will
    ever fix it — so that path must surface as itself.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _enforce_foreign_keys(dbapi_connection, _record):  # noqa: ANN001, ANN202
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(CharacterRow.__table__.create)
        await conn.run_sync(LineReactivationCampaignRow.__table__.create)
        await conn.run_sync(LineReactivationCampaignItemRow.__table__.create)
    try:
        repository = SALineReactivationCampaignRepository(
            build_session_factory(engine),
        )

        with pytest.raises(IntegrityError):
            await repository.create(_campaign(total=1), ["no-such-character"])

        # And nothing half-landed: the campaign row rolled back with it.
        assert await repository.get(_CAMPAIGN_ID) is None
    finally:
        await engine.dispose()
