"""Request / response shapes for the hosted locale lifecycle (plan G2).

Kept out of ``routes/auth.py`` so the auth router stays the thin
setup / login / me surface it has always been, and so ``GET /auth/me``
can project a location hint without importing the locale router.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from kokoro_link.application.services.player_locale_service import LocationHint
from kokoro_link.domain.entities.operator_profile import normalise_language_tag
from kokoro_link.domain.value_objects.timezone import normalise_timezone_id


def _validate_optional_timezone(value: str | None) -> str | None:
    if value is None:
        return None
    return normalise_timezone_id(value)


def _validate_optional_language(value: str | None) -> str | None:
    if value is None:
        return None
    # Structural validation lives in the domain helper so the entity
    # invariant and the API contract cannot drift apart; pydantic turns the
    # ValueError into a 422.
    return normalise_language_tag(value)


class LocationHintResponse(BaseModel):
    """A "you seem to be somewhere else" suggestion — never an applied fact."""

    country_code: str
    label: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    timezone_id: str | None = None
    detected_at: datetime | None = None

    @classmethod
    def from_domain(cls, hint: LocationHint) -> "LocationHintResponse":
        return cls(
            country_code=hint.country_code,
            label=hint.label,
            latitude=hint.latitude,
            longitude=hint.longitude,
            timezone_id=hint.timezone_id,
            detected_at=hint.detected_at,
        )


class LocaleChangeRequest(BaseModel):
    """Deliberate, guardrail-consuming change of timezone and/or language."""

    timezone_id: str | None = Field(default=None, max_length=64)
    primary_language: str | None = Field(default=None, max_length=16)

    @field_validator("timezone_id")
    @classmethod
    def _normalise_timezone(cls, v: str | None) -> str | None:
        return _validate_optional_timezone(v)

    @field_validator("primary_language")
    @classmethod
    def _normalise_language(cls, v: str | None) -> str | None:
        return _validate_optional_language(v)

    @model_validator(mode="after")
    def _require_one_field(self) -> "LocaleChangeRequest":
        if self.timezone_id is None and self.primary_language is None:
            raise ValueError(
                "at least one of timezone_id / primary_language is required",
            )
        return self


class LocaleConfirmRequest(BaseModel):
    """First-login confirmation, optionally correcting the seeded values.

    Corrections here are free (no guardrail spend) because nothing has been
    generated yet — see ``PlayerLocaleService.confirm``.
    """

    timezone_id: str | None = Field(default=None, max_length=64)
    primary_language: str | None = Field(default=None, max_length=16)
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    location_label: str | None = Field(default=None, max_length=128)
    dismiss_location_hint: bool = False

    @field_validator("timezone_id")
    @classmethod
    def _normalise_timezone(cls, v: str | None) -> str | None:
        return _validate_optional_timezone(v)

    @field_validator("primary_language")
    @classmethod
    def _normalise_language(cls, v: str | None) -> str | None:
        return _validate_optional_language(v)


class GeoSearchResultResponse(BaseModel):
    """One city suggestion, already shaped for the location form."""

    label: str
    latitude: float
    longitude: float
    country_code: str | None = None
    timezone_id: str | None = None


class GeoSearchResponse(BaseModel):
    results: list[GeoSearchResultResponse] = Field(default_factory=list)
