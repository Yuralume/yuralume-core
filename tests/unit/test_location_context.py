"""Unit tests for :mod:`location_context` operator-location projections.

Behaviour under test:

* ``weather_location_from_operator`` derives the WeatherLocation label
  from ``operator.location_label`` / ``country_code`` when set, and
  falls back to a deterministic per-locale "current location" string
  (keyed off ``operator.primary_language``) only when neither is set —
  never a hardcoded Chinese literal, so en/ja operators don't leak
  Chinese text into the weather prompt fact layer.
* ``calendar_region_from_operator`` still derives purely from
  ``country_code`` (unaffected by this change).
"""

from __future__ import annotations

from kokoro_link.application.services.location_context import (
    weather_location_from_operator,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile


def _operator(**overrides: object) -> OperatorProfile:
    defaults: dict[str, object] = {
        "id": "op-1",
        "display_name": "Tester",
        "latitude": 25.03,
        "longitude": 121.56,
    }
    defaults.update(overrides)
    return OperatorProfile(**defaults)  # type: ignore[arg-type]


def test_label_fallback_localizes_to_english_for_en_us_operator() -> None:
    operator = _operator(
        location_label=None,
        country_code=None,
        primary_language="en-US",
    )
    location = weather_location_from_operator(operator)
    assert location is not None
    assert location.label == "Current location"


def test_label_fallback_defaults_to_zh_tw_without_primary_language() -> None:
    # No primary_language override → entity default (zh-TW).
    operator = _operator(location_label=None, country_code=None)
    location = weather_location_from_operator(operator)
    assert location is not None
    assert location.label == "目前位置"


def test_label_fallback_localizes_to_japanese_for_ja_jp_operator() -> None:
    operator = _operator(
        location_label=None,
        country_code=None,
        primary_language="ja-JP",
    )
    location = weather_location_from_operator(operator)
    assert location is not None
    assert location.label == "現在地"


def test_location_label_still_wins_over_fallback() -> None:
    operator = _operator(
        location_label="台北",
        country_code="TW",
        primary_language="en-US",
    )
    location = weather_location_from_operator(operator)
    assert location is not None
    assert location.label == "台北"


def test_country_code_wins_over_localized_fallback() -> None:
    operator = _operator(
        location_label=None,
        country_code="TW",
        primary_language="en-US",
    )
    location = weather_location_from_operator(operator)
    assert location is not None
    assert location.label == "TW"


def test_none_operator_returns_none() -> None:
    assert weather_location_from_operator(None) is None


def test_missing_coordinates_returns_none() -> None:
    operator = _operator(latitude=None, longitude=None)
    assert weather_location_from_operator(operator) is None
