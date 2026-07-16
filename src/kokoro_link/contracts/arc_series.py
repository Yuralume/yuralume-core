"""Ports for authored arc series."""

from __future__ import annotations

from abc import ABC, abstractmethod

from kokoro_link.domain.entities.arc_series import (
    ArcSeries,
    CharacterSeriesProgress,
)


class ArcSeriesRepositoryPort(ABC):
    """Persistence port for ArcSeries + per-character progress."""

    @abstractmethod
    async def get_for_user(
        self, series_id: str, *, user_id: str | None,
    ) -> ArcSeries | None:
        """Return a pack-visible or caller-owned series."""

    @abstractmethod
    async def list_for_user(self, user_id: str | None) -> list[ArcSeries]:
        """List enabled pack rows and rows owned by ``user_id``."""

    @abstractmethod
    async def save_for_user(
        self,
        series: ArcSeries,
        *,
        user_id: str,
        overwrite: bool = False,
    ) -> str:
        """Insert/update a user-owned series."""

    @abstractmethod
    async def delete_for_user(self, series_id: str, *, user_id: str) -> bool:
        """Delete a caller-owned series. Pack rows are not deleted."""

    @abstractmethod
    async def upsert_pack(
        self,
        series: ArcSeries,
        *,
        pack_id: str,
        external_id: str | None = None,
    ) -> str:
        """Insert/update a bundled series row."""

    @abstractmethod
    async def get_progress(
        self, character_id: str, series_id: str,
    ) -> CharacterSeriesProgress | None:
        """Return per-character progress for this series."""

    @abstractmethod
    async def save_progress(self, progress: CharacterSeriesProgress) -> None:
        """Upsert per-character progress."""

    @abstractmethod
    async def clear_progress_for_character(self, character_id: str) -> int:
        """Remove all series progress for a character."""

    @abstractmethod
    async def clear_progress_for_series(self, series_id: str) -> int:
        """Remove all character progress for a series."""


__all__ = ["ArcSeriesRepositoryPort"]
