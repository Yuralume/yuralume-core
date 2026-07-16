"""SA-backed ``FusionStoryRepositoryPort`` implementation.

Mirrors the split-transaction pattern from ``SAStoryArcRepository``:

- Tx 1: upsert head + delete child beats + delete *new* versions whose
  numbers exceed what's already on disk (the existing rows we already
  persisted).
- Tx 2: insert fresh beats + append never-before-seen version rows.

Versions are append-only — we identify the new ones by ``version_number
not in <existing set>`` so a redundant ``save`` after a no-op iterate
doesn't double-write history rows.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.fusion_story import FusionStoryRepositoryPort
from kokoro_link.domain.entities.fusion_story import (
    FusionStory,
    FusionStoryBeat,
    FusionStoryVersion,
)
from kokoro_link.domain.value_objects.fusion_outline import (
    FusionBeatPlan,
    FusionOutline,
)
from kokoro_link.infrastructure.persistence.fusion_story_models import (
    FusionStoryBeatRow,
    FusionStoryRow,
    FusionStoryVersionRow,
)


_LOGGER = logging.getLogger(__name__)


class SAFusionStoryRepository(FusionStoryRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, story: FusionStory) -> None:
        async with self._session_factory() as session:
            session.add(_story_to_row(story))
            await session.commit()
        # Beats / versions inserted in a follow-up tx for the same FK-
        # ordering reasons as ``SAStoryArcRepository.add``.
        async with self._session_factory() as session:
            for beat in story.beats:
                session.add(_beat_to_row(story.id, beat))
            for version in story.versions:
                session.add(_version_to_row(version))
            await session.commit()

    async def get(self, story_id: str) -> FusionStory | None:
        async with self._session_factory() as session:
            row = await session.get(FusionStoryRow, story_id)
            if row is None:
                return None
            beats = await self._load_beats(session, story_id)
            versions = await self._load_versions(session, story_id)
        return _row_to_story(row, beats, versions)

    async def list_recent(self, *, limit: int = 50) -> list[FusionStory]:
        async with self._session_factory() as session:
            stmt = (
                select(FusionStoryRow)
                .order_by(FusionStoryRow.updated_at.desc())
                .limit(max(1, limit))
            )
            rows = list((await session.execute(stmt)).scalars())
            stories: list[FusionStory] = []
            for row in rows:
                beats = await self._load_beats(session, row.id)
                versions = await self._load_versions(session, row.id)
                stories.append(_row_to_story(row, beats, versions))
        return stories

    async def save(self, story: FusionStory) -> None:
        async with self._session_factory() as session:
            existing = await session.get(FusionStoryRow, story.id)
            if existing is None:
                session.add(_story_to_row(story))
            else:
                _apply_updates(existing, story)
            await session.execute(
                delete(FusionStoryBeatRow).where(
                    FusionStoryBeatRow.story_id == story.id,
                ),
            )
            existing_versions = list(
                (
                    await session.execute(
                        select(FusionStoryVersionRow.version_number).where(
                            FusionStoryVersionRow.story_id == story.id,
                        ),
                    )
                ).scalars()
            )
            await session.commit()

        already_persisted = set(existing_versions)
        async with self._session_factory() as session:
            for beat in story.beats:
                session.add(_beat_to_row(story.id, beat))
            for version in story.versions:
                if version.version_number in already_persisted:
                    continue
                session.add(_version_to_row(version))
            await session.commit()

    async def delete(self, story_id: str) -> None:
        async with self._session_factory() as session:
            row = await session.get(FusionStoryRow, story_id)
            if row is None:
                return
            await session.delete(row)
            await session.commit()

    # --- loaders ------------------------------------------------------

    async def _load_beats(
        self, session: AsyncSession, story_id: str,
    ) -> list[FusionStoryBeat]:
        stmt = (
            select(FusionStoryBeatRow)
            .where(FusionStoryBeatRow.story_id == story_id)
            .order_by(FusionStoryBeatRow.sequence)
        )
        rows = (await session.execute(stmt)).scalars()
        return [_row_to_beat(r) for r in rows]

    async def _load_versions(
        self, session: AsyncSession, story_id: str,
    ) -> list[FusionStoryVersion]:
        stmt = (
            select(FusionStoryVersionRow)
            .where(FusionStoryVersionRow.story_id == story_id)
            .order_by(FusionStoryVersionRow.version_number)
        )
        rows = (await session.execute(stmt)).scalars()
        return [_row_to_version(r) for r in rows]


# --- mapping helpers --------------------------------------------------


def _story_to_row(story: FusionStory) -> FusionStoryRow:
    return FusionStoryRow(
        id=story.id,
        character_ids_json=json.dumps(
            list(story.character_ids), ensure_ascii=False,
        ),
        prompt=story.prompt,
        title=story.title,
        premise=story.premise,
        theme=story.theme,
        outline_json=_serialize_outline(story.outline),
        full_text=story.full_text,
        status=story.status,
        head_version=story.head_version,
        error_message=story.error_message,
        created_at=story.created_at,
        updated_at=story.updated_at,
    )


def _apply_updates(row: FusionStoryRow, story: FusionStory) -> None:
    row.character_ids_json = json.dumps(
        list(story.character_ids), ensure_ascii=False,
    )
    row.prompt = story.prompt
    row.title = story.title
    row.premise = story.premise
    row.theme = story.theme
    row.outline_json = _serialize_outline(story.outline)
    row.full_text = story.full_text
    row.status = story.status
    row.head_version = story.head_version
    row.error_message = story.error_message
    row.updated_at = story.updated_at


def _beat_to_row(story_id: str, beat: FusionStoryBeat) -> FusionStoryBeatRow:
    return FusionStoryBeatRow(
        id=beat.id,
        story_id=story_id,
        sequence=beat.sequence,
        act=beat.act,
        title=beat.title,
        hook=beat.hook,
        dramatic_question=beat.dramatic_question,
        target_chars=beat.target_chars,
        actual_chars=beat.actual_chars,
        content=beat.content,
        focus_character_ids_json=json.dumps(
            list(beat.focus_character_ids), ensure_ascii=False,
        ),
    )


def _version_to_row(version: FusionStoryVersion) -> FusionStoryVersionRow:
    return FusionStoryVersionRow(
        id=version.id,
        story_id=version.story_id,
        version_number=version.version_number,
        title=version.title,
        premise=version.premise,
        theme=version.theme,
        full_text=version.full_text,
        outline_json=version.outline_json,
        iteration_label=version.iteration_label,
        beats_json=version.beats_json,
        created_at=version.created_at,
    )


def _row_to_story(
    row: FusionStoryRow,
    beats: list[FusionStoryBeat],
    versions: list[FusionStoryVersion],
) -> FusionStory:
    outline = _deserialize_outline(row.outline_json)
    return FusionStory(
        id=row.id,
        character_ids=_decode_str_list(row.character_ids_json),
        prompt=row.prompt,
        title=row.title,
        premise=row.premise,
        theme=row.theme,
        outline=outline,
        beats=tuple(beats),
        full_text=row.full_text,
        status=row.status,
        head_version=row.head_version,
        versions=tuple(versions),
        error_message=row.error_message,
        created_at=_ensure_aware(row.created_at),
        updated_at=_ensure_aware(row.updated_at),
    )


def _row_to_beat(row: FusionStoryBeatRow) -> FusionStoryBeat:
    return FusionStoryBeat(
        id=row.id,
        sequence=row.sequence,
        act=row.act,
        title=row.title,
        hook=row.hook,
        dramatic_question=row.dramatic_question,
        target_chars=row.target_chars,
        content=row.content,
        actual_chars=row.actual_chars,
        focus_character_ids=_decode_str_list(row.focus_character_ids_json),
    )


def _row_to_version(row: FusionStoryVersionRow) -> FusionStoryVersion:
    return FusionStoryVersion(
        id=row.id,
        story_id=row.story_id,
        version_number=row.version_number,
        title=row.title,
        premise=row.premise,
        theme=row.theme,
        full_text=row.full_text,
        outline_json=row.outline_json,
        iteration_label=row.iteration_label,
        beats_json=row.beats_json or "[]",
        created_at=_ensure_aware(row.created_at),
    )


def _serialize_outline(outline: FusionOutline | None) -> str:
    if outline is None:
        return "{}"
    return json.dumps(
        {
            "title": outline.title,
            "premise": outline.premise,
            "theme": outline.theme,
            "beats": [
                {
                    "sequence": b.sequence,
                    "act": b.act,
                    "title": b.title,
                    "hook": b.hook,
                    "dramatic_question": b.dramatic_question,
                    "target_chars": b.target_chars,
                    "focus_character_ids": list(b.focus_character_ids),
                    "entry_state": b.entry_state,
                    "exit_state": b.exit_state,
                    "transition_from_previous": b.transition_from_previous,
                }
                for b in outline.beats
            ],
        },
        ensure_ascii=False,
    )


def _deserialize_outline(raw: str | None) -> FusionOutline | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        _LOGGER.warning("fusion_stories.outline_json decode failed raw=%r", raw)
        return None
    if not isinstance(data, dict) or not data:
        return None
    title = str(data.get("title") or "").strip()
    premise = str(data.get("premise") or "").strip()
    if not title or not premise:
        return None
    raw_beats = data.get("beats")
    if not isinstance(raw_beats, list) or not raw_beats:
        return None
    beats: list[FusionBeatPlan] = []
    for entry in raw_beats:
        if not isinstance(entry, dict):
            continue
        try:
            beats.append(
                FusionBeatPlan.create(
                    sequence=int(entry.get("sequence") or 0),
                    act=str(entry.get("act") or "opening"),
                    title=str(entry.get("title") or "（未命名）"),
                    hook=str(entry.get("hook") or "（無）"),
                    dramatic_question=str(entry.get("dramatic_question") or ""),
                    target_chars=int(entry.get("target_chars") or 500),
                    focus_character_ids=tuple(
                        e for e in (entry.get("focus_character_ids") or [])
                        if isinstance(e, str)
                    ),
                    entry_state=str(entry.get("entry_state") or ""),
                    exit_state=str(entry.get("exit_state") or ""),
                    transition_from_previous=str(
                        entry.get("transition_from_previous") or "",
                    ),
                ),
            )
        except (ValueError, TypeError):
            continue
    if not beats:
        return None
    try:
        return FusionOutline.create(
            title=title,
            premise=premise,
            theme=str(data.get("theme") or "custom"),
            beats=beats,
        )
    except ValueError:
        return None


def _decode_str_list(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(decoded, list):
        return ()
    return tuple(
        e.strip() for e in decoded
        if isinstance(e, str) and e.strip()
    )


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
