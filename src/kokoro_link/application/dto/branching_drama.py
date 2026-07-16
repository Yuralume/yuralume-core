"""DTOs for the branching-drama REST API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from kokoro_link.domain.entities.branching_drama import (
    BranchingDrama,
    DEFAULT_TOTAL_SEGMENTS,
    DramaNode,
    DramaSession,
    DramaSessionTurn,
    Exchange,
    SEGMENTS_WARNING_THRESHOLD,
    _MAX_CHARACTERS,
    _MIN_CHARACTERS,
)


# ── requests ──────────────────────────────────────────────────────────


class CreateBranchingDramaRequest(BaseModel):
    character_ids: list[str] = Field(
        ..., min_length=_MIN_CHARACTERS, max_length=_MAX_CHARACTERS,
    )
    prompt: str = Field(..., min_length=1, max_length=2000)
    total_segments: int = Field(default=DEFAULT_TOTAL_SEGMENTS, ge=2)


class InteractSessionRequest(BaseModel):
    player_input: str = Field(..., min_length=1, max_length=4000)


# ── responses ─────────────────────────────────────────────────────────


class DramaNodeResponse(BaseModel):
    id: str
    drama_id: str
    parent_node_id: str | None
    depth: int
    tone: str | None
    title: str
    summary: str
    appearing_character_ids: list[str]
    image_path: str | None

    @classmethod
    def from_domain(cls, node: DramaNode) -> DramaNodeResponse:
        return cls(
            id=node.id,
            drama_id=node.drama_id,
            parent_node_id=node.parent_node_id,
            depth=node.depth,
            tone=node.tone,
            title=node.title,
            summary=node.summary,
            appearing_character_ids=list(node.appearing_character_ids),
            image_path=node.image_path,
        )


class ExchangeResponse(BaseModel):
    player_input: str
    response: str

    @classmethod
    def from_domain(cls, ex: Exchange) -> ExchangeResponse:
        return cls(player_input=ex.player_input, response=ex.response)


class DramaSessionTurnResponse(BaseModel):
    node_id: str
    narration: str
    player_input: str
    chosen_tone: str | None
    exchanges: list[ExchangeResponse] = []

    @classmethod
    def from_domain(
        cls, turn: DramaSessionTurn,
    ) -> DramaSessionTurnResponse:
        return cls(
            node_id=turn.node_id,
            narration=turn.narration,
            player_input=turn.player_input,
            chosen_tone=turn.chosen_tone,
            exchanges=[
                ExchangeResponse.from_domain(ex) for ex in turn.exchanges
            ],
        )


class DramaSessionResponse(BaseModel):
    id: str
    drama_id: str
    current_node_id: str
    status: str
    turns: list[DramaSessionTurnResponse]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, session: DramaSession) -> DramaSessionResponse:
        return cls(
            id=session.id,
            drama_id=session.drama_id,
            current_node_id=session.current_node_id,
            status=session.status,
            turns=[
                DramaSessionTurnResponse.from_domain(t)
                for t in session.turns
            ],
            created_at=session.created_at,
            updated_at=session.updated_at,
        )


class BranchingDramaResponse(BaseModel):
    id: str
    character_ids: list[str]
    prompt: str
    title: str
    total_segments: int
    status: str
    error_message: str | None = None
    expected_node_count: int
    generated_node_count: int = 0
    first_scene_image_path: str | None = None
    warning: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(
        cls,
        drama: BranchingDrama,
        *,
        generated_node_count: int = 0,
        first_scene_image_path: str | None = None,
    ) -> BranchingDramaResponse:
        warning = None
        if drama.total_segments >= SEGMENTS_WARNING_THRESHOLD:
            count = drama.expected_node_count()
            warning = (
                f"段落數 {drama.total_segments} 將產生 {count} 個節點，"
                f"生成時間可能較長。"
            )
        return cls(
            id=drama.id,
            character_ids=list(drama.character_ids),
            prompt=drama.prompt,
            title=drama.title,
            total_segments=drama.total_segments,
            status=drama.status,
            error_message=drama.error_message,
            expected_node_count=drama.expected_node_count(),
            generated_node_count=generated_node_count,
            first_scene_image_path=first_scene_image_path,
            warning=warning,
            created_at=drama.created_at,
            updated_at=drama.updated_at,
        )


class BranchingDramaSummaryResponse(BaseModel):
    id: str
    character_ids: list[str]
    title: str
    total_segments: int
    status: str
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(
        cls, drama: BranchingDrama,
    ) -> BranchingDramaSummaryResponse:
        return cls(
            id=drama.id,
            character_ids=list(drama.character_ids),
            title=drama.title,
            total_segments=drama.total_segments,
            status=drama.status,
            error_message=drama.error_message,
            created_at=drama.created_at,
            updated_at=drama.updated_at,
        )


class InteractSessionResponse(BaseModel):
    """Returned after a player interacts within the current beat."""

    session: DramaSessionResponse
    response: str
    advance_hint: str | None = None


class AdvanceSessionResponse(BaseModel):
    """Returned after a player advances to the next beat."""

    session: DramaSessionResponse
    current_node: DramaNodeResponse
    is_ending: bool
