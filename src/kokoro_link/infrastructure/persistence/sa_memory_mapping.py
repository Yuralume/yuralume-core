"""Bidirectional mapping between ``MemoryItem`` and ``MemoryItemRow``.

Kept separate from the repository to make the domain/ORM boundary
explicit and to let unit tests exercise the mapping in isolation.
"""

import json
from datetime import datetime, timezone

from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.actor import ParticipantRef
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.persistence.models import MemoryItemRow


def _ensure_utc(value: datetime | None) -> datetime | None:
    """Defensive: reattach UTC tzinfo at the domain boundary if missing."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def item_to_row(item: MemoryItem) -> MemoryItemRow:
    return MemoryItemRow(
        id=item.id,
        character_id=item.character_id,
        conversation_id=item.conversation_id,
        kind=item.kind.value,
        content=item.content,
        salience=item.salience,
        tags=json.dumps(list(item.tags)),
        created_at=item.created_at,
        last_accessed_at=item.last_accessed_at,
        access_count=item.access_count,
        embedding=list(item.embedding) if item.embedding is not None else None,
        tags_embedding=(
            list(item.tags_embedding) if item.tags_embedding is not None else None
        ),
        participants_json=json.dumps(
            [p.to_dict() for p in item.participants], ensure_ascii=False,
        ),
        world_id=item.world_id,
        location=item.location,
        audience=item.audience or "",
    )


def row_to_item(row: MemoryItemRow) -> MemoryItem:
    try:
        tags_raw = json.loads(row.tags) if row.tags else []
    except json.JSONDecodeError:
        tags_raw = []
    tags = tuple(str(tag) for tag in tags_raw if isinstance(tag, (str, int, float)))
    created_at = _ensure_utc(row.created_at)
    assert created_at is not None  # column is NOT NULL
    embedding = _coerce_vector(row.embedding)
    tags_embedding = _coerce_vector(row.tags_embedding)
    participants = _coerce_participants(row.participants_json)
    return MemoryItem(
        id=row.id,
        character_id=row.character_id,
        conversation_id=row.conversation_id,
        kind=MemoryKind.from_string(row.kind),
        content=row.content,
        salience=float(row.salience),
        tags=tags,
        created_at=created_at,
        last_accessed_at=_ensure_utc(row.last_accessed_at),
        access_count=int(row.access_count),
        embedding=embedding,
        tags_embedding=tags_embedding,
        participants=participants,
        world_id=row.world_id,
        location=row.location,
        audience=row.audience or "",
    )


def _coerce_vector(raw) -> tuple[float, ...] | None:
    """Driver-agnostic pgvector → ``tuple[float, ...]`` coercion.

    pgvector returns a numpy array via asyncpg or a list via psycopg2;
    we flatten both to a plain float tuple so the domain layer never
    has to import numpy. Bad values fall through as ``None`` rather
    than crash the read path."""
    if raw is None:
        return None
    try:
        return tuple(float(v) for v in raw)
    except (TypeError, ValueError):
        return None


def _coerce_participants(raw: str | None) -> tuple[ParticipantRef, ...]:
    """Decode ``participants_json`` back into typed refs.

    Any malformed entry is dropped silently — the read path stays
    forgiving so a single bad row from manual DB editing doesn't break
    every query that touches it. The migration's ``server_default``
    pre-fills ``[]`` for legacy rows so the JSON is never NULL."""
    if not raw:
        return ()
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(decoded, list):
        return ()
    refs: list[ParticipantRef] = []
    for entry in decoded:
        if not isinstance(entry, dict):
            continue
        ref = ParticipantRef.from_dict(entry)
        if ref is not None:
            refs.append(ref)
    return tuple(refs)
