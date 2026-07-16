from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.image_profile import FeatureImageProfileOverride
from kokoro_link.contracts.video_profile import FeatureVideoProfileOverride
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.entities.character import (
    Character,
    CharacterLora,
    FeatureModelOverride,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.companion import CharacterCompanion
from kokoro_link.domain.value_objects.disposition import CharacterDisposition
from kokoro_link.domain.value_objects.personality_type import (
    CharacterPersonalityType,
)
from kokoro_link.domain.value_objects.voice_profile import VoiceProfile
from kokoro_link.domain.value_objects.visual_subject import (
    DEFAULT_VISUAL_SUBJECT_TYPE,
    normalise_visual_subject_type,
)
from kokoro_link.domain.value_objects.visual_generation_style import (
    normalise_character_visual_generation_style,
)
from kokoro_link.infrastructure.persistence.models import CharacterRow


def _ensure_utc(value: datetime | None) -> datetime | None:
    """Defensive: reattach UTC tzinfo if missing."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


class SACharacterRepository(CharacterRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list(self) -> list[Character]:
        async with self._session_factory() as session:
            result = await session.execute(select(CharacterRow))
            rows = result.scalars().all()
            return [_row_to_domain(row) for row in rows]

    async def list_for_user(self, user_id: str) -> list[Character]:
        """Filter to characters owned by ``user_id``.

        Used by the multi-user list-characters endpoint. When auth is
        disabled the caller passes ``DEFAULT_OPERATOR_ID`` and gets the
        same result as the unfiltered ``list()`` would have on a clean
        install."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(CharacterRow).where(CharacterRow.user_id == user_id)
            )
            rows = result.scalars().all()
            return [_row_to_domain(row) for row in rows]

    async def list_active(self) -> list[Character]:
        """List only non-frozen characters (CHARACTER_FREEZE_PLAN).

        The background scheduler iterates this instead of ``list()`` so a
        frozen character incurs zero per-tick background work. Frozen
        characters keep their row and are still reachable by id / owner
        for foreground chat and admin surfaces."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(CharacterRow)
                .where(CharacterRow.frozen.is_(False))
                .where(CharacterRow.subscription_locked.is_(False))
            )
            rows = result.scalars().all()
            return [_row_to_domain(row) for row in rows]

    async def set_frozen(
        self,
        character_id: str,
        *,
        frozen: bool,
        now: datetime,
        reason: str | None = None,
    ) -> bool:
        """Flip the freeze flag for a single character.

        Freezing stamps ``frozen_at=now`` and ``frozen_reason=reason``;
        unfreezing clears both back to ``None`` (``reason`` ignored).
        Targeted update so it never races the character's state-tracking
        ``save()`` on unrelated fields. Returns ``True`` when a row was
        actually updated."""
        async with self._session_factory() as session:
            result = await session.execute(
                update(CharacterRow)
                .where(CharacterRow.id == character_id)
                .values(
                    frozen=frozen,
                    frozen_at=now if frozen else None,
                    frozen_reason=reason if frozen else None,
                )
            )
            await session.commit()
            return bool(result.rowcount)

    async def set_subscription_locked(
        self, character_id: str, *, locked: bool,
    ) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                update(CharacterRow)
                .where(CharacterRow.id == character_id)
                .values(subscription_locked=bool(locked))
            )
            await session.commit()
            return bool(result.rowcount)

    async def get(self, character_id: str) -> Character | None:
        async with self._session_factory() as session:
            row = await session.get(CharacterRow, character_id)
            if row is None:
                return None
            return _row_to_domain(row)

    async def save(self, character: Character) -> None:
        async with self._session_factory() as session:
            row = await session.get(CharacterRow, character.id)
            is_new = row is None
            if row is None:
                # New row — user_id is NOT NULL so the row must carry an
                # owner from the start. _domain_to_row writes it; passing
                # it to the constructor avoids a transient NULL state
                # that Postgres' deferred constraints would tolerate but
                # SQLite would not.
                row = CharacterRow(id=character.id, user_id=character.user_id)
                session.add(row)
            _domain_to_row(character, row, include_control_fields=is_new)
            await session.commit()

    async def delete(self, character_id: str) -> bool:
        async with self._session_factory() as session:
            row = await session.get(CharacterRow, character_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True


def _loras_from_json(raw: str | None) -> tuple[CharacterLora, ...]:
    if not raw:
        return ()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(data, list):
        return ()
    results: list[CharacterLora] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        strength_raw = item.get("strength", 1.0)
        try:
            strength = float(strength_raw)
        except (TypeError, ValueError):
            strength = 1.0
        try:
            results.append(CharacterLora(name=name, strength=strength))
        except ValueError:
            continue
    return tuple(results)


def _feature_models_from_json(raw: str | None) -> tuple[FeatureModelOverride, ...]:
    """Parse the JSON list, dropping malformed / blank entries.

    Defensive: a hand-edited row or a partial migration shouldn't blow
    up the character read path — if the blob isn't a list of dicts we
    return an empty tuple and let the resolver fall through to the
    global pref like a fresh character would."""
    if not raw:
        return ()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(data, list):
        return ()
    out: list[FeatureModelOverride] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        key = entry.get("feature_key")
        if not isinstance(key, str) or not key.strip():
            continue
        provider = entry.get("provider_id")
        model = entry.get("model_id")
        try:
            override = FeatureModelOverride(
                feature_key=key,
                provider_id=provider if isinstance(provider, str) else None,
                model_id=model if isinstance(model, str) else None,
            )
        except ValueError:
            continue
        if override.is_empty:
            continue
        out.append(override)
    return tuple(out)


def _feature_models_to_json(overrides: tuple[FeatureModelOverride, ...]) -> str:
    return json.dumps(
        [
            {
                "feature_key": entry.feature_key,
                "provider_id": entry.provider_id,
                "model_id": entry.model_id,
            }
            for entry in overrides
        ],
        ensure_ascii=False,
    )


def _feature_image_profiles_from_json(
    raw: str | None,
) -> tuple[FeatureImageProfileOverride, ...]:
    """Mirror of :func:`_feature_models_from_json` for image-profile
    overrides. Defensive: a hand-edited / partially-migrated row falls
    back to empty rather than crashing the character read path."""
    if not raw:
        return ()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(data, list):
        return ()
    out: list[FeatureImageProfileOverride] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        key = entry.get("feature_key")
        if not isinstance(key, str) or not key.strip():
            continue
        profile = entry.get("profile_id")
        try:
            override = FeatureImageProfileOverride(
                feature_key=key,
                profile_id=profile if isinstance(profile, str) else None,
            )
        except ValueError:
            continue
        if override.is_empty:
            continue
        out.append(override)
    return tuple(out)


def _feature_image_profiles_to_json(
    overrides: tuple[FeatureImageProfileOverride, ...],
) -> str:
    return json.dumps(
        [
            {"feature_key": e.feature_key, "profile_id": e.profile_id}
            for e in overrides
        ],
        ensure_ascii=False,
    )


def _feature_video_profiles_from_json(
    raw: str | None,
) -> tuple[FeatureVideoProfileOverride, ...]:
    if not raw:
        return ()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(data, list):
        return ()
    out: list[FeatureVideoProfileOverride] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        key = entry.get("feature_key")
        if not isinstance(key, str) or not key.strip():
            continue
        profile = entry.get("profile_id")
        try:
            override = FeatureVideoProfileOverride(
                feature_key=key,
                profile_id=profile if isinstance(profile, str) else None,
            )
        except ValueError:
            continue
        if override.is_empty:
            continue
        out.append(override)
    return tuple(out)


def _feature_video_profiles_to_json(
    overrides: tuple[FeatureVideoProfileOverride, ...],
) -> str:
    return json.dumps(
        [
            {"feature_key": e.feature_key, "profile_id": e.profile_id}
            for e in overrides
        ],
        ensure_ascii=False,
    )


def _companions_from_json(raw: str | None) -> tuple[CharacterCompanion, ...]:
    """Parse the JSON list, dropping malformed entries.

    Mirrors the defensive shape of :func:`_loras_from_json`: any single
    bad entry (missing name, wrong types, refusing to construct) is
    skipped so a hand-edited row can't tank the character read path."""
    if not raw:
        return ()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(data, list):
        return ()
    out: list[CharacterCompanion] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        sketch_raw = entry.get("personality_sketch") or []
        if not isinstance(sketch_raw, list):
            sketch_raw = []
        sketch = tuple(s for s in sketch_raw if isinstance(s, str))
        identifier = entry.get("id")
        try:
            companion = CharacterCompanion.create(
                name=name,
                role=entry.get("role") if isinstance(entry.get("role"), str) else "",
                brief_profile=(
                    entry.get("brief_profile")
                    if isinstance(entry.get("brief_profile"), str) else ""
                ),
                personality_sketch=sketch,
                relationship_snippet=(
                    entry.get("relationship_snippet")
                    if isinstance(entry.get("relationship_snippet"), str) else ""
                ),
                id_=identifier if isinstance(identifier, str) and identifier else None,
            )
        except ValueError:
            continue
        out.append(companion)
    return tuple(out)


