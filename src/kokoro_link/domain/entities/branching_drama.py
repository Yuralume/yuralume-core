"""Branching drama (分歧劇場) domain entities.

A ``BranchingDrama`` is a pre-generated branching story tree where each
segment offers three tonal variants (dark / sunny / neutral). Players
navigate by acting in-character — the LLM classifies their input to
pick the matching branch.

Generation strategy:
- Segment outlines: first 2 layers pre-generated at creation time;
  deeper layers generated lazily as the player approaches.
- Images: first 2 layers pre-generated; deeper layers generated lazily
  as the player approaches.

Entities:
- ``BranchingDrama`` — top-level metadata + generation status.
- ``DramaNode``      — one segment in the branching tree.
- ``DramaSession``   — a player's playthrough state.
- ``DramaSessionTurn`` — one completed step within a playthrough.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from uuid import uuid4

from kokoro_link.domain.value_objects.visual_generation_style import (
    VISUAL_GENERATION_STYLE_VALUES,
    normalise_character_visual_generation_style,
)


# ── tone constants ────────────────────────────────────────────────────

TONE_DARK = "dark"
TONE_SUNNY = "sunny"
TONE_NEUTRAL = "neutral"
VALID_TONES = frozenset({TONE_DARK, TONE_SUNNY, TONE_NEUTRAL})

# Every non-root node offers this many tonal children (dark / sunny /
# neutral) — the branching factor of the tree. Named separately from
# ``VALID_TONES`` so call sites doing node-count arithmetic (full tree,
# initial-prefetch target) read as "branch factor" rather than re-deriving
# it from a set meant for tone validation.
BRANCH_FACTOR = len(VALID_TONES)

# ── drama status ──────────────────────────────────────────────────────

STATUS_GENERATING_OUTLINES = "generating_outlines"
STATUS_GENERATING_IMAGES = "generating_images"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

_VALID_DRAMA_STATUSES = frozenset({
    STATUS_GENERATING_OUTLINES,
    STATUS_GENERATING_IMAGES,
    STATUS_READY,
    STATUS_FAILED,
})
_TERMINAL_DRAMA_STATUSES = frozenset({STATUS_READY, STATUS_FAILED})

# ── session status ────────────────────────────────────────────────────

SESSION_PLAYING = "playing"
SESSION_ENDED = "ended"

_VALID_SESSION_STATUSES = frozenset({SESSION_PLAYING, SESSION_ENDED})

# ── operator position (BD2) ───────────────────────────────────────────
#
# Where the *player* stands inside this drama. Until this slot existed
# every drama was written as if the player were the protagonist, because
# nothing in the prompt said otherwise — a player who wanted to watch two
# characters play out a scene had no way to say so, and the narration
# addressed 「你」 anyway.
#
# Deliberately a **closed** vocabulary, same reasoning as
# ``StoryArcBeat.operator_position``: the three prompt stages render a
# different framing per value, so an off-vocabulary string would silently
# take a framing nobody authored.
#
# Deliberately **not** the same slot as the arc beat's: that one carries a
# fourth *unjudged* state (``None``) because a planner may decline to
# judge. Here the player answers the question directly on the create form,
# so there is always an answer and ``central`` is the default.
OPERATOR_POSITION_CENTRAL = "central"
"""主演 — the drama is about the player; narration addresses 「你」."""
OPERATOR_POSITION_PRESENT = "present"
"""在場配角 — the player is in the scene but it is not about them."""
OPERATOR_POSITION_ABSENT = "absent"
"""旁觀者 — the player watches the characters play; nobody speaks to them
and their input reads as a director's note, not a line in the scene."""

VALID_OPERATOR_POSITIONS = frozenset(
    {
        OPERATOR_POSITION_CENTRAL,
        OPERATOR_POSITION_PRESENT,
        OPERATOR_POSITION_ABSENT,
    },
)

DEFAULT_OPERATOR_POSITION = OPERATOR_POSITION_CENTRAL
"""What a client that never heard of this slot gets — and what rows
written before the column existed read back as. Keeps every pre-BD2
drama playing exactly as it did."""

OPERATOR_NOTE_MAX_CHARS = 120
"""Ceiling on the free-text note, aligned with the arc-side ceiling
(``llm_arc_planner._MAX_OPERATOR_NOTE_CHARS`` /
``arc_template_intake_service._MAX_OPERATOR_NOTE_CHARS``). It is a
staging line inside a prompt, not a document."""


