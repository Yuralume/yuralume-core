"""SQLAlchemy character relationship repository."""

from __future__ import annotations

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.character_relationship import (
    CharacterRelationshipRepositoryPort,
)
from kokoro_link.domain.entities.character_relationship import CharacterRelationship
from kokoro_link.infrastructure.persistence.models import CharacterRelationshipRow


def canonical_pair(a: str, b: str) -> tuple[str, str]:
    first = a.strip()
    second = b.strip()
    if first == second:
        raise ValueError("Relationship cannot point to the same character")
    return tuple(sorted((first, second)))  # type: ignore[return-value]


class SACharacterRelationshipRepository(CharacterRelationshipRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, relationship_id: str) -> CharacterRelationship | None:
        async with self._session_factory() as session:
            row = await session.get(CharacterRelationshipRow, relationship_id)
            return _row_to_domain(row) if row is not None else None

    async def get_pair(
        self, character_a_id: str, character_b_id: str,
    ) -> CharacterRelationship | None:
        a, b = canonical_pair(character_a_id, character_b_id)
        async with self._session_factory() as session:
            stmt = select(CharacterRelationshipRow).where(
                CharacterRelationshipRow.from_character_id == a,
                CharacterRelationshipRow.to_character_id == b,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            return _row_to_domain(row) if row is not None else None

    async def list_for_character(self, character_id: str) -> list[CharacterRelationship]:
        async with self._session_factory() as session:
            stmt = (
                select(CharacterRelationshipRow)
                .where(
                    or_(
                        CharacterRelationshipRow.from_character_id == character_id,
                        CharacterRelationshipRow.to_character_id == character_id,
                    )
                )
                .order_by(CharacterRelationshipRow.updated_at.desc())
            )
            rows = list((await session.execute(stmt)).scalars().all())
        return [_row_to_domain(row) for row in rows]

    async def list_enabled(self) -> list[CharacterRelationship]:
        async with self._session_factory() as session:
            stmt = (
                select(CharacterRelationshipRow)
                .where(CharacterRelationshipRow.enabled.is_(True))
                .order_by(CharacterRelationshipRow.updated_at.desc())
            )
            rows = list((await session.execute(stmt)).scalars().all())
        return [_row_to_domain(row) for row in rows]

    async def save(self, relationship: CharacterRelationship) -> None:
        async with self._session_factory() as session:
            row = await session.get(CharacterRelationshipRow, relationship.id)
            if row is None:
                session.add(_domain_to_row(relationship))
            else:
                _apply_domain(row, relationship)
            await session.commit()

    async def delete_for_character(self, character_id: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(CharacterRelationshipRow).where(
                    or_(
                        CharacterRelationshipRow.from_character_id == character_id,
                        CharacterRelationshipRow.to_character_id == character_id,
                    )
                )
            )
            await session.commit()
            return int(result.rowcount or 0)


def _row_to_domain(row: CharacterRelationshipRow) -> CharacterRelationship:
    return CharacterRelationship(
        id=row.id,
        character_a_id=row.from_character_id,
        character_b_id=row.to_character_id,
        enabled=bool(getattr(row, "enabled", True)),
        relationship_label=row.label or "",
        how_a_sees_b=getattr(row, "how_a_sees_b", "") or row.description or "",
        how_b_sees_a=getattr(row, "how_b_sees_a", "") or "",
        affection_a_to_b=getattr(row, "affection_a_to_b", row.affection),
        affection_b_to_a=getattr(row, "affection_b_to_a", row.affection),
        trust_a_to_b=getattr(row, "trust_a_to_b", row.trust),
        trust_b_to_a=getattr(row, "trust_b_to_a", row.trust),
        last_interaction_at=getattr(row, "last_interaction_at", None),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _domain_to_row(item: CharacterRelationship) -> CharacterRelationshipRow:
    row = CharacterRelationshipRow(
        id=item.id,
        from_character_id=item.character_a_id,
        to_character_id=item.character_b_id,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )
    _apply_domain(row, item)
    return row


def _apply_domain(row: CharacterRelationshipRow, item: CharacterRelationship) -> None:
    row.from_character_id = item.character_a_id
    row.to_character_id = item.character_b_id
    row.label = item.relationship_label
    row.affection = item.affection_a_to_b
    row.trust = item.trust_a_to_b
    row.tension = 0
    row.description = item.how_a_sees_b
    row.enabled = item.enabled
    row.how_a_sees_b = item.how_a_sees_b
    row.how_b_sees_a = item.how_b_sees_a
    row.affection_a_to_b = item.affection_a_to_b
    row.affection_b_to_a = item.affection_b_to_a
    row.trust_a_to_b = item.trust_a_to_b
    row.trust_b_to_a = item.trust_b_to_a
    row.last_interaction_at = item.last_interaction_at
    row.updated_at = item.updated_at
