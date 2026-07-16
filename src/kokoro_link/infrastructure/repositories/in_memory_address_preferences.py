"""In-memory ``OperatorAddressPreference`` store for tests / dev (§4.2)."""

from __future__ import annotations

from kokoro_link.contracts.operator_address_preference import (
    OperatorAddressPreferenceRepositoryPort,
)
from kokoro_link.domain.entities.operator_address_preference import (
    OperatorAddressPreference,
)


class InMemoryOperatorAddressPreferenceRepository(
    OperatorAddressPreferenceRepositoryPort,
):
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], OperatorAddressPreference] = {}

    async def get(
        self, *, character_id: str, operator_id: str,
    ) -> OperatorAddressPreference | None:
        return self._rows.get((character_id, operator_id))

    async def upsert(self, pref: OperatorAddressPreference) -> None:
        self._rows[(pref.character_id, pref.operator_id)] = pref
