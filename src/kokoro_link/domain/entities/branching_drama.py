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


# ── tone constants ────────────────────────────────────────────────────

TONE_DARK = "dark"
TONE_SUNNY = "sunny"
TONE_NEUTRAL = "neutral"
VALID_TONES = frozenset({TONE_DARK, TONE_SUNNY, TONE_NEUTRAL})

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

# ── defaults ──────────────────────────────────────────────────────────

DEFAULT_TOTAL_SEGMENTS = 6
SEGMENTS_WARNING_THRESHOLD = 9
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
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("BranchingDrama.id must be non-empty")
        if not self.character_ids:
            raise ValueError(
                "BranchingDrama.character_ids must contain at least one id",
            )
        if not self.prompt.strip():
            raise ValueError("BranchingDrama.prompt must be non-empty")
        if self.total_segments < 2:
            raise ValueError("BranchingDrama.total_segments must be >= 2")
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
        if total_segments < 2:
            raise ValueError("total_segments must be >= 2")
        now = _utcnow()
        return cls(
            id=id or uuid4().hex,
            character_ids=tuple(deduped),
            prompt=cleaned_prompt,
            title="(generating…)",
            total_segments=total_segments,
            status=STATUS_GENERATING_OUTLINES,
            error_message=None,
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
    ) -> BranchingDrama:
        if status not in _VALID_DRAMA_STATUSES:
            raise ValueError(
                f"with_status: {status!r} not in "
                f"{sorted(_VALID_DRAMA_STATUSES)}",
            )
        return replace(
            self,
            status=status,
            error_message=error_message,
            updated_at=_utcnow(),
        )

    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_DRAMA_STATUSES

    def expected_node_count(self) -> int:
        """Total nodes in the full tree: (3^N - 1) / 2 where N = total_segments."""
        return (3 ** self.total_segments - 1) // 2
