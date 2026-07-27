"""SQLAlchemy adapter for the external-chat turn receipt (LH2).

The durable side of the recoverable turn state machine (DR-LH0-004). Async
session conventions mirror ``sa_background_jobs.py``: never hold a DB
transaction across the (long) LLM call; every mutation is a single conditional
UPDATE fenced on ``owner_id`` + ``lease_version`` (a stale — taken-over — owner
fails closed with ``rowcount == 0``); lease liveness is judged by the DB clock
(``CURRENT_TIMESTAMP``), never the caller's wall clock.

``claim_or_get`` locks the existing receipt ``FOR UPDATE`` so concurrent
claimers of the same ``(authenticated_caller, request_id)`` serialise: exactly
one wins a fresh insert (enforced by the UNIQUE index) or an expired-lease
takeover; the rest observe the just-refreshed live lease and report
``InProgress``. On PostgreSQL this is a real row lock; on SQLite the whole
connection serialises writes anyway (the unit twin proves behaviour, the PG
integration suite proves the concurrency).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.contracts.external_chat_turn import (
    ACTIVE_STATES,
    CHANNEL_LINE,
    Claimed,
    ClaimOrGetResult,
    CommittedRecovery,
    CompletedReplay,
    ExternalChatTurnReceipt,
    ExternalChatTurnReceiptPort,
    ExternalChatTurnState,
    GeneratedRecovery,
    HashConflict,
    InProgress,
    TerminalReplay,
)
from kokoro_link.infrastructure.persistence.models import (
    ExternalChatTurnReceiptRow,
)

DEFAULT_LEASE_SECONDS = 300
"""Default receipt lease TTL. A claimer / recoverer must ``heartbeat_lease``
inside this window or its lease becomes takeover-eligible."""

_CLAIM_INSERT_RETRIES = 3

_Row = ExternalChatTurnReceiptRow
_S = ExternalChatTurnState
_ACTIVE_VALUES = tuple(state.value for state in ACTIVE_STATES)


class SAExternalChatTurnReceiptRepository(ExternalChatTurnReceiptPort):
    def __init__(
        self,
        session_factory: sessionmaker[AsyncSession],
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._lease_seconds = lease_seconds

    async def claim_or_get(
        self,
        *,
        authenticated_caller: str,
        request_id: str,
        canonical_request_hash: str,
        canonical_hash_version: int | None,
        contract_version: int | None,
        tenant_id: str,
        account_id: str,
        character_id: str,
        requested_conversation_id: str | None = None,
    ) -> ClaimOrGetResult:
        for _attempt in range(_CLAIM_INSERT_RETRIES):
            async with self._session_factory() as session:
                now = await self._db_now(session)
                row = (
                    await session.execute(
                        select(_Row)
                        .where(
                            _Row.authenticated_caller == authenticated_caller,
                            _Row.request_id == request_id,
                        )
                        .with_for_update(),
                    )
                ).scalar_one_or_none()

                if row is not None:
                    result = self._decide_existing(
                        row, canonical_request_hash, now,
                    )
                    await session.commit()
                    return result

                new_row = _Row(
                    turn_id=uuid4().hex,
                    authenticated_caller=authenticated_caller,
                    request_id=request_id,
                    contract_version=contract_version,
                    canonical_hash_version=canonical_hash_version,
                    canonical_request_hash=canonical_request_hash,
                    tenant_id=tenant_id,
                    account_id=account_id,
                    character_id=character_id,
                    channel=CHANNEL_LINE,
                    state=_S.PROCESSING.value,
                    owner_id=uuid4().hex,
                    lease_until=now + timedelta(seconds=self._lease_seconds),
                    lease_version=1,
                    attempt_count=0,
                    post_turn_state="pending",
                    created_at=now,
                    updated_at=now,
                )
                session.add(new_row)
                # Build the DTO BEFORE commit — ``expire_on_commit`` would evict
                # the row's attributes and re-reading them outside the async
                # greenlet raises MissingGreenlet (same discipline as the queue
                # adapter). All values are Python-set, so the view is complete.
                claimed = Claimed(_to_receipt(new_row))
                try:
                    await session.commit()
                except IntegrityError:
                    # A concurrent claimer inserted the same
                    # (caller, request_id) first — resolve the existing row on
                    # the next loop pass rather than masking a lost claim.
                    await session.rollback()
                    continue
                return claimed
        raise RuntimeError(
            "claim_or_get: exhausted insert retries against a racing claimer",
        )

    def _decide_existing(
        self,
        row: ExternalChatTurnReceiptRow,
        canonical_request_hash: str,
        now: datetime,
    ) -> ClaimOrGetResult:
        if row.canonical_request_hash != canonical_request_hash:
            return HashConflict(_to_receipt(row))

        state = ExternalChatTurnState(row.state)
        if state is _S.COMPLETED:
            return CompletedReplay(_to_receipt(row), row.response_snapshot_json)
        if state is _S.FAILED_TERMINAL:
            return TerminalReplay(_to_receipt(row), row.terminal_error_json)

        lease_live = (
            row.lease_until is not None
            and ensure_utc(row.lease_until) >= now
        )

        if state is _S.COMMITTED:
            # Assistant already durably appended; recover to finalize only.
            self._takeover(row, now)
            return CommittedRecovery(_to_receipt(row))
        if state is _S.GENERATED:
            if lease_live:
                return InProgress(_to_receipt(row))
            # Expired GENERATED: finalize from snapshot — NEVER re-run the LLM.
            self._takeover(row, now)
            return GeneratedRecovery(_to_receipt(row))
        if state is _S.PROCESSING:
            if lease_live:
                return InProgress(_to_receipt(row))
            # Expired PROCESSING (no durable GENERATED): re-run is allowed.
            self._takeover(row, now)
            return Claimed(_to_receipt(row))
        if state is _S.FAILED_RETRYABLE:
            self._takeover(row, now)
            row.state = _S.PROCESSING.value
            return Claimed(_to_receipt(row))

        raise AssertionError(f"unhandled receipt state {state!r}")

    def _takeover(self, row: ExternalChatTurnReceiptRow, now: datetime) -> None:
        row.owner_id = uuid4().hex
        row.lease_version += 1
        row.attempt_count += 1
        row.lease_until = now + timedelta(seconds=self._lease_seconds)
        row.updated_at = now

    async def record_conversation(
        self,
        *,
        turn_id: str,
        owner_id: str,
        lease_version: int,
        conversation_id: str,
    ) -> bool:
        return await self._fenced_update(
            turn_id,
            owner_id,
            lease_version,
            from_states=(_S.PROCESSING.value,),
            values={"conversation_id": conversation_id},
        )

    async def record_user_turn(
        self,
        *,
        turn_id: str,
        owner_id: str,
        lease_version: int,
        user_message_row_id: int,
    ) -> bool:
        return await self._fenced_update(
            turn_id,
            owner_id,
            lease_version,
            from_states=(_S.PROCESSING.value,),
            values={"user_message_row_id": user_message_row_id},
        )

    async def mark_generated(
        self,
        *,
        turn_id: str,
        owner_id: str,
        lease_version: int,
        generated_snapshot_json: str,
    ) -> bool:
        return await self._fenced_update(
            turn_id,
            owner_id,
            lease_version,
            from_states=(_S.PROCESSING.value,),
            values={
                "state": _S.GENERATED.value,
                "generated_snapshot_json": generated_snapshot_json,
                "generated_at": func.current_timestamp(),
            },
        )

    async def mark_committed(
        self,
        *,
        turn_id: str,
        owner_id: str,
        lease_version: int,
        assistant_message_row_id: int,
        assistant_position: int,
        response_snapshot_json: str,
    ) -> bool:
        return await self._fenced_update(
            turn_id,
            owner_id,
            lease_version,
            from_states=(_S.PROCESSING.value, _S.GENERATED.value),
            values={
                "state": _S.COMMITTED.value,
                "assistant_message_row_id": assistant_message_row_id,
                "assistant_position": assistant_position,
                "response_snapshot_json": response_snapshot_json,
                "committed_at": func.current_timestamp(),
            },
        )

    async def mark_completed(
        self,
        *,
        turn_id: str,
        owner_id: str,
        lease_version: int,
    ) -> bool:
        # COMMITTED → COMPLETED. ``response_snapshot_json`` is deliberately NOT
        # in ``values`` — a completed snapshot is never overwritten.
        return await self._fenced_update(
            turn_id,
            owner_id,
            lease_version,
            from_states=(_S.COMMITTED.value,),
            values={
                "state": _S.COMPLETED.value,
                "completed_at": func.current_timestamp(),
            },
        )

    async def mark_failed(
        self,
        *,
        turn_id: str,
        owner_id: str,
        lease_version: int,
        retryable: bool,
        error_code: str,
        error_message: str,
        terminal_error_json: str | None = None,
    ) -> bool:
        values: dict[str, object] = {
            "state": (
                _S.FAILED_RETRYABLE.value
                if retryable
                else _S.FAILED_TERMINAL.value
            ),
            "last_error_code": error_code,
            "last_error_message": error_message,
            # The failing owner releases the lease; a FAILED_RETRYABLE turn is
            # re-taken on the same turn_id via ``claim_or_get``.
            "owner_id": None,
            "lease_until": None,
        }
        if not retryable:
            values["terminal_error_json"] = terminal_error_json
        return await self._fenced_update(
            turn_id,
            owner_id,
            lease_version,
            from_states=(_S.PROCESSING.value, _S.GENERATED.value),
            values=values,
        )

    async def heartbeat_lease(
        self,
        *,
        turn_id: str,
        owner_id: str,
        lease_version: int,
    ) -> bool:
        async with self._session_factory() as session:
            now = await self._db_now(session)
            result = await session.execute(
                update(_Row)
                .where(
                    _Row.turn_id == turn_id,
                    _Row.owner_id == owner_id,
                    _Row.lease_version == lease_version,
                    _Row.state.in_(_ACTIVE_VALUES),
                    _Row.lease_until.is_not(None),
                    _Row.lease_until >= func.current_timestamp(),
                )
                .values(
                    lease_until=now + timedelta(seconds=self._lease_seconds),
                    updated_at=func.current_timestamp(),
                ),
            )
            await session.commit()
            return result.rowcount == 1

    async def get(self, turn_id: str) -> ExternalChatTurnReceipt | None:
        async with self._session_factory() as session:
            row = await session.get(_Row, turn_id)
            return _to_receipt(row) if row is not None else None

    async def _fenced_update(
        self,
        turn_id: str,
        owner_id: str,
        lease_version: int,
        *,
        from_states: tuple[str, ...],
        values: dict[str, object],
    ) -> bool:
        """Single conditional UPDATE, fenced on owner + lease_version + a live
        lease + an allowed source state. ``rowcount == 1`` iff the fenced owner
        still held the turn in an allowed state with an unexpired lease."""
        payload = dict(values)
        payload.setdefault("updated_at", func.current_timestamp())
        async with self._session_factory() as session:
            result = await session.execute(
                update(_Row)
                .where(
                    _Row.turn_id == turn_id,
                    _Row.owner_id == owner_id,
                    _Row.lease_version == lease_version,
                    _Row.state.in_(from_states),
                    _Row.lease_until.is_not(None),
                    _Row.lease_until >= func.current_timestamp(),
                )
                .values(**payload),
            )
            await session.commit()
            return result.rowcount == 1

    @staticmethod
    async def _db_now(session: AsyncSession) -> datetime:
        raw = (
            await session.execute(select(func.current_timestamp()))
        ).scalar_one()
        if isinstance(raw, str):
            # SQLite renders CURRENT_TIMESTAMP as 'YYYY-MM-DD HH:MM:SS'.
            raw = datetime.fromisoformat(raw)
        return ensure_utc(raw)


def _to_receipt(row: ExternalChatTurnReceiptRow) -> ExternalChatTurnReceipt:
    return ExternalChatTurnReceipt(
        turn_id=row.turn_id,
        authenticated_caller=row.authenticated_caller,
        request_id=row.request_id,
        contract_version=row.contract_version,
        canonical_hash_version=row.canonical_hash_version,
        canonical_request_hash=row.canonical_request_hash,
        tenant_id=row.tenant_id,
        account_id=row.account_id,
        character_id=row.character_id,
        channel=row.channel,
        state=ExternalChatTurnState(row.state),
        owner_id=row.owner_id,
        lease_until=_aware(row.lease_until),
        lease_version=row.lease_version,
        attempt_count=row.attempt_count,
        conversation_id=row.conversation_id,
        user_message_row_id=row.user_message_row_id,
        assistant_message_row_id=row.assistant_message_row_id,
        assistant_position=row.assistant_position,
        generated_snapshot_json=row.generated_snapshot_json,
        response_snapshot_json=row.response_snapshot_json,
        terminal_error_json=row.terminal_error_json,
        post_turn_state=row.post_turn_state,
        post_turn_job_key=row.post_turn_job_key,
        last_error_code=row.last_error_code,
        last_error_message=row.last_error_message,
        created_at=ensure_utc(row.created_at),
        updated_at=ensure_utc(row.updated_at),
        generated_at=_aware(row.generated_at),
        committed_at=_aware(row.committed_at),
        completed_at=_aware(row.completed_at),
    )


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return ensure_utc(value)
