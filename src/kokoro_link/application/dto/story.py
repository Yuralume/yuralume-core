"""DTOs for the story-event / story-seed admin surface."""

from datetime import datetime

from pydantic import BaseModel, Field

from kokoro_link.domain.entities.story_event import StoryEvent
from kokoro_link.domain.entities.story_seed import StorySeed


class StoryEventResponse(BaseModel):
    id: str
    character_id: str
    date: str
    seed_id: str | None = None
    """``None`` when the event is driven by a story-arc beat instead of
    a seed (``StoryEvent.create`` requires exactly one of
    ``seed_id`` / ``arc_beat_id``)."""
    arc_beat_id: str | None = None
    narrative: str
    emotional_tone: str | None = None
    memorialized: bool
    created_at: datetime

    @classmethod
    def from_domain(cls, event: StoryEvent) -> "StoryEventResponse":
        return cls(
            id=event.id,
            character_id=event.character_id,
            date=event.date,
            seed_id=event.seed_id,
            arc_beat_id=event.arc_beat_id,
            narrative=event.narrative,
            emotional_tone=event.emotional_tone,
            memorialized=event.memorialized,
            created_at=event.created_at,
        )


class StorySeedResponse(BaseModel):
    id: str
    seed_text: str
    tags: list[str]
    world_frames: list[str]
    weight: float
    cooldown_days: int
    enabled: bool
    language: str = "zh-TW"
    character_id: str | None
    external_id: str | None
    pack_id: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, seed: StorySeed) -> "StorySeedResponse":
        return cls(
            id=seed.id,
            seed_text=seed.seed_text,
            tags=list(seed.tags),
            world_frames=list(seed.world_frames),
            weight=seed.weight,
            cooldown_days=seed.cooldown_days,
            enabled=seed.enabled,
            language=seed.language,
            character_id=seed.character_id,
            external_id=seed.external_id,
            pack_id=seed.pack_id,
            created_at=seed.created_at,
            updated_at=seed.updated_at,
        )


class CreateStorySeedRequest(BaseModel):
    seed_text: str
    tags: list[str] = Field(default_factory=list)
    world_frames: list[str] = Field(default_factory=lambda: ["any"])
    weight: float = 1.0
    cooldown_days: int = 7


class UpdateStorySeedRequest(BaseModel):
    seed_text: str | None = None
    tags: list[str] | None = None
    world_frames: list[str] | None = None
    weight: float | None = None
    cooldown_days: int | None = None
    enabled: bool | None = None