def normalise_operator_position(value: object) -> str:
    """Canonical operator position, or raise.

    ``None`` / blank → :data:`DEFAULT_OPERATOR_POSITION`; a known label in
    any casing → the canonical lowercase constant; anything else raises
    ``ValueError``. Strict on purpose: the prompt stages branch on this
    value, so a typo must fail loudly at the write boundary rather than
    quietly framing the drama as something the player did not pick.
    """
    if value is None:
        return DEFAULT_OPERATOR_POSITION
    if not isinstance(value, str):
        raise ValueError(
            "operator_position must be a string or None, got "
            f"{type(value).__name__}",
        )
    cleaned = value.strip().lower()
    if not cleaned:
        return DEFAULT_OPERATOR_POSITION
    if cleaned not in VALID_OPERATOR_POSITIONS:
        raise ValueError(
            f"operator_position {value!r} must be one of "
            f"{sorted(VALID_OPERATOR_POSITIONS)}",
        )
    return cleaned


def normalise_operator_note(value: object) -> str | None:
    """Free-text note about who the player plays in this drama.

    Coerce-don't-reject (mirrors ``story_arc.normalise_operator_note``):
    blank and non-string collapse to ``None``, and an over-long note is
    clipped rather than rejected. Prose, not vocabulary — it is read by a
    writer prompt ("我演她失聯多年的哥哥"), never branched on.

    The clip is a *defensive* last line, not the API's answer to a long
    note: the request DTO declares ``max_length=OPERATOR_NOTE_MAX_CHARS``
    and refuses an over-long body with a ``422`` before this is ever
    reached. What survives to here is the non-HTTP writer — a resumed
    pipeline, an import, a test rig — where silently keeping the first
    120 characters beats losing the whole drama over a staging line.
    """
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned[:OPERATOR_NOTE_MAX_CHARS]


# ── visual style (BD10) ───────────────────────────────────────────────
#
# Which look every scene image in *this* drama is drawn in — anime or
# realistic, from the shared
# :data:`VISUAL_GENERATION_STYLE_VALUES` vocabulary.
#
# Before this slot the answer was implicit: the scene prompt styled itself
# off ``characters[0]``, so a cast mixing an anime character with a
# realistic one drew the second one in the first one's look, and merely
# reordering the picker flipped the whole production. A drama is one
# production with one art direction, so the art direction belongs to the
# drama.
#
# ``None`` is a real state and means "written before this column existed".
# It is *not* a third style: the read path falls back to the old
# ``characters[0]`` resolution so every pre-BD10 drama keeps drawing
# exactly as it did. New dramas never store ``None`` — the create path
# resolves the implicit answer and writes it down (see
# ``BranchingDramaService.create``), which is the whole point of the slot.


def normalise_drama_visual_style(value: object) -> str | None:
    """Canonical drama-level visual style, ``None`` for "not stated", or raise.

    ``None`` / blank → ``None``; a known style in any casing → the
    canonical lowercase id; anything else raises ``ValueError``.

    Strict like :func:`normalise_operator_position` and for the same
    reason: the value selects one of two authored prompt blocks, so a typo
    must fail at the write boundary rather than quietly drawing the drama
    in a look nobody picked. Blank collapsing to ``None`` rather than to
    the product default is deliberate — "the client said nothing" and "the
    client said anime" are different facts, and only the first may inherit
    the legacy per-character resolution.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(
            "visual_style must be a string or None, got "
            f"{type(value).__name__}",
        )
    cleaned = value.strip().lower()
    if not cleaned:
        return None
    if cleaned not in VISUAL_GENERATION_STYLE_VALUES:
        raise ValueError(
            f"visual_style {value!r} must be one of "
            f"{sorted(VISUAL_GENERATION_STYLE_VALUES)}",
        )
    return cleaned


def read_stored_visual_style(value: object) -> str | None:
    """Lenient reader for a stored ``visual_style`` column.

    The write boundary is strict; the *read* boundary must not be. This
    constructor doubles as the row-mapping path for every drama in the
    list, so an unexpected value — a hand-edited row, a style id retired
    by a later release — must degrade to "not stated" and take the legacy
    resolution, not 500 the drama list and the delete endpoint that is the
    only way to get rid of the row (the same lesson as ``total_segments``
    in :meth:`BranchingDrama.__post_init__`).
    """
    return normalise_character_visual_generation_style(value) or None


# ── defaults ──────────────────────────────────────────────────────────

DEFAULT_TOTAL_SEGMENTS = 6
SEGMENTS_WARNING_THRESHOLD = 9
MAX_TOTAL_SEGMENTS = 12
"""Hard ceiling on tree depth (BD4). ``expected_node_count()`` grows as
``(3**N - 1) / 2`` — at the cap that is already ~265k nodes, and one more
segment would nearly triple it. This bounds worst-case generation fan-out
and storage rather than reflecting any narrative limit.

