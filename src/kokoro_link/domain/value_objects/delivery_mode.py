"""Inbound delivery mode for messaging accounts."""

from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True, slots=True)
class DeliveryMode:
    value: str

    WEBHOOK: "ClassVar[DeliveryMode]"
    POLLING: "ClassVar[DeliveryMode]"
    GATEWAY: "ClassVar[DeliveryMode]"

    def __post_init__(self) -> None:
        if not self.value or not self.value.strip():
            raise ValueError("DeliveryMode value must be non-empty")
        object.__setattr__(self, "value", self.value.strip().lower())

    def __str__(self) -> str:
        return self.value

    @classmethod
    def from_string(cls, raw: str) -> "DeliveryMode":
        mode = cls(raw)
        if mode not in CANONICAL_DELIVERY_MODES:
            raise ValueError(f"Unsupported delivery mode {raw!r}")
        return mode


DeliveryMode.WEBHOOK = DeliveryMode("webhook")
DeliveryMode.POLLING = DeliveryMode("polling")
DeliveryMode.GATEWAY = DeliveryMode("gateway")


CANONICAL_DELIVERY_MODES: tuple[DeliveryMode, ...] = (
    DeliveryMode.WEBHOOK,
    DeliveryMode.POLLING,
    DeliveryMode.GATEWAY,
)
