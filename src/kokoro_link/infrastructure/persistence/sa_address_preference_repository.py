"""SQLAlchemy ``OperatorAddressPreference`` repository (§4.2)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.operator_address_preference import (
    OperatorAddressPreferenceRepositoryPort,
)
from kokoro_link.domain.entities.operator_address_preference import (
    OperatorAddressPreference,
)
from kokoro_link.infrastructure.persistence.models import (
    OperatorAddressPreferenceRow,
)


def _row_to_domain(row: OperatorAddressPreferenceRow) -> OperatorAddressPreference:
    updated_at = row.updated_at
    if updated_at is not None and updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return OperatorAddressPreference(
        character_id=row.character_id,
        operator_id=row.operator_id,
        salutation=row.salutation or "",
        formality_level=row.formality_level or "medium",
        response_length_pref=row.response_length_pref or "medium",
        evidence_quote=row.evidence_quote or "",
        updated_at=updated_at,
    )


class SAOperatorAddressPreferenceRepository(
    OperatorAddressPreferenceRepositoryPort,
):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(
        self, *, character_id: str, operator_id: str,
    ) -> OperatorAddressPreference | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(OperatorAddressPreferenceRow).where(
                    OperatorAddressPreferenceRow.character_id == character_id,
                    OperatorAddressPreferenceRow.operator_id == operator_id,
                ),
            )
            row = result.scalar_one_or_none()
            return _row_to_domain(row) if row else None

    async def upsert(self, pref: OperatorAddressPreference) -> None:
        now = pref.updated_at or datetime.now(timezone.utc)
        async with self._session_factory() as session:
            dialect = session.bind.dialect.name if session.bind else ""
            values = {
                "character_id": pref.character_id,
                "operator_id": pref.operator_id,
                "salutation": pref.salutation,
                "formality_level": pref.formality_level,
                "response_length_pref": pref.response_length_pref,
                "evidence_quote": pref.evidence_quote,
                "updated_at": now,
            }
            if dialect == "postgresql":
                stmt = pg_insert(OperatorAddressPreferenceRow).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[
                        OperatorAddressPreferenceRow.character_id,
                        OperatorAddressPreferenceRow.operator_id,
                    ],
                    set_={
                        "salutation": values["salutation"],
                        "formality_level": values["formality_level"],
                        "response_length_pref": values["response_length_pref"],
                        "evidence_quote": values["evidence_quote"],
                        "updated_at": now,
                    },
                )
            else:
                stmt = sqlite_insert(OperatorAddressPreferenceRow).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[
                        OperatorAddressPreferenceRow.character_id,
                        OperatorAddressPreferenceRow.operator_id,
                    ],
                    set_={
                        "salutation": values["salutation"],
                        "formality_level": values["formality_level"],
                        "response_length_pref": values["response_length_pref"],
                        "evidence_quote": values["evidence_quote"],
                        "updated_at": now,
                    },
                )
            await session.execute(stmt)
            await session.commit()
