"""DTOs for the branching-drama REST API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from kokoro_link.application.services.branching_drama_gallery import (
    CollectedScene,
    DramaSceneGallery,
)
from kokoro_link.contracts.fusion_to_arc import (
    VALID_FUSION_OPERATOR_MODES,
    normalise_fusion_operator_mode,
)
from kokoro_link.domain.entities.branching_drama import (
    BranchingDrama,
    DEFAULT_OPERATOR_POSITION,
    DEFAULT_TOTAL_SEGMENTS,
    DramaNode,
    DramaSession,
    DramaSessionTurn,
    Exchange,
    MAX_TOTAL_SEGMENTS,
    MIN_TOTAL_SEGMENTS,
    OPERATOR_NOTE_MAX_CHARS,
    SEGMENTS_WARNING_THRESHOLD,
    _MAX_CHARACTERS,
    _MIN_CHARACTERS,
    normalise_drama_visual_style,
    normalise_operator_position,
)


# ── requests ──────────────────────────────────────────────────────────


class CreateBranchingDramaRequest(BaseModel):
    character_ids: list[str] = Field(
        ..., min_length=_MIN_CHARACTERS, max_length=_MAX_CHARACTERS,
    )
    prompt: str = Field(..., min_length=1, max_length=2000)
    total_segments: int = Field(
        default=DEFAULT_TOTAL_SEGMENTS,
        ge=MIN_TOTAL_SEGMENTS,
        le=MAX_TOTAL_SEGMENTS,
    )
    """Bounded here *and* in ``create_pending`` — the create path is where
    the BD4 ceiling is enforced, because the entity constructor doubles as
    the row-reading path and must stay tolerant of dramas created under an
    older, higher cap (FX3)."""
    operator_position: str = Field(default=DEFAULT_OPERATOR_POSITION)
    """Where the player stands in the drama (BD2). Defaulted rather than
    required so a client that predates the slot creates exactly the drama
    it always created."""
    operator_note: str | None = Field(
        default=None, max_length=OPERATOR_NOTE_MAX_CHARS,
    )
    visual_style: str | None = Field(default=None, max_length=32)
    """Which look this drama's scene images are drawn in (BD10).

    Optional rather than required, and ``None`` is *not* "anime": it means
    the client did not state a look — the fusion 「換個玩法」 handoff, a
    pre-BD10 client — and the service answers it by resolving the first
    character's style at create time and **storing that**. So the implicit
    answer becomes an explicit one on the way in, and every drama made
    from here on has a look written on it."""
    quoted_price_cr: float | None = None
    """The fixed price this client had on screen when the player pressed
    create (BD3). The charge binds to it, so a back-office edit mid-session
    refuses with ``409 price_changed`` instead of billing a number nobody was
    shown. ``None`` = nothing quotable (self-host, a token-billed tier, a
    price list that never loaded) and the server falls back to its own cache."""

    @field_validator("operator_position")
    @classmethod
    def _check_position(cls, value: str) -> str:
        """Reject an off-vocabulary position at the edge (422).

        Reuses the domain normaliser so casing is canonicalised and the
        legal set cannot drift from the entity's.
        """
        return normalise_operator_position(value)

    @field_validator("visual_style")
    @classmethod
    def _check_visual_style(cls, value: str | None) -> str | None:
        """Reject an off-vocabulary look at the edge (422).

        Same domain normaliser the entity uses, so casing is canonicalised
        and blank collapses to ``None`` (= "not stated") rather than to a
        style nobody picked.
        """
        return normalise_drama_visual_style(value)


class InteractSessionRequest(BaseModel):
    player_input: str = Field(..., min_length=1, max_length=4000)
    quoted_price_cr: float | None = None
    """Same binding as create — the price on the player's screen (BD3)."""


class AdvanceSessionRequest(BaseModel):
    """Optional body on ``/advance`` — it exists only to carry the quote.

    Advance took no body before BD3 and still accepts none: a client that
    posts nothing advances exactly as it always did, and the server quotes
    from its own price cache. Hosted clients post the number they displayed
    so the charge binds to it.
    """

    quoted_price_cr: float | None = None


class StartSessionRequest(BaseModel):
    """Optional body on ``/sessions`` — it exists only to carry the quote.

    Starting a playthrough took no body before FX1 and still accepts none.
    The opening is charged as one ``branching_drama_advance`` (it *is* the
    zeroth press), so a hosted client posts the advance price it displayed
    and the charge binds to that number; anything else posts nothing and the
    server quotes from its own cache.
    """

    quoted_price_cr: float | None = None


class RegenerateSceneImageRequest(BaseModel):
    """Optional body on the manual scene redraw — it carries the quote only.

    Same shape and same reason as :class:`AdvanceSessionRequest`: a client
    with nothing quotable (self-host, a token-billed tier, a price list that
    never loaded) posts nothing and the server falls back to its own cache.
    """

    quoted_price_cr: float | None = None


