from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.initial_relationship import (
    CharacterOperatorRelationshipSeedRepositoryPort,
)
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.infrastructure.persistence.models import (
    CharacterOperatorRelationshipSeedRow,
)


class SACharacterOperatorRelationshipSeedRepository(
    CharacterOperatorRelationshipSeedRepositoryPort,
):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(
        self, character_id: str, operator_id: str,
    ) -> CharacterOperatorRelationshipSeed | None:
        async with self._session_factory() as session:
            row = await session.get(
                CharacterOperatorRelationshipSeedRow,
                {"character_id": character_id, "operator_id": operator_id},
            )
            return _row_to_domain(row) if row is not None else None

    async def save(self, seed: CharacterOperatorRelationshipSeed) -> None:
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            row = await session.get(
                CharacterOperatorRelationshipSeedRow,
                {
                    "character_id": seed.character_id,
                    "operator_id": seed.operator_id,
                },
            )
            if row is None:
                row = CharacterOperatorRelationshipSeedRow(
                    character_id=seed.character_id,
                    operator_id=seed.operator_id,
                    created_at=seed.created_at or now,
                    updated_at=seed.updated_at or now,
                )
                session.add(row)
            _apply(seed, row, updated_at=seed.updated_at or now)
            await session.commit()

    async def delete_for_character(self, character_id: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(CharacterOperatorRelationshipSeedRow).where(
                    CharacterOperatorRelationshipSeedRow.character_id == character_id,
                )
            )
            await session.commit()
            return int(result.rowcount or 0)


def _row_to_domain(
    row: CharacterOperatorRelationshipSeedRow,
) -> CharacterOperatorRelationshipSeed:
    return CharacterOperatorRelationshipSeed(
        character_id=row.character_id,
        operator_id=row.operator_id,
        relationship_label=row.relationship_label,
        known_context=row.known_context,
        living_arrangement=getattr(row, "living_arrangement", ""),
        user_address_name=row.user_address_name,
        character_address_name=row.character_address_name,
        tone_distance=row.tone_distance,
        familiarity_boundary=row.familiarity_boundary,
        schedule_involvement_policy=row.schedule_involvement_policy,
        proactive_permission=bool(row.proactive_permission),
        proactive_cadence_hint=row.proactive_cadence_hint,
        user_profile_notes=row.user_profile_notes,
        confirmed_by_user=bool(row.confirmed_by_user),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _apply(
    seed: CharacterOperatorRelationshipSeed,
    row: CharacterOperatorRelationshipSeedRow,
    *,
    updated_at,
) -> None:
    row.relationship_label = seed.relationship_label
    row.known_context = seed.known_context
    row.living_arrangement = seed.living_arrangement
    row.user_address_name = seed.user_address_name
    row.character_address_name = seed.character_address_name
    row.tone_distance = seed.tone_distance
    row.familiarity_boundary = seed.familiarity_boundary
    row.schedule_involvement_policy = seed.schedule_involvement_policy
    row.proactive_permission = seed.proactive_permission
    row.proactive_cadence_hint = seed.proactive_cadence_hint
    row.user_profile_notes = seed.user_profile_notes
    row.confirmed_by_user = seed.confirmed_by_user
    row.updated_at = updated_at
