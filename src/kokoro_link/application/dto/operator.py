"""DTOs for the operator profile REST surface."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from kokoro_link.domain.entities.operator_profile import OperatorProfile

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from kokoro_link.application.services.quota_overage_service import (
        OverageSettings,
    )


class OperatorProfileResponse(BaseModel):
    id: str
    display_name: str
    aliases: list[str] = Field(default_factory=list)
    pronouns: str | None = None
    timezone_id: str
    has_real_name: bool
    display_name_locked: bool = False
    current_status: str | None = None
    current_status_set_at: datetime | None = None
    country_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    location_label: str | None = None

    @classmethod
    def from_domain(cls, profile: OperatorProfile) -> "OperatorProfileResponse":
        return cls(
            id=profile.id,
            display_name=profile.display_name,
            aliases=list(profile.aliases),
            pronouns=profile.pronouns,
            timezone_id=profile.timezone_id,
            has_real_name=profile.has_real_name(),
            display_name_locked=profile.display_name_locked,
            current_status=profile.current_status,
            current_status_set_at=profile.current_status_set_at,
            country_code=profile.country_code,
            latitude=profile.latitude,
            longitude=profile.longitude,
            location_label=profile.location_label,
        )


class UpdateOperatorProfileRequest(BaseModel):
    """Partial update payload.

    ``None`` means "leave alone" for both ``display_name`` and
    ``pronouns``; pass an empty string in the future if we want to
    distinguish "clear" — for now empty strings are also "leave alone"
    since the caller almost always wants to keep at least a name.
    ``aliases=None`` leaves the list alone; an empty list clears it.
    """

    display_name: str | None = None
    aliases: list[str] | None = None
    pronouns: str | None = None
    current_status: str | None = None
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    location_label: str | None = Field(default=None, max_length=128)


class OverageSettingsResponse(BaseModel):
    """AP4 quota-overage switches, plus the tier context that bounds them.

    ``tier_overage_enabled`` and ``daily_limit`` are read-only: they come from
    the control-plane tier profile, not from anything the player can change.
    They ride along so the settings UI can explain a switch that has no effect
    (a tier that never opened overage) and state the ceiling in the same
    disclosure that asks for consent.

    The two prices are this player's tier's current price for one purchase, and
    ``None`` when it cannot be stated — which is also the signal that the switch
    cannot be turned on at all right now, since consent is to a number. The UI
    disables the control and says so rather than letting the player discover it
    through a rejected write.
    """

    chat_image_enabled: bool
    feed_post_enabled: bool
    tier_overage_enabled: bool
    daily_limit: int
    chat_image_price_cr: float | None = None
    feed_post_price_cr: float | None = None

    @classmethod
    def from_domain(cls, settings: "OverageSettings") -> "OverageSettingsResponse":
        return cls(
            chat_image_enabled=settings.chat_image_enabled,
            feed_post_enabled=settings.feed_post_enabled,
            tier_overage_enabled=settings.tier_overage_enabled,
            daily_limit=settings.daily_limit,
            chat_image_price_cr=settings.chat_image_price_cr,
            feed_post_price_cr=settings.feed_post_price_cr,
        )


class UpdateOverageSettingsRequest(BaseModel):
    """Partial update — an omitted switch keeps its stored value.

    Both switches are separately consented to (turning on background LumeGram
    spend is a much bigger ask than paying for one more chat picture), so the
    payload must never carry an implicit "and the other one stays/goes" —
    hence ``None`` for "not mentioned" rather than a full-object PUT.
    """

    chat_image_enabled: bool | None = None
    feed_post_enabled: bool | None = None
