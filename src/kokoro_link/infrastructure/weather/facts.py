"""Pure weather-fact derivation and rendering.

Mirrors :mod:`kokoro_link.infrastructure.calendar.facts`: structured
data class + ``to_prompt_block()`` Chinese prose renderer, no I/O.

This split lets adapters focus on protocol-specific decoding (HTTP /
JSON / CSV) and hand the parsed numbers to :class:`WeatherFacts` for
consistent prompt rendering across all providers.

**LLM-first**: the renderer emits **facts only** (天氣狀況、溫度、降雨
機率…). It deliberately omits behavioural cues (「適合戶外活動」)；
那些屬於 LLM 該自行從事實層判斷的範圍。

We use coarse condition phrases (mapped from WMO weather codes) rather
than provider-specific text so the LLM sees a stable vocabulary
regardless of which adapter is wired.
"""

from __future__ import annotations

from dataclasses import dataclass


# WMO weather interpretation codes (Open-Meteo 等多家通用) → 中文短語。
# 涵蓋常見天況；未列 code 落到「天氣狀況不明」並仍輸出溫度。
# 來源：https://open-meteo.com/en/docs#api_form 的 weather_code 表。
_WMO_CONDITION: dict[int, str] = {
    0: "晴朗",
    1: "大致晴朗",
    2: "局部多雲",
    3: "陰天",
    45: "霧",
    48: "凍霧",
    51: "毛毛雨",
    53: "毛毛雨",
    55: "毛毛雨（較密）",
    56: "凍毛毛雨",
    57: "凍毛毛雨（較密）",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "凍雨",
    67: "凍雨（較強）",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "陣雨",
    81: "陣雨（較強）",
    82: "大雷雨陣雨",
    85: "陣雪",
    86: "陣雪（較強）",
    95: "雷雨",
    96: "雷雨夾冰雹",
    99: "雷雨夾冰雹（強）",
}


def condition_phrase(code: int | None) -> str:
    """Map a WMO weather code into a short Chinese phrase.

    Defensive: unknown / ``None`` codes fall back to a generic phrase
    rather than dropping the entire weather block — temperature alone
    is still useful context."""
    if code is None:
        return "天氣狀況不明"
    return _WMO_CONDITION.get(int(code), "天氣狀況不明")


def _fmt_temp(value: float | None) -> str | None:
    """Render a Celsius temperature to one decimal place, or ``None``.

    Returning ``None`` when the value is missing lets the prose renderer
    omit that sentence cleanly rather than emitting "高溫 None°C"."""
    if value is None:
        return None
    return f"{round(float(value), 1)}°C"


@dataclass(frozen=True, slots=True)
class WeatherFacts:
    """Structured weather snapshot for a single location at a single
    moment, ready for prompt rendering.

    All fields except ``location_label`` are optional — adapters fill
    in whatever the upstream gave them and the renderer suppresses
    missing lines. This shape works whether the provider gives only
    current conditions, only daily highs/lows, or both.
    """

    location_label: str
    """Human-readable location ("台北")。Always present so the prompt
    can mention where the weather refers to (avoids confusion when a
    user travels and the character's locale is different)."""

    condition_code: int | None = None
    """WMO weather code (current condition). ``None`` if the adapter
    didn't expose it — :func:`condition_phrase` handles that gracefully."""

    temperature_c: float | None = None
    """Current temperature in Celsius."""

    high_c: float | None = None
    """Today's forecast high temperature in Celsius."""

    low_c: float | None = None
    """Today's forecast low temperature in Celsius."""

    precipitation_probability: int | None = None
    """Today's max precipitation probability (0–100). When ≥ 60 we add
    a "出門可能會用到傘" hint sentence in the prompt block."""

    is_day: bool | None = None
    """Whether the provider considers it daytime at the reference
    moment. Drives the "白天 / 入夜" phrase to anchor the LLM's tonal
    choices (避免角色說 '剛起床' 但其實是深夜)."""

    @property
    def has_any_signal(self) -> bool:
        """``True`` when at least one weather signal is present.

        Used by the rendering path to short-circuit to empty string —
        emitting "台北的天氣：" with no follow-up lines is worse than
        emitting nothing.
        """
        return any(
            value is not None for value in (
                self.condition_code,
                self.temperature_c,
                self.high_c,
                self.low_c,
                self.precipitation_probability,
            )
        )

    def to_prompt_block(self) -> str:
        """Render to a prompt-ready Chinese block.

        Empty string when no usable signal is present, so callers can
        splice unconditionally and rely on the empty case dropping
        zero lines.
        """
        if not self.has_any_signal:
            return ""
        lines: list[str] = []
        lines.append(f"{self.location_label}目前天氣（事實層；請自行從中推導角色該如何反應）：")
        condition = condition_phrase(self.condition_code)
        now_temp = _fmt_temp(self.temperature_c)
        if now_temp is not None:
            lines.append(f"- 現在：{condition}，氣溫 {now_temp}")
        else:
            lines.append(f"- 現在：{condition}")
        high = _fmt_temp(self.high_c)
        low = _fmt_temp(self.low_c)
        if high is not None and low is not None:
            lines.append(f"- 今日溫度：高溫 {high}、低溫 {low}")
        elif high is not None:
            lines.append(f"- 今日高溫：{high}")
        elif low is not None:
            lines.append(f"- 今日低溫：{low}")
        if self.precipitation_probability is not None:
            lines.append(f"- 今日最高降雨機率：{int(self.precipitation_probability)}%")
            # 60% 是一般日常「會不會帶傘」的心理門檻；當作事實提醒，
            # 不寫「應該帶傘」這種行為指令（LLM 自行從事實判斷）。
            if int(self.precipitation_probability) >= 60:
                lines.append("- 提醒：今日有相當機率會下雨")
        if self.is_day is False:
            lines.append("- 此刻為夜間時段")
        elif self.is_day is True:
            lines.append("- 此刻為白天時段")
        return "\n".join(lines)
