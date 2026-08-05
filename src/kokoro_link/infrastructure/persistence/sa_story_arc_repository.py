"""SA-backed ``StoryArcRepositoryPort`` implementation.

``save`` is the workhorse: it upserts the arc row and rebuilds the
beat rows atomically in a single transaction. The beat set is small
(< 10 rows per arc) so delete-all + re-insert is cheaper to reason
about than per-beat diffing and doesn't measurably hurt write
performance at our scale.

Its cost is that the rebuild is last-writer-wins over every beat row:
whatever another writer committed since the caller loaded the arc is
gone. ``skip_beats_if_pending`` / ``complete_arc_if_all_terminal`` are
the narrow way out — single conditional statements whose predicate is
evaluated by the database, so a caller retiring dead beats cannot
un-realize a scene that finished (and was charged) underneath it.

Both writes are fenced by ``uq_story_arcs_active_character``, the partial
unique index that makes "one active arc per character" a schema invariant
instead of a service-layer hope. A violation is the cross-replica planning
race, not a defect, so it is translated into
:class:`~kokoro_link.contracts.story_arc.ActiveArcConflict` after a clean
rollback — the same benign-race-recovery shape ``daily_schedules`` uses for its
P3-Dedup constraint, except the loser must ADOPT the winner rather than replace
it (two arcs are different stories; overwriting would destroy one).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import date, datetime, timezone

from sqlalchemy import delete, exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.story_arc import (
    ActiveArcConflict,
    StoryArcRepositoryPort,
)
from kokoro_link.domain.entities.story_arc import (
    ARC_ACTIVE,
    ARC_COMPLETED,
    BEAT_PENDING,
    BEAT_REALIZED,
    BEAT_SKIPPED,
    SCENE_ENCOUNTER,
    StoryArc,
    StoryArcBeat,
    normalise_operator_position,
)
from kokoro_link.infrastructure.persistence.models import (
    StoryArcBeatRow,
    StoryArcRow,
)

_LOGGER = logging.getLogger(__name__)

# Only a violation of THIS index (a concurrent replica winning the character's
# single active-arc slot) is the benign race; any other integrity failure is a
# real defect and must surface.
_ACTIVE_ARC_INDEX = "uq_story_arcs_active_character"

# PostgreSQL names the offending index ("duplicate key value violates unique
# constraint \"uq_story_arcs_active_character\""); SQLite names the columns
# instead ("UNIQUE constraint failed: story_arcs.character_id"). Match either.
# ``character_id`` carries no other unique index on this table, so the column
# form cannot be confused with an unrelated violation.
_SQLITE_ACTIVE_ARC_COLUMNS = "story_arcs.character_id"


def _is_active_arc_violation(error: IntegrityError) -> bool:
    message = str(getattr(error, "orig", error))
    return _ACTIVE_ARC_INDEX in message or _SQLITE_ACTIVE_ARC_COLUMNS in message


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
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                if not _is_active_arc_violation(exc):
                    raise
                # Another replica planned + inserted this character's active
                # arc while we were planning ours. Drop ours entirely (no
                # beats are written) and let the caller adopt the winner —
                # the losing plan is discarded, never merged.
                _LOGGER.info(
                    "story arc insert lost the active slot character=%s arc=%s",
                    arc.character_id, arc.id,
                )
                raise ActiveArcConflict(arc.character_id) from exc
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

        A save that would make this a *second* active arc for the character
        raises ``ActiveArcConflict`` with nothing written (the beat delete is
        rolled back with it), so the winner's arc and beats stay intact.
        """
        async with self._session_factory() as session:
            # The whole unit of work is guarded, not just the commit: the beat
            # delete below triggers an autoflush, so a status change that
            # violates the active-arc index surfaces *there*, before commit.
            try:
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
            except IntegrityError as exc:
                await session.rollback()
                if not _is_active_arc_violation(exc):
                    raise
                _LOGGER.info(
                    "story arc save lost the active slot character=%s arc=%s",
                    arc.character_id, arc.id,
                )
                raise ActiveArcConflict(arc.character_id) from exc
        async with self._session_factory() as session:
            for beat in arc.beats:
                session.add(_beat_to_row(arc.id, beat))
            await session.commit()

    async def skip_beats_if_pending(
        self,
        arc_id: str,
        beat_ids: Sequence[str],
        *,
        play_result: str,
    ) -> int:
        """One ``UPDATE`` fenced on ``status = 'pending'``. See the port doc.

        Deliberately not expressed as read → edit → ``save``: the whole
        point is that the DB, not this process's snapshot, decides which
        rows may move. ``synchronize_session=False`` because no ORM
        objects are loaded here — the rows are read back by the caller.
        """
        ids = list(dict.fromkeys(beat_ids))
        if not ids:
            return 0
        stmt = (
            update(StoryArcBeatRow)
            .where(
                StoryArcBeatRow.arc_id == arc_id,
                StoryArcBeatRow.id.in_(ids),
                StoryArcBeatRow.status == BEAT_PENDING,
            )
            .values(
                status=BEAT_SKIPPED,
                last_play_attempt_result=play_result,
            )
            .execution_options(synchronize_session=False)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            await session.commit()
        return int(result.rowcount or 0)

    async def complete_arc_if_all_terminal(self, arc_id: str) -> bool:
        """One ``UPDATE`` whose terminality test is a correlated subquery.

        Evaluating "are all beats terminal?" inside the statement is what
        keeps a beat realized a moment ago from being erased: the flip
        either sees the newer row and declines, or it never runs.
        """
        unfinished = (
            exists()
            .where(
                StoryArcBeatRow.arc_id == arc_id,
                StoryArcBeatRow.status.not_in((BEAT_REALIZED, BEAT_SKIPPED)),
            )
        )
        any_beat = exists().where(StoryArcBeatRow.arc_id == arc_id)
        stmt = (
            update(StoryArcRow)
            .where(
                StoryArcRow.id == arc_id,
                StoryArcRow.status == ARC_ACTIVE,
                # A beat-less arc is not "finished" — same rule the
                # service's ``_all_terminal`` applies to an empty list.
                any_beat,
                ~unfinished,
            )
            .values(
                status=ARC_COMPLETED,
                updated_at=datetime.now(timezone.utc),
            )
            .execution_options(synchronize_session=False)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            await session.commit()
        return bool(result.rowcount)

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
        source_seed_ids=json.dumps(list(arc.source_seed_ids), ensure_ascii=False),
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
    row.source_seed_ids = json.dumps(list(arc.source_seed_ids), ensure_ascii=False)
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
        play_failure_count=beat.play_failure_count,
        last_play_failure_at=beat.last_play_failure_at,
        scene_characters=json.dumps(list(beat.scene_characters), ensure_ascii=False),
        location=beat.location,
        dramatic_question=beat.dramatic_question,
        scene_type=beat.scene_type,
        required=beat.required,
        operator_position=beat.operator_position,
        operator_note=beat.operator_note,
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
        source_seed_ids=_decode_source_seed_ids(
            getattr(row, "source_seed_ids", None),
        ),
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
        # ``getattr`` defaults keep pre-SC0 rows readable the same way
        # the older attempt columns do: no known failures, no anchor.
        play_failure_count=getattr(row, "play_failure_count", 0) or 0,
        last_play_failure_at=_ensure_optional_aware(
            getattr(row, "last_play_failure_at", None),
        ),
        scene_characters=_decode_scene_characters(row.scene_characters),
        location=row.location,
        dramatic_question=row.dramatic_question,
        scene_type=row.scene_type or SCENE_ENCOUNTER,
        required=bool(row.required),
        operator_position=_decode_operator_position(
            getattr(row, "operator_position", None),
        ),
        operator_note=getattr(row, "operator_note", None),
    )


def _decode_operator_position(raw: object) -> str | None:
    """Best-effort read of the closed-vocabulary position column.

    Same forgiving contract as ``_decode_scene_characters``: a value the
    domain would reject (hand-edited row, a label from a newer Core than
    this one) degrades to ``None`` = unjudged rather than crashing arc
    load. ``None`` is a real state here, so falling back to it costs a
    framing decision, never a beat.
    """
    try:
        return normalise_operator_position(raw)
    except ValueError:
        _LOGGER.warning(
            "story_arc_beats.operator_position %r is not a known position "
            "— treating as unjudged",
            raw,
        )
        return None


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


def _decode_source_seed_ids(raw: str | None) -> tuple[str, ...]:
    """Best-effort decode of the JSON-encoded seed-provenance id list.

    Same forgiving contract as ``_decode_scene_characters``: missing /
    malformed JSON (including rows written before this column existed,
    which read back as ``NULL`` via ``getattr`` above) degrades to an
    empty tuple rather than blocking arc load. Non-string entries are
    filtered here too, but the dedupe/truncate/cap invariants are left
    to ``StoryArc.__post_init__`` (``_normalise_source_seed_ids`` runs on
    every construction), so this only has to get from Text to a list.
    """
    if not raw:
        return ()
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        _LOGGER.warning(
            "story_arcs.source_seed_ids decode failed raw=%r — "
            "treating as empty",
            raw,
        )
        return ()
    if not isinstance(decoded, list):
        return ()
    return tuple(entry for entry in decoded if isinstance(entry, str))


def _ensure_aware(value: datetime) -> datetime:
    # asyncpg returns tz-aware; safety net for mixed-backend tests.
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _ensure_optional_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _ensure_aware(value)
