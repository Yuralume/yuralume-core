"""Helpers for projecting operator location into fact-layer inputs."""

from __future__ import annotations

from kokoro_link.contracts.weather_context import WeatherLocation
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.infrastructure.localization.fallback_texts import (
    localized_fallback_text,
)


def calendar_region_from_operator(operator: OperatorProfile | None) -> str | None:
    if operator is None:
        return None
    code = (operator.country_code or "").strip().upper()
    return code or None


def weather_location_from_operator(
    operator: OperatorProfile | None,
) -> WeatherLocation | None:
    if operator is None:
        return None
    if operator.latitude is None or operator.longitude is None:
        return None
    label = (
        operator.location_label
        or operator.country_code
        or localized_fallback_text(
            "weather.current_location_label", operator.primary_language,
        )
    )
    return WeatherLocation(
        latitude=operator.latitude,
        longitude=operator.longitude,
        label=label,
        timezone_id=operator.timezone_id or "auto",
    )


def prompt_location_fact(operator: OperatorProfile | None) -> str:
    """Return a compact player-location fact for LLM prompts.

    Empty string means the operator has no editable location set; callers
    should omit the line and rely on provider fallbacks.
    """
    if operator is None:
        return ""
    label = (operator.location_label or "").strip()
    country = (operator.country_code or "").strip().upper()
    parts: list[str] = []
    if label:
        parts.append(label)
    if country:
        parts.append(country)
    if not parts:
        return ""
    return "使用者所在地：" + " / ".join(parts)