class DramaToArcDraftRequest(BaseModel):
    """Optional body on 「把這條路寫成劇本」 (BD7).

    Both fields are optional in both directions. An omitted body is a
    player who pressed the button without touching the mode chips, and
    the service answers that with the framing the drama was actually
    played under (``operator_position``) rather than a blanket default —
    unlike the fusion path, this source knows where the player stood.
    An off-vocabulary mode is a 422, never a best-guess branch.
    """

    instruction: str | None = Field(default=None, max_length=2000)
    operator_mode: str | None = Field(default=None, max_length=32)

    @field_validator("operator_mode")
    @classmethod
    def _check_operator_mode(cls, value: str | None) -> str | None:
        # Blank collapses to "unanswered" rather than to the fusion
        # protocol default: on this path an unanswered mode has a better
        # answer waiting (the drama's own position), and letting an empty
        # string mean ``unchanged`` would silently overrule it.
        if value is None or not value.strip():
            return None
        try:
            return normalise_fusion_operator_mode(value)
        except ValueError as exc:
            raise ValueError(
                "operator_mode must be one of "
                f"{sorted(VALID_FUSION_OPERATOR_MODES)}",
            ) from exc


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


class CollectedSceneResponse(BaseModel):
    """One picture the player walked past — safe to show whole (BD9)."""

    node_id: str
    title: str
    depth: int
    tone: str | None
    image_path: str

    @classmethod
    def from_domain(cls, scene: CollectedScene) -> CollectedSceneResponse:
        return cls(
            node_id=scene.node_id,
            title=scene.title,
            depth=scene.depth,
            tone=scene.tone,
            image_path=scene.image_path,
        )


class DramaSceneGalleryResponse(BaseModel):
    """劇場圖集 (BD9) — what one player collected out of a painted tree.

    The un-walked pictures are represented by ``locked_count`` alone. That
    is the whole anti-spoiler boundary: this model has no field a title,
    a summary or an image path for a node the player has not reached could
    ride out on, so no future view can leak one by rendering too much.
    """

    collected: list[CollectedSceneResponse]
    locked_count: int
    total_with_images: int
    """Nodes of this drama that currently have a picture — walked or not.

    It grows as lazy generation paints deeper layers, so the same drama
    reports a larger count between visits; that is the tree filling in, not a
    miscount.

    **Not a progress denominator.** It once backed a 「已收集 X/Y」 reading in
    the gallery; that reading is retired, because the prefetch paints branches
    nobody walks into, so this count runs *ahead* of the player instead of
    tracking the walk — the ratio it produced was low and flat and said
    nothing about how much of the drama was left. Completion is measured
    client-side against the whole tree's node count instead. The field's
    meaning and wire format are unchanged."""

    @classmethod
    def from_domain(
        cls, gallery: DramaSceneGallery,
    ) -> DramaSceneGalleryResponse:
        return cls(
            collected=[
                CollectedSceneResponse.from_domain(scene)
                for scene in gallery.collected
            ],
            locked_count=gallery.locked_count,
            total_with_images=gallery.total_with_images,
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
    error_code: str | None = None
    """Machine-readable failure reason for a ``failed`` drama (U4b).

    Creation is 202 + poll, so this is the only channel by which a
    player-actionable refusal (``insufficient_credits``) reaches the
    client. ``None`` for ordinary crashes."""
    operator_position: str = DEFAULT_OPERATOR_POSITION
    """Echoed back so the detail view can show the framing the tree was
    planned against, and so BD7's adapt flow can pre-fill it."""
    operator_note: str | None = None
    visual_style: str | None = None
    """The look this drama's scene images are drawn in (BD10), echoed so the
    detail view can show it. ``None`` only for a drama created before the
    slot existed — those still draw off their first character, and saying
    ``anime`` here would name a look that is not what they render."""
    expected_node_count: int
    initial_node_target: int
    """Nodes pre-generated synchronously at creation (root + prefetch
    layers) — the correct denominator for an early progress bar. See
    ``BranchingDrama.initial_node_target()``. ``expected_node_count`` above
    is the full-tree total and stays for the segment-count warning copy."""
    generated_node_count: int = 0
    first_scene_image_path: str | None = None
    first_scene_node_id: str | None = None
    """Root node id, so the detail view's title image can name the node it
    wants redrawn (BD6). ``None`` only while the root does not exist yet —
    the picture may legitimately be missing while the node is not, which is
    exactly the case the redraw button exists to repair."""
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
        first_scene_node_id: str | None = None,
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
            error_code=drama.error_code,
            operator_position=drama.operator_position,
            operator_note=drama.operator_note,
            visual_style=drama.visual_style,
            expected_node_count=drama.expected_node_count(),
            initial_node_target=drama.initial_node_target(),
            generated_node_count=generated_node_count,
            first_scene_image_path=first_scene_image_path,
            first_scene_node_id=first_scene_node_id,
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
    error_code: str | None = None
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
            error_code=drama.error_code,
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
