"""DTOs for player-side chat starter assistance."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ChatAssistSuggestion(BaseModel):
    text: str = Field(min_length=1, max_length=240)
    reason: str | None = Field(default=None, max_length=240)


class ChatAssistSuggestionsResponse(BaseModel):
    suggestions: list[ChatAssistSuggestion] = Field(default_factory=list)
