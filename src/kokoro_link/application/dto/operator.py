"""DTOs for the operator profile REST surface."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from kokoro_link.domain.entities.operator_profile import OperatorProfile


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
