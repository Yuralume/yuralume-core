"""Application service for the operator persona aggregate.

Sits between the persistence port and the prompt builder. Two
responsibilities:

1. Load the persona (confirmed fields + pending candidates) and
   attach a freshly-computed Layer 4 snapshot so callers get a
   complete five-layer view from one call.
2. Render the persona for prompt injection — turning structured
   fields into natural-language Chinese lines, applying per-layer
   confidence thresholds, and translating Layer 4 ``familiarity_band``
   into qualitative phrases so the LLM never sees raw counts.

Layer 4 is cached in-memory (TTL from settings) because
``InteractionStrengthCalculator.compute`` runs a few aggregation
queries — fine to do once per minute, not once per prompt.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from kokoro_link.bootstrap.settings import PersonaSettings
from kokoro_link.contracts.operator_persona import OperatorPersonaRepositoryPort
from kokoro_link.domain.entities.operator_persona import (
    InteractionStrength,
    OperatorPersona,
)
from kokoro_link.domain.entities.conversation import MessageContentMode
from kokoro_link.domain.value_objects.familiarity import Familiarity
from kokoro_link.infrastructure.prompts import get_default_loader
from kokoro_link.domain.value_objects.profile_field import (
    EvidenceRef,
    ProfileField,
)
from kokoro_link.infrastructure.persona.interaction_strength_calculator import (
    InteractionStrengthCalculator,
)


# Persona fields a player may explicitly correct from the UI, mapped to
# their layer. Deliberately narrow: identity terms the character uses to
# address the player (name / nickname) are the only learned facts worth a
# manual override — the rest are observations the dream job owns. Keeping
# this an allowlist stops a caller from writing arbitrary layer/field_key
# rows through the correction endpoint.
PLAYER_EDITABLE_PERSONA_FIELDS: dict[str, int] = {
    "name": 1,
    "nickname": 1,
}

# Confidence stamped on a player's explicit correction — same level the
# creation-time seed push uses, so a correction outranks every observed /
# inferred value at render time without special-casing.
_PLAYER_EXPLICIT_CONFIDENCE = 0.95

# Confidence for a name *observed* from conversation (「叫我森森」), routed
# in by the post-turn extractor. Above the Layer-1 inject threshold (0.7)
# so it still renders, but below a deliberate settings edit so an explicit
# correction always carries more authority.
_OBSERVED_ADDRESS_CONFIDENCE = 0.85


# Inject thresholds — Layer 3 / 5 are tighter because mistakes there
# are more embarrassing ("you said you were depressed" when they
# weren't, vs "you like coffee" when they prefer tea).
_LAYER_INJECT_THRESHOLD: dict[int, float] = {
    1: 0.7,
    2: 0.7,
    3: 0.8,
    5: 0.8,
}


# Layer 1: rendered in a single compact line — "對方叫 X，N 歲，職業…"
_LAYER1_RENDER_ORDER: tuple[str, ...] = (
    "name", "nickname", "age", "occupation",
    "company_or_school", "residence", "family",
)


# Layer 2: each key on its own line with a Chinese label.
_LAYER2_LABELS: dict[str, str] = {
    "interests": "興趣",
    "diet": "飲食習慣",
    "routine": "作息",
    "consumption_style": "消費風格",
    "income_band": "收入大致",
    "relationship_status": "感情狀態",
    "life_goals": "人生目標",
}


_WORLD_EVENT_LAYER1_KEYS: tuple[str, ...] = (
    "age", "occupation", "company_or_school", "residence",
)


_WORLD_EVENT_LAYER2_KEYS: tuple[str, ...] = (
    "interests", "diet", "routine", "consumption_style", "life_goals",
)


_LAYER3_LABELS: dict[str, str] = {
    "anxieties": "在意 / 敏感的事",
    "traumas": "過去的傷",
    "secrets": "曾透露的秘密",
    "vulnerabilities": "脆弱面",
    "values": "珍視的價值觀",
    "openness_level": "對你的開放程度",
}


_LAYER5_LABELS: dict[str, str] = {
    "money_borrowed": "曾經借過錢",
    "help_asked": "曾求過你的幫忙",
    "vulnerability_shown": "曾袒露脆弱",
    "family_introduced": "願意介紹家人",
    "resource_shared": "願意分享資源",
    "secret_kept": "願意託付秘密",
}


_INTERACTION_HEAT_LABELS: dict[str, str] = {
    "stranger": "互動還很少",
    "acquaintance": "互動漸多",
    "familiar": "互動頻繁",
    "close": "互動很密切",
}


class OperatorPersonaService:
    def __init__(
        self,
        *,
        repository: OperatorPersonaRepositoryPort,
        strength_calculator: InteractionStrengthCalculator,
        settings: PersonaSettings,
    ) -> None:
        self._repository = repository
        self._calculator = strength_calculator
        self._settings = settings
        self._strength_cache: dict[
            tuple[str, str], tuple[InteractionStrength, datetime],
        ] = {}
        self._cache_lock = asyncio.Lock()

    async def get_current(
        self, character_id: str, operator_id: str,
    ) -> OperatorPersona:
        """Load the persona for one ``(character, operator)`` pair with
        Layer 4 computed and attached.

        Never returns ``None``; brand-new pairs come back with empty
        layers and Layer 4 in its ``empty()`` sentinel (stranger band).
        """
        persona = await self._repository.get(character_id, operator_id)
        strength = await self.get_interaction_strength(
            character_id, operator_id,
        )
        return OperatorPersona(
            character_id=persona.character_id,
            operator_id=persona.operator_id,
            layer1_identity=persona.layer1_identity,
            layer2_life=persona.layer2_life,
            layer3_emotional=persona.layer3_emotional,
            layer5_trust=persona.layer5_trust,
            layer4_interaction=strength,
            pending_candidates=persona.pending_candidates,
        )

    async def get_interaction_strength(
        self, character_id: str, operator_id: str,
    ) -> InteractionStrength:
        """Cached Layer 4 lookup per ``(character, operator)`` pair.

        TTL is short (default 60s) so the prompt reflects recent
        activity without hammering the DB. Computed inside the lock to
        avoid the thundering-herd case where many concurrent chat
        turns all miss the cache on the same tick.
        """
        ttl = self._settings.interaction_strength_cache_ttl_seconds
        now = datetime.now(timezone.utc)
        cache_key = (character_id, operator_id)
        cached = self._strength_cache.get(cache_key)
        if cached is not None:
            value, computed_at = cached
            if (now - computed_at).total_seconds() < ttl:
                return value
        async with self._cache_lock:
            cached = self._strength_cache.get(cache_key)
            if cached is not None:
                value, computed_at = cached
                if (now - computed_at).total_seconds() < ttl:
                    return value
            value = await self._calculator.compute(character_id, operator_id)
            self._strength_cache[cache_key] = (value, now)
            return value

    async def reject_candidate_for_operator(
        self, candidate_id: str, operator_id: str,
    ) -> bool:
        """Reject a pending candidate **only if it belongs to ``operator_id``**.

        Returns ``True`` when the row was found, owned by the caller, and
        rejected; ``False`` when the row is missing or owned by a
        different operator. The route maps ``False`` to 404 so a caller
        can neither mutate nor enumerate another operator's persona rows.
        """
        scope = await self.get_row_scope(candidate_id)
        if scope is None or scope[1] != operator_id:
            return False
        character_id, _ = scope
        await self._repository.mark_state(candidate_id, "rejected")
        self.invalidate_cache(character_id, operator_id)
        return True

    async def transition_field_state_for_operator(
        self, field_id: str, state: str, operator_id: str,
    ) -> bool:
        """Move a confirmed field into ``state`` **only if it belongs to
        ``operator_id``**.

        Same ownership contract as :meth:`reject_candidate_for_operator`:
        ``False`` when the row is missing or owned by another operator.
        State validity is enforced by the caller (route) before this runs.
        """
        scope = await self.get_row_scope(field_id)
        if scope is None or scope[1] != operator_id:
            return False
        character_id, _ = scope
        await self._repository.mark_field_state(field_id, state)
        self.invalidate_cache(character_id, operator_id)
        return True

    async def set_explicit_field_for_operator(
        self,
        *,
        character_id: str,
        operator_id: str,
        field_key: str,
        value: str,
        observed: bool = False,
        now: datetime | None = None,
    ) -> ProfileField:
        """Apply a correction of a learned identity field (name / nickname)
        for one ``(character, operator)`` pair.

        Supersede-then-insert — mirrors
        ``persona_dream_service._apply_supersede``: any existing confirmed
        row for this ``field_key`` is stamped ``superseded`` *before* the
        new field is written, so the unique
        ``(character_id, operator_id, layer, field_key, state='confirmed')``
        constraint never collides and the prior value survives as history
        rather than being overwritten in place.

        ``observed=False`` (default) is a deliberate settings-UI edit:
        written as ``user_explicit`` at the highest confidence.
        ``observed=True`` is a name captured from conversation (「叫我森森」)
        by the post-turn extractor: written as ``extraction`` at a lower
        confidence, and it **never retires an existing ``user_explicit``
        row** — a deliberate edit always outranks a passive observation
        (the seed line, which both paths update, still carries the new
        address, so the rendered name is unaffected). Returns the existing
        row unchanged in that protected case.

        Never writes back to the global ``OperatorProfile`` — a
        per-character correction must not leak into sibling characters'
        prompts (the ``_maybe_sync_operator_display_name`` no-op
        invariant). ``ValueError`` for a non-editable field or empty value
        so the route can map it to 400.
        """
        layer = PLAYER_EDITABLE_PERSONA_FIELDS.get(field_key)
        if layer is None:
            raise ValueError(
                f"persona field {field_key!r} is not player-editable "
                f"(allowed: {sorted(PLAYER_EDITABLE_PERSONA_FIELDS)})",
            )
        clean_value = (value or "").strip()
        if not clean_value:
            raise ValueError("persona field value must be non-empty")
        when = now or datetime.now(timezone.utc)
        persona = await self._repository.get(character_id, operator_id)
        existing = persona.fields_by_layer(layer).get(field_key)
        # A passive observation must not overwrite a deliberate edit.
        if (
            observed
            and existing is not None
            and existing.source == "user_explicit"
        ):
            return existing
        new_field = ProfileField(
            character_id=character_id,
            field_key=field_key,
            layer=layer,
            value=clean_value,
            confidence=(
                _OBSERVED_ADDRESS_CONFIDENCE if observed
                else _PLAYER_EXPLICIT_CONFIDENCE
            ),
            evidence_refs=(
                EvidenceRef(
                    turn_id="persona_player_edit",
                    conversation_id="persona_player_edit",
                    quote=clean_value,
                    extracted_at=when,
                ),
            ),
            last_updated=when,
            update_count=1,
            source="extraction" if observed else "user_explicit",
            content_mode=MessageContentMode.NORMAL,
        )
        # Order matters: retire the old confirmed row first so the
        # subsequent write inserts a fresh confirmed row instead of
        # colliding on the unique constraint (same pattern as the dream
        # supersede path).
        if existing is not None and existing.field_id:
            await self._repository.mark_field_state(
                existing.field_id, "superseded",
            )
        persisted = await self._repository.upsert_field(
            character_id, operator_id, new_field,
        )
        self.invalidate_cache(character_id, operator_id)
        return persisted

    async def get_row_scope(self, row_id: str) -> tuple[str, str] | None:
        """Resolve a persona row id to ``(character_id, operator_id)``.

        API routes use this before id-only mutations so they can verify
        both the row's operator scope and the parent character owner
        without reaching into the repository directly.
        """
        return await self._repository.get_row_scope(row_id)

    def invalidate_cache(
        self,
        character_id: str | None = None,
        operator_id: str | None = None,
    ) -> None:
        """Drop cached interaction strength.

        Passing both ids drops one entry; passing neither clears the
        whole cache (useful in tests). Passing only one is treated as
        clear-all because the cache key is a pair — we don't keep a
        secondary index to query by one half.
        """
        if character_id is None or operator_id is None:
            self._strength_cache.clear()
            return
        self._strength_cache.pop((character_id, operator_id), None)

    def render_for_prompt(self, persona: OperatorPersona) -> list[str]:
        """Project the persona into a list of Chinese lines for the
        prompt builder. Empty list when there's nothing worth showing.

        Layer 4 is rendered as long as we have a real ``first_message_at``
        (i.e. at least one user message exists); the other layers are
        filtered by the confidence threshold table above.
        """
        layer1 = _render_layer1(persona.layer1_identity)
        layer2 = _render_layer2(persona.layer2_life)
        layer3 = _render_layer3(persona.layer3_emotional)
        layer4 = _render_layer4(
            persona.layer4_interaction,
            frequent_min_7d=self._settings.recent_activity_frequent_min_7d,
            inactive_max_7d=self._settings.recent_activity_inactive_max_7d,
        )
        layer5 = _render_layer5(persona.layer5_trust)
        body: list[str] = []
        body.extend(layer1)
        body.extend(layer2)
        body.extend(layer3)
        body.extend(layer4)
        body.extend(layer5)
        if not body:
            return []
        header_lines = get_default_loader().render_lines(
            "operator_persona/for_prompt_header",
        )
        return ["", *header_lines, *body]

    def render_world_event_relevance(self, persona: OperatorPersona) -> list[str]:
        """Render low-risk operator profile lines for event curation.

        This is intentionally narrower than :meth:`render_for_prompt`.
        External event discovery should use public-ish interests,
        occupation, routine, and familiarity, not emotional vulnerabilities
        or trust facts. The semantic matching still happens through
        embeddings; this method only chooses which structured fields are
        safe to expose to that matcher.
        """
        lines: list[str] = []
        layer1_parts: list[str] = []
        for key in _WORLD_EVENT_LAYER1_KEYS:
            fld = persona.layer1_identity.get(key)
            if fld is None or not _passes_threshold(fld):
                continue
            layer1_parts.append(_format_layer1_clause(key, fld.value))
        if layer1_parts:
            lines.append("- 使用者公開背景：" + "，".join(layer1_parts) + "。")

        for key in _WORLD_EVENT_LAYER2_KEYS:
            fld = persona.layer2_life.get(key)
            if fld is None or not _passes_threshold(fld):
                continue
            label = _LAYER2_LABELS.get(key, key)
            lines.append(f"- 使用者{label}：{fld.value}")

        strength = persona.layer4_interaction
        if (
            strength is not None
            and strength.first_message_at is not None
            and strength.total_user_messages > 0
        ):
            band_label = _INTERACTION_HEAT_LABELS.get(
                strength.familiarity_band.value, "互動還很少",
            )
            lines.append(
                f"- 與使用者互動熱度：{band_label}；"
                "互動越多，越可以留意對方公開在意的話題。",
            )
        return lines

    def render_for_peer_gossip(
        self,
        persona: OperatorPersona,
        *,
        closeness_tier: str,
    ) -> list[str]:
        """Render what this character may share about the operator with a peer."""
        tier = (closeness_tier or "low").strip().lower()
        if tier == "low":
            return self.render_world_event_relevance(persona)
        lines: list[str] = []
        lines.extend(_render_layer1(persona.layer1_identity))
        lines.extend(_render_layer2(persona.layer2_life))
        if tier == "medium":
            lines.extend(
                _render_layer3_subset(
                    persona.layer3_emotional,
                    allowed_keys={"values", "anxieties", "openness_level"},
                )
            )
        elif tier == "high":
            lines.extend(_render_layer3(persona.layer3_emotional))
            lines.extend(_render_layer5(persona.layer5_trust))
        else:
            lines.extend(self.render_world_event_relevance(persona))
        strength_lines = _render_layer4(
            persona.layer4_interaction,
            frequent_min_7d=self._settings.recent_activity_frequent_min_7d,
            inactive_max_7d=self._settings.recent_activity_inactive_max_7d,
        )
        lines.extend(strength_lines)
        return lines


def _passes_threshold(fld: ProfileField) -> bool:
    if fld.content_mode is MessageContentMode.NSFW:
        return False
    threshold = _LAYER_INJECT_THRESHOLD.get(fld.layer, 1.0)
    return fld.confidence >= threshold


def _render_layer1(fields: dict) -> list[str]:
    parts: list[str] = []
    for key in _LAYER1_RENDER_ORDER:
        fld = fields.get(key)
        if fld is None or not _passes_threshold(fld):
            continue
        parts.append(_format_layer1_clause(key, fld.value))
    if not parts:
        return []
    return [f"- 對方資料：{'，'.join(parts)}。"]


def _format_layer1_clause(key: str, value: str) -> str:
    if key == "name":
        return f"叫 {value}"
    if key == "nickname":
        return f"小名 {value}"
    if key == "age":
        return f"{value} 歲"
    if key == "occupation":
        return f"職業是 {value}"
    if key == "company_or_school":
        return f"在 {value}"
    if key == "residence":
        return f"住在 {value}"
    if key == "family":
        return f"家庭：{value}"
    return f"{key}：{value}"


def _render_layer2(fields: dict) -> list[str]:
    lines: list[str] = []
    for key, label in _LAYER2_LABELS.items():
        fld = fields.get(key)
        if fld is None or not _passes_threshold(fld):
            continue
        lines.append(f"- {label}：{fld.value}")
    return lines


def _render_layer3(fields: dict) -> list[str]:
    lines: list[str] = []
    has_sensitive = False
    for key, label in _LAYER3_LABELS.items():
        fld = fields.get(key)
        if fld is None or not _passes_threshold(fld):
            continue
        prefix = "（dream 推論）" if fld.source == "dream_inference" else ""
        lines.append(f"- {label}：{prefix}{fld.value}")
        if key in {"anxieties", "traumas", "secrets", "vulnerabilities"}:
            has_sensitive = True
    if has_sensitive:
        lines.append(
            "  → 提醒：上面這些是對方信任你才透露的，請小心對待，"
            "不要主動拿來戳對方。",
        )
    return lines


def _render_layer3_subset(fields: dict, *, allowed_keys: set[str]) -> list[str]:
    lines: list[str] = []
    for key, label in _LAYER3_LABELS.items():
        if key not in allowed_keys:
            continue
        fld = fields.get(key)
        if fld is None or not _passes_threshold(fld):
            continue
        prefix = "（dream 推論）" if fld.source == "dream_inference" else ""
        lines.append(f"- {label}：{prefix}{fld.value}")
    return lines


def _render_layer4(
    strength: InteractionStrength | None,
    *,
    frequent_min_7d: int,
    inactive_max_7d: int,
) -> list[str]:
    """Render interaction volume, not the relationship truth.

    The relationship itself may be anchored by an initial relationship
    seed; Layer 4 only tells the model how much conversation has accrued.
    """
    if strength is None or strength.first_message_at is None:
        return []
    if strength.total_user_messages <= 0:
        return []
    band_label = _INTERACTION_HEAT_LABELS.get(
        strength.familiarity_band.value, "互動還很少",
    )
    lines = [
        f"- 與對方的互動熱度：{band_label}；"
        f"互動已持續 {strength.days_since_first_contact} 天。",
    ]
    activity_label = _resolve_activity_label(
        strength.messages_last_7_days,
        frequent_min_7d=frequent_min_7d,
        inactive_max_7d=inactive_max_7d,
    )
    if activity_label:
        lines.append(f"- 最近 7 天{activity_label}。")
    return lines


def _resolve_activity_label(
    messages_last_7_days: int,
    *,
    frequent_min_7d: int,
    inactive_max_7d: int,
) -> str | None:
    if messages_last_7_days <= 0:
        return None
    if messages_last_7_days >= frequent_min_7d:
        return "聊得很頻繁"
    if messages_last_7_days <= inactive_max_7d:
        return "比較少互動"
    return "偶爾聊聊"


def _render_layer5(fields: dict) -> list[str]:
    lines: list[str] = []
    items: list[str] = []
    for key, label in _LAYER5_LABELS.items():
        fld = fields.get(key)
        if fld is None or not _passes_threshold(fld):
            continue
        items.append(label)
    if items:
        lines.append(
            f"- 對方曾經{ '、'.join(items) }，這代表對方對你有相當程度的信任。",
        )
        lines.append(
            "  → 提醒：信任是雙向的，請珍惜；也不要主動提及讓對方有負擔。",
        )
    return lines
