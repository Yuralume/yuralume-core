"""Messaging platform value object.

String-based so new platforms can be added without schema migrations.
Callers should prefer the canonical constants.
"""

from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True, slots=True)
class Platform:
    value: str

    TELEGRAM: "ClassVar[Platform]"
    LINE: "ClassVar[Platform]"
    DISCORD: "ClassVar[Platform]"
    WHATSAPP: "ClassVar[Platform]"

    def __post_init__(self) -> None:
        if not self.value or not self.value.strip():
            raise ValueError("Platform value must be non-empty")
        object.__setattr__(self, "value", self.value.strip().lower())

    def __str__(self) -> str:
        return self.value

    @classmethod
    def from_string(cls, raw: str) -> "Platform":
        return cls(raw)


Platform.TELEGRAM = Platform("telegram")
Platform.LINE = Platform("line")
Platform.DISCORD = Platform("discord")
Platform.WHATSAPP = Platform("whatsapp")


CANONICAL_PLATFORMS: tuple[Platform, ...] = (
    Platform.TELEGRAM,
    Platform.LINE,
    Platform.DISCORD,
    Platform.WHATSAPP,
)
