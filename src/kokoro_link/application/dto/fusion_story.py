"""DTOs for the fusion-story REST API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from kokoro_link.domain.entities.fusion_story import (
    FusionStory,
    FusionStoryBeat,
    FusionStoryVersion,
)


class FusionStoryBeatResponse(BaseModel):
    id: str
    sequence: int
    act: str
    title: str
    hook: str
    dramatic_question: str
    target_chars: int
    actual_chars: int
    content: str
    focus_character_ids: list[str]

    @classmethod
    def from_domain(cls, beat: FusionStoryBeat) -> "FusionStoryBeatResponse":
        return cls(
            id=beat.id,
            sequence=beat.sequence,
            act=beat.act,
            title=beat.title,
            hook=beat.hook,
            dramatic_question=beat.dramatic_question,
            target_chars=beat.target_chars,
            actual_chars=beat.actual_chars,
            content=beat.content,
            focus_character_ids=list(beat.focus_character_ids),
        )


class FusionStoryVersionResponse(BaseModel):
    id: str
    story_id: str
    version_number: int
    title: str
    premise: str
    theme: str
    full_text: str
    iteration_label: str
    created_at: datetime

    @classmethod
    def from_domain(
        cls, version: FusionStoryVersion,
    ) -> "FusionStoryVersionResponse":
        return cls(
            id=version.id,
            story_id=version.story_id,
            version_number=version.version_number,
            title=version.title,
            premise=version.premise,
            theme=version.theme,
            full_text=version.full_text,
            iteration_label=version.iteration_label,
            created_at=version.created_at,
        )


class FusionStoryProgressResponse(BaseModel):
    """Deterministic pipeline progress — pure bookkeeping, no LLM.

    ``stage`` mirrors ``status``; ``percent`` maps the stage plus the
    completed-beat ratio onto a stable 0–100 scale so the UI progress
    bar never moves backwards within a run: planning=5, writing spans
    10–85 by beats done, polishing=90, ready=100. ``None`` for failed
    (the UI shows the error state instead of a bar).
    """

    stage: str
    beats_total: int
    beats_done: int
    percent: int | None

    @classmethod
    def from_domain(
        cls, story: FusionStory,
    ) -> "FusionStoryProgressResponse":
        total = len(story.beats)
        done = sum(1 for b in story.beats if (b.content or "").strip())
        percent: int | None
        if story.status == "planning":
            percent = 5
        elif story.status == "writing":
            percent = 10 + (int(75 * done / total) if total else 0)
        elif story.status == "polishing":
            percent = 90
        elif story.status == "ready":
            percent = 100
            done = total
        else:  # failed
            percent = None
        return cls(
            stage=story.status,
            beats_total=total,
            beats_done=done,
            percent=percent,
        )


class FusionStoryResponse(BaseModel):
    id: str
    character_ids: list[str]
    prompt: str
    title: str
    premise: str
    theme: str
    status: str
    head_version: int
    full_text: str
    error_message: str | None = None
    progress: FusionStoryProgressResponse
    beats: list[FusionStoryBeatResponse]
    versions: list[FusionStoryVersionResponse]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, story: FusionStory) -> "FusionStoryResponse":
        return cls(
            id=story.id,
            character_ids=list(story.character_ids),
            prompt=story.prompt,
            title=story.title,
            premise=story.premise,
            theme=story.theme,
            status=story.status,
            head_version=story.head_version,
            full_text=story.full_text,
            error_message=story.error_message,
            progress=FusionStoryProgressResponse.from_domain(story),
            beats=[
                FusionStoryBeatResponse.from_domain(b) for b in story.beats
            ],
            versions=[
                FusionStoryVersionResponse.from_domain(v)
                for v in story.versions
            ],
            created_at=story.created_at,
            updated_at=story.updated_at,
        )


class FusionStorySummaryResponse(BaseModel):
    """Lightweight payload for the index listing.

    Drops ``full_text`` / ``beats`` / ``versions`` so the operator can
    scroll a long index without pulling several megabytes per page.
    """

    id: str
    character_ids: list[str]
    title: str
    premise: str
    status: str
    head_version: int
    error_message: str | None = None
    progress: FusionStoryProgressResponse
    total_chars: int = 0
    """Reading length for the bookshelf display — polished text length
    when ready, else the sum of written beats."""
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(
        cls, story: FusionStory,
    ) -> "FusionStorySummaryResponse":
        full = (story.full_text or "").strip()
        total_chars = (
            len(full)
            if full
            else sum(len((b.content or "").strip()) for b in story.beats)
        )
        return cls(
            id=story.id,
            character_ids=list(story.character_ids),
            title=story.title,
            premise=story.premise,
            status=story.status,
            head_version=story.head_version,
            error_message=story.error_message,
            progress=FusionStoryProgressResponse.from_domain(story),
            total_chars=total_chars,
            created_at=story.created_at,
            updated_at=story.updated_at,
        )


class CreateFusionStoryRequest(BaseModel):
    # 1–5 characters (C1-5): solo casts are allowed; the second+ member is
    # optional. ``min_length=1`` rejects an empty cast at the schema edge
    # (422) while the service enforces the same floor for non-HTTP callers.
    character_ids: list[str] = Field(..., min_length=1, max_length=5)
    prompt: str = Field(..., min_length=1, max_length=2000)


class IterateOutlineRequest(BaseModel):
    hint: str | None = Field(default=None, max_length=2000)


class IterateBeatRequest(BaseModel):
    beat_index: int = Field(..., ge=0, le=10)
    hint: str | None = Field(default=None, max_length=2000)


class FusionToArcDraftRequest(BaseModel):
    instruction: str | None = Field(default=None, max_length=2000)
