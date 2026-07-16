"""SA-backed StorySeed + StoryEvent repositories."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.story import (
    StoryEventRepositoryPort,
    StorySeedRepositoryPort,
)
from kokoro_link.domain.entities.story_event import StoryEvent
from kokoro_link.domain.entities.story_seed import StorySeed
from kokoro_link.infrastructure.persistence.models import (
    StoryEventRow,
    StorySeedRow,
)


# ---- Seed repository -------------------------------------------------


class SAStorySeedRepository(StorySeedRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def upsert_by_external_id(self, seed: StorySeed) -> StorySeed:
        if not seed.external_id:
            raise ValueError("upsert_by_external_id requires external_id")
        async with self._session_factory() as session:
            stmt = select(StorySeedRow).where(
                StorySeedRow.external_id == seed.external_id,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                row = _seed_to_row(seed)
                session.add(row)
            else:
                _apply_seed_updates(row, seed)
            await session.commit()
            await session.refresh(row)
        return _row_to_seed(row)

    async def add(self, seed: StorySeed) -> StorySeed:
        async with self._session_factory() as session:
            row = _seed_to_row(seed)
            session.add(row)
            await session.commit()
        return seed

    async def get(self, seed_id: str) -> StorySeed | None:
        async with self._session_factory() as session:
            row = await session.get(StorySeedRow, seed_id)
            if row is None:
                return None
            return _row_to_seed(row)

    async def list_for_character(
        self,
        character_id: str,
        *,
        include_global: bool = True,
        enabled_only: bool = True,
    ) -> list[StorySeed]:
        async with self._session_factory() as session:
            stmt = select(StorySeedRow)
            if include_global:
                stmt = stmt.where(
                    or_(
                        StorySeedRow.character_id == character_id,
                        StorySeedRow.character_id.is_(None),
                    ),
                )
            else:
                stmt = stmt.where(StorySeedRow.character_id == character_id)
            if enabled_only:
                stmt = stmt.where(StorySeedRow.enabled.is_(True))
            stmt = stmt.order_by(StorySeedRow.created_at.asc())
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        return [_row_to_seed(row) for row in rows]

    async def list_by_pack(self, pack_id: str) -> list[StorySeed]:
        async with self._session_factory() as session:
            stmt = select(StorySeedRow).where(StorySeedRow.pack_id == pack_id)
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        return [_row_to_seed(row) for row in rows]

    async def update(self, seed: StorySeed) -> StorySeed:
        async with self._session_factory() as session:
            row = await session.get(StorySeedRow, seed.id)
            if row is None:
                raise ValueError(f"StorySeed {seed.id} not found")
            _apply_seed_updates(row, seed)
            await session.commit()
            await session.refresh(row)
        return _row_to_seed(row)

    async def delete(self, seed_id: str) -> bool:
        async with self._session_factory() as session:
            row = await session.get(StorySeedRow, seed_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
        return True


def _seed_to_row(seed: StorySeed) -> StorySeedRow:
    return StorySeedRow(
        id=seed.id,
        seed_text=seed.seed_text,
        tags=json.dumps(list(seed.tags), ensure_ascii=False),
        world_frames=json.dumps(list(seed.world_frames), ensure_ascii=False),
        weight=float(seed.weight),
        cooldown_days=int(seed.cooldown_days),
        enabled=bool(seed.enabled),
        language=seed.language or "zh-TW",
        character_id=seed.character_id,
        external_id=seed.external_id,
        pack_id=seed.pack_id,
        created_at=_ensure_utc(seed.created_at),
        updated_at=_ensure_utc(seed.updated_at),
    )


def _apply_seed_updates(row: StorySeedRow, seed: StorySeed) -> None:
    """Mutate ``row`` toward ``seed`` while preserving identity."""
    row.seed_text = seed.seed_text
    row.tags = json.dumps(list(seed.tags), ensure_ascii=False)
    row.world_frames = json.dumps(list(seed.world_frames), ensure_ascii=False)
    row.weight = float(seed.weight)
    row.cooldown_days = int(seed.cooldown_days)
    row.enabled = bool(seed.enabled)
    row.language = seed.language or "zh-TW"
    row.character_id = seed.character_id
    row.pack_id = seed.pack_id
    row.updated_at = datetime.now(timezone.utc)


def _row_to_seed(row: StorySeedRow) -> StorySeed:
    try:
        tags = tuple(json.loads(row.tags or "[]"))
    except json.JSONDecodeError:
        tags = ()
    try:
        frames_raw = json.loads(row.world_frames or '["any"]')
        frames = tuple(frames_raw) if isinstance(frames_raw, list) else ("any",)
    except json.JSONDecodeError:
        frames = ("any",)
    return StorySeed(
        id=row.id,
        seed_text=row.seed_text,
        tags=tags,
        world_frames=frames,
        weight=float(row.weight),
        cooldown_days=int(row.cooldown_days),
        enabled=bool(row.enabled),
        language=getattr(row, "language", None) or "zh-TW",
        character_id=row.character_id,
        external_id=row.external_id,
        pack_id=row.pack_id,
        created_at=_ensure_utc(row.created_at),
        updated_at=_ensure_utc(row.updated_at),
    )


# ---- Event repository ------------------------------------------------


class SAStoryEventRepository(StoryEventRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, event: StoryEvent) -> StoryEvent:
        async with self._session_factory() as session:
            session.add(_event_to_row(event))
            await session.commit()
        return event

    async def get_for_day(
        self, character_id: str, date: str,
    ) -> list[StoryEvent]:
        async with self._session_factory() as session:
            stmt = (
                select(StoryEventRow)
                .where(
                    StoryEventRow.character_id == character_id,
                    StoryEventRow.date == date,
                )
                .order_by(StoryEventRow.created_at.asc())
            )
            rows = list((await session.execute(stmt)).scalars().all())
        return [_row_to_event(row) for row in rows]

    async def list_recent(
        self, character_id: str, *, limit: int = 10,
    ) -> list[StoryEvent]:
        async with self._session_factory() as session:
            stmt = (
                select(StoryEventRow)
                .where(StoryEventRow.character_id == character_id)
                .order_by(StoryEventRow.date.desc(), StoryEventRow.created_at.desc())
                .limit(limit)
            )
            rows = list((await session.execute(stmt)).scalars().all())
        return [_row_to_event(row) for row in rows]

    async def last_roll_dates(
        self, character_id: str,
    ) -> dict[str, str]:
        async with self._session_factory() as session:
            # Filter out arc-driven events (seed_id IS NULL) so the
            # gacha cooldown check stays only about actual gacha rolls.
            stmt = (
                select(
                    StoryEventRow.seed_id,
                    func.max(StoryEventRow.date).label("last_date"),
                )
                .where(
                    StoryEventRow.character_id == character_id,
                    StoryEventRow.seed_id.is_not(None),
                )
                .group_by(StoryEventRow.seed_id)
            )
            rows = list((await session.execute(stmt)).all())
        return {seed_id: last_date for seed_id, last_date in rows if seed_id}

    async def mark_memorialized(self, event_id: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(StoryEventRow)
                .where(StoryEventRow.id == event_id)
                .values(memorialized=True),
            )
            await session.commit()

    async def delete_for_character(self, character_id: str) -> int:
        async with self._session_factory() as session:
            count_stmt = select(func.count(StoryEventRow.id)).where(
                StoryEventRow.character_id == character_id,
            )
            count = int((await session.execute(count_stmt)).scalar_one() or 0)
            if count == 0:
                return 0
            await session.execute(
                delete(StoryEventRow).where(
                    StoryEventRow.character_id == character_id,
                ),
            )
            await session.commit()
        return count


def _event_to_row(event: StoryEvent) -> StoryEventRow:
    return StoryEventRow(
        id=event.id,
        character_id=event.character_id,
        date=event.date,
        seed_id=event.seed_id,
        arc_beat_id=event.arc_beat_id,
        narrative=event.narrative,
        emotional_tone=event.emotional_tone,
        memorialized=bool(event.memorialized),
        created_at=_ensure_utc(event.created_at),
    )


def _row_to_event(row: StoryEventRow) -> StoryEvent:
    return StoryEvent(
        id=row.id,
        character_id=row.character_id,
        date=row.date,
        seed_id=row.seed_id,
        arc_beat_id=row.arc_beat_id,
        narrative=row.narrative,
        emotional_tone=row.emotional_tone,
        memorialized=bool(row.memorialized),
        created_at=_ensure_utc(row.created_at),
    )


def _ensure_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
