"""In-memory ``ArcSeriesRepositoryPort`` implementation."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from kokoro_link.contracts.arc_series import ArcSeriesRepositoryPort
from kokoro_link.domain.entities.arc_series import (
    ArcSeries,
    CharacterSeriesProgress,
)


@dataclass(slots=True)
class _StoredSeries:
    series: ArcSeries
    user_id: str | None
    pack_id: str | None = None
    external_id: str | None = None
    enabled: bool = True


@dataclass(slots=True)
class InMemoryArcSeriesRepository(ArcSeriesRepositoryPort):
    _rows: dict[str, _StoredSeries] = field(default_factory=dict)
    _progress: dict[tuple[str, str], CharacterSeriesProgress] = field(
        default_factory=dict,
    )
    _lock: threading.RLock = field(default_factory=threading.RLock)

    async def get_for_user(
        self, series_id: str, *, user_id: str | None,
    ) -> ArcSeries | None:
        with self._lock:
            row = self._rows.get(series_id)
            if row is None or not row.enabled:
                return None
            if row.user_id is None:
                return row.series
            if user_id is not None and row.user_id == user_id:
                return row.series
            return None

    async def list_for_user(self, user_id: str | None) -> list[ArcSeries]:
        with self._lock:
            visible: list[_StoredSeries] = []
            for row in self._rows.values():
                if not row.enabled:
                    continue
                if row.user_id is None:
                    visible.append(row)
                elif user_id is not None and row.user_id == user_id:
                    visible.append(row)
            visible.sort(key=lambda row: row.series.id)
            return [row.series for row in visible]

    async def save_for_user(
        self,
        series: ArcSeries,
        *,
        user_id: str,
        overwrite: bool = False,
    ) -> str:
        with self._lock:
            existing = self._rows.get(series.id)
            if existing is not None:
                if existing.user_id is None:
                    raise ValueError(
                        f"Arc series id {series.id!r} is reserved by a bundled pack.",
                    )
                if existing.user_id != user_id:
                    raise ValueError(
                        f"Arc series id {series.id!r} already exists.",
                    )
                if not overwrite:
                    raise ValueError(
                        f"Arc series id {series.id!r} already exists.",
                    )
            owned = _with_owner(series, user_id=user_id, pack_id=None, external_id=None)
            self._rows[owned.id] = _StoredSeries(
                series=owned,
                user_id=user_id,
                enabled=owned.enabled,
            )
            return owned.id

    async def delete_for_user(self, series_id: str, *, user_id: str) -> bool:
        with self._lock:
            row = self._rows.get(series_id)
            if row is None or row.user_id != user_id:
                return False
            del self._rows[series_id]
            return True

    async def upsert_pack(
        self,
        series: ArcSeries,
        *,
        pack_id: str,
        external_id: str | None = None,
    ) -> str:
        with self._lock:
            existing = self._rows.get(series.id)
            if existing is not None and existing.user_id is not None:
                raise ValueError(
                    f"Cannot upsert pack {series.id!r}: a user-authored row owns it.",
                )
            packed = _with_owner(
                series,
                user_id=None,
                pack_id=pack_id,
                external_id=external_id,
            )
            self._rows[packed.id] = _StoredSeries(
                series=packed,
                user_id=None,
                pack_id=pack_id,
                external_id=external_id,
                enabled=packed.enabled,
            )
            return packed.id

    async def get_progress(
        self, character_id: str, series_id: str,
    ) -> CharacterSeriesProgress | None:
        with self._lock:
            return self._progress.get((character_id, series_id))

    async def save_progress(self, progress: CharacterSeriesProgress) -> None:
        with self._lock:
            self._progress[(progress.character_id, progress.series_id)] = progress

    async def clear_progress_for_character(self, character_id: str) -> int:
        with self._lock:
            victims = [
                key for key in self._progress
                if key[0] == character_id
            ]
            for key in victims:
                del self._progress[key]
            return len(victims)

    async def clear_progress_for_series(self, series_id: str) -> int:
        with self._lock:
            victims = [
                key for key in self._progress
                if key[1] == series_id
            ]
            for key in victims:
                del self._progress[key]
            return len(victims)


def _with_owner(
    series: ArcSeries,
    *,
    user_id: str | None,
    pack_id: str | None,
    external_id: str | None,
) -> ArcSeries:
    return ArcSeries(
        id=series.id,
        title=series.title,
        premise=series.premise,
        theme=series.theme,
        tone=series.tone,
        binding=series.binding,
        members=series.members,
        user_id=user_id,
        pack_id=pack_id,
        external_id=external_id,
        enabled=series.enabled,
        created_at=series.created_at,
        updated_at=series.updated_at,
    )


__all__ = ["InMemoryArcSeriesRepository"]
