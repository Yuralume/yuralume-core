"""In-process album candidate-batch store for tests and DB-less dev mode.

Single-process only — the SQL twin
(:class:`~kokoro_link.infrastructure.persistence.sa_character_image_candidate_batch_repository.SACharacterImageCandidateBatchRepository`)
is what makes the gacha survive a multi-replica deployment. The
compare-and-swap semantics are mirrored here anyway: the service is written
against the port, and a twin that silently accepted stale writes would let
every test pass while the deployed adapter refused them.
"""

from __future__ import annotations

from collections.abc import Sequence


class InMemoryCharacterImageCandidateBatchRepository:
    def __init__(self) -> None:
        self._batches: dict[str, tuple[str, ...]] = {}

    async def get(self, character_id: str) -> tuple[str, ...]:
        return self._batches.get(character_id, ())

    async def replace(
        self,
        character_id: str,
        object_keys: Sequence[str],
        *,
        expected_keys: Sequence[str] | None = None,
    ) -> bool:
        keys = _as_keys(object_keys)
        if not keys:
            return await self.clear(character_id, expected_keys=expected_keys)
        if expected_keys is not None and not self._matches(
            character_id, expected_keys,
        ):
            return False
        self._batches[character_id] = keys
        return True

    async def clear(
        self,
        character_id: str,
        *,
        expected_keys: Sequence[str] | None = None,
    ) -> bool:
        if expected_keys is None:
            self._batches.pop(character_id, None)
            return True
        if not self._matches(character_id, expected_keys):
            return False
        self._batches.pop(character_id, None)
        return True

    def _matches(self, character_id: str, expected_keys: Sequence[str]) -> bool:
        """Whether the stored batch is still exactly the one the caller read.

        Order counts: the batch is an ordered list, and the SQL twin compares
        the serialized list, so a reordered expectation must miss here too.
        An empty expectation never matches — a stored batch is never empty.
        """
        expected = _as_keys(expected_keys)
        return bool(expected) and self._batches.get(character_id) == expected


def _as_keys(object_keys: Sequence[str]) -> tuple[str, ...]:
    return tuple(str(object_key) for object_key in object_keys)
