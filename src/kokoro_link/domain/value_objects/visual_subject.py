from __future__ import annotations

from typing import Literal

VisualSubjectType = Literal[
    "auto",
    "human",
    "animal",
    "anthropomorphic",
    "creature",
    "object",
]

VISUAL_SUBJECT_TYPES: tuple[str, ...] = (
    "auto",
    "human",
    "animal",
    "anthropomorphic",
    "creature",
    "object",
)
DEFAULT_VISUAL_SUBJECT_TYPE: VisualSubjectType = "auto"


def normalise_visual_subject_type(value: object) -> VisualSubjectType:
    if not isinstance(value, str):
        return DEFAULT_VISUAL_SUBJECT_TYPE
    candidate = value.strip().lower()
    if candidate in VISUAL_SUBJECT_TYPES:
        return candidate  # type: ignore[return-value]
    return DEFAULT_VISUAL_SUBJECT_TYPE