def _companions_to_json(companions: tuple[CharacterCompanion, ...]) -> str:
    return json.dumps(
        [
            {
                "id": c.id,
                "name": c.name,
                "role": c.role,
                "brief_profile": c.brief_profile,
                "personality_sketch": list(c.personality_sketch),
                "relationship_snippet": c.relationship_snippet,
            }
            for c in companions
        ],
        ensure_ascii=False,
    )


def _disposition_from_json(raw: str | None) -> CharacterDisposition:
    """Decode the JSON blob into a :class:`CharacterDisposition`.

    Empty / missing / malformed → ``DEFAULT`` (all medium). Mirrors the
    defensive shape of :func:`_loras_from_json`: a hand-edited row or a
    partially migrated DB shouldn't crash the character read path."""
    if not raw:
        return CharacterDisposition.DEFAULT
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return CharacterDisposition.DEFAULT
    try:
        return CharacterDisposition.from_payload(data)
    except ValueError:
        return CharacterDisposition.DEFAULT


def _disposition_to_json(disposition: CharacterDisposition) -> str:
    return json.dumps(disposition.to_payload(), ensure_ascii=False)


def _body_state_from_json(raw: str | None) -> "BodyState":
    """Decode the JSON blob into a :class:`BodyState`.

    Empty / missing / malformed → ``DEFAULT`` (all low). Same defensive
    shape as :func:`_disposition_from_json`."""
    from kokoro_link.domain.value_objects.body_state import BodyState
    if not raw:
        return BodyState.DEFAULT
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return BodyState.DEFAULT
    try:
        return BodyState.from_payload(data)
    except ValueError:
        return BodyState.DEFAULT


