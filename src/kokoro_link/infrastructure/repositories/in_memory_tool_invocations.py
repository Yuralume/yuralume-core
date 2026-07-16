"""In-process ``ToolInvocationRepositoryPort`` — list-backed."""

from __future__ import annotations

from collections import defaultdict

from kokoro_link.contracts.tool import ToolInvocationRepositoryPort
from kokoro_link.domain.entities.tool_invocation import ToolInvocation


class InMemoryToolInvocationRepository(ToolInvocationRepositoryPort):
    def __init__(self) -> None:
        self._by_character: dict[str, list[ToolInvocation]] = defaultdict(list)

    async def add(self, invocation: ToolInvocation) -> ToolInvocation:
        self._by_character[invocation.character_id].append(invocation)
        return invocation

    async def save(self, invocation: ToolInvocation) -> ToolInvocation:
        bucket = self._by_character.setdefault(invocation.character_id, [])
        for index, existing in enumerate(bucket):
            if existing.id == invocation.id:
                bucket[index] = invocation
                return invocation
        bucket.append(invocation)
        return invocation

    async def list_for_character(
        self,
        character_id: str,
        *,
        limit: int = 50,
    ) -> list[ToolInvocation]:
        items = list(self._by_character.get(character_id, []))
        items.sort(key=lambda i: i.started_at, reverse=True)
        return items[:limit]

    async def delete_for_character(self, character_id: str) -> int:
        removed = self._by_character.pop(character_id, None)
        return len(removed) if removed else 0
