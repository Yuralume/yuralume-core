"""SQLAlchemy ArcSeries repository."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.arc_series import ArcSeriesRepositoryPort
from kokoro_link.domain.entities.arc_series import (
    ArcSeries,
    ArcSeriesMember,
    CharacterSeriesProgress,
)
from kokoro_link.domain.entities.arc_template import ArcTemplateBinding
from kokoro_link.infrastructure.persistence.models import (
    ArcSeriesMemberRow,
    ArcSeriesRow,
    CharacterSeriesProgressRow,
)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _dump_str_list(values: tuple[str, ...]) -> str:
    return json.dumps(list(values), ensure_ascii=False)


def _load_str_list(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(data, list):
        return ()
    return tuple(entry for entry in data if isinstance(entry, str))


class SAArcSeriesRepository(ArcSeriesRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_for_user(
        self, series_id: str, *, user_id: str | None,
    ) -> ArcSeries | None:
        async with self._session_factory() as session:
            row = await self._fetch_visible(session, series_id, user_id)
            if row is None:
                return None
            members = await self._load_members(session, row.id)
        return _row_to_domain(row, members)

    async def list_for_user(self, user_id: str | None) -> list[ArcSeries]:
        async with self._session_factory() as session:
            if user_id is None:
                stmt = (
                    select(ArcSeriesRow)
                    .where(
                        ArcSeriesRow.user_id.is_(None),
                        ArcSeriesRow.enabled.is_(True),
                    )
                    .order_by(ArcSeriesRow.id)
                )
            else:
                stmt = (
                    select(ArcSeriesRow)
                    .where(
                        or_(
                            ArcSeriesRow.user_id.is_(None),
                            ArcSeriesRow.user_id == user_id,
                        ),
                        ArcSeriesRow.enabled.is_(True),
                    )
                    .order_by(ArcSeriesRow.id)
                )
            rows = list((await session.execute(stmt)).scalars())
            out: list[ArcSeries] = []
            for row in rows:
                members = await self._load_members(session, row.id)
                out.append(_row_to_domain(row, members))
            return out

    async def save_for_user(
        self,
        series: ArcSeries,
        *,
        user_id: str,
        overwrite: bool = False,
    ) -> str:
        now = _now_utc()
        async with self._session_factory() as session:
            existing = await session.get(ArcSeriesRow, series.id)
            if existing is not None:
                if existing.user_id is None:
                    raise ValueError(
                        f"Arc series id {series.id!r} is reserved by a bundled pack.",
                    )
                if existing.user_id != user_id:
                    raise ValueError(f"Arc series id {series.id!r} already exists.")
                if not overwrite:
                    raise ValueError(f"Arc series id {series.id!r} already exists.")
                _populate_row(existing, series, now=now)
                await session.execute(
                    delete(ArcSeriesMemberRow).where(
                        ArcSeriesMemberRow.series_id == existing.id,
                    ),
                )
                for member in series.members:
                    session.add(_member_to_row(existing.id, member))
                await session.commit()
                return existing.id
            row = _series_to_row(
                series,
                now=now,
                user_id=user_id,
                pack_id=None,
                external_id=None,
            )
            session.add(row)
            await session.flush()
            for member in series.members:
                session.add(_member_to_row(row.id, member))
            await session.commit()
            return row.id

    async def delete_for_user(self, series_id: str, *, user_id: str) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(ArcSeriesRow).where(
                    ArcSeriesRow.id == series_id,
                    ArcSeriesRow.user_id == user_id,
                ),
            )
            await session.commit()
            return bool(result.rowcount)

    async def upsert_pack(
        self,
        series: ArcSeries,
        *,
        pack_id: str,
        external_id: str | None = None,
    ) -> str:
        now = _now_utc()
        async with self._session_factory() as session:
            existing = await session.get(ArcSeriesRow, series.id)
            if existing is not None:
                if existing.user_id is not None:
                    raise ValueError(
                        f"Cannot upsert pack {series.id!r}: a user-authored row owns it.",
                    )
                _populate_row(existing, series, now=now)
                existing.pack_id = pack_id
                existing.external_id = external_id
                existing.enabled = series.enabled
                await session.execute(
                    delete(ArcSeriesMemberRow).where(
                        ArcSeriesMemberRow.series_id == existing.id,
                    ),
                )
                for member in series.members:
                    session.add(_member_to_row(existing.id, member))
                await session.commit()
                return existing.id
            row = _series_to_row(
                series,
                now=now,
                user_id=None,
                pack_id=pack_id,
                external_id=external_id,
            )
            session.add(row)
            await session.flush()
            for member in series.members:
                session.add(_member_to_row(row.id, member))
            await session.commit()
            return row.id

    async def get_progress(
        self, character_id: str, series_id: str,
    ) -> CharacterSeriesProgress | None:
        async with self._session_factory() as session:
            stmt = select(CharacterSeriesProgressRow).where(
                CharacterSeriesProgressRow.character_id == character_id,
                CharacterSeriesProgressRow.series_id == series_id,
            )
            row = (await session.execute(stmt)).scalars().first()
            return _progress_row_to_domain(row) if row is not None else None

    async def save_progress(self, progress: CharacterSeriesProgress) -> None:
        async with self._session_factory() as session:
            stmt = select(CharacterSeriesProgressRow).where(
                CharacterSeriesProgressRow.character_id == progress.character_id,
                CharacterSeriesProgressRow.series_id == progress.series_id,
            )
            row = (await session.execute(stmt)).scalars().first()
            if row is None:
                session.add(_progress_to_row(progress))
            else:
                row.current_index = progress.current_index
                row.status = progress.status
                row.last_arc_id = progress.last_arc_id
                row.updated_at = progress.updated_at
            await session.commit()

    async def clear_progress_for_character(self, character_id: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(CharacterSeriesProgressRow).where(
                    CharacterSeriesProgressRow.character_id == character_id,
                ),
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def clear_progress_for_series(self, series_id: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(CharacterSeriesProgressRow).where(
                    CharacterSeriesProgressRow.series_id == series_id,
                ),
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def _fetch_visible(
        self,
        session: AsyncSession,
        series_id: str,
        user_id: str | None,
    ) -> ArcSeriesRow | None:
        row = await session.get(ArcSeriesRow, series_id)
        if row is None or not row.enabled:
            return None
        if row.user_id is None:
            return row
        if user_id is not None and row.user_id == user_id:
            return row
        return None

    async def _load_members(
        self, session: AsyncSession, series_id: str,
    ) -> list[ArcSeriesMember]:
        stmt = (
            select(ArcSeriesMemberRow)
            .where(ArcSeriesMemberRow.series_id == series_id)
            .order_by(ArcSeriesMemberRow.position)
        )
        rows = list((await session.execute(stmt)).scalars())
        return [
            ArcSeriesMember(
                template_id=row.template_id,
                position=row.position,
            )
            for row in rows
        ]


def _row_to_domain(
    row: ArcSeriesRow, members: list[ArcSeriesMember],
) -> ArcSeries:
    return ArcSeries(
        id=row.id,
        title=row.title,
        premise=row.premise,
        theme=row.theme,
        tone=row.tone,
        binding=ArcTemplateBinding(
            world_frames=_load_str_list(row.world_frames_json),
            required_traits=_load_str_list(row.required_traits_json),
        ),
        members=tuple(members),
        user_id=row.user_id,
        pack_id=row.pack_id,
        external_id=row.external_id,
        enabled=row.enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _populate_row(row: ArcSeriesRow, series: ArcSeries, *, now: datetime) -> None:
    row.title = series.title
    row.premise = series.premise
    row.theme = series.theme
    row.tone = series.tone
    row.world_frames_json = _dump_str_list(series.binding.world_frames)
    row.required_traits_json = _dump_str_list(series.binding.required_traits)
    row.enabled = series.enabled
    row.updated_at = now


def _series_to_row(
    series: ArcSeries,
    *,
    now: datetime,
    user_id: str | None,
    pack_id: str | None,
    external_id: str | None,
) -> ArcSeriesRow:
    return ArcSeriesRow(
        id=series.id,
        user_id=user_id,
        pack_id=pack_id,
        external_id=external_id,
        title=series.title,
        premise=series.premise,
        theme=series.theme,
        tone=series.tone,
        world_frames_json=_dump_str_list(series.binding.world_frames),
        required_traits_json=_dump_str_list(series.binding.required_traits),
        enabled=series.enabled,
        created_at=now,
        updated_at=now,
    )


def _member_to_row(series_id: str, member: ArcSeriesMember) -> ArcSeriesMemberRow:
    return ArcSeriesMemberRow(
        series_id=series_id,
        template_id=member.template_id,
        position=member.position,
    )


def _progress_row_to_domain(
    row: CharacterSeriesProgressRow,
) -> CharacterSeriesProgress:
    return CharacterSeriesProgress(
        character_id=row.character_id,
        series_id=row.series_id,
        current_index=row.current_index,
        status=row.status,
        last_arc_id=row.last_arc_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _progress_to_row(
    progress: CharacterSeriesProgress,
) -> CharacterSeriesProgressRow:
    return CharacterSeriesProgressRow(
        character_id=progress.character_id,
        series_id=progress.series_id,
        current_index=progress.current_index,
        status=progress.status,
        last_arc_id=progress.last_arc_id,
        created_at=progress.created_at,
        updated_at=progress.updated_at,
    )


__all__ = ["SAArcSeriesRepository"]