def _body_state_to_json(state: "BodyState") -> str:
    return json.dumps(state.to_payload(), ensure_ascii=False)


def _personality_type_from_json(raw: str | None) -> CharacterPersonalityType:
    if not raw:
        return CharacterPersonalityType.DEFAULT  # type: ignore[attr-defined]
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return CharacterPersonalityType.DEFAULT  # type: ignore[attr-defined]
    try:
        return CharacterPersonalityType.from_payload(data)
    except ValueError:
        return CharacterPersonalityType.DEFAULT  # type: ignore[attr-defined]


def _personality_type_to_json(value: CharacterPersonalityType) -> str:
    return json.dumps(value.to_payload(), ensure_ascii=False)


def _voice_profile_from_json(raw: str | None) -> VoiceProfile | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return VoiceProfile.from_payload(data)


def _voice_profile_to_json(profile: VoiceProfile | None) -> str | None:
    if profile is None or profile.is_empty:
        return None
    return json.dumps(profile.to_payload(), ensure_ascii=False)


def _row_to_domain(row: CharacterRow) -> Character:
    aspirations_raw = row.aspirations or "[]"
    return Character(
        id=row.id,
        name=row.name,
        summary=row.summary,
        user_id=row.user_id,
        personality=json.loads(row.personality),
        interests=json.loads(row.interests),
        speaking_style=row.speaking_style,
        boundaries=json.loads(row.boundaries),
        aspirations=json.loads(aspirations_raw),
        appearance=row.appearance or "",
        gender_identity=getattr(row, "gender_identity", "") or "",
        third_person_pronoun=getattr(row, "third_person_pronoun", "") or "",
        visual_gender_presentation=(
            getattr(row, "visual_gender_presentation", "") or ""
        ),
        visual_subject_type=normalise_visual_subject_type(
            getattr(row, "visual_subject_type", DEFAULT_VISUAL_SUBJECT_TYPE),
        ),
        visual_generation_style=normalise_character_visual_generation_style(
            getattr(row, "visual_generation_style", ""),
        ),
        date_of_birth=row.date_of_birth,
        image_urls=tuple(json.loads(row.image_urls or "[]")),
        allowed_tools=tuple(json.loads(row.allowed_tools or "[]")),
        loras=_loras_from_json(row.loras_json),
        state=CharacterState(
            emotion=row.state_emotion,
            affection=row.state_affection,
            fatigue=row.state_fatigue,
            trust=row.state_trust,
            energy=row.state_energy,
            last_active_at=_ensure_utc(row.state_last_active_at),
            current_intent=row.state_current_intent,
        ),
        proactive_enabled=row.proactive_enabled,
        proactive_daily_limit=row.proactive_daily_limit,
        proactive_cooldown_minutes=row.proactive_cooldown_minutes,
        world_awareness_enabled=row.world_awareness_enabled,
        world_topics=tuple(json.loads(row.world_topics or "[]")),
        subscribed_categories=tuple(
            json.loads(row.subscribed_categories or "[]")
        ),
        excluded_topics=tuple(json.loads(row.excluded_topics or "[]")),
        world_frame=row.world_frame or "modern",
        accepts_web_proactive=bool(row.accepts_web_proactive),
        unread_proactive_count=int(row.unread_proactive_count or 0),
        unread_feed_reply_count=int(row.unread_feed_reply_count or 0),
        voice_profile=_voice_profile_from_json(row.voice_profile_json),
        arc_template_id=row.arc_template_id or None,
        arc_series_id=getattr(row, "arc_series_id", None) or None,
        feature_models=_feature_models_from_json(row.feature_models_json),
        feature_image_profiles=_feature_image_profiles_from_json(
            row.feature_image_profiles_json,
        ),
        feature_video_profiles=_feature_video_profiles_from_json(
            row.feature_video_profiles_json,
        ),
        feed_daily_limit=int(row.feed_daily_limit or 0),
        companions=_companions_from_json(
            getattr(row, "companions_json", None),
        ),
        disposition=_disposition_from_json(
            getattr(row, "disposition_json", None),
        ),
        body_state=_body_state_from_json(
            getattr(row, "body_state_json", None),
        ),
        operator_pace_preference=getattr(row, "operator_pace_preference", "") or "",
        personality_type=_personality_type_from_json(
            getattr(row, "personality_type_json", None),
        ),
        frozen=bool(getattr(row, "frozen", False)),
        frozen_at=_ensure_utc(getattr(row, "frozen_at", None)),
        frozen_reason=getattr(row, "frozen_reason", None),
        subscription_locked=bool(getattr(row, "subscription_locked", False)),
        created_at=_ensure_utc(getattr(row, "created_at", None)),
    )


