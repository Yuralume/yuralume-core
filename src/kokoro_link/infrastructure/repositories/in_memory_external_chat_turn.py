"""In-memory external-chat turn receipt store — first-class test citizen.

A full-fidelity twin of :class:`SAExternalChatTurnReceiptRepository`: same
claim / replay / recovery dispatch, the same ``owner_id`` + ``lease_version``
fencing, the same terminal immutability and never-overwrite-a-completed-snapshot
rules — so the shared contract suite (``tests/unit``) exercises the real state
machine without a database. Its clock is the process wall clock (the SA twin's
DB clock); both judge lease liveness against a single ``now`` read per
operation, so a receipt built with a negative lease TTL is immediately
takeover-eligible on either backend.

See ``contracts/external_chat_turn.py``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

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

DEFAULT_LEASE_SECONDS = 300

_S = ExternalChatTurnState


@dataclass(slots=True)
class _ReceiptRecord:
    """Mutable in-memory row (converted to a frozen view on read)."""

    turn_id: str
    authenticated_caller: str
    request_id: str
    contract_version: int | None
    canonical_hash_version: int | None
    canonical_request_hash: str
    tenant_id: str
    account_id: str
    character_id: str
    channel: str
    state: ExternalChatTurnState
    owner_id: str | None
    lease_until: datetime | None
    lease_version: int
    attempt_count: int
    created_at: datetime
    updated_at: datetime
    conversation_id: str | None = None
    user_message_row_id: int | None = None
    assistant_message_row_id: int | None = None
    assistant_position: int | None = None
    generated_snapshot_json: str | None = None
    response_snapshot_json: str | None = None
    terminal_error_json: str | None = None
    post_turn_state: str = "pending"
    post_turn_job_key: str | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    generated_at: datetime | None = None
    committed_at: datetime | None = None
    completed_at: datetime | None = None


class InMemoryExternalChatTurnReceiptRepository(ExternalChatTurnReceiptPort):
    def __init__(self, *, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> None:
        self._records: dict[str, _ReceiptRecord] = {}
        self._by_request: dict[tuple[str, str], str] = {}
        self._lease_seconds = lease_seconds
        self._mutex = asyncio.Lock()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

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
        async with self._mutex:
            now = self._now()
            key = (authenticated_caller, request_id)
            existing_id = self._by_request.get(key)
            if existing_id is not None:
                return self._decide_existing(
                    self._records[existing_id], canonical_request_hash, now,
                )
            turn_id = uuid4().hex
            record = _ReceiptRecord(
                turn_id=turn_id,
                authenticated_caller=authenticated_caller,
                request_id=request_id,
                contract_version=contract_version,
                canonical_hash_version=canonical_hash_version,
                canonical_request_hash=canonical_request_hash,
                tenant_id=tenant_id,
                account_id=account_id,
                character_id=character_id,
                channel=CHANNEL_LINE,
                state=_S.PROCESSING,
                owner_id=uuid4().hex,
                lease_until=now + timedelta(seconds=self._lease_seconds),
                lease_version=1,
                attempt_count=0,
                created_at=now,
                updated_at=now,
            )
            self._records[turn_id] = record
            self._by_request[key] = turn_id
            return Claimed(_to_receipt(record))

    def _decide_existing(
        self,
        record: _ReceiptRecord,
        canonical_request_hash: str,
        now: datetime,
    ) -> ClaimOrGetResult:
        if record.canonical_request_hash != canonical_request_hash:
            return HashConflict(_to_receipt(record))

        state = record.state
        if state is _S.COMPLETED:
            return CompletedReplay(
                _to_receipt(record), record.response_snapshot_json,
            )
        if state is _S.FAILED_TERMINAL:
            return TerminalReplay(
                _to_receipt(record), record.terminal_error_json,
            )

        lease_live = (
            record.lease_until is not None
            and ensure_utc(record.lease_until) >= now
        )

        if state is _S.COMMITTED:
            self._takeover(record, now)
            return CommittedRecovery(_to_receipt(record))
        if state is _S.GENERATED:
            if lease_live:
                return InProgress(_to_receipt(record))
            self._takeover(record, now)
            return GeneratedRecovery(_to_receipt(record))
        if state is _S.PROCESSING:
            if lease_live:
                return InProgress(_to_receipt(record))
            self._takeover(record, now)
            return Claimed(_to_receipt(record))
        if state is _S.FAILED_RETRYABLE:
            self._takeover(record, now)
            record.state = _S.PROCESSING
            return Claimed(_to_receipt(record))

        raise AssertionError(f"unhandled receipt state {state!r}")

    def _takeover(self, record: _ReceiptRecord, now: datetime) -> None:
        record.owner_id = uuid4().hex
        record.lease_version += 1
        record.attempt_count += 1
        record.lease_until = now + timedelta(seconds=self._lease_seconds)
        record.updated_at = now

    async def record_conversation(
        self,
        *,
        turn_id: str,
        owner_id: str,
        lease_version: int,
        conversation_id: str,
    ) -> bool:
        async with self._mutex:
            record = self._fenced(
                turn_id, owner_id, lease_version, (_S.PROCESSING,),
            )
            if record is None:
                return False
            record.conversation_id = conversation_id
            record.updated_at = self._now()
            return True

    async def record_user_turn(
        self,
        *,
        turn_id: str,
        owner_id: str,
        lease_version: int,
        user_message_row_id: int,
    ) -> bool:
        async with self._mutex:
            record = self._fenced(
                turn_id, owner_id, lease_version, (_S.PROCESSING,),
            )
            if record is None:
                return False
            record.user_message_row_id = user_message_row_id
            record.updated_at = self._now()
            return True

    async def mark_generated(
        self,
        *,
        turn_id: str,
        owner_id: str,
        lease_version: int,
        generated_snapshot_json: str,
    ) -> bool:
        async with self._mutex:
            record = self._fenced(
                turn_id, owner_id, lease_version, (_S.PROCESSING,),
            )
            if record is None:
                return False
            now = self._now()
            record.state = _S.GENERATED
            record.generated_snapshot_json = generated_snapshot_json
            record.generated_at = now
            record.updated_at = now
            return True

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
        async with self._mutex:
            record = self._fenced(
                turn_id,
                owner_id,
                lease_version,
                (_S.PROCESSING, _S.GENERATED),
            )
            if record is None:
                return False
            now = self._now()
            record.state = _S.COMMITTED
            record.assistant_message_row_id = assistant_message_row_id
            record.assistant_position = assistant_position
            record.response_snapshot_json = response_snapshot_json
            record.committed_at = now
            record.updated_at = now
            return True

    async def mark_completed(
        self,
        *,
        turn_id: str,
        owner_id: str,
        lease_version: int,
    ) -> bool:
        async with self._mutex:
            record = self._fenced(
                turn_id, owner_id, lease_version, (_S.COMMITTED,),
            )
            if record is None:
                return False
            now = self._now()
            # response_snapshot is left untouched — never overwritten.
            record.state = _S.COMPLETED
            record.completed_at = now
            record.updated_at = now
            return True

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
        async with self._mutex:
            record = self._fenced(
                turn_id,
                owner_id,
                lease_version,
                (_S.PROCESSING, _S.GENERATED),
            )
            if record is None:
                return False
            now = self._now()
            record.state = (
                _S.FAILED_RETRYABLE if retryable else _S.FAILED_TERMINAL
            )
            record.last_error_code = error_code
            record.last_error_message = error_message
            record.owner_id = None
            record.lease_until = None
            if not retryable:
                record.terminal_error_json = terminal_error_json
            record.updated_at = now
            return True

    async def heartbeat_lease(
        self,
        *,
        turn_id: str,
        owner_id: str,
        lease_version: int,
    ) -> bool:
        async with self._mutex:
            record = self._fenced(
                turn_id, owner_id, lease_version, tuple(ACTIVE_STATES),
            )
            if record is None:
                return False
            now = self._now()
            record.lease_until = now + timedelta(seconds=self._lease_seconds)
            record.updated_at = now
            return True

    async def get(self, turn_id: str) -> ExternalChatTurnReceipt | None:
        record = self._records.get(turn_id)
        return _to_receipt(record) if record is not None else None

    def _fenced(
        self,
        turn_id: str,
        owner_id: str,
        lease_version: int,
        from_states: tuple[ExternalChatTurnState, ...],
    ) -> _ReceiptRecord | None:
        """Return the record iff the caller is the fenced owner, in an allowed
        source state, with an unexpired lease — else ``None`` (mirrors the SA
        adapter's conditional-UPDATE ``rowcount == 0``)."""
        record = self._records.get(turn_id)
        if record is None:
            return None
        if record.owner_id != owner_id or record.lease_version != lease_version:
            return None
        if record.state not in from_states:
            return None
        if (
            record.lease_until is None
            or ensure_utc(record.lease_until) < self._now()
        ):
            return None
        return record


def _to_receipt(record: _ReceiptRecord) -> ExternalChatTurnReceipt:
    return ExternalChatTurnReceipt(
        turn_id=record.turn_id,
        authenticated_caller=record.authenticated_caller,
        request_id=record.request_id,
        contract_version=record.contract_version,
        canonical_hash_version=record.canonical_hash_version,
        canonical_request_hash=record.canonical_request_hash,
        tenant_id=record.tenant_id,
        account_id=record.account_id,
        character_id=record.character_id,
        channel=record.channel,
        state=record.state,
        owner_id=record.owner_id,
        lease_until=record.lease_until,
        lease_version=record.lease_version,
        attempt_count=record.attempt_count,
        conversation_id=record.conversation_id,
        user_message_row_id=record.user_message_row_id,
        assistant_message_row_id=record.assistant_message_row_id,
        assistant_position=record.assistant_position,
        generated_snapshot_json=record.generated_snapshot_json,
        response_snapshot_json=record.response_snapshot_json,
        terminal_error_json=record.terminal_error_json,
        post_turn_state=record.post_turn_state,
        post_turn_job_key=record.post_turn_job_key,
        last_error_code=record.last_error_code,
        last_error_message=record.last_error_message,
        created_at=record.created_at,
        updated_at=record.updated_at,
        generated_at=record.generated_at,
        committed_at=record.committed_at,
        completed_at=record.completed_at,
    )
