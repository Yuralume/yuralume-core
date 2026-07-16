from __future__ import annotations

from kokoro_link.contracts.repositories import PreferencesRepositoryPort


class InMemoryPreferencesRepository(PreferencesRepositoryPort):
    def __init__(self) -> None:
        self._store: dict[str, object] = {}

    async def get(self, key: str) -> object | None:
        return self._store.get(key)

    async def set(self, key: str, value: object) -> None:
        self._store[key] = value

    async def delete(self, key: str) -> bool:
        return self._store.pop(key, _MISSING) is not _MISSING


_MISSING = object()
