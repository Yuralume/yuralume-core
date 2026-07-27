"""External-chat turn receipt state-machine contract tests (LH2).

One suite, run against BOTH the in-memory twin and the real SQLAlchemy adapter
on ``sqlite+aiosqlite`` (``params=["memory", "sqlite"]``) so both honour the
DR-LH0-004 recoverable state machine identically: the full happy-path
transitions, completed/terminal replay, hash conflict, live-lease
``InProgress``, expired-lease takeover (attempt charged, same turn_id, carried
anchors), owner+lease_version fencing, terminal immutability, the
never-overwrite-a-completed-snapshot rule, and expired ``GENERATED`` recovery.

Lease liveness is judged by each backend's clock (DB ``CURRENT_TIMESTAMP`` for
SA, wall clock for the twin). To exercise expiry deterministically — without
sleeping or racing a 1-second SQLite clock — ``_force_expire`` back-dates the
stored ``lease_until`` in a backend-appropriate way; the actual receipt
contract methods stay clock-driven and untouched.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from kokoro_link.contracts.external_chat_turn import (
    Claimed,
    CommittedRecovery,
    CompletedReplay,
    ExternalChatTurnState,
    GeneratedRecovery,
    HashConflict,
    InProgress,
    TerminalReplay,
)
from kokoro_link.infrastructure.persistence.engine import build_session_factory
from kokoro_link.infrastructure.persistence.models import (
    ExternalChatTurnReceiptRow,
)
from kokoro_link.infrastructure.persistence.sa_external_chat_turn_repository import (  # noqa: E501
    SAExternalChatTurnReceiptRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_external_chat_turn import (  # noqa: E501
    InMemoryExternalChatTurnReceiptRepository,
)

pytestmark = pytest.mark.asyncio

_PAST = datetime(2000, 1, 1, tzinfo=timezone.utc)


@pytest_asyncio.fixture(params=["memory", "sqlite"])
async def make_repo(request):
    """Yield an async factory ``make_repo(lease_seconds=...)`` for one backend.

    Each SQLite repo gets its own shared-connection in-memory database
    (``StaticPool`` so every session sees the one created table).
    """
    engines = []

    async def factory(lease_seconds: int = 300):
        if request.param == "memory":
            return InMemoryExternalChatTurnReceiptRepository(
                lease_seconds=lease_seconds,
            )
        engine = create_async_engine(
            "sqlite+aiosqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        async with engine.begin() as conn:
            await conn.run_sync(ExternalChatTurnReceiptRow.__table__.create)
        engines.append(engine)
        return SAExternalChatTurnReceiptRepository(
            build_session_factory(engine), lease_seconds=lease_seconds,
        )

    yield factory
    for engine in engines:
        await engine.dispose()


async def _force_expire(repo) -> None:
    """Back-date every receipt's lease so it is takeover-eligible now."""
    if isinstance(repo, InMemoryExternalChatTurnReceiptRepository):
        for record in repo._records.values():
            if record.lease_until is not None:
                record.lease_until = _PAST
        return
    async with repo._session_factory() as session:
        await session.execute(
            update(ExternalChatTurnReceiptRow).values(lease_until=_PAST),
        )
        await session.commit()


def _claim_kwargs(**over):
    base = dict(
        authenticated_caller="cloud-channel",
        request_id="req-1",
        canonical_request_hash="hash-a",
        canonical_hash_version=1,
        contract_version=1,
        tenant_id="tenant-1",
        account_id="acct-1",
        character_id="char-1",
        requested_conversation_id=None,
    )
    base.update(over)
    return base


# -- fresh claim + full happy path ----------------------------------------- #


async def test_fresh_claim_returns_claimed_processing(make_repo) -> None:
    repo = await make_repo()
    result = await repo.claim_or_get(**_claim_kwargs())
    assert isinstance(result, Claimed)
    r = result.receipt
    assert r.state is ExternalChatTurnState.PROCESSING
    assert r.lease_version == 1
    assert r.attempt_count == 0
    assert r.owner_id
    assert r.turn_id
    assert r.channel == "line"
    assert r.canonical_request_hash == "hash-a"
    assert r.post_turn_state == "pending"


