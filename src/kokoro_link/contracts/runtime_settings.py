"""Repository port for ``app_runtime_settings`` (HUMANIZATION_ROADMAP §4.5).

A thin generic KV interface: keys live in a flat namespace, callers map
their domain values (``QuietHoursWindow``, etc.) on top. The repository
intentionally exposes get/set/list rather than typed accessors so future
P2/P3 runtime knobs can persist without a port-surface bump per key.
"""

from __future__ import annotations

from typing import Protocol


class RuntimeSettingsRepositoryPort(Protocol):
    async def get(self, key: str) -> str | None:
        """Return the raw string value or ``None`` when unset.

        Callers are responsible for fallback to env-driven defaults
        — the repository does not synthesise defaults of its own."""

    async def set(self, key: str, value: str) -> None:
        """Upsert ``(key, value)`` and stamp ``updated_at`` to now."""

    async def all(self) -> dict[str, str]:
        """Return every persisted key/value pair, for admin readback."""
