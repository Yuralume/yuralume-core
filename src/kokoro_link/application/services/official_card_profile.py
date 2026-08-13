"""Reading a Cloud official-card ``profile`` map into Core's own DTOs.

Cloud publishes a card's prose as an untyped map on purpose — the field set
belongs to the card translator on both sides of the wire, and a record on
either side would be a third definition that goes stale the moment somebody
adds a field. That leaves *reading* it as the thing that must exist exactly
once, because there are now two readers with different destinations:

* the **browse path** projects it into a :class:`CharacterCardPreview` for
  the gallery (public and cloud-exclusive cards alike);
* the **cloud-exclusive install** (EC4) projects it into a
  :class:`CharacterCardProfile`, the same portable A-layer shape a
  ``.lumecard`` manifest carries, and creates a character from it.

Both have to make the same judgement calls — an unusable personality-type
code must not blank a card, a companion without a name is not a companion —
so those calls live here rather than twice.

**The map is prose only.** It carries no structural settings: disposition
bands, cadence numbers, world frame, tool ids, arc bindings and
visual-subject type are not translatable, so they are not in the translatable
field set Cloud publishes as ``profile``. For the **browse** path that is the
end of it — a gallery face shows prose, and :class:`CharacterCardPreview`
says so in its own ``source`` docstring. For the **install** path it was a
fidelity hole: the character a player got was the licensor's words over the
installing build's defaults. :func:`card_profile_with_document_structure`
closes it by laying that same prose over the card's own ``manifest.json``
(EC4-C), which Cloud publishes beside the prose for exclusive cards.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from kokoro_link.application.dto.character import (
    CharacterCompanionPayload,
    CharacterPersonalityTypePayload,
)
from kokoro_link.application.dto.character_card import (
    CHARACTER_CARD_SCHEMA_VERSION,
    CharacterCardManifest,
    CharacterCardPreviewCompanion,
    CharacterCardProfile,
)

_LOGGER = logging.getLogger(__name__)

CATALOG_PROSE_FIELDS: tuple[str, ...] = (
    "name",
    "summary",
    "personality",
    "interests",
    "speaking_style",
    "boundaries",
    "aspirations",
    "appearance",
    "gender_identity",
    "third_person_pronoun",
    "visual_gender_presentation",
    "world_topics",
    "excluded_topics",
    "personality_type",
    "companions",
)
"""Exactly the fields :func:`card_profile_from_catalog` fills, and no others.