async def test_full_state_transitions_to_completed(make_repo) -> None:
    repo = await make_repo()
    claimed = await repo.claim_or_get(**_claim_kwargs())
    r = claimed.receipt
    fence = dict(turn_id=r.turn_id, owner_id=r.owner_id, lease_version=1)

    assert await repo.record_conversation(**fence, conversation_id="conv-1")
    assert await repo.record_user_turn(**fence, user_message_row_id=11)
    assert await repo.mark_generated(**fence, generated_snapshot_json="G")
    assert await repo.mark_committed(
        **fence,
        assistant_message_row_id=22,
        assistant_position=3,
        response_snapshot_json="R",
    )
    assert await repo.mark_completed(**fence)

    final = await repo.get(r.turn_id)
    assert final.state is ExternalChatTurnState.COMPLETED
    assert final.conversation_id == "conv-1"
    assert final.user_message_row_id == 11
    assert final.assistant_message_row_id == 22
    assert final.assistant_position == 3
    assert final.generated_snapshot_json == "G"
    assert final.response_snapshot_json == "R"
    assert final.generated_at is not None
    assert final.committed_at is not None
    assert final.completed_at is not None


# -- replays --------------------------------------------------------------- #


async def test_completed_replays_response_snapshot(make_repo) -> None:
    repo = await make_repo()
    claimed = await repo.claim_or_get(**_claim_kwargs())
    r = claimed.receipt
    fence = dict(turn_id=r.turn_id, owner_id=r.owner_id, lease_version=1)
    await repo.mark_generated(**fence, generated_snapshot_json="G")
    await repo.mark_committed(
        **fence,
        assistant_message_row_id=22,
        assistant_position=1,
        response_snapshot_json="R-final",
    )
    await repo.mark_completed(**fence)

    replay = await repo.claim_or_get(**_claim_kwargs())
    assert isinstance(replay, CompletedReplay)
    assert replay.response_snapshot_json == "R-final"
    assert replay.receipt.turn_id == r.turn_id


async def test_terminal_failure_replays_error(make_repo) -> None:
    repo = await make_repo()
    claimed = await repo.claim_or_get(**_claim_kwargs())
    r = claimed.receipt
    fence = dict(turn_id=r.turn_id, owner_id=r.owner_id, lease_version=1)
    assert await repo.mark_failed(
        **fence,
        retryable=False,
        error_code="character_mismatch",
        error_message="conversation belongs to another character",
        terminal_error_json='{"code":"character_mismatch"}',
    )

    replay = await repo.claim_or_get(**_claim_kwargs())
    assert isinstance(replay, TerminalReplay)
    assert replay.terminal_error_json == '{"code":"character_mismatch"}'
    assert replay.receipt.state is ExternalChatTurnState.FAILED_TERMINAL


# -- hash conflict --------------------------------------------------------- #


async def test_same_id_different_hash_is_conflict(make_repo) -> None:
    repo = await make_repo()
    await repo.claim_or_get(**_claim_kwargs(canonical_request_hash="hash-a"))
    conflict = await repo.claim_or_get(
        **_claim_kwargs(canonical_request_hash="hash-b"),
    )
    assert isinstance(conflict, HashConflict)


# -- lease liveness -------------------------------------------------------- #


async def test_live_lease_reports_in_progress(make_repo) -> None:
    repo = await make_repo()
    await repo.claim_or_get(**_claim_kwargs())
    second = await repo.claim_or_get(**_claim_kwargs())
    assert isinstance(second, InProgress)
    assert second.receipt.state is ExternalChatTurnState.PROCESSING


async def test_expired_processing_is_taken_over_with_attempt_charged(
    make_repo,
) -> None:
    repo = await make_repo()
    first = await repo.claim_or_get(**_claim_kwargs())
    r0 = first.receipt
    fence = dict(turn_id=r0.turn_id, owner_id=r0.owner_id, lease_version=1)
    # Anchors durable under the live lease must survive the takeover.
    await repo.record_conversation(**fence, conversation_id="conv-9")
    await repo.record_user_turn(**fence, user_message_row_id=77)

    await _force_expire(repo)

    second = await repo.claim_or_get(**_claim_kwargs())
    assert isinstance(second, Claimed)
    r1 = second.receipt
    assert r1.turn_id == r0.turn_id  # same turn, not a new one
    assert r1.attempt_count == 1  # attempt charged on takeover
    assert r1.lease_version == 2  # fencing token bumped
    assert r1.owner_id != r0.owner_id  # new owner
    assert r1.state is ExternalChatTurnState.PROCESSING
    # Existing user/conversation anchors carried into the takeover.
    assert r1.conversation_id == "conv-9"
    assert r1.user_message_row_id == 77


