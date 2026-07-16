"""Goal status value object.

String-based so new statuses can be added without schema migrations.
Callers should prefer the canonical constants.
"""

from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True, slots=True)
class GoalStatus:
    value: str

    ACTIVE: "ClassVar[GoalStatus]"
    PAUSED: "ClassVar[GoalStatus]"
    DONE: "ClassVar[GoalStatus]"
    ABANDONED: "ClassVar[GoalStatus]"

    def __post_init__(self) -> None:
        if not self.value or not self.value.strip():
            raise ValueError("GoalStatus value must be non-empty")
        object.__setattr__(self, "value", self.value.strip().lower())

    def __str__(self) -> str:
        return self.value

    @classmethod
    def from_string(cls, raw: str) -> "GoalStatus":
        return cls(raw)

    @property
    def is_terminal(self) -> bool:
        return self.value in {"done", "abandoned"}


GoalStatus.ACTIVE = GoalStatus("active")
GoalStatus.PAUSED = GoalStatus("paused")
GoalStatus.DONE = GoalStatus("done")
GoalStatus.ABANDONED = GoalStatus("abandoned")


CANONICAL_STATUSES: tuple[GoalStatus, ...] = (
    GoalStatus.ACTIVE,
    GoalStatus.PAUSED,
    GoalStatus.DONE,
    GoalStatus.ABANDONED,
)
