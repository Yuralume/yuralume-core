"""SQLAlchemy :class:`PlayerIdentityCard` repository."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.player_identity_card import (
    PlayerIdentityCardNameConflictError,
    PlayerIdentityCardRepositoryPort,
)
from kokoro_link.domain.entities.player_identity_card import (
    PLAYER_IDENTITY_CARD_CONTENT_FIELDS,
    PlayerIdentityCard,
)
from kokoro_link.infrastructure.persistence.models import PlayerIdentityCardRow


_NAME_CONSTRAINT = "uq_player_identity_cards_operator_name"
"""PostgreSQL names the violated constraint in the error text."""

_SQLITE_NAME_CONSTRAINT = "player_identity_cards.name"
"""SQLite (the embedded self-host) never mentions the constraint name —
it lists the offending columns instead: ``UNIQUE constraint failed:
player_identity_cards.operator_id, player_identity_cards.name``."""


def _is_name_conflict(error: IntegrityError) -> bool:
    """True only for a violation of ``(operator_id, name)``.

    Deliberately narrow. The row also carries an ``operator_id`` foreign
    key, and a save for an operator that no longer exists must keep
    surfacing as the defect it is rather than being answered "that name
    is taken"."""
    text = str(getattr(error, "orig", error))
    return _NAME_CONSTRAINT in text or _SQLITE_NAME_CONSTRAINT in text


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _row_to_domain(row: PlayerIdentityCardRow) -> PlayerIdentityCard:
    return PlayerIdentityCard(
        id=row.id,
        operator_id=row.operator_id,
        name=row.name,
        created_at=_aware(row.created_at),
        updated_at=_aware(row.updated_at),
        **{
            field: getattr(row, field)
            for field in PLAYER_IDENTITY_CARD_CONTENT_FIELDS
        },
    )


def _values(card: PlayerIdentityCard) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    values: dict[str, object] = {
        "id": card.id,
        "operator_id": card.operator_id,
        "name": card.name,
        "created_at": card.created_at or now,
        "updated_at": card.updated_at or now,
    }
    values.update(
        {
            field: getattr(card, field)
            for field in PLAYER_IDENTITY_CARD_CONTENT_FIELDS
        },
    )
    return values


class SAPlayerIdentityCardRepository(PlayerIdentityCardRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_for_operator(
        self, operator_id: str,
    ) -> list[PlayerIdentityCard]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(PlayerIdentityCardRow)
                .where(PlayerIdentityCardRow.operator_id == operator_id)
                .order_by(
                    PlayerIdentityCardRow.updated_at.desc(),
                    PlayerIdentityCardRow.id.desc(),
                ),
            )
            return [_row_to_domain(row) for row in result.scalars().all()]

    async def get(
        self, *, card_id: str, operator_id: str,
    ) -> PlayerIdentityCard | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(PlayerIdentityCardRow).where(
                    PlayerIdentityCardRow.id == card_id,
                    PlayerIdentityCardRow.operator_id == operator_id,
                ),
            )
            row = result.scalar_one_or_none()
            return _row_to_domain(row) if row else None

    async def find_by_name(
        self, *, operator_id: str, name: str,
    ) -> PlayerIdentityCard | None:
        wanted = (name or "").strip()
        if not wanted:
            return None
        async with self._session_factory() as session:
            result = await session.execute(
                select(PlayerIdentityCardRow).where(
                    PlayerIdentityCardRow.operator_id == operator_id,
                    PlayerIdentityCardRow.name == wanted,
                ),
            )
            row = result.scalar_one_or_none()
            return _row_to_domain(row) if row else None

    async def count_for_operator(self, operator_id: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.count())
                .select_from(PlayerIdentityCardRow)
                .where(PlayerIdentityCardRow.operator_id == operator_id),
            )
            return int(result.scalar_one() or 0)

    async def _write(self, values: dict[str, object]) -> None:
        async with self._session_factory() as session:
            dialect = session.bind.dialect.name if session.bind else ""
            insert = pg_insert if dialect == "postgresql" else sqlite_insert
            stmt = insert(PlayerIdentityCardRow).values(**values)
            # ``created_at`` is intentionally absent from the update set:
            # re-saving under an existing name overwrites the content and
            # bumps ``updated_at``, but the card was still created when it
            # was created.
            stmt = stmt.on_conflict_do_update(
                index_elements=[PlayerIdentityCardRow.id],
                set_={
                    key: value
                    for key, value in values.items()
                    if key not in ("id", "operator_id", "created_at")
                },
            )
            await session.execute(stmt)
            await session.commit()

    async def upsert(self, card: PlayerIdentityCard) -> None:
        """Write the card, translating a name collision into the typed error.

        ``ON CONFLICT`` arbitrates on the primary key only, so it absorbs
        a re-save of *this* card and nothing else. The second unique key,
        ``(operator_id, name)``, is what two concurrent first-time saves
        of the same name collide on — the service's ``find_by_name``
        pre-check ran before either insert, so both read "free". Left
        alone that surfaces as a driver ``IntegrityError`` and a 500 for
        what is an ordinary taken-name answer.
        """
        values = _values(card)
        try:
            await self._write(values)
        except IntegrityError as exc:
            if not _is_name_conflict(exc):
                raise
            existing = await self.find_by_name(
                operator_id=card.operator_id, name=card.name,
            )
            if existing is None:
                # The row we lost to is already gone (the winner was
                # deleted between our failed write and this read), so
                # there is no conflict left to report — and no card to
                # report it against. Take the name that just freed up.
                await self._write(values)
                return
            raise PlayerIdentityCardNameConflictError(existing) from exc

    async def delete(self, *, card_id: str, operator_id: str) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                sa_delete(PlayerIdentityCardRow).where(
                    PlayerIdentityCardRow.id == card_id,
                    PlayerIdentityCardRow.operator_id == operator_id,
                ),
            )
            await session.commit()
            return bool(result.rowcount)