def _domain_to_row(
    character: Character,
    row: CharacterRow,
    *,
    include_control_fields: bool,
) -> None:
    row.user_id = character.user_id
    row.name = character.name
    row.summary = character.summary
    row.personality = json.dumps(character.personality, ensure_ascii=False)
    row.interests = json.dumps(character.interests, ensure_ascii=False)
    row.speaking_style = character.speaking_style
    row.boundaries = json.dumps(character.boundaries, ensure_ascii=False)
    row.aspirations = json.dumps(character.aspirations, ensure_ascii=False)
    row.appearance = character.appearance
    row.gender_identity = character.gender_identity
    row.third_person_pronoun = character.third_person_pronoun
    row.visual_gender_presentation = character.visual_gender_presentation
    row.visual_subject_type = normalise_visual_subject_type(
        character.visual_subject_type,
    )
    row.visual_generation_style = normalise_character_visual_generation_style(
        character.visual_generation_style,
    )
    row.date_of_birth = character.date_of_birth
    row.image_urls = json.dumps(list(character.image_urls), ensure_ascii=False)
    row.allowed_tools = json.dumps(list(character.allowed_tools), ensure_ascii=False)
    row.loras_json = json.dumps(
        [{"name": l.name, "strength": l.strength} for l in character.loras],
        ensure_ascii=False,
    )
    row.state_emotion = character.state.emotion
    row.state_affection = character.state.affection
    row.state_fatigue = character.state.fatigue
    row.state_trust = character.state.trust
    row.state_energy = character.state.energy
    row.state_last_active_at = character.state.last_active_at
    row.state_current_intent = character.state.current_intent
    row.proactive_enabled = character.proactive_enabled
    row.proactive_daily_limit = character.proactive_daily_limit
    row.proactive_cooldown_minutes = character.proactive_cooldown_minutes
    row.world_awareness_enabled = character.world_awareness_enabled
    row.world_topics = json.dumps(list(character.world_topics), ensure_ascii=False)
    row.subscribed_categories = json.dumps(
        list(character.subscribed_categories), ensure_ascii=False,
    )
    row.excluded_topics = json.dumps(
        list(character.excluded_topics), ensure_ascii=False,
    )
    row.world_frame = character.world_frame or "modern"
    row.accepts_web_proactive = character.accepts_web_proactive
    row.unread_proactive_count = max(0, int(character.unread_proactive_count))
    row.unread_feed_reply_count = max(0, int(character.unread_feed_reply_count))
    row.voice_profile_json = _voice_profile_to_json(character.voice_profile)
    row.arc_template_id = character.arc_template_id or None
    row.arc_series_id = character.arc_series_id or None
    row.feature_models_json = _feature_models_to_json(character.feature_models)
    row.feature_image_profiles_json = _feature_image_profiles_to_json(
        character.feature_image_profiles,
    )
    row.feature_video_profiles_json = _feature_video_profiles_to_json(
        character.feature_video_profiles,
    )
    row.feed_daily_limit = max(0, int(character.feed_daily_limit))
    row.companions_json = _companions_to_json(character.companions)
    row.disposition_json = _disposition_to_json(character.disposition)
    row.body_state_json = _body_state_to_json(character.body_state)
    row.operator_pace_preference = character.operator_pace_preference or ""
    row.personality_type_json = _personality_type_to_json(
        character.personality_type,
    )
    # Dedicated control fields are initialized on insert only. Existing-row
    # changes must go through their targeted repository methods so a stale
    # aggregate save cannot undo an admin freeze or subscription projection.
    # ``created_at`` is server-managed and intentionally never written here.
    if include_control_fields:
        row.frozen = bool(character.frozen)
        row.frozen_at = character.frozen_at if character.frozen else None
        row.frozen_reason = character.frozen_reason if character.frozen else None
        row.subscription_locked = bool(character.subscription_locked)
