"""SQLAlchemy dialogue-checkpoint repository, with a real CAS on save."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.dialogue_checkpoint import (
    DialogueCheckpointRepositoryPort,
)
from kokoro_link.domain.entities.dialogue_checkpoint import DialogueCheckpoint
from kokoro_link.infrastructure.persistence.models import DialogueCheckpointRow


def _as_utc(value: datetime) -> datetime:
    return (
        value.astimezone(timezone.utc)
        if value.tzinfo
        else value.replace(tzinfo=timezone.utc)
    )


def _row_to_domain(row: DialogueCheckpointRow) -> DialogueCheckpoint:
    return DialogueCheckpoint(
        character_id=row.character_id,
        operator_id=row.operator_id,
        summary_text=row.summary_text or "",
        covers_until_message_key=row.covers_until_message_key,
        covers_until_created_at=_as_utc(row.covers_until_created_at),
        updated_at=_as_utc(row.updated_at),
        model=row.model or "",
        stale=bool(row.stale),
    )


class SADialogueCheckpointRepository(DialogueCheckpointRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(
        self, *, character_id: str, operator_id: str,
    ) -> DialogueCheckpoint | None:
        async with self._session_factory() as session:
            row = await session.get(
                DialogueCheckpointRow, (character_id, operator_id),
            )
            return _row_to_domain(row) if row is not None else None

    async def save(
        self,
        checkpoint: DialogueCheckpoint,
        *,
        expected_message_key: str | None,
        expected_stale: bool = False,
    ) -> bool:
        """Land the checkpoint only if the stored row still reads as it did.

        Two shapes, one for each thing ``expected_message_key`` can say:

        * ``None`` — "there was no row when I read". An ``INSERT`` is
          therefore the whole claim: the pair is the primary key, so a
          replica that got there first turns this into an
          ``IntegrityError``, which is a lost race and not a failure.
        * a key — "the row said this". A predicated ``UPDATE`` whose
          ``rowcount`` is the verdict. The predicate is re-evaluated
          against the committed row after any concurrent writer releases
          its lock, so exactly one of two racing merges sees ``1``.

        The ``UPDATE`` predicate carries ``stale`` alongside the cursor.
        ``mark_stale`` moves ``stale`` and nothing else, so a
        cursor-only predicate would let an in-flight merge overwrite the
        latch without ever having seen it — see the port docstring.

        Never a blind upsert. The whole point is that a summary computed
        against a state somebody else has already moved past must not
        overwrite the write that moved it.
        """
        async with self._session_factory() as session:
            if expected_message_key is None:
                try:
                    await session.execute(
                        insert(DialogueCheckpointRow).values(
                            **_insert_values(checkpoint),
                        ),
                    )
                    await session.commit()
                    return True
                except IntegrityError:
                    await session.rollback()
                    return False
            result = await session.execute(
                update(DialogueCheckpointRow)
                .where(
                    DialogueCheckpointRow.character_id
                    == checkpoint.character_id,
                    DialogueCheckpointRow.operator_id
                    == checkpoint.operator_id,
                    DialogueCheckpointRow.covers_until_message_key
                    == expected_message_key,
                    DialogueCheckpointRow.stale == bool(expected_stale),
                )
                .values(
                    summary_text=checkpoint.summary_text,
                    covers_until_message_key=(
                        checkpoint.covers_until_message_key
                    ),
                    covers_until_created_at=(
                        checkpoint.covers_until_created_at
                    ),
                    updated_at=checkpoint.updated_at,
                    model=checkpoint.model,
                    stale=checkpoint.stale,
                ),
            )
            await session.commit()
            return bool(result.rowcount)

    async def mark_stale(
        self, *, character_id: str, operator_id: str, now: datetime,
    ) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                update(DialogueCheckpointRow)
                .where(
                    DialogueCheckpointRow.character_id == character_id,
                    DialogueCheckpointRow.operator_id == operator_id,
                )
                .values(stale=True, updated_at=_as_utc(now)),
            )
            await session.commit()
            return bool(result.rowcount)

    async def delete_for_character(self, character_id: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(DialogueCheckpointRow).where(
                    DialogueCheckpointRow.character_id == character_id,
                ),
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def list_for_character(
        self, character_id: str,
    ) -> list[DialogueCheckpoint]:
        """Every operator's checkpoint for one character.

        Not on the port — used by tests and by ad-hoc inspection, where
        "show me what the character actually remembers" is the question.
        """
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(DialogueCheckpointRow).where(
                        DialogueCheckpointRow.character_id == character_id,
                    ),
                )
            ).scalars().all()
            return [_row_to_domain(row) for row in rows]


def _insert_values(checkpoint: DialogueCheckpoint) -> dict[str, object]:
    return {
        "character_id": checkpoint.character_id,
        "operator_id": checkpoint.operator_id,
        "summary_text": checkpoint.summary_text,
        "covers_until_message_key": checkpoint.covers_until_message_key,
        "covers_until_created_at": checkpoint.covers_until_created_at,
        "updated_at": checkpoint.updated_at,
        "model": checkpoint.model,
        "stale": checkpoint.stale,
    }


__all__ = ["SADialogueCheckpointRepository"]