async def test_expired_owner_cannot_write_before_takeover(make_repo) -> None:
    repo = await make_repo()
    first = await repo.claim_or_get(**_claim_kwargs())
    r = first.receipt
    await _force_expire(repo)
    # The original owner still nominally holds the row, but an expired lease
    # is not writable — it must be re-claimed.
    ok = await repo.record_conversation(
        turn_id=r.turn_id, owner_id=r.owner_id, lease_version=1,
        conversation_id="x",
    )
    assert ok is False


# -- fencing --------------------------------------------------------------- #


async def test_stale_owner_and_wrong_version_are_rejected(make_repo) -> None:
    repo = await make_repo()
    first = await repo.claim_or_get(**_claim_kwargs())
    r = first.receipt

    # Wrong owner id → rejected.
    assert not await repo.record_user_turn(
        turn_id=r.turn_id, owner_id="not-the-owner", lease_version=1,
        user_message_row_id=1,
    )
    # Wrong lease_version → rejected.
    assert not await repo.record_user_turn(
        turn_id=r.turn_id, owner_id=r.owner_id, lease_version=999,
        user_message_row_id=1,
    )
    # Correct fence → applied.
    assert await repo.record_user_turn(
        turn_id=r.turn_id, owner_id=r.owner_id, lease_version=1,
        user_message_row_id=1,
    )

    # After a takeover the OLD fence is stale and the NEW one works.
    await _force_expire(repo)
    taken = await repo.claim_or_get(**_claim_kwargs())
    new = taken.receipt
    assert not await repo.record_user_turn(
        turn_id=r.turn_id, owner_id=r.owner_id, lease_version=1,
        user_message_row_id=2,
    )
    assert await repo.record_user_turn(
        turn_id=new.turn_id, owner_id=new.owner_id,
        lease_version=new.lease_version, user_message_row_id=2,
    )


# -- terminal immutability ------------------------------------------------- #


async def test_terminal_state_is_immutable(make_repo) -> None:
    repo = await make_repo()
    first = await repo.claim_or_get(**_claim_kwargs())
    r = first.receipt
    fence = dict(turn_id=r.turn_id, owner_id=r.owner_id, lease_version=1)
    await repo.mark_failed(
        **fence, retryable=False, error_code="e", error_message="m",
        terminal_error_json="{}",
    )
    # No mutation can touch a FAILED_TERMINAL receipt.
    assert not await repo.record_user_turn(**fence, user_message_row_id=1)
    assert not await repo.mark_generated(**fence, generated_snapshot_json="G")
    assert not await repo.mark_completed(**fence)
    assert not await repo.mark_failed(
        **fence, retryable=True, error_code="e2", error_message="m2",
    )
    assert not await repo.heartbeat_lease(**fence)


async def test_completed_snapshot_is_never_overwritten(make_repo) -> None:
    repo = await make_repo()
    first = await repo.claim_or_get(**_claim_kwargs())
    r = first.receipt
    fence = dict(turn_id=r.turn_id, owner_id=r.owner_id, lease_version=1)
    await repo.mark_generated(**fence, generated_snapshot_json="G")
    await repo.mark_committed(
        **fence, assistant_message_row_id=1, assistant_position=0,
        response_snapshot_json="ORIGINAL",
    )
    await repo.mark_completed(**fence)

    # A late commit attempt against the completed turn cannot re-write it.
    assert not await repo.mark_committed(
        **fence, assistant_message_row_id=2, assistant_position=1,
        response_snapshot_json="TAMPERED",
    )
    final = await repo.get(r.turn_id)
    assert final.state is ExternalChatTurnState.COMPLETED
    assert final.response_snapshot_json == "ORIGINAL"


# -- recovery variants ----------------------------------------------------- #


