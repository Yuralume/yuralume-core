"""In-memory dialogue-checkpoint repository.

The CAS is modelled faithfully rather than waved away: the embedded
single-process deployment cannot race, but every unit test of the update
machine runs against this class, and a store that always accepted a
write would make the loser-drops-its-work path untestable.
"""

from __future__ import annotations

from datetime import datetime, timezone

from kokoro_link.contracts.dialogue_checkpoint import (
    DialogueCheckpointRepositoryPort,
)
from kokoro_link.domain.entities.dialogue_checkpoint import DialogueCheckpoint


def _as_utc(value: datetime) -> datetime:
    return (
        value.astimezone(timezone.utc)
        if value.tzinfo
        else value.replace(tzinfo=timezone.utc)
    )


class InMemoryDialogueCheckpointRepository(DialogueCheckpointRepositoryPort):
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], DialogueCheckpoint] = {}

    async def get(
        self, *, character_id: str, operator_id: str,
    ) -> DialogueCheckpoint | None:
        return self._items.get((character_id, operator_id))

    async def save(
        self,
        checkpoint: DialogueCheckpoint,
        *,
        expected_message_key: str | None,
        expected_stale: bool = False,
    ) -> bool:
        """The SA repository's predicate, modelled exactly.

        Both halves of it: the cursor *and* ``stale``. Dropping the
        second half here would make ``mark_stale``'s latch untestable
        against the adapter every unit test in the package runs on,
        which is the same as not having it.
        """
        key = (checkpoint.character_id, checkpoint.operator_id)
        current = self._items.get(key)
        if expected_message_key is None:
            if current is not None:
                return False
        elif (
            current is None
            or current.covers_until_message_key != expected_message_key
            or current.stale != bool(expected_stale)
        ):
            return False
        self._items[key] = checkpoint
        return True

    async def mark_stale(
        self, *, character_id: str, operator_id: str, now: datetime,
    ) -> bool:
        key = (character_id, operator_id)
        current = self._items.get(key)
        if current is None:
            return False
        self._items[key] = current.marked_stale(now=_as_utc(now))
        return True

    async def delete_for_character(self, character_id: str) -> int:
        targets = [
            key for key in self._items if key[0] == character_id
        ]
        for key in targets:
            del self._items[key]
        return len(targets)

    async def list_for_character(
        self, character_id: str,
    ) -> list[DialogueCheckpoint]:
        return [
            value for key, value in self._items.items()
            if key[0] == character_id
        ]


__all__ = ["InMemoryDialogueCheckpointRepository"]
