"""SQLAlchemy store for the post-turn-budgeted material digest.

Session-per-call, like the rest of the repositories here. The upsert is
written portably (predicated ``UPDATE``, then ``INSERT`` on a miss, then
one retry) rather than with a dialect ``ON CONFLICT``: production is
PostgreSQL but every unit test of this class runs it against SQLite, and
a store whose only real implementation cannot be exercised in a unit test
is a store whose behaviour is asserted nowhere.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, insert, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.prompt_material_digest import (
    PromptMaterialDigest,
    PromptMaterialDigestStorePort,
    StoredPromptMaterialDigest,
)
from kokoro_link.infrastructure.persistence.models import (
    PromptMaterialDigestRow,
)

_LOGGER = logging.getLogger(__name__)


def _as_utc(value: datetime) -> datetime:
    return (
        value.astimezone(timezone.utc)
        if value.tzinfo
        else value.replace(tzinfo=timezone.utc)
    )


def encode_digest(digest: PromptMaterialDigest) -> str:
    """Lossless JSON for one digest.

    ``ensure_ascii=False`` because the bullets are Chinese / Japanese
    prose and escaping every character would triple the column for no
    benefit. Both fields are written even when empty so the decoder never
    has to guess whether a missing key meant "empty" or "old row".
    """
    return json.dumps(
        {
            "bullets": list(digest.bullets),
            "digest_metadata": dict(digest.digest_metadata or {}),
        },
        ensure_ascii=False,
    )


def decode_digest(payload: str) -> PromptMaterialDigest | None:
    """The inverse, or ``None`` for anything unreadable.

    A row that will not decode is treated exactly like an absent one: the
    turn renders the source blocks and the next post-turn overwrites it.
    Raising here would turn a corrupt cache row into a dead chat turn.
    """
    try:
        obj: Any = json.loads(payload or "")
    except (TypeError, ValueError):
        _LOGGER.warning("prompt material digest row is not valid JSON")
        return None
    if not isinstance(obj, dict):
        return None
    raw_bullets = obj.get("bullets")
    if not isinstance(raw_bullets, list):
        return None
    bullets = tuple(item for item in raw_bullets if isinstance(item, str))
    if not bullets:
        # An empty digest is indistinguishable from no digest to every
        # consumer, so it is stored by nobody and read as nothing.
        return None
    metadata = obj.get("digest_metadata")
    return PromptMaterialDigest(
        bullets=bullets,
        digest_metadata=dict(metadata) if isinstance(metadata, dict) else {},
    )


def _row_to_domain(
    row: PromptMaterialDigestRow,
) -> StoredPromptMaterialDigest | None:
    digest = decode_digest(row.digest_json)
    if digest is None:
        return None
    return StoredPromptMaterialDigest(
        character_id=row.character_id,
        operator_id=row.operator_id,
        content_tolerance=row.content_tolerance or "",
        digest=digest,
        updated_at=_as_utc(row.updated_at),
    )


class SAPromptMaterialDigestRepository(PromptMaterialDigestStorePort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(
        self, *, character_id: str, operator_id: str,
    ) -> StoredPromptMaterialDigest | None:
        async with self._session_factory() as session:
            row = await session.get(
                PromptMaterialDigestRow, (character_id, operator_id),
            )
            return _row_to_domain(row) if row is not None else None

    async def upsert(self, stored: StoredPromptMaterialDigest) -> bool:
        """Land the row unless a newer one is already there.

        The ``UPDATE`` carries ``updated_at <= :new`` as its predicate, so
        a post-turn that read the source material earlier cannot overwrite
        one that read it later. That ordering is not hypothetical: two
        post-turn jobs for one character really can run at once, and the
        slower one finishes last while holding the *older* material.

        A zero ``rowcount`` is therefore ambiguous — either there is no
        row at all, or the row present is newer than this write. The
        lookup that follows tells the two apart, because only the first
        may be turned into an ``INSERT``.
        """
        stamp = _as_utc(stored.updated_at)
        values = {
            "content_tolerance": stored.content_tolerance,
            "digest_json": encode_digest(stored.digest),
            "updated_at": stamp,
        }

        def _predicated_update():  # noqa: ANN202 - local statement builder
            return (
                update(PromptMaterialDigestRow)
                .where(
                    PromptMaterialDigestRow.character_id
                    == stored.character_id,
                    PromptMaterialDigestRow.operator_id
                    == stored.operator_id,
                    PromptMaterialDigestRow.updated_at <= stamp,
                )
                .values(**values)
            )

        async with self._session_factory() as session:
            result = await session.execute(_predicated_update())
            if result.rowcount:
                await session.commit()
                return True
            existing = await session.get(
                PromptMaterialDigestRow,
                (stored.character_id, stored.operator_id),
            )
            if existing is not None:
                # A newer read is already stored. This one is stale by
                # construction; dropping it is the whole point.
                return False
            try:
                await session.execute(
                    insert(PromptMaterialDigestRow).values(
                        character_id=stored.character_id,
                        operator_id=stored.operator_id,
                        **values,
                    ),
                )
                await session.commit()
                return True
            except IntegrityError:
                # Somebody inserted the pair between our UPDATE and our
                # INSERT. Retry the predicated UPDATE: if theirs is the
                # newer read it stands, otherwise ours does.
                await session.rollback()
        async with self._session_factory() as session:
            result = await session.execute(_predicated_update())
            await session.commit()
            return bool(result.rowcount)

    async def delete(
        self,
        *,
        character_id: str,
        operator_id: str | None = None,
        not_newer_than: datetime | None = None,
    ) -> int:
        statement = delete(PromptMaterialDigestRow).where(
            PromptMaterialDigestRow.character_id == character_id,
        )
        if operator_id is not None:
            statement = statement.where(
                PromptMaterialDigestRow.operator_id == operator_id,
            )
        if not_newer_than is not None:
            statement = statement.where(
                PromptMaterialDigestRow.updated_at <= _as_utc(not_newer_than),
            )
        async with self._session_factory() as session:
            result = await session.execute(statement)
            await session.commit()
            return int(result.rowcount or 0)


__all__ = [
    "SAPromptMaterialDigestRepository",
    "decode_digest",
    "encode_digest",
]
