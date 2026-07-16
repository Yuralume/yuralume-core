"""Compute Layer 4 (interaction strength) from system state.

Pure read-side: this module never writes. The application service
caches results in memory; the calculator just answers "what does the
data say right now" each time it's asked.

Per-character: every signal is filtered by ``character_id`` so a
brand-new character genuinely starts at zero. Sharing across
characters would have a 5-character operator landing on "close" for
character #5 the moment they're created — exactly the magical-rapport
problem the per-character persona pivot was meant to fix.

Signal sources
--------------

- ``turn_journals`` filtered by ``character_id``: one row per chat
  turn (user→assistant exchange). Used for total / recent message
  counts and the first-contact date. Messages themselves have no
  ``created_at`` column, so turn_journals is the authoritative
  timeline. Yuralume is single-operator today, so every turn
  implicitly belongs to ``DEFAULT_OPERATOR_ID``; the operator_id
  parameter is carried for the future multi-operator world.
- ``story_arc_beats`` joined through ``story_arcs.character_id``,
  ``status='realized'``: counts narrative milestones this character
  has lived through with the operator.
- ``branching_drama_sessions`` whose owning drama lists this
  character in ``character_ids_json``, ``status='completed'``: counts
  finished interactive dramas this character starred in.

The band thresholds live in ``PersonaSettings`` so deployments can
tune accumulation pace without a code change.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.bootstrap.settings import PersonaSettings
from kokoro_link.domain.entities.operator_persona import InteractionStrength
from kokoro_link.domain.value_objects.familiarity import Familiarity
from kokoro_link.infrastructure.persistence.branching_drama_models import (
    BranchingDramaRow,
    BranchingDramaSessionRow,
)
from kokoro_link.infrastructure.persistence.models import (
    StoryArcBeatRow,
    StoryArcRow,
    TurnJournalRow,
)


class InteractionStrengthCalculator:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[AsyncSession],
        settings: PersonaSettings,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings

    async def compute(
        self, character_id: str, operator_id: str,
    ) -> InteractionStrength:
        now = datetime.now(timezone.utc)
        seven_days_ago = now - timedelta(days=7)
        thirty_days_ago = now - timedelta(days=30)

        async with self._session_factory() as session:
            first_at = await self._earliest_turn_at(session, character_id)
            total = await self._count_turns_since(
                session, character_id=character_id, since=None,
            )
            last_7 = await self._count_turns_since(
                session, character_id=character_id, since=seven_days_ago,
            )
            last_30 = await self._count_turns_since(
                session, character_id=character_id, since=thirty_days_ago,
            )
            longest_session = await self._longest_session_minutes(
                session, character_id,
            )
            arcs_realized = await self._realized_arc_beats(
                session, character_id,
            )
            dramas_done = await self._completed_dramas(session, character_id)

        if first_at is None:
            return InteractionStrength.empty(
                character_id, operator_id, now=now,
            )

        days_since = max(0, (now - first_at).days)
        band = _resolve_band(
            total_msgs=total,
            days_since=days_since,
            shared_arcs=arcs_realized,
            shared_dramas=dramas_done,
            settings=self._settings,
        )
        return InteractionStrength(
            character_id=character_id,
            operator_id=operator_id,
            first_message_at=first_at,
            total_user_messages=total,
            days_since_first_contact=days_since,
            messages_last_7_days=last_7,
            messages_last_30_days=last_30,
            longest_session_minutes=longest_session,
            shared_arc_realized_count=arcs_realized,
            shared_drama_count=dramas_done,
            familiarity_band=band,
            computed_at=now,
        )

    async def _earliest_turn_at(
        self, session: AsyncSession, character_id: str,
    ) -> datetime | None:
        stmt = select(func.min(TurnJournalRow.created_at)).where(
            TurnJournalRow.character_id == character_id,
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _count_turns_since(
        self,
        session: AsyncSession,
        *,
        character_id: str,
        since: datetime | None,
    ) -> int:
        stmt = select(func.count(TurnJournalRow.id)).where(
            TurnJournalRow.character_id == character_id,
        )
        if since is not None:
            stmt = stmt.where(TurnJournalRow.created_at >= since)
        result = await session.execute(stmt)
        value = result.scalar_one_or_none()
        return int(value or 0)

    async def _longest_session_minutes(
        self, session: AsyncSession, character_id: str,
    ) -> int:
        """Approximate: a "session" is a run of turns inside the same
        conversation with < 30-minute gaps. Computed in Python over the
        sorted timeline — small data volume, simple to read."""
        stmt = (
            select(TurnJournalRow.conversation_id, TurnJournalRow.created_at)
            .where(TurnJournalRow.character_id == character_id)
            .order_by(
                TurnJournalRow.conversation_id, TurnJournalRow.created_at,
            )
        )
        result = await session.execute(stmt)
        rows = list(result.all())
        if not rows:
            return 0
        longest = 0
        current_conv: str | None = None
        session_start: datetime | None = None
        prev_at: datetime | None = None
        gap = timedelta(minutes=30)
        for conv_id, created_at in rows:
            if current_conv != conv_id or prev_at is None:
                current_conv = conv_id
                session_start = created_at
                prev_at = created_at
                continue
            if created_at - prev_at > gap:
                if session_start is not None:
                    duration = int((prev_at - session_start).total_seconds() // 60)
                    if duration > longest:
                        longest = duration
                session_start = created_at
            prev_at = created_at
        if session_start is not None and prev_at is not None:
            duration = int((prev_at - session_start).total_seconds() // 60)
            if duration > longest:
                longest = duration
        return longest

    async def _realized_arc_beats(
        self, session: AsyncSession, character_id: str,
    ) -> int:
        """Beats this character has realised. Joining via StoryArcRow
        because the beat row doesn't carry character_id directly."""
        stmt = (
            select(func.count(StoryArcBeatRow.id))
            .join(StoryArcRow, StoryArcBeatRow.arc_id == StoryArcRow.id)
            .where(
                StoryArcRow.character_id == character_id,
                StoryArcBeatRow.status == "realized",
            )
        )
        result = await session.execute(stmt)
        return int(result.scalar_one_or_none() or 0)

    async def _completed_dramas(
        self, session: AsyncSession, character_id: str,
    ) -> int:
        """Dramas this character appeared in that have a completed
        session. The drama → character link is JSON-encoded in
        ``character_ids_json`` (e.g. ``'["c1","c2"]'``); a LIKE on the
        quoted id keeps us out of dialect-specific JSON operators and
        is fine because the row count is small (one drama is a
        whole-narrative unit, not high-cardinality)."""
        like_pattern = f'%"{character_id}"%'
        stmt = (
            select(func.count(BranchingDramaSessionRow.id.distinct()))
            .join(
                BranchingDramaRow,
                BranchingDramaSessionRow.drama_id == BranchingDramaRow.id,
            )
            .where(
                BranchingDramaSessionRow.status == "completed",
                BranchingDramaRow.character_ids_json.like(like_pattern),
            )
        )
        result = await session.execute(stmt)
        return int(result.scalar_one_or_none() or 0)


def _resolve_band(
    *,
    total_msgs: int,
    days_since: int,
    shared_arcs: int,
    shared_dramas: int,
    settings: PersonaSettings,
) -> Familiarity:
    """Band rules (in order):

    1. ``close``: enough days AND at least one shared arc/drama.
    2. ``familiar``: past the acquaintance message count OR day count.
    3. ``acquaintance``: past the stranger thresholds.
    4. ``stranger``: anything else.

    Statistical, not semantic — purely about whether enough signal has
    accumulated. The exact numbers come from settings.
    """
    if days_since >= settings.familiarity_close_min_days and (
        shared_arcs >= 1 or shared_dramas >= 1
    ):
        return Familiarity.CLOSE
    if (
        total_msgs >= settings.familiarity_acquaintance_max_msgs
        or days_since >= settings.familiarity_acquaintance_max_days
    ):
        return Familiarity.FAMILIAR
    if (
        total_msgs >= settings.familiarity_stranger_max_msgs
        or days_since >= settings.familiarity_stranger_max_days
    ):
        return Familiarity.ACQUAINTANCE
    return Familiarity.STRANGER
