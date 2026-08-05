"""SQLAlchemy repository for player-pulled story scene sessions (SC1-A).

The interesting part is the write path, not the reads: the "one open
scene per character" rule is fenced by ``uq_story_scene_sessions_open_
character``, a partial unique index, and a violation of *that* index is a
benign race (two taps of 起幕 landing on two api replicas) rather than a
defect. It is translated into :class:`SceneSessionConflict` after a clean
rollback, the same shape ``SAStoryArcRepository`` uses for its
single-active-arc index. Any other integrity failure is a real bug and is
allowed to surface.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Sequence

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.contracts.story_scene import (
    SceneSessionConflict,
    StorySceneSessionRepositoryPort,
)
from kokoro_link.domain.entities.story_arc import normalise_operator_position
from kokoro_link.domain.entities.story_scene_session import (
    SCENE_CLOSED,
    SCENE_OPEN,
    StorySceneSession,
)
from kokoro_link.infrastructure.persistence.models import StorySceneSessionRow


_LOGGER = logging.getLogger(__name__)

_OPEN_SCENE_INDEX = "uq_story_scene_sessions_open_character"

# PostgreSQL names the offending index in the error text; SQLite names the
# columns instead ("UNIQUE constraint failed: story_scene_sessions.
# character_id"). ``character_id`` carries no other unique index on this
# table, so the column form cannot be confused with an unrelated violation.
_SQLITE_OPEN_SCENE_COLUMNS = "story_scene_sessions.character_id"


def _is_open_scene_violation(error: IntegrityError) -> bool:
    message = str(getattr(error, "orig", error))
    return (
        _OPEN_SCENE_INDEX in message
        or _SQLITE_OPEN_SCENE_COLUMNS in message
    )


class SAStorySceneSessionRepository(StorySceneSessionRepositoryPort):
    def __init__(self, session_factory: "sessionmaker[AsyncSession]") -> None:
        self._session_factory = session_factory

    async def add(self, scene: StorySceneSession) -> None:
        async with self._session_factory() as session:
            session.add(_to_row(scene))
            await self._commit_fenced(session, scene)

    async def get(self, session_id: str) -> StorySceneSession | None:
        async with self._session_factory() as session:
            row = await session.get(StorySceneSessionRow, session_id)
            return _to_domain(row) if row is not None else None

    async def get_open_for_character(
        self, character_id: str,
    ) -> StorySceneSession | None:
        async with self._session_factory() as session:
            stmt = (
                select(StorySceneSessionRow)
                .where(StorySceneSessionRow.character_id == character_id)
                .where(StorySceneSessionRow.status == SCENE_OPEN)
                # The partial unique index makes this at most one row; the
                # ordering only decides which row a *pre-migration* database
                # would surface, and newest-first matches what the player
                # last saw.
                .order_by(StorySceneSessionRow.opened_at.desc())
                .limit(1)
            )
            row = (await session.execute(stmt)).scalars().first()
            return _to_domain(row) if row is not None else None

    async def save(self, scene: StorySceneSession) -> None:
        async with self._session_factory() as session:
            row = await session.get(StorySceneSessionRow, scene.id)
            if row is None:
                session.add(_to_row(scene))
            else:
                _apply(row, scene)
            await self._commit_fenced(session, scene)

    async def delete(self, session_id: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                delete(StorySceneSessionRow).where(
                    StorySceneSessionRow.id == session_id,
                ),
            )
            await session.commit()

    async def touch_activity(self, session_id: str, *, at: datetime) -> bool:
        moment = ensure_utc(at)
        async with self._session_factory() as session:
            stmt = (
                update(StorySceneSessionRow)
                .where(StorySceneSessionRow.id == session_id)
                .where(StorySceneSessionRow.status == SCENE_OPEN)
                # Monotonic in SQL rather than read-modify-write: two
                # replicas finishing turns out of order both run this and
                # only the later instant survives, with no lost update in
                # between.
                .where(StorySceneSessionRow.last_activity_at < moment)
                .values(last_activity_at=moment)
            )
            result = await session.execute(stmt)
            await session.commit()
            if result.rowcount:
                return True
            # Zero rows means one of two very different things — closed
            # (chips are moot) or already stamped later (scene is live and
            # the answer is yes). Only this rare branch pays for the read
            # that tells them apart.
            row = await session.get(StorySceneSessionRow, session_id)
            return row is not None and row.status == SCENE_OPEN

    async def close(
        self, session_id: str, *, reason: str, at: datetime,
    ) -> StorySceneSession | None:
        moment = ensure_utc(at)
        async with self._session_factory() as session:
            stmt = (
                update(StorySceneSessionRow)
                .where(StorySceneSessionRow.id == session_id)
                # The whole fence. Three closers can arrive at once (the
                # in-turn verdict, the player's button, the idle sweep) on
                # three replicas; the one whose UPDATE matches an ``open``
                # row is the one that gets to write the closing narration.
                .where(StorySceneSessionRow.status == SCENE_OPEN)
                .values(
                    status=SCENE_CLOSED,
                    closed_at=moment,
                    closed_reason=reason,
                    # Mirrors the entity's monotonic rule in SQL. ``GREATEST``
                    # would be shorter and does not exist on SQLite, so the
                    # self-host backend would only find out in production.
                    last_activity_at=case(
                        (
                            StorySceneSessionRow.last_activity_at > moment,
                            StorySceneSessionRow.last_activity_at,
                        ),
                        else_=moment,
                    ),
                )
            )
            result = await session.execute(stmt)
            await session.commit()
            if not result.rowcount:
                return None
            # Only the winner pays for this read, and it needs the row
            # anyway: the closed session is what the caller answers with.
            row = await session.get(StorySceneSessionRow, session_id)
            return _to_domain(row) if row is not None else None

    async def _commit_fenced(
        self, session: AsyncSession, scene: StorySceneSession,
    ) -> None:
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            if not _is_open_scene_violation(exc):
                raise
            _LOGGER.info(
                "story scene lost the open-scene slot character=%s scene=%s",
                scene.character_id,
                scene.id,
            )
            raise SceneSessionConflict(scene.character_id) from exc

    async def list_stale_open(
        self, *, idle_before: datetime, limit: int = 100,
    ) -> tuple[StorySceneSession, ...]:
        if limit <= 0:
            return ()
        cutoff = ensure_utc(idle_before)
        async with self._session_factory() as session:
            stmt = (
                select(StorySceneSessionRow)
                # ``(status, last_activity_at)`` — the composite SC1-A
                # created for this sweep. The character column is
                # deliberately absent from both the filter and the order:
                # the rows worth finding here are the ones no character
                # owns any more.
                .where(StorySceneSessionRow.status == SCENE_OPEN)
                .where(StorySceneSessionRow.last_activity_at < cutoff)
                .order_by(StorySceneSessionRow.last_activity_at.asc())
                .limit(limit)
            )
            rows = (await session.execute(stmt)).scalars().all()
            return tuple(_to_domain(row) for row in rows)

    async def count_opened_since(
        self, character_ids: Sequence[str], *, since: datetime,
    ) -> int:
        ids = tuple(character_ids)
        if not ids:
            return 0
        since_utc = ensure_utc(since)
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.count())
                .select_from(StorySceneSessionRow)
                .where(
                    StorySceneSessionRow.character_id.in_(ids),
                    StorySceneSessionRow.opened_at >= since_utc,
                ),
            )
            return int(result.scalar_one())


def _to_row(scene: StorySceneSession) -> StorySceneSessionRow:
    return StorySceneSessionRow(
        id=scene.id,
        character_id=scene.character_id,
        conversation_id=scene.conversation_id,
        status=scene.status,
        source_layer=scene.source_layer,
        arc_id=scene.arc_id,
        beat_id=scene.beat_id,
        title=scene.title,
        location=scene.location,
        mood=scene.mood,
        scene_type=scene.scene_type,
        dramatic_question=scene.dramatic_question,
        operator_position=scene.operator_position,
        operator_note=scene.operator_note,
        opened_at=scene.opened_at,
        last_activity_at=scene.last_activity_at,
        closed_at=scene.closed_at,
        closed_reason=scene.closed_reason,
    )


def _apply(row: StorySceneSessionRow, scene: StorySceneSession) -> None:
    # ``character_id`` / ``conversation_id`` / ``opened_at`` are identity,
    # not state: rebinding a live scene to another thread mid-performance
    # would strand its own narration, so they are never rewritten here.
    row.status = scene.status
    row.source_layer = scene.source_layer
    row.arc_id = scene.arc_id
    row.beat_id = scene.beat_id
    row.title = scene.title
    row.location = scene.location
    row.mood = scene.mood
    row.scene_type = scene.scene_type
    row.dramatic_question = scene.dramatic_question
    row.operator_position = scene.operator_position
    row.operator_note = scene.operator_note
    row.last_activity_at = scene.last_activity_at
    row.closed_at = scene.closed_at
    row.closed_reason = scene.closed_reason


def _to_domain(row: StorySceneSessionRow) -> StorySceneSession:
    return StorySceneSession(
        id=row.id,
        character_id=row.character_id,
        conversation_id=row.conversation_id,
        source_layer=row.source_layer,
        status=row.status,
        arc_id=row.arc_id,
        beat_id=row.beat_id,
        title=row.title or "",
        location=row.location,
        mood=row.mood,
        scene_type=row.scene_type,
        dramatic_question=row.dramatic_question,
        operator_position=_decode_operator_position(row.operator_position),
        operator_note=row.operator_note,
        opened_at=row.opened_at,
        last_activity_at=row.last_activity_at,
        closed_at=row.closed_at,
        closed_reason=row.closed_reason,
    )


def _decode_operator_position(raw: object) -> str | None:
    """Best-effort read of the closed-vocabulary position column.

    Same forgiving contract ``sa_story_arc_repository._decode_operator_
    position`` uses for the beat row this value started life on: a value
    the domain would reject degrades to ``None`` (unjudged) rather than
    crashing the whole scene load.
    """
    try:
        return normalise_operator_position(raw)
    except ValueError:
        _LOGGER.warning(
            "story_scene_sessions.operator_position %r is not a known "
            "position — treating as unjudged",
            raw,
        )
        return None