async def test_expired_generated_returns_generated_recovery(make_repo) -> None:
    repo = await make_repo()
    first = await repo.claim_or_get(**_claim_kwargs())
    r = first.receipt
    fence = dict(turn_id=r.turn_id, owner_id=r.owner_id, lease_version=1)
    await repo.mark_generated(**fence, generated_snapshot_json="SNAP")

    await _force_expire(repo)

    recovery = await repo.claim_or_get(**_claim_kwargs())
    assert isinstance(recovery, GeneratedRecovery)
    rr = recovery.receipt
    assert rr.state is ExternalChatTurnState.GENERATED  # LLM never re-run
    assert rr.generated_snapshot_json == "SNAP"
    assert rr.attempt_count == 1
    assert rr.lease_version == 2
    assert rr.owner_id != r.owner_id


async def test_committed_turn_recovers_to_finalize(make_repo) -> None:
    repo = await make_repo()
    first = await repo.claim_or_get(**_claim_kwargs())
    r = first.receipt
    fence = dict(turn_id=r.turn_id, owner_id=r.owner_id, lease_version=1)
    await repo.mark_generated(**fence, generated_snapshot_json="G")
    await repo.mark_committed(
        **fence, assistant_message_row_id=5, assistant_position=2,
        response_snapshot_json="R",
    )

    recovery = await repo.claim_or_get(**_claim_kwargs())
    assert isinstance(recovery, CommittedRecovery)
    rr = recovery.receipt
    assert rr.state is ExternalChatTurnState.COMMITTED
    assert rr.assistant_message_row_id == 5
    assert rr.response_snapshot_json == "R"
    assert rr.owner_id != r.owner_id

    # The stale original owner can no longer finalize; the recoverer can.
    assert not await repo.mark_completed(**fence)
    assert await repo.mark_completed(
        turn_id=rr.turn_id, owner_id=rr.owner_id,
        lease_version=rr.lease_version,
    )


async def test_failed_retryable_is_taken_over_on_same_turn(make_repo) -> None:
    repo = await make_repo()
    first = await repo.claim_or_get(**_claim_kwargs())
    r = first.receipt
    fence = dict(turn_id=r.turn_id, owner_id=r.owner_id, lease_version=1)
    await repo.record_user_turn(**fence, user_message_row_id=90)
    assert await repo.mark_failed(
        **fence, retryable=True, error_code="upstream_timeout",
        error_message="llm timed out",
    )

    retry = await repo.claim_or_get(**_claim_kwargs())
    assert isinstance(retry, Claimed)
    rr = retry.receipt
    assert rr.turn_id == r.turn_id  # same turn_id reused
    assert rr.state is ExternalChatTurnState.PROCESSING
    assert rr.user_message_row_id == 90  # user turn preserved
    assert rr.attempt_count == 1
    assert rr.owner_id != r.owner_id


# -- heartbeat + misc ------------------------------------------------------ #


async def test_heartbeat_extends_and_is_fenced(make_repo) -> None:
    repo = await make_repo()
    first = await repo.claim_or_get(**_claim_kwargs())
    r = first.receipt
    assert await repo.heartbeat_lease(
        turn_id=r.turn_id, owner_id=r.owner_id, lease_version=1,
    )
    assert not await repo.heartbeat_lease(
        turn_id=r.turn_id, owner_id="stale", lease_version=1,
    )
    assert not await repo.heartbeat_lease(
        turn_id=r.turn_id, owner_id=r.owner_id, lease_version=999,
    )
    # An already-expired lease is not extendable — must be re-claimed.
    await _force_expire(repo)
    assert not await repo.heartbeat_lease(
        turn_id=r.turn_id, owner_id=r.owner_id, lease_version=1,
    )


async def test_get_unknown_turn_returns_none(make_repo) -> None:
    repo = await make_repo()
    assert await repo.get("does-not-exist") is None


async def test_distinct_request_ids_are_independent_turns(make_repo) -> None:
    repo = await make_repo()
    a = await repo.claim_or_get(**_claim_kwargs(request_id="r-a"))
    b = await repo.claim_or_get(**_claim_kwargs(request_id="r-b"))
    assert isinstance(a, Claimed)
    assert isinstance(b, Claimed)
    assert a.receipt.turn_id != b.receipt.turn_id
