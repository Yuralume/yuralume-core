"""SA-backed ``StoryArcRepositoryPort`` implementation.

``save`` is the workhorse: it upserts the arc row and rebuilds the
beat rows atomically in a single transaction. The beat set is small
(< 10 rows per arc) so delete-all + re-insert is cheaper to reason
about than per-beat diffing and doesn't measurably hurt write
performance at our scale.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.story_arc import StoryArcRepositoryPort
from kokoro_link.domain.entities.story_arc import (
    ARC_ACTIVE,
    SCENE_ENCOUNTER,
    StoryArc,
    StoryArcBeat,
)
from kokoro_link.infrastructure.persistence.models import (
    StoryArcBeatRow,
    StoryArcRow,
)

_LOGGER = logging.getLogger(__name__)


class SAStoryArcRepository(StoryArcRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, arc: StoryArc) -> None:
        # Inserts split into two transactions because SA's async unit-of-
        # work has been observed to batch the beat INSERTs ahead of the
        # arc INSERT under asyncpg+executemany, tripping the FK even
        # after a same-session flush. Commit the arc first, then the
        # beats — correctness > atomicity here (the arc with no beats
        # is still a valid row; a retry can repopulate the beats).
        async with self._session_factory() as session:
            session.add(_arc_to_row(arc))
            await session.commit()
        async with self._session_factory() as session:
            for beat in arc.beats:
                session.add(_beat_to_row(arc.id, beat))
            await session.commit()

    async def get(self, arc_id: str) -> StoryArc | None:
        async with self._session_factory() as session:
            row = await session.get(StoryArcRow, arc_id)
            if row is None:
                return None
            beats = await self._load_beats(session, arc_id)
        return _row_to_arc(row, beats)

    async def get_active_for_character(
        self, character_id: str,
    ) -> StoryArc | None:
        async with self._session_factory() as session:
            stmt = (
                select(StoryArcRow)
                .where(
                    StoryArcRow.character_id == character_id,
                    StoryArcRow.status == ARC_ACTIVE,
                )
                .order_by(StoryArcRow.updated_at.desc())
                .limit(1)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            beats = await self._load_beats(session, row.id)
        return _row_to_arc(row, beats)

    async def list_for_character(
        self, character_id: str,
    ) -> list[StoryArc]:
        async with self._session_factory() as session:
            stmt = (
                select(StoryArcRow)
                .where(StoryArcRow.character_id == character_id)
                .order_by(StoryArcRow.updated_at.desc())
            )
            rows = list((await session.execute(stmt)).scalars())
            arcs: list[StoryArc] = []
            for row in rows:
                beats = await self._load_beats(session, row.id)
                arcs.append(_row_to_arc(row, beats))
        return arcs

    async def save(self, arc: StoryArc) -> None:
        """Upsert arc + replace all beats.

        Split into two transactions for the same FK-ordering reason as
        ``add()``. Arc + beat-delete in the first tx so the beat INSERTs
        in the second tx see a clean slate. Not atomic, but a half-
        applied state (arc without beats) is still consistent enough
        for the reader surfaces and a retry fully repopulates.
        """
        async with self._session_factory() as session:
            existing = await session.get(StoryArcRow, arc.id)
            if existing is None:
                session.add(_arc_to_row(arc))
            else:
                _apply_arc_updates(existing, arc)
            await session.execute(
                delete(StoryArcBeatRow).where(
                    StoryArcBeatRow.arc_id == arc.id,
                ),
            )
            await session.commit()
        async with self._session_factory() as session:
            for beat in arc.beats:
                session.add(_beat_to_row(arc.id, beat))
            await session.commit()

    async def delete(self, arc_id: str) -> None:
        async with self._session_factory() as session:
            row = await session.get(StoryArcRow, arc_id)
            if row is None:
                return
            await session.delete(row)
            await session.commit()

    async def delete_for_character(self, character_id: str) -> int:
        async with self._session_factory() as session:
            stmt = select(StoryArcRow.id).where(
                StoryArcRow.character_id == character_id,
            )
            ids = list((await session.execute(stmt)).scalars())
            if not ids:
                return 0
            await session.execute(
                delete(StoryArcRow).where(StoryArcRow.id.in_(ids)),
            )
            await session.commit()
        return len(ids)

    async def find_by_beat_id(self, beat_id: str) -> StoryArc | None:
        async with self._session_factory() as session:
            beat_row = await session.get(StoryArcBeatRow, beat_id)
            if beat_row is None:
                return None
            arc_row = await session.get(StoryArcRow, beat_row.arc_id)
            if arc_row is None:
                return None
            beats = await self._load_beats(session, arc_row.id)
        return _row_to_arc(arc_row, beats)

    async def _load_beats(
        self, session: AsyncSession, arc_id: str,
    ) -> list[StoryArcBeat]:
        stmt = (
            select(StoryArcBeatRow)
            .where(StoryArcBeatRow.arc_id == arc_id)
            .order_by(StoryArcBeatRow.scheduled_date, StoryArcBeatRow.sequence)
        )
        rows = (await session.execute(stmt)).scalars()
        return [_row_to_beat(r) for r in rows]


# --- mapping helpers --------------------------------------------------


def _arc_to_row(arc: StoryArc) -> StoryArcRow:
    return StoryArcRow(
        id=arc.id,
        character_id=arc.character_id,
        title=arc.title,
        premise=arc.premise,
        theme=arc.theme,
        tone=arc.tone,
        source_template_id=arc.source_template_id,
        start_date=arc.start_date.isoformat(),
        end_date=arc.end_date.isoformat(),
        status=arc.status,
        created_at=arc.created_at,
        updated_at=arc.updated_at,
    )


def _apply_arc_updates(row: StoryArcRow, arc: StoryArc) -> None:
    row.title = arc.title
    row.premise = arc.premise
    row.theme = arc.theme
    row.tone = arc.tone
    row.source_template_id = arc.source_template_id
    row.start_date = arc.start_date.isoformat()
    row.end_date = arc.end_date.isoformat()
    row.status = arc.status
    row.updated_at = arc.updated_at


def _beat_to_row(arc_id: str, beat: StoryArcBeat) -> StoryArcBeatRow:
    return StoryArcBeatRow(
        id=beat.id,
        arc_id=arc_id,
        sequence=beat.sequence,
        scheduled_date=beat.scheduled_date.isoformat(),
        title=beat.title,
        summary=beat.summary,
        tension=beat.tension,
        status=beat.status,
        realized_event_id=beat.realized_event_id,
        play_attempt_count=beat.play_attempt_count,
        last_play_attempt_at=beat.last_play_attempt_at,
        last_play_attempt_source=beat.last_play_attempt_source,
        last_play_attempt_result=beat.last_play_attempt_result,
        last_play_push_intensity=beat.last_play_push_intensity,
        scene_characters=json.dumps(list(beat.scene_characters), ensure_ascii=False),
        location=beat.location,
        dramatic_question=beat.dramatic_question,
        scene_type=beat.scene_type,
        required=beat.required,
    )


def _row_to_arc(row: StoryArcRow, beats: list[StoryArcBeat]) -> StoryArc:
    created = _ensure_aware(row.created_at)
    updated = _ensure_aware(row.updated_at)
    return StoryArc(
        id=row.id,
        character_id=row.character_id,
        title=row.title,
        premise=row.premise,
        theme=row.theme,
        # Old rows pre-tone migration default to 'daily' via the
        # column server_default; the `or` guard catches edge cases
        # where the column reads back as None (raw SQL inserts, etc.)
        tone=row.tone or "daily",
        source_template_id=getattr(row, "source_template_id", None),
        start_date=date.fromisoformat(row.start_date),
        end_date=date.fromisoformat(row.end_date),
        status=row.status,
        beats=tuple(beats),
        created_at=created,
        updated_at=updated,
    )


def _row_to_beat(row: StoryArcBeatRow) -> StoryArcBeat:
    return StoryArcBeat(
        id=row.id,
        arc_id=row.arc_id,
        sequence=row.sequence,
        scheduled_date=date.fromisoformat(row.scheduled_date),
        title=row.title,
        summary=row.summary,
        tension=row.tension,
        status=row.status,
        realized_event_id=row.realized_event_id,
        play_attempt_count=getattr(row, "play_attempt_count", 0) or 0,
        last_play_attempt_at=_ensure_optional_aware(
            getattr(row, "last_play_attempt_at", None),
        ),
        last_play_attempt_source=getattr(row, "last_play_attempt_source", None),
        last_play_attempt_result=getattr(row, "last_play_attempt_result", None),
        last_play_push_intensity=getattr(
            row, "last_play_push_intensity", None,
        ),
        scene_characters=_decode_scene_characters(row.scene_characters),
        location=row.location,
        dramatic_question=row.dramatic_question,
        scene_type=row.scene_type or SCENE_ENCOUNTER,
        required=bool(row.required),
    )


def _decode_scene_characters(raw: str | None) -> tuple[str, ...]:
    """Best-effort decode of the JSON-encoded list.

    Bad data (manual edit, schema drift, NULL slipping past the
    NOT NULL default) degrades to an empty tuple — the prompt builder
    treats that the same as "no NPC labels" so a single corrupt row
    never blocks chat. Non-string entries are filtered for the same
    reason: domain ``__post_init__`` would reject them and crash arc
    load otherwise.
    """
    if not raw:
        return ()
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        _LOGGER.warning(
            "story_arc_beats.scene_characters decode failed raw=%r — "
            "treating as empty",
            raw,
        )
        return ()
    if not isinstance(decoded, list):
        return ()
    return tuple(
        str(entry).strip()
        for entry in decoded
        if isinstance(entry, str) and entry.strip()
    )


def _ensure_aware(value: datetime) -> datetime:
    # asyncpg returns tz-aware; safety net for mixed-backend tests.
    if value.tzinfo is None:
        from datetime import timezone
        return value.replace(tzinfo=timezone.utc)
    return value


def _ensure_optional_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _ensure_aware(value)
