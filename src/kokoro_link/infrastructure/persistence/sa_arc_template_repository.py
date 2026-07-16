"""SQLAlchemy arc-template repository (per-user authorship + pack rows).

Backs ``ArcTemplateRepositoryPort`` after migration ``cy0d2e50075``.
Pack rows (``user_id IS NULL``) are upserted from YAML on startup by
``ArcTemplatePackSyncService``; user-authored rows are written by the
intake save endpoint. Ownership is enforced here so route handlers
can stay thin: ``get_for_user`` collapses missing / cross-user access
to ``None`` and ``save_for_user`` refuses to write over a pack slug.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.arc_template import ArcTemplateRepositoryPort
from kokoro_link.domain.entities.arc_template import (
    ArcTemplate,
    ArcTemplateBeat,
    ArcTemplateBinding,
)
from kokoro_link.infrastructure.persistence.models import ArcTemplateRow


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _dump_beats(beats: tuple[ArcTemplateBeat, ...]) -> str:
    return json.dumps(
        [
            {
                "sequence": b.sequence,
                "day_offset": b.day_offset,
                "title": b.title,
                "summary": b.summary,
                "tension": b.tension,
                "scene_type": b.scene_type,
                "location": b.location,
                "scene_characters": list(b.scene_characters),
                "dramatic_question": b.dramatic_question,
                "required": b.required,
            }
            for b in beats
        ],
        ensure_ascii=False,
    )


def _load_beats(raw: str | None) -> list[ArcTemplateBeat]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    beats: list[ArcTemplateBeat] = []
    for index, entry in enumerate(data):
        if not isinstance(entry, dict):
            continue
        try:
            beats.append(
                ArcTemplateBeat.create(
                    sequence=int(entry.get("sequence", index)),
                    day_offset=int(entry.get("day_offset", 0)),
                    title=str(entry.get("title", "")),
                    summary=str(entry.get("summary", "")),
                    tension=str(entry.get("tension") or "setup"),
                    scene_type=str(entry.get("scene_type") or "encounter"),
                    location=entry.get("location"),
                    scene_characters=tuple(
                        s for s in (entry.get("scene_characters") or [])
                        if isinstance(s, str)
                    ),
                    dramatic_question=entry.get("dramatic_question"),
                    required=bool(entry.get("required", True)),
                )
            )
        except (TypeError, ValueError):
            # Skip malformed beat entries — better to lose one beat
            # than crash the entire template lookup. The next pack sync
            # or operator save will replace the row entirely.
            continue
    return beats


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
    return tuple(s for s in data if isinstance(s, str))


def _row_to_domain(row: ArcTemplateRow) -> ArcTemplate:
    return ArcTemplate.create(
        id=row.id,
        title=row.title,
        premise=row.premise,
        theme=row.theme,
        tone=row.tone,
        language=row.language or "zh-TW",
        duration_days=row.duration_days,
        beats=_load_beats(row.beats_json),
        binding=ArcTemplateBinding(
            world_frames=_load_str_list(row.world_frames_json),
            required_traits=_load_str_list(row.required_traits_json),
        ),
        applicability_scope=row.applicability_scope or "generic",
        target_character_ids=_load_str_list(row.target_character_ids_json),
    )


def _populate_row(
    row: ArcTemplateRow, template: ArcTemplate, *, now: datetime,
) -> None:
    """Apply ``template`` field values onto ``row`` (in-place).

    Used by both insert and update paths so the column-level mapping
    lives in one place. ``row.id`` / ``row.user_id`` / ``row.pack_id``
    are owner / identity fields and are NOT touched here — callers set
    those before calling.
    """
    row.title = template.title
    row.premise = template.premise
    row.theme = template.theme
    row.tone = template.tone
    row.language = template.language
    row.duration_days = template.duration_days
    row.world_frames_json = _dump_str_list(template.binding.world_frames)
    row.required_traits_json = _dump_str_list(template.binding.required_traits)
    row.applicability_scope = template.applicability_scope
    row.target_character_ids_json = _dump_str_list(template.target_character_ids)
    row.beats_json = _dump_beats(template.beats)
    row.updated_at = now


class SAArcTemplateRepository(ArcTemplateRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_for_user(
        self, template_id: str, *, user_id: str | None,
    ) -> ArcTemplate | None:
        async with self._session_factory() as session:
            row = await self._fetch_visible(session, template_id, user_id)
            return _row_to_domain(row) if row is not None else None

    async def list_for_user(
        self, user_id: str | None,
    ) -> list[ArcTemplate]:
        async with self._session_factory() as session:
            if user_id is None:
                stmt = (
                    select(ArcTemplateRow)
                    .where(
                        ArcTemplateRow.user_id.is_(None),
                        ArcTemplateRow.enabled.is_(True),
                    )
                    .order_by(ArcTemplateRow.id)
                )
            else:
                stmt = (
                    select(ArcTemplateRow)
                    .where(
                        or_(
                            ArcTemplateRow.user_id.is_(None),
                            ArcTemplateRow.user_id == user_id,
                        ),
                        ArcTemplateRow.enabled.is_(True),
                    )
                    .order_by(ArcTemplateRow.id)
                )
            result = await session.execute(stmt)
            return [_row_to_domain(r) for r in result.scalars().all()]

    async def list_packs(self) -> list[ArcTemplate]:
        async with self._session_factory() as session:
            stmt = (
                select(ArcTemplateRow)
                .where(ArcTemplateRow.user_id.is_(None))
                .order_by(ArcTemplateRow.id)
            )
            result = await session.execute(stmt)
            return [_row_to_domain(r) for r in result.scalars().all()]

    async def save_for_user(
        self,
        template: ArcTemplate,
        *,
        user_id: str,
        overwrite: bool = False,
    ) -> str:
        now = _now_utc()
        async with self._session_factory() as session:
            existing = (
                await session.execute(
                    select(ArcTemplateRow).where(
                        ArcTemplateRow.id == template.id,
                    ),
                )
            ).scalars().first()
            if existing is not None:
                # Pack slugs are reserved — owner saves cannot collide
                # with a pack id regardless of ``overwrite``.
                if existing.user_id is None:
                    raise ValueError(
                        f"Template id {template.id!r} is reserved by a "
                        "bundled pack — choose a different id."
                    )
                if existing.user_id != user_id:
                    # Another user already owns this slug. Behave the
                    # same as pack collision so we don't leak the fact
                    # someone else picked the slug first.
                    raise ValueError(
                        f"Template id {template.id!r} already exists — "
                        "choose a different id."
                    )
                if not overwrite:
                    raise ValueError(
                        f"Template id {template.id!r} already exists — "
                        "pass overwrite=True to replace."
                    )
                _populate_row(existing, template, now=now)
                await session.commit()
                return existing.id
            row = ArcTemplateRow(
                id=template.id,
                user_id=user_id,
                pack_id=None,
                external_id=None,
                enabled=True,
                created_at=now,
                updated_at=now,
                title=template.title,
                premise=template.premise,
                theme=template.theme,
                tone=template.tone,
                language=template.language,
                duration_days=template.duration_days,
                world_frames_json=_dump_str_list(template.binding.world_frames),
                required_traits_json=_dump_str_list(
                    template.binding.required_traits,
                ),
                applicability_scope=template.applicability_scope,
                target_character_ids_json=_dump_str_list(
                    template.target_character_ids,
                ),
                beats_json=_dump_beats(template.beats),
            )
            session.add(row)
            await session.commit()
            return row.id

    async def delete_for_user(
        self, template_id: str, *, user_id: str,
    ) -> bool:
        async with self._session_factory() as session:
            # Owner-bound delete: pack rows (user_id IS NULL) and rows
            # owned by other users are both invisible here, so this is
            # the same shape as the get_for_user guard.
            result = await session.execute(
                delete(ArcTemplateRow).where(
                    ArcTemplateRow.id == template_id,
                    ArcTemplateRow.user_id == user_id,
                ),
            )
            await session.commit()
            return bool(result.rowcount)

    async def upsert_pack(
        self,
        template: ArcTemplate,
        *,
        pack_id: str,
        external_id: str | None = None,
    ) -> str:
        now = _now_utc()
        async with self._session_factory() as session:
            existing = (
                await session.execute(
                    select(ArcTemplateRow).where(
                        ArcTemplateRow.id == template.id,
                    ),
                )
            ).scalars().first()
            if existing is not None:
                # If a user owns this slug, the pack sync is *not* allowed
                # to clobber it. The collision is logged at the sync
                # service layer; the repo just refuses to overwrite.
                if existing.user_id is not None:
                    raise ValueError(
                        f"Cannot upsert pack {template.id!r}: a user-"
                        "authored row already owns this slug."
                    )
                _populate_row(existing, template, now=now)
                existing.pack_id = pack_id
                existing.external_id = external_id
                existing.enabled = True
                await session.commit()
                return existing.id
            row = ArcTemplateRow(
                id=template.id,
                user_id=None,
                pack_id=pack_id,
                external_id=external_id,
                enabled=True,
                created_at=now,
                updated_at=now,
                title=template.title,
                premise=template.premise,
                theme=template.theme,
                tone=template.tone,
                language=template.language,
                duration_days=template.duration_days,
                world_frames_json=_dump_str_list(template.binding.world_frames),
                required_traits_json=_dump_str_list(
                    template.binding.required_traits,
                ),
                applicability_scope=template.applicability_scope,
                target_character_ids_json=_dump_str_list(
                    template.target_character_ids,
                ),
                beats_json=_dump_beats(template.beats),
            )
            session.add(row)
            await session.commit()
            return row.id

    # ----- helpers -------------------------------------------------------

    async def _fetch_visible(
        self,
        session: AsyncSession,
        template_id: str,
        user_id: str | None,
    ) -> ArcTemplateRow | None:
        """Single-row visibility filter shared by get + ownership checks."""
        stmt = select(ArcTemplateRow).where(
            ArcTemplateRow.id == template_id,
            ArcTemplateRow.enabled.is_(True),
        )
        row = (await session.execute(stmt)).scalars().first()
        if row is None:
            return None
        if row.user_id is None:
            return row
        if user_id is not None and row.user_id == user_id:
            return row
        return None


__all__ = ["SAArcTemplateRepository"]
