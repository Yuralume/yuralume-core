"""In-process turn-record repository for dev / tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from kokoro_link.contracts.observability import (
    LatencyBucket,
    TurnRecordRepositoryPort,
)
from kokoro_link.domain.entities.turn_record import TurnKind, TurnRecord
from kokoro_link.infrastructure.persistence.sa_turn_record_repository import (
    _bucketize,
)


class InMemoryTurnRecordRepository(TurnRecordRepositoryPort):
    def __init__(self) -> None:
        self._rows: list[TurnRecord] = []

    async def add(self, record: TurnRecord) -> None:
        self._rows.append(record)

    async def get(self, record_id: str) -> TurnRecord | None:
        for row in self._rows:
            if row.id == record_id:
                return row
        return None

    async def list_recent(
        self,
        *,
        character_id: str | None = None,
        kind: TurnKind | None = None,
        since: datetime | None = None,
        operator_feedback_kind: str | None = None,
        exclude_content_mode: str | None = None,
        limit: int = 50,
    ) -> list[TurnRecord]:
        excluded = _normalize_content_mode(exclude_content_mode)
        matches = [
            r for r in self._rows
            if (character_id is None or r.character_id == character_id)
            and (kind is None or r.kind == kind)
            and (since is None or r.created_at >= since)
            and (
                operator_feedback_kind is None
                or r.operator_feedback.get("kind") == operator_feedback_kind
            )
            and (
                excluded is None
                or _record_content_mode(r) != excluded
            )
        ]
        matches.sort(key=lambda r: r.created_at, reverse=True)
        return matches[:limit]

    async def update_operator_feedback(
        self,
        record_id: str,
        feedback: dict[str, object],
    ) -> TurnRecord | None:
        for index, row in enumerate(self._rows):
            if row.id != record_id:
                continue
            updated = replace(row, operator_feedback=dict(feedback))
            self._rows[index] = updated
            return updated
        return None

    async def latency_histogram(
        self,
        *,
        character_id: str | None = None,
        kind: TurnKind | None = None,
        since: datetime | None = None,
        buckets_ms: tuple[int, ...] = (50, 200, 500, 1000, 3000),
    ) -> list[LatencyBucket]:
        latencies = [
            r.latency_ms
            for r in self._rows
            if r.latency_ms is not None
            and (character_id is None or r.character_id == character_id)
            and (kind is None or r.kind == kind)
            and (since is None or r.created_at >= since)
        ]
        return _bucketize([int(v) for v in latencies], buckets_ms)


def _record_content_mode(record: TurnRecord) -> str | None:
    raw = record.post_turn_refs.get("content_mode")
    return _normalize_content_mode(raw)


def _normalize_content_mode(value: object) -> str | None:
    if value is None:
        return None
    return str(value).strip().lower() or None
