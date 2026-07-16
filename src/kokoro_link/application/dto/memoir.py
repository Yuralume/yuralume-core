"""DTOs for the player-side memoir page (docs/MEMOIR_PLAN.md)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from kokoro_link.domain.entities.memoir import (
    ENTRY_EMOTION,
    ENTRY_MEMORY,
    ENTRY_MILESTONE,
    MemoirChapter,
    MemoirEntry,
    MemoirView,
)


class MemoirChapterResponse(BaseModel):
    period: Literal["week", "month"]
    period_start: date
    period_end: date
    narrative: str
    dominant_themes: list[str]
    evidence_quotes: list[str]

    @classmethod
    def from_domain(cls, chapter: MemoirChapter) -> "MemoirChapterResponse":
        return cls(
            period=chapter.period,  # type: ignore[arg-type]
            period_start=chapter.period_start,
            period_end=chapter.period_end,
            narrative=chapter.narrative,
            dominant_themes=list(chapter.dominant_themes),
            evidence_quotes=list(chapter.evidence_quotes),
        )


class MemoirEntryResponse(BaseModel):
    kind: Literal["memory", "emotion", "milestone"]
    entry_id: str
    occurred_at: datetime
    summary: str
    score: float
    pinned: bool
    extras: dict[str, str]

    @classmethod
    def from_domain(cls, entry: MemoirEntry) -> "MemoirEntryResponse":
        return cls(
            kind=entry.kind,  # type: ignore[arg-type]
            entry_id=entry.entry_id,
            occurred_at=entry.occurred_at,
            summary=entry.summary,
            score=entry.score,
            pinned=entry.pinned,
            extras=dict(entry.extras),
        )


class MemoirViewResponse(BaseModel):
    chapters: list[MemoirChapterResponse]
    timeline: list[MemoirEntryResponse]
    pin_count: int
    pin_limit: int

    @classmethod
    def from_domain(cls, view: MemoirView) -> "MemoirViewResponse":
        return cls(
            chapters=[
                MemoirChapterResponse.from_domain(c) for c in view.chapters
            ],
            timeline=[
                MemoirEntryResponse.from_domain(e) for e in view.timeline
            ],
            pin_count=view.pin_count,
            pin_limit=view.pin_limit,
        )


class MemoirPinRequest(BaseModel):
    entry_kind: Literal["memory", "emotion", "milestone"] = Field(
        description=(
            "Display kind of the entry to pin. Must match the kind the "
            "memoir view reported for the same ``entry_id`` — pinning a "
            "milestone as a regular memory would create a phantom row."
        ),
    )
    entry_id: str = Field(min_length=1, max_length=64)
