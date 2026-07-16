"""Feed post classification.

Open-ended string-based VO mirroring ``MemoryKind`` so the composer can
introduce new shades without a schema migration. The constants below
are the canonical set Phase 1 emits; storage layer must tolerate
unknown values when reading rows persisted by future versions.
"""

from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True, slots=True)
class FeedKind:
    """Categorical label for a feed post.

    Drives card styling on the frontend (icon, accent colour) and the
    composer's tone hint to the LLM. Equality / hashing follow the
    underlying string so unknown labels round-trip cleanly.
    """

    value: str

    MOOD: "ClassVar[FeedKind]"
    """心情抒發 — 角色當下情緒的短文。"""
    REFLECTION: "ClassVar[FeedKind]"
    """關係反思 — 對使用者或他人的內省。"""
    WORK: "ClassVar[FeedKind]"
    """日常工作 / 行程片段。"""
    SCENE_BEAT: "ClassVar[FeedKind]"
    """主線劇情節拍的同步發文。"""
    EXTERNAL: "ClassVar[FeedKind]"
    """外部世界事件的回應。"""
    DAILY: "ClassVar[FeedKind]"
    """例行日常記錄,沒有特別來源。"""

    def __post_init__(self) -> None:
        if not self.value or not self.value.strip():
            raise ValueError("FeedKind value must be non-empty")
        object.__setattr__(self, "value", self.value.strip().lower())

    def __str__(self) -> str:
        return self.value

    @classmethod
    def from_string(cls, raw: str) -> "FeedKind":
        return cls(raw)


FeedKind.MOOD = FeedKind("mood")
FeedKind.REFLECTION = FeedKind("reflection")
FeedKind.WORK = FeedKind("work")
FeedKind.SCENE_BEAT = FeedKind("scene_beat")
FeedKind.EXTERNAL = FeedKind("external")
FeedKind.DAILY = FeedKind("daily")


CANONICAL_FEED_KINDS: tuple[FeedKind, ...] = (
    FeedKind.MOOD,
    FeedKind.REFLECTION,
    FeedKind.WORK,
    FeedKind.SCENE_BEAT,
    FeedKind.EXTERNAL,
    FeedKind.DAILY,
)
