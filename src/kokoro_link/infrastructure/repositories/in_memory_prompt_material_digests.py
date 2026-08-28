"""In-memory material-digest store.

The embedded / self-host store. There the post-turn runs in the same
process as the chat turn that will read it, so a dict is a faithful
implementation of the port rather than a shortcut — and it is what every
unit test that does not care about SQL runs against.

Its one real difference from the SA store is lifetime: a restart empties
it, which reads as "nothing budgeted yet" and renders the source blocks.
That is the same state every pair starts in.
"""

from __future__ import annotations

from datetime import datetime, timezone

from kokoro_link.contracts.prompt_material_digest import (
    PromptMaterialDigestStorePort,
    StoredPromptMaterialDigest,
)


def _as_utc(value: datetime) -> datetime:
    return (
        value.astimezone(timezone.utc)
        if value.tzinfo
        else value.replace(tzinfo=timezone.utc)
    )


class InMemoryPromptMaterialDigestRepository(PromptMaterialDigestStorePort):
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], StoredPromptMaterialDigest] = {}

    async def get(
        self, *, character_id: str, operator_id: str,
    ) -> StoredPromptMaterialDigest | None:
        return self._items.get((character_id, operator_id))

    async def upsert(self, stored: StoredPromptMaterialDigest) -> bool:
        """The SA store's version predicate, modelled exactly.

        Not decoration: embedded runs one process but still runs the
        post-turn as a background task, and every unit test of the
        newer-wins rule runs against this class. A store that always
        accepted a write would make the rule untestable where it is
        actually asserted.
        """
        key = (stored.character_id, stored.operator_id)
        stamp = _as_utc(stored.updated_at)
        current = self._items.get(key)
        if current is not None and _as_utc(current.updated_at) > stamp:
            return False
        self._items[key] = StoredPromptMaterialDigest(
            character_id=stored.character_id,
            operator_id=stored.operator_id,
            content_tolerance=stored.content_tolerance,
            digest=stored.digest,
            updated_at=stamp,
        )
        return True

    async def delete(
        self,
        *,
        character_id: str,
        operator_id: str | None = None,
        not_newer_than: datetime | None = None,
    ) -> int:
        ceiling = _as_utc(not_newer_than) if not_newer_than is not None else None
        targets = [
            key for key, value in self._items.items()
            if key[0] == character_id
            and (operator_id is None or key[1] == operator_id)
            and (ceiling is None or _as_utc(value.updated_at) <= ceiling)
        ]
        for key in targets:
            del self._items[key]
        return len(targets)


__all__ = ["InMemoryPromptMaterialDigestRepository"]