**Enforced on the create path only** (:meth:`BranchingDrama.create_pending`
and the request DTO's ``le=``), never when reading a stored row — see
:meth:`BranchingDrama.__post_init__`."""
MIN_TOTAL_SEGMENTS = 2
"""Floor on tree depth, enforced everywhere including reads. Unlike the
ceiling this is not a policy number: ``total_segments - 1`` is the index
of the ending layer throughout the service, so a tree shallower than this
has no beat to end on and nothing downstream can render it."""
_MIN_CHARACTERS = 2
_MAX_CHARACTERS = 5
IMAGE_PREFETCH_DEPTH = 2
OUTLINE_PREFETCH_DEPTH = 2


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── DramaNode ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DramaNode:
    """A single segment in the branching tree.

    Root node: ``depth=0``, ``tone=None``, ``parent_node_id=None``.
    Non-root: ``depth>0``, ``tone`` in {dark, sunny, neutral},
    ``parent_node_id`` points to the parent.
    """

    id: str
    drama_id: str
    parent_node_id: str | None
    depth: int
    tone: str | None
    title: str
    summary: str
    appearing_character_ids: tuple[str, ...]
    image_path: str | None = None

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("DramaNode.id must be non-empty")
        if not self.drama_id:
            raise ValueError("DramaNode.drama_id must be non-empty")
        if self.depth < 0:
            raise ValueError("DramaNode.depth must be >= 0")
        if self.tone is not None and self.tone not in VALID_TONES:
            raise ValueError(
                f"DramaNode.tone {self.tone!r} not in {sorted(VALID_TONES)}",
            )
        if self.depth == 0 and self.tone is not None:
            raise ValueError("Root node (depth=0) must have tone=None")
        if self.depth > 0 and self.tone is None:
            raise ValueError("Non-root node must have a tone")
        if self.depth == 0 and self.parent_node_id is not None:
            raise ValueError("Root node must have parent_node_id=None")
        if self.depth > 0 and not self.parent_node_id:
            raise ValueError("Non-root node must have a parent_node_id")

    @classmethod
    def create_root(
        cls,
        *,
        drama_id: str,
        title: str,
        summary: str,
        appearing_character_ids: tuple[str, ...],
        id: str | None = None,
    ) -> DramaNode:
        return cls(
            id=id or uuid4().hex,
            drama_id=drama_id,
            parent_node_id=None,
            depth=0,
            tone=None,
            title=title,
            summary=summary,
            appearing_character_ids=appearing_character_ids,
        )

    @classmethod
    def create_child(
        cls,
        *,
        drama_id: str,
        parent_node_id: str,
        depth: int,
        tone: str,
        title: str,
        summary: str,
        appearing_character_ids: tuple[str, ...],
        id: str | None = None,
    ) -> DramaNode:
        return cls(
            id=id or uuid4().hex,
            drama_id=drama_id,
            parent_node_id=parent_node_id,
            depth=depth,
            tone=tone,
            title=title,
            summary=summary,
            appearing_character_ids=appearing_character_ids,
        )

    def with_image_path(self, path: str) -> DramaNode:
        return replace(self, image_path=path)

    @property
    def is_root(self) -> bool:
        return self.depth == 0


# ── Exchange ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Exchange:
    """A single player-input / scene-response pair within a beat."""

    player_input: str
    response: str


# ── DramaSessionTurn ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DramaSessionTurn:
    """One completed step: arriving at a node + seeing its narration.

    ``player_input`` and ``chosen_tone`` are empty for the first turn
    (the opening scene plays without player input).

    ``exchanges`` holds multi-round interactions that happened at this
    node before the player chose to advance to the next beat.
    """

    node_id: str
    narration: str
    player_input: str
    chosen_tone: str | None
    exchanges: tuple[Exchange, ...] = ()

    def __post_init__(self) -> None:
        if not self.node_id:
            raise ValueError("DramaSessionTurn.node_id must be non-empty")
        if self.chosen_tone is not None and self.chosen_tone not in VALID_TONES:
            raise ValueError(
                f"chosen_tone {self.chosen_tone!r} not in {sorted(VALID_TONES)}",
            )


# ── DramaSession ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DramaSession:
    """A player's playthrough of a branching drama."""

    id: str
    drama_id: str
    current_node_id: str
    status: str
    turns: tuple[DramaSessionTurn, ...]
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("DramaSession.id must be non-empty")
        if not self.drama_id:
            raise ValueError("DramaSession.drama_id must be non-empty")
        if not self.current_node_id:
            raise ValueError("DramaSession.current_node_id must be non-empty")
        if self.status not in _VALID_SESSION_STATUSES:
            raise ValueError(
                f"DramaSession.status {self.status!r} not valid",
            )

    @classmethod
    def start(
        cls,
        *,
        drama_id: str,
        root_node_id: str,
        id: str | None = None,
    ) -> DramaSession:
        now = _utcnow()
        return cls(
            id=id or uuid4().hex,
            drama_id=drama_id,
            current_node_id=root_node_id,
            status=SESSION_PLAYING,
            turns=(),
            created_at=now,
            updated_at=now,
        )

    def with_turn(
        self,
        *,
        node_id: str,
        narration: str,
        player_input: str = "",
        chosen_tone: str | None = None,
    ) -> DramaSession:
        turn = DramaSessionTurn(
            node_id=node_id,
            narration=narration,
            player_input=player_input,
            chosen_tone=chosen_tone,
        )
        return replace(
            self,
            current_node_id=node_id,
            turns=(*self.turns, turn),
            updated_at=_utcnow(),
        )

    def with_exchange(
        self,
        *,
        player_input: str,
        response: str,
    ) -> DramaSession:
        """Append an exchange to the current (last) turn."""
        if not self.turns:
            raise ValueError("no turns to add exchange to")
        last = self.turns[-1]
        exchange = Exchange(player_input=player_input, response=response)
        updated = replace(last, exchanges=(*last.exchanges, exchange))
        return replace(
            self,
            turns=(*self.turns[:-1], updated),
            updated_at=_utcnow(),
        )

    def end(self) -> DramaSession:
        return replace(
            self,
            status=SESSION_ENDED,
            updated_at=_utcnow(),
        )

    @property
    def is_ended(self) -> bool:
        return self.status == SESSION_ENDED


# ── BranchingDrama ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BranchingDrama:
    """Top-level entity for a branching drama.

    ``character_ids`` is set at creation and immutable — changing the
    cast mid-tree would invalidate every outline that references them.
    """

    id: str
    character_ids: tuple[str, ...]
    prompt: str
    title: str
    total_segments: int
    status: str
    error_message: str | None = None
    error_code: str | None = None
    """Machine-readable reason for a ``failed`` status, when there is one.

    Set only for deliberate cloud-gateway refusals (``insufficient_credits``
    and friends) so the polling client can offer a top-up affordance instead
    of the generic failure copy. ``None`` for ordinary crashes."""
    operator_position: str = DEFAULT_OPERATOR_POSITION
    """Where the player stands in this drama (BD2) — one of
    :data:`VALID_OPERATOR_POSITIONS`. Set at creation and immutable for
    the same reason ``character_ids`` is: the whole tree is planned
    against this framing."""
    operator_note: str | None = None
    """Optional free-text 「你在劇中演誰／與角色的關係」. In-story staging
    only — it never touches the memory pool or the player persona note
    (BD's persona-only red line)."""
    visual_style: str | None = None
    """The look every scene image in this drama is drawn in (BD10) — one of
    :data:`VISUAL_GENERATION_STYLE_VALUES`, or ``None`` for a row written
    before the slot existed. Set at creation and immutable for the same
    reason ``character_ids`` is: half a tree drawn as anime and half as
    live-action is not a drama, it is two."""
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("BranchingDrama.id must be non-empty")
        if self.operator_position not in VALID_OPERATOR_POSITIONS:
            raise ValueError(
                f"BranchingDrama.operator_position "
                f"{self.operator_position!r} must be one of "
                f"{sorted(VALID_OPERATOR_POSITIONS)}",
            )
        if (
            self.visual_style is not None
            and self.visual_style not in VISUAL_GENERATION_STYLE_VALUES
        ):
            raise ValueError(
                f"BranchingDrama.visual_style {self.visual_style!r} must be "
                f"one of {sorted(VISUAL_GENERATION_STYLE_VALUES)} or None",
            )
        if not self.character_ids:
            raise ValueError(
                "BranchingDrama.character_ids must contain at least one id",
            )
        if not self.prompt.strip():
            raise ValueError("BranchingDrama.prompt must be non-empty")
        # Deliberately *not* checking ``> MAX_TOTAL_SEGMENTS`` here (FX3).
        # The ceiling is a policy on what may be *created*, and policy
        # numbers move: the create form allowed 15 before BD4 lowered it to
        # 12, so rows above today's cap exist. This constructor is also the
        # read path — every repository mapper builds the entity from a row —
        # and raising here would not stop those dramas, it would take the
        # whole列表 down with them: ``list_recent`` maps rows before any
        # caller can filter, so one legacy row 500s the drama list *and* the
        # delete endpoint that is the only way to get rid of it. Existing
        # over-cap dramas therefore list, play and delete exactly as before;
        # ``create_pending`` and the request DTO are what keep new ones from
        # appearing.
        if self.total_segments < MIN_TOTAL_SEGMENTS:
            raise ValueError(
                f"BranchingDrama.total_segments must be at least "
                f"{MIN_TOTAL_SEGMENTS}",
            )
        if self.status not in _VALID_DRAMA_STATUSES:
            raise ValueError(
                f"BranchingDrama.status {self.status!r} must be one of "
                f"{sorted(_VALID_DRAMA_STATUSES)}",
            )

    @classmethod
    def create_pending(
        cls,
        *,
        character_ids: Iterable[str],
        prompt: str,
        total_segments: int = DEFAULT_TOTAL_SEGMENTS,
        operator_position: str | None = None,
        operator_note: str | None = None,
        visual_style: str | None = None,
        id: str | None = None,
    ) -> BranchingDrama:
        seen: set[str] = set()
        deduped: list[str] = []
        for cid in character_ids:
            cleaned = (cid if isinstance(cid, str) else "").strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            deduped.append(cleaned)
        if not deduped:
            raise ValueError(
                "BranchingDrama.create_pending requires at least one character_id",
            )
        cleaned_prompt = prompt.strip()
        if not cleaned_prompt:
            raise ValueError("BranchingDrama.prompt must be non-empty")
        # The ceiling lives here rather than in ``__post_init__``: this is
        # the only path that mints a *new* drama, so it is the only place
        # where refusing costs nobody anything already stored (FX3).
        if (
            total_segments < MIN_TOTAL_SEGMENTS
            or total_segments > MAX_TOTAL_SEGMENTS
        ):
            raise ValueError(
                f"total_segments must be between {MIN_TOTAL_SEGMENTS} and "
                f"{MAX_TOTAL_SEGMENTS}",
            )
        now = _utcnow()
        return cls(
            id=id or uuid4().hex,
            character_ids=tuple(deduped),
            prompt=cleaned_prompt,
            title="(generating…)",
            total_segments=total_segments,
            status=STATUS_GENERATING_OUTLINES,
            error_message=None,
            operator_position=normalise_operator_position(operator_position),
            operator_note=normalise_operator_note(operator_note),
            visual_style=normalise_drama_visual_style(visual_style),
            created_at=now,
            updated_at=now,
        )

    def with_title(self, title: str) -> BranchingDrama:
        return replace(self, title=title, updated_at=_utcnow())

    def with_status(
        self,
        status: str,
        *,
        error_message: str | None = None,
        error_code: str | None = None,
    ) -> BranchingDrama:
        """Transition status, replacing both failure fields wholesale.

        Omitting them clears them — a drama that reaches ``ready`` must not
        keep advertising the reason an earlier run failed.
        """
        if status not in _VALID_DRAMA_STATUSES:
            raise ValueError(
                f"with_status: {status!r} not in "
                f"{sorted(_VALID_DRAMA_STATUSES)}",
            )
        return replace(
            self,
            status=status,
            error_message=error_message,
            error_code=error_code,
            updated_at=_utcnow(),
        )

    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_DRAMA_STATUSES

    def expected_node_count(self) -> int:
        """Total nodes in the full tree: (3^N - 1) / 2 where N = total_segments."""
        return (BRANCH_FACTOR ** self.total_segments - 1) // (BRANCH_FACTOR - 1)

    def initial_node_target(self) -> int:
        """Node count pre-generated synchronously at creation time.

        Mirrors the layer loop in ``BranchingDramaService._generate_tree``:
        only the root plus each complete ``BRANCH_FACTOR``-wide layer down
        to ``OUTLINE_PREFETCH_DEPTH`` (bounded by the tree's own depth) is
        generated before the drama is handed back — deeper layers are
        lazily generated as the player approaches them. Unlike
        ``expected_node_count()`` (the full-tree total), this is the right
        denominator for an early-generation progress bar: with the default
        6-segment tree the full total is 364 nodes but only 4 (root + 3
        children) are ever produced during the ``generating_outlines``
        phase, so dividing by the full total pins the bar at ~1%.
        """
        initial_depth = min(OUTLINE_PREFETCH_DEPTH, self.total_segments)
        if initial_depth <= 0:
            return 0
        return (BRANCH_FACTOR ** initial_depth - 1) // (BRANCH_FACTOR - 1)
