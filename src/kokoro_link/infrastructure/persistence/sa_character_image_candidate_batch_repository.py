"""SQLAlchemy repository for the pending album-gacha candidate batch.

This is the implementation that makes the gacha correct under the hosted
topology (``core-router`` in front of N api replicas): ``generate`` and
``commit`` for the same batch are two independent HTTP requests and may be
served by different processes, so the batch's object keys have to live in the
database rather than in either process.

Two consequences shape the code below.

*Dialect.* The container wires this adapter for *any* configured database, and
a self-host deployment may well be on SQLite. The upsert therefore picks the
dialect's own ``INSERT ... ON CONFLICT`` construct rather than importing the
PostgreSQL one and hoping it compiles elsewhere (same convention as
:mod:`kokoro_link.infrastructure.persistence.sa_inbound_receipts`).

*Concurrency.* Nothing holds a lock between the read at the start of a commit
and the write at the end of it, so a replica acting on a batch it read earlier
must prove that batch is still the live one. The stored key list doubles as the
version marker: conditional writes match on ``object_keys_json``, and a
``rowcount`` of zero means "somebody generated a newer batch, stand down".
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import delete, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.character_image_candidate_batch import (
    CharacterImageCandidateBatchRepositoryPort,
)
from kokoro_link.infrastructure.persistence.models import (
    CharacterImageCandidateBatchRow,
)


class SACharacterImageCandidateBatchRepository(
    CharacterImageCandidateBatchRepositoryPort,
):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, character_id: str) -> tuple[str, ...]:
        key = _normalise_character_id(character_id)
        async with self._session_factory() as session:
            row = await session.get(CharacterImageCandidateBatchRow, key)
            if row is None:
                return ()
            return _decode_object_keys(row.object_keys_json)

    async def replace(
        self,
        character_id: str,
        object_keys: Sequence[str],
        *,
        expected_keys: Sequence[str] | None = None,
    ) -> bool:
        key = _normalise_character_id(character_id)
        keys = [str(object_key) for object_key in object_keys]
        if not keys:
            return await self.clear(key, expected_keys=expected_keys)
        payload = _encode_object_keys(keys)
        now = datetime.now(timezone.utc)
        if expected_keys is not None:
            # Compare-and-swap: shrinking a batch we read earlier is only
            # legitimate while that batch is still the live one.
            statement = (
                update(CharacterImageCandidateBatchRow)
                .where(
                    CharacterImageCandidateBatchRow.character_id == key,
                    CharacterImageCandidateBatchRow.object_keys_json
                    == _encode_object_keys(expected_keys),
                )
                .values(object_keys_json=payload, created_at=now)
            )
            return await self._execute_conditional(statement)
        # Upsert, not insert-or-fail: two replicas generating for the same
        # character concurrently is an accepted (and unnatural) race — the
        # last writer wins and the loser's objects become best-effort
        # orphans, but neither request may 500.
        async with self._session_factory() as session:
            insert = _insert_for(session)
            statement = (
                insert(CharacterImageCandidateBatchRow)
                .values(
                    character_id=key,
                    object_keys_json=payload,
                    created_at=now,
                )
                .on_conflict_do_update(
                    index_elements=[CharacterImageCandidateBatchRow.character_id],
                    set_={"object_keys_json": payload, "created_at": now},
                )
            )
            await session.execute(statement)
            await session.commit()
        return True

    async def clear(
        self,
        character_id: str,
        *,
        expected_keys: Sequence[str] | None = None,
    ) -> bool:
        key = _normalise_character_id(character_id)
        statement = delete(CharacterImageCandidateBatchRow).where(
            CharacterImageCandidateBatchRow.character_id == key,
        )
        if expected_keys is None:
            async with self._session_factory() as session:
                await session.execute(statement)
                await session.commit()
            return True
        return await self._execute_conditional(
            statement.where(
                CharacterImageCandidateBatchRow.object_keys_json
                == _encode_object_keys(expected_keys),
            ),
        )

    async def _execute_conditional(self, statement) -> bool:  # noqa: ANN001
        """Run a CAS statement; ``rowcount`` is the verdict.

        A driver that cannot report ``rowcount`` (negative) is read as "did not
        apply": the caller then leaves the row alone and logs an orphan, which
        is the harmless direction — claiming a successful swap we cannot prove
        would let the next writer delete a batch it does not own.
        """
        async with self._session_factory() as session:
            result = await session.execute(statement)
            await session.commit()
            rowcount = result.rowcount
        return int(rowcount or 0) > 0


def _insert_for(session: AsyncSession):  # noqa: ANN201
    """The bound dialect's upsert construct.

    PostgreSQL in hosted, SQLite in a DB-backed self-host. Both spell the
    clause ``ON CONFLICT``, but they are separate constructs and only the
    matching one is guaranteed to compile.
    """
    bind = session.bind
    if bind is not None and bind.dialect.name == "postgresql":
        return pg_insert
    return sqlite_insert


def _encode_object_keys(object_keys: Sequence[str]) -> str:
    """The single serializer for stored *and* expected key lists.

    Compare-and-swap compares the encoded text, so both sides must go through
    this one function — that is what makes the comparison deterministic
    without needing the raw column value to travel back out of :meth:`get`.
    A row written by hand (different spacing) simply never matches, which
    fails safe: the caller skips its write instead of clobbering.
    """
    return json.dumps([str(object_key) for object_key in object_keys])


def _decode_object_keys(raw: str) -> tuple[str, ...]:
    try:
        decoded = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return ()
    if not isinstance(decoded, list):
        return ()
    return tuple(str(item) for item in decoded)


def _normalise_character_id(character_id: str) -> str:
    value = (character_id or "").strip()
    if not value:
        raise ValueError("character_id must be non-empty")
    return value
