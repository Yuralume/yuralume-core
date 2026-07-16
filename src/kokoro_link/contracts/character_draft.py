"""Character draft generator port.

Used by the "AI 草稿" flow so users don't need to start from a blank
form. Accepts a free-form text prompt and/or an image; the generator
returns best-effort suggested fields for a ``Character``.

The image path is optional: if the backing model can't accept images
(e.g. a text-only LLM), the generator is expected to fall back to
text-only silently rather than raising.
"""

from __future__ import annotations

from datetime import date
from dataclasses import dataclass, field
from typing import Protocol

from kokoro_link.domain.value_objects.personality_type import (
    CharacterPersonalityType,
)
from kokoro_link.domain.value_objects.visual_subject import (
    DEFAULT_VISUAL_SUBJECT_TYPE,
    VisualSubjectType,
)


@dataclass(frozen=True, slots=True)
class ImageInput:
    data: bytes
    mime_type: str


@dataclass(frozen=True, slots=True)
class CompanionDraft:
    """One AI-suggested companion sketch.

    Mirrors :class:`kokoro_link.domain.value_objects.companion.CharacterCompanion`
    minus the ``id`` — drafts are unsaved suggestions, the operator can
    accept / edit / discard before they get persisted (which is when
    ids are minted).
    """

    name: str = ""
    role: str = ""
    brief_profile: str = ""
    personality_sketch: list[str] = field(default_factory=list)
    relationship_snippet: str = ""


@dataclass(frozen=True, slots=True)
class CharacterNameCandidate:
    """One AI-suggested name option plus a short design reason."""

    name: str = ""
    rationale: str = ""


@dataclass(frozen=True, slots=True)
class CharacterDraft:
    name: str = ""
    name_candidates: list[CharacterNameCandidate] = field(default_factory=list)
    summary: str = ""
    personality: list[str] = field(default_factory=list)
    interests: list[str] = field(default_factory=list)
    speaking_style: str = ""
    boundaries: list[str] = field(default_factory=list)
    aspirations: list[str] = field(default_factory=list)
    appearance: str = ""
    gender_identity: str = ""
    third_person_pronoun: str = ""
    visual_gender_presentation: str = ""
    visual_subject_type: VisualSubjectType = DEFAULT_VISUAL_SUBJECT_TYPE
    date_of_birth: date | None = None
    world_frame: str = "modern"
    personality_type: CharacterPersonalityType = field(
        default_factory=lambda: CharacterPersonalityType.DEFAULT,  # type: ignore[attr-defined]
    )
    companions: list[CompanionDraft] = field(default_factory=list)
    """Best-effort suggested companions (NPC sketches). Empty list when
    the model didn't surface any — operator can still add them manually
    or call the dedicated companion generator endpoint later."""


@dataclass(frozen=True, slots=True)
class CompanionGenerationContext:
    """Inputs for the standalone companion generator port.

    Used when the character already exists (the operator is browsing
    the settings tab and wants "give me a few NPCs around this character").
    All fields are pre-rendered strings so the port stays free of any
    domain entity import — the application service flattens
    ``Character`` into these fields and passes them in.
    """

    character_name: str
    character_summary: str = ""
    character_personality: str = ""
    character_interests: str = ""
    existing_companions_summary: str = ""
    """One-line-per-existing-companion text so the LLM doesn't suggest
    duplicates / near-duplicates. Empty string = no existing companions."""
    hint: str = ""
    """Optional operator instruction (e.g. 「再多生兩個她在公司的同事」)."""
    count: int = 3
    operator_primary_language: str = "zh-TW"
    """BCP 47 tag for player-visible draft text fields."""


class CharacterDraftGeneratorPort(Protocol):
    async def generate(
        self,
        *,
        prompt: str | None,
        image: ImageInput | None,
        operator_primary_language: str = "zh-TW",
        operator_id: str | None = None,
    ) -> CharacterDraft:
        """Generate a draft character profile from hints."""


class CompanionDraftGeneratorPort(Protocol):
    async def generate(
        self, *, context: CompanionGenerationContext,
    ) -> list[CompanionDraft]:
        """Generate AI-suggested companions for an existing character.

        Returns a (possibly empty) list of sketches; the operator gets
        to accept / edit / discard each before they get persisted via
        the regular character update endpoint."""
