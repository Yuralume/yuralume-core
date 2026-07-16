"""Player-safe projection of one character's view of the operator."""

from __future__ import annotations

from pydantic import BaseModel, Field


class PersonaProjectionFactResponse(BaseModel):
    field_id: str = Field(min_length=1)
    field_key: str = Field(default="", max_length=48)
    """Stable enum key (e.g. ``name`` / ``interests``) the frontend
    translates via its trilingual bundle (plan D6). ``label`` remains the
    zh-TW default so older clients keep rendering a sensible string."""
    label: str = Field(min_length=1, max_length=32)
    value: str = Field(min_length=1, max_length=160)


class PersonaProjectionResponse(BaseModel):
    character_id: str
    narrative: str = Field(default="", max_length=800)
    facts: list[PersonaProjectionFactResponse] = Field(default_factory=list)
    empty: bool = True
