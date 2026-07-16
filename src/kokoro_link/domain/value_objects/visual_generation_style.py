from __future__ import annotations

from typing import Literal

VISUAL_GENERATION_STYLE_DEFAULT = "anime"
VISUAL_GENERATION_STYLE_VALUES = ("anime", "realistic")

VisualGenerationStyle = Literal["anime", "realistic"]


def normalise_visual_generation_style(value: object) -> str:
    """Return a supported visual style id, falling back to product default."""
    if isinstance(value, dict):
        value = value.get("style")
    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in VISUAL_GENERATION_STYLE_VALUES:
            return candidate
    return VISUAL_GENERATION_STYLE_DEFAULT


def normalise_character_visual_generation_style(value: object) -> str:
    """Return a per-character style override, or empty string for inherit."""
    if value is None:
        return ""
    if isinstance(value, dict):
        value = value.get("style")
    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in VISUAL_GENERATION_STYLE_VALUES:
            return candidate
    return ""


def is_supported_visual_generation_style(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return value.strip().lower() in VISUAL_GENERATION_STYLE_VALUES
