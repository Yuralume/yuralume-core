"""Weather-facts port.

Schedule planner, chat prompt builder, proactive decider and feed
composer all benefit from knowing "what's the weather like right now"
so the LLM can reflect real-world conditions in arrangements ("下雨改
室內咖啡廳"), opening lines ("好熱，剛從外面回來"), and feed posts ("窗
外正在下大雨")。

We expose weather as a single ``describe(now=...)`` call returning a
pre-rendered natural-language block. This mirrors :class:`CalendarContextPort`
exactly — both are "real-world fact layers"; call sites never need to
know whether the provider hit the internet, used a stale cache, or
fell back to ``""`` due to network error.

**LLM-first contract**: the port returns *facts only*. It must not
tell the LLM how the character should react to the weather (no
"if rain then skip outdoor activities"). The LLM decides; the port
just supplies "外面在下雨，氣溫 23°C，今天高溫 26°C，低溫 21°C"-style
sentences. This keeps the project's CLAUDE.md red line intact.

Returning an empty string means "weather unavailable" — callers must
splice the block in unconditionally and let the empty case naturally
produce zero lines (same shape as :class:`CalendarContextPort`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class WeatherLocation:
    """Coordinates used by weather providers.

    This is intentionally smaller than GeoIP's ``GeoLocation`` because
    weather only needs coordinates plus a prompt label. Region/country
    belongs to the calendar/RSS fact layer.
    """

    latitude: float
    longitude: float
    label: str
    timezone_id: str = "auto"

    def __post_init__(self) -> None:
        lat = float(self.latitude)
        lon = float(self.longitude)
        if lat < -90.0 or lat > 90.0:
            raise ValueError(f"invalid latitude: {self.latitude!r}")
        if lon < -180.0 or lon > 180.0:
            raise ValueError(f"invalid longitude: {self.longitude!r}")
        object.__setattr__(self, "latitude", lat)
        object.__setattr__(self, "longitude", lon)
        label = (self.label or "").strip() or "目前位置"
        object.__setattr__(self, "label", label)
        timezone_id = (self.timezone_id or "auto").strip() or "auto"
        object.__setattr__(self, "timezone_id", timezone_id)


class WeatherContextPort(Protocol):
    async def describe(
        self,
        *,
        now: datetime | None = None,
        location: WeatherLocation | None = None,
    ) -> str:
        """Return a natural-language block describing current weather.

        The block typically covers:

        - location label (city / region)
        - current condition phrase (晴 / 多雲 / 雨 / …)
        - current temperature
        - today's high and low temperature
        - any noteworthy phenomenon (大雨 / 高溫警示 / 寒流) when the
          provider exposes it

        Returns an empty string when the provider is disabled, the
        upstream API failed, or the data is stale beyond the freshness
        guard — callers should treat that as "no weather context
        available" and render nothing rather than fabricating values.

        ``location`` overrides the provider's deployment fallback for
        this single call. Passing ``None`` preserves the legacy env-based
        location behaviour.

        ``now`` is for tests / deterministic clocks; production callers
        omit it and let the adapter use its own clock.
        """
