"""In-process runtime settings store for dev / tests (HUMANIZATION_ROADMAP §4.5)."""

from __future__ import annotations

from kokoro_link.contracts.runtime_settings import RuntimeSettingsRepositoryPort


class InMemoryRuntimeSettingsRepository(RuntimeSettingsRepositoryPort):
    def __init__(self, seed: dict[str, str] | None = None) -> None:
        self._values: dict[str, str] = dict(seed or {})

    async def get(self, key: str) -> str | None:
        return self._values.get(key)

    async def set(self, key: str, value: str) -> None:
        self._values[key] = value

    async def all(self) -> dict[str, str]:
        return dict(self._values)
