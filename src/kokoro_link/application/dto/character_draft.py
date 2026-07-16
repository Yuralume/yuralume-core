"""Character draft DTOs."""

from datetime import date

from pydantic import BaseModel, Field

from kokoro_link.application.dto.character import (
    CharacterCompanionPayload,
    CharacterPersonalityTypePayload,
)
from kokoro_link.contracts.character_draft import (
    CharacterDraft,
    CharacterNameCandidate,
)
from kokoro_link.domain.value_objects.visual_subject import (
    DEFAULT_VISUAL_SUBJECT_TYPE,
    VisualSubjectType,
    normalise_visual_subject_type,
)


class CharacterNameCandidatePayload(BaseModel):
    name: str = ""
    rationale: str = ""

    @classmethod
    def from_domain(
        cls, candidate: CharacterNameCandidate,
    ) -> "CharacterNameCandidatePayload":
        return cls(name=candidate.name, rationale=candidate.rationale)


class CharacterDraftResponse(BaseModel):
    name: str = ""
    name_candidates: list[CharacterNameCandidatePayload] = Field(
        default_factory=list,
    )
    summary: str = ""
    personality: list[str] = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)
    speaking_style: str = ""
    boundaries: list[str] = Field(default_factory=list)
    aspirations: list[str] = Field(default_factory=list)
    appearance: str = ""
    gender_identity: str = ""
    third_person_pronoun: str = ""
    visual_gender_presentation: str = ""
    visual_subject_type: VisualSubjectType = DEFAULT_VISUAL_SUBJECT_TYPE
    date_of_birth: date | None = None
    world_frame: str = "modern"
    personality_type: CharacterPersonalityTypePayload = Field(
        default_factory=CharacterPersonalityTypePayload,
    )
    companions: list[CharacterCompanionPayload] = Field(default_factory=list)

    @classmethod
    def from_domain(cls, draft: CharacterDraft) -> "CharacterDraftResponse":
        return cls(
            name=draft.name,
            name_candidates=[
                CharacterNameCandidatePayload.from_domain(candidate)
                for candidate in draft.name_candidates
            ],
            summary=draft.summary,
            personality=list(draft.personality),
            interests=list(draft.interests),
            speaking_style=draft.speaking_style,
            boundaries=list(draft.boundaries),
            aspirations=list(draft.aspirations),
            appearance=draft.appearance,
            gender_identity=draft.gender_identity,
            third_person_pronoun=draft.third_person_pronoun,
            visual_gender_presentation=draft.visual_gender_presentation,
            visual_subject_type=normalise_visual_subject_type(
                draft.visual_subject_type,
            ),
            date_of_birth=draft.date_of_birth,
            world_frame=draft.world_frame,
            personality_type=CharacterPersonalityTypePayload.from_domain(
                draft.personality_type,
            ),
            companions=[
                CharacterCompanionPayload(
                    id=None,
                    name=c.name,
                    role=c.role,
                    brief_profile=c.brief_profile,
                    personality_sketch=list(c.personality_sketch),
                    relationship_snippet=c.relationship_snippet,
                )
                for c in draft.companions
            ],
        )


class GenerateCompanionsRequest(BaseModel):
    """Body for ``POST /characters/{id}/companions/generate``.

    Both fields optional. ``hint`` lets the operator nudge the LLM
    ("再多生兩個她在公司的同事"); ``count`` caps how many sketches to
    return per call (the LLM adapter clamps the upper bound)."""

    hint: str | None = None
    count: int = 3


class GenerateCompanionsResponse(BaseModel):
    """Wire format for the companion generator endpoint.

    Suggestions come back with ``id=null`` —— they're unsaved drafts;
    the operator is expected to accept / edit and then PATCH the
    character with the full companion list (or merge with existing on
    the frontend). The backend mints real ids at save time, not here,
    so a "regenerate" doesn't burn ids for sketches that get tossed."""

    suggestions: list[CharacterCompanionPayload] = Field(default_factory=list)