This is the seam between the two halves of an exclusive card, so it has to
be one list rather than a judgement made twice: a field named here comes
from the catalog (and from its approved translation, which is the entire
point of it being prose), and a field *not* named here comes from the card's
own manifest. Getting that wrong in either direction is silent — a
structural key overwritten with a default, or a translated name thrown away
in favour of the source. ``test_official_card_profile`` pins the list against
what the projection actually sets.
"""


def profile_text(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    return value.strip() if isinstance(value, str) else ""


def profile_text_list(payload: Mapping[str, Any], field: str) -> list[str]:
    value = payload.get(field)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item.strip() for item in value if isinstance(item, str)]


def profile_personality_type(raw: object) -> CharacterPersonalityTypePayload:
    """The 16-type reference, or an empty one when it cannot be used.

    An unknown code fails the domain validator, and one bad field must not
    blank a whole card in the gallery — nor sink an install that is
    otherwise perfectly good.
    """
    if not isinstance(raw, Mapping):
        return CharacterPersonalityTypePayload()
    try:
        return CharacterPersonalityTypePayload(
            code=profile_text(raw, "code"),
            rationale=profile_text(raw, "rationale"),
            consistency_notes=profile_text_list(raw, "consistency_notes"),
        )
    except ValueError:
        _LOGGER.warning(
            "official card catalog: unusable personality_type — dropping",
            exc_info=True,
        )
        return CharacterPersonalityTypePayload()


def _companion_entries(raw: object) -> list[Mapping[str, Any]]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    return [
        entry for entry in raw
        if isinstance(entry, Mapping) and profile_text(entry, "name")
    ]


def preview_companions(raw: object) -> list[CharacterCardPreviewCompanion]:
    """The player-safe companion projection a card face renders."""
    return [
        CharacterCardPreviewCompanion(
            name=profile_text(entry, "name"), role=profile_text(entry, "role"),
        )
        for entry in _companion_entries(raw)
    ]


def profile_companions(raw: object) -> list[CharacterCompanionPayload]:
    """The full companion payload an install creates real NPC rows from.

    Wider than :func:`preview_companions` because this one becomes data: the
    sketch and the relationship snippet are what make a companion more than
    a name in a list. No ``id`` is carried — Cloud strips it and the domain
    mints a fresh one, so a companion is never handed an identifier from
    another deployment.
    """
    return [
        CharacterCompanionPayload(
            name=profile_text(entry, "name"),
            role=profile_text(entry, "role"),
            brief_profile=profile_text(entry, "brief_profile"),
            personality_sketch=profile_text_list(entry, "personality_sketch"),
            relationship_snippet=profile_text(entry, "relationship_snippet"),
        )
        for entry in _companion_entries(raw)
    ]


def card_profile_from_catalog(
    payload: Mapping[str, Any], *, fallback_name: str,
) -> CharacterCardProfile:
    """Project a catalog profile map into the portable A-layer profile.

    ``fallback_name`` covers the one field with no default worth having: a
    character must be called something, and the card's catalog title is what
    the player just read on the shelf.
    """
    return CharacterCardProfile(
        name=profile_text(payload, "name") or fallback_name,
        summary=profile_text(payload, "summary"),
        personality=profile_text_list(payload, "personality"),
        interests=profile_text_list(payload, "interests"),
        speaking_style=profile_text(payload, "speaking_style") or "natural",
        boundaries=profile_text_list(payload, "boundaries"),
        aspirations=profile_text_list(payload, "aspirations"),
        appearance=profile_text(payload, "appearance"),
        gender_identity=profile_text(payload, "gender_identity"),
        third_person_pronoun=profile_text(payload, "third_person_pronoun"),
        visual_gender_presentation=profile_text(
            payload, "visual_gender_presentation",
        ),
        world_topics=profile_text_list(payload, "world_topics"),
        excluded_topics=profile_text_list(payload, "excluded_topics"),
        personality_type=profile_personality_type(
            payload.get("personality_type"),
        ),
        companions=profile_companions(payload.get("companions")),
    )


def card_profile_with_document_structure(
    prose: CharacterCardProfile,
    document: Mapping[str, Any] | None,
) -> CharacterCardProfile:
    """The card's structural settings, wearing the catalog's prose (EC4-C).

    ``document`` is the card's own ``manifest.json`` as Cloud stored it. The
    manifest is the authority on everything the translatable field set
    cannot carry — world frame, allowed tools, disposition bands, proactive
    cadence, visual subject type, date of birth — and ``prose`` is the
    authority on the words, because it is the half that has been through the
    locale chain and a human's approval.

    Returns ``prose`` unchanged when there is no usable document, which is
    the pre-EC4-C install and a perfectly good character: Cloud omits the
    document rather than fail an install over an unreadable artifact, and a
    manifest this build cannot parse must not be a worse outcome than one it
    was never sent.

    Arc refs are dropped on purpose. A ``.lumecard`` carries its bundled
    templates as separate zip members and the document is the manifest
    alone, so an ``arc_template_ref`` here would name a template that was
    never landed — the importer already refuses to bind a dangling ref, and
    carrying one forward would only invite a future reader to trust it.
    """
    structure = _document_profile(document)
    if structure is None:
        return prose
    return structure.model_copy(update={
        **{name: getattr(prose, name) for name in CATALOG_PROSE_FIELDS},
        "arc_template_ref": None,
        "arc_series_ref": None,
    })


def _document_profile(
    document: Mapping[str, Any] | None,
) -> CharacterCardProfile | None:
    """Validate a stored manifest through the ``.lumecard`` schema, or shrug.

    The same DTO and the same version rule the ordinary import path applies,
    rather than a second reading of the manifest: a card document that
    ``import_card`` would refuse must not land a character here through a
    laxer door. What differs is the consequence — an import has nothing left
    to do if the manifest is unreadable, whereas here the prose payload is
    still a complete, installable card.
    """
    if not isinstance(document, Mapping) or not document:
        return None
    declared = document.get("schema_version")
    if (
        isinstance(declared, int)
        and not isinstance(declared, bool)
        and declared > CHARACTER_CARD_SCHEMA_VERSION
    ):
        _LOGGER.warning(
            "official card document declares schema_version %s, newer than "
            "this build supports (%s) — installing on structural defaults",
            declared, CHARACTER_CARD_SCHEMA_VERSION,
        )
        return None
    try:
        manifest = CharacterCardManifest.model_validate(dict(document))
    except (ValueError, TypeError):
        # pydantic's ValidationError is a ValueError.
        _LOGGER.warning(
            "official card document does not match the character-card "
            "schema — installing on structural defaults", exc_info=True,
        )
        return None
    return manifest.character


__all__ = [
    "CATALOG_PROSE_FIELDS",
    "card_profile_from_catalog",
    "card_profile_with_document_structure",
    "preview_companions",
    "profile_companions",
    "profile_personality_type",
    "profile_text",
    "profile_text_list",
]
