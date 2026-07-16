"""Convert a parsed SillyTavern card into a ``CharacterCardManifest``.

This is the Phase-2 front layer of the SillyTavern import feature (see
``docs/SILLYTAVERN_CARD_IMPORT_PLAN.md``). It sits between the pure parser
(``infrastructure/character_card/sillytavern.py``) and the unchanged
``.lumecard`` import pipeline: the route packs the manifest this service
builds into an in-memory ``.lumecard`` and feeds it back through the
existing ``CharacterCardImportService`` (D1 — downstream stays untouched).

Responsibilities:
- Deterministic 1:1 metadata mapping (name / tags / creator / notes).
- LLM normalization of the free-text prose into structured Core fields,
  via :class:`SillyTavernNormalizerPort` (D4).
- Route the PNG bytes into a single stage portrait member (D6, P3=a).
- Surface ``suggested_known_context`` (D5) and ``dropped_fields`` (D7/D8)
  *alongside* the manifest — never inside it, so nothing crosses the
  relationship red line automatically.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kokoro_link.application.dto.character_card import (
    CHARACTER_CARD_SCHEMA_VERSION,
    CharacterCardManifest,
    CharacterCardMeta,
    CharacterCardProfile,
)
from kokoro_link.contracts.sillytavern_normalizer import (
    SillyTavernNormalizerInput,
    SillyTavernNormalizerPort,
)
from kokoro_link.infrastructure.character_card.sillytavern import SillyTavernCard

# The PNG carrier doubles as the sole stage portrait (D6). Reuse the same
# member-path convention the packager / import pipeline already expects.
_STAGE_PORTRAIT_MEMBER = "assets/stage/0.png"

# Dropped-field markers surfaced in the preview note (D7/D8/D9). Stable
# keys so the frontend can localise them; the frontend maps each to a
# translated line.
DROPPED_CHARACTER_BOOK = "character_book"
DROPPED_GREETINGS = "greetings"
DROPPED_EXTRA_ASSETS = "extra_assets"


@dataclass(frozen=True, slots=True)
class ConvertedSillyTavernCard:
    """Result of converting a ST card into ``.lumecard`` semantics.

    ``suggested_known_context`` and ``dropped_fields`` ride alongside the
    manifest, never inside it — the manifest is the exact same shape the
    unchanged import pipeline consumes, while these two carry the extra
    ST-specific hints the route folds into the preview response."""

    manifest: CharacterCardManifest
    png: bytes | None
    suggested_known_context: str = ""
    dropped_fields: list[str] = field(default_factory=list)


class SillyTavernConvertService:
    def __init__(
        self,
        *,
        normalizer: SillyTavernNormalizerPort,
    ) -> None:
        self._normalizer = normalizer

    async def to_manifest(
        self,
        card: SillyTavernCard,
        *,
        png: bytes | None = None,
        operator_primary_language: str = "zh-TW",
        operator_id: str | None = None,
    ) -> ConvertedSillyTavernCard:
        normalized = await self._normalizer.normalize(
            SillyTavernNormalizerInput(
                name=card.name,
                description=card.description,
                personality=card.personality,
                scenario=card.scenario,
                mes_example=card.mes_example,
                first_mes=card.first_mes,
                operator_primary_language=operator_primary_language,
            ),
            operator_id=operator_id,
        )

        stage_images = [_STAGE_PORTRAIT_MEMBER] if png else []
        profile = CharacterCardProfile(
            name=card.name or _fallback_name(operator_primary_language),
            summary=normalized.summary,
            personality=list(normalized.personality),
            interests=list(normalized.interests),
            speaking_style=normalized.speaking_style or "natural",
            boundaries=list(normalized.boundaries),
            aspirations=list(normalized.aspirations),
            appearance=normalized.appearance,
            world_frame="modern",
            arc_template_ref=None,
        )
        manifest = CharacterCardManifest(
            schema_version=CHARACTER_CARD_SCHEMA_VERSION,
            card=CharacterCardMeta(
                title=card.name,
                author=card.creator,
                description=normalized.summary,
                tags=list(card.tags),
                note=card.creator_notes,
            ),
            character=profile,
            stage_images=stage_images,
            bundled_arc_templates=[],
        )
        return ConvertedSillyTavernCard(
            manifest=manifest,
            png=png,
            suggested_known_context=normalized.suggested_known_context,
            dropped_fields=_dropped_fields(card),
        )


def _dropped_fields(card: SillyTavernCard) -> list[str]:
    """List the ST fields the MVP intentionally does not import, so the
    preview note can be honest about what did not cross over (D7)."""
    dropped: list[str] = []
    if card.character_book is not None and card.character_book.entry_count > 0:
        dropped.append(DROPPED_CHARACTER_BOOK)
    if card.first_mes or card.alternate_greetings:
        dropped.append(DROPPED_GREETINGS)
    if card.has_assets:
        dropped.append(DROPPED_EXTRA_ASSETS)
    return dropped


# Locale-appropriate fallback name when a card omits ``name`` entirely.
# A static placeholder the operator immediately overwrites — not
# business logic. Mirrors the draft stub's per-locale fallback table.
_FALLBACK_NAME = {
    "zh-TW": "匯入角色",
    "en-US": "Imported Character",
    "ja-JP": "インポートされたキャラクター",
}


def _fallback_name(language_tag: str | None) -> str:
    tag = (language_tag or "").strip()
    if tag in _FALLBACK_NAME:
        return _FALLBACK_NAME[tag]
    subtag = tag.split("-", 1)[0].lower() if tag else ""
    for known, value in _FALLBACK_NAME.items():
        if known.split("-", 1)[0].lower() == subtag:
            return value
    return _FALLBACK_NAME["zh-TW"]


__all__ = [
    "ConvertedSillyTavernCard",
    "DROPPED_CHARACTER_BOOK",
    "DROPPED_EXTRA_ASSETS",
    "DROPPED_GREETINGS",
    "SillyTavernConvertService",
]
