"""Write boundary for *managed* (IP-partner) characters.

Read masking alone is not protection: the edit panel sends the whole
form on every save, so a client that predates the mask — or simply
echoes back the emptied fields it was handed — would overwrite the
partner's persona with blanks the moment the player pressed Save. This
module is the other half of the boundary, and it is deliberately an
**allowlist**: only the fields a player legitimately owns on someone
else's character can be written, so a field added to
:class:`UpdateCharacterRequest` next month is protected by default
rather than silently writable until someone remembers this file.

Two outcomes, never a third:

- A protected field carrying a **real value** is refused, loudly, naming
  the fields (the service turns this into a 400). Silently dropping it
  would tell the player their edit was saved when it was discarded.
- A protected field carrying a **blank** value — ``None``, ``""``,
  ``[]``, or a payload equal to its own default, which is exactly what a
  masked response hands back — is dropped from the payload. This is the
  stale-client case: the write must be a no-op on those fields, not a
  wipe and not an error, so the player's legitimate edits in the same
  request still land.

Ordinary characters never reach this module; the service checks
``Character.is_managed`` first, so a self-host update path is untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from pydantic import BaseModel

from kokoro_link.application.dto.character import UpdateCharacterRequest


MANAGED_WRITABLE_UPDATE_FIELDS: frozenset[str] = frozenset({
    # Runtime state the player and the world both move.
    "state",
    # Cadence / reach knobs — how much this character reaches out to
    # *this* player. Not persona, and the whole point of the hosted
    # character being yours to live with.
    "proactive_enabled",
    "proactive_daily_limit",
    "proactive_cooldown_minutes",
    "feed_daily_limit",
    "accepts_web_proactive",
    "world_awareness_enabled",
    "subscribed_categories",
    "operator_pace_preference",
    # The player's side of the relationship: their own NPC cast, the
    # disposition dial, and the character's embodied signals.
    "companions",
    "disposition",
    "body_state",
    # Tool permissions and model/profile routing are deployment
    # concerns, already gated elsewhere (routing is admin-only), and
    # carry none of the partner's authored content.
    "allowed_tools",
    "feature_models",
    "feature_image_profiles",
    "feature_video_profiles",
})
"""Everything a managed character will accept from ``PATCH``.

Everything else in :class:`UpdateCharacterRequest` — the persona prose,
the visual-generation inputs, ``name`` / ``summary`` / ``image_urls``
(the partner's identity and official art), ``voice_profile`` (the
character's licensed voice) and the arc bindings — is protected."""


def managed_protected_update_fields() -> frozenset[str]:
    """The refused half, derived from the DTO so it cannot go stale."""
    return frozenset(UpdateCharacterRequest.model_fields) - (
        MANAGED_WRITABLE_UPDATE_FIELDS
    )


@dataclass(frozen=True, slots=True)
class ManagedUpdateReview:
    """Verdict on one update payload aimed at a managed character."""

    rejected_fields: tuple[str, ...]
    """Protected fields that carried a real value, sorted. Non-empty
    means the caller must refuse the whole request."""

    payload: UpdateCharacterRequest
    """The payload reduced to its writable fields. Only meaningful when
    ``rejected_fields`` is empty."""


def review_managed_update(
    payload: UpdateCharacterRequest,
) -> ManagedUpdateReview:
    """Split ``payload`` into "refuse this" and "apply this"."""
    provided = payload.model_fields_set
    protected = managed_protected_update_fields()
    rejected = tuple(sorted(
        name
        for name in provided & protected
        if not _is_blank(getattr(payload, name))
    ))
    writable = {
        name: getattr(payload, name)
        for name in sorted(provided & MANAGED_WRITABLE_UPDATE_FIELDS)
    }
    return ManagedUpdateReview(
        rejected_fields=rejected,
        payload=UpdateCharacterRequest(**writable),
    )


def _is_blank(value: Any) -> bool:
    """Whether ``value`` carries no instruction to change anything.

    Blank means "the client had nothing here", which is what a masked
    response round-trips: an omitted or null field, an empty string or
    list, or a nested payload identical to its own defaults (the shape
    :class:`CharacterPersonalityTypePayload` and
    :class:`VoiceProfilePayload` take when they were masked or cleared).
    Numbers and dates are never blank — ``0`` and a birthday are real
    values a client had to mean.
    """
    if value is None:
        return True
    if isinstance(value, bool):
        # A protected flag can only be turned *on* by mistake; leaving
        # False as blank keeps a stale client's default from 400-ing a
        # request that would not have changed anything.
        return value is False
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return not value
    if isinstance(value, BaseModel):
        return _equals_own_default(value)
    if isinstance(value, date):
        return False
    return False


def _equals_own_default(value: BaseModel) -> bool:
    try:
        return value == type(value)()
    except (TypeError, ValueError):
        # A payload that cannot be default-constructed necessarily
        # carries required content — treat it as a real value.
        return False
