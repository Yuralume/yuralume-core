"""One player-pulled story scene («起幕») from opening to wrap-up.

A scene session is the *state* that makes a stretch of the ordinary chat
thread a scene: while one is ``open`` the character is inside a framed
performance (SC1-C injects its context into every turn, SC1-D/E close it
and land it as canon, and proactive delivery for the character pauses).
The messages themselves stay in the normal conversation — the session is
the frame around them, not a parallel transcript.

Two design points are load-bearing:

* **State lives only in the database.** Under the hosted topology one
  replica opens the scene and another serves the next turn, so a
  process-local "currently in a scene" flag would be invisible to half
  the cluster (2026-07-26 cross-instance sweep). Everything a later turn
  needs — layer, beat, frame, dramatic question — is a column.
* **At most one ``open`` session per character**, enforced by a partial
  unique index rather than a service-layer read-then-write. Two taps of
  「起幕」 landing on two replicas is a plain race, and the second one
  must lose in the schema.

Layer / close-reason vocabularies are closed sets written only by our own
code (never by a model, never by a player), so validating membership is a
domain invariant, not the keyword special-casing the LLM-first rule bans.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from uuid import uuid4

from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.domain.entities.story_arc import (
    normalise_operator_note,
    normalise_operator_position,
)

# --- Session status ---------------------------------------------------
SCENE_OPEN = "open"
"""The player is inside the scene; turns carry scene context."""
SCENE_CLOSED = "closed"
"""Terminal. A closed session never reopens — the next 起幕 mints a new
row, so the history of played scenes stays intact."""

_VALID_STATUSES = frozenset({SCENE_OPEN, SCENE_CLOSED})

# --- Material layer (STORY_SCENE_PLAN §3.1 waterfall) -----------------
SCENE_LAYER_BEAT = "beat"
"""Layer 1 — the active arc had a pending beat and it was played now."""
SCENE_LAYER_NEW_SEASON = "new_season"
"""Layer 2 (SC1-B) — dormant character, a season was force-started."""
SCENE_LAYER_SIDE_STORY = "side_story"
"""Layer 3 (SC1-B) — ad-hoc side story; no arc, no beat, lands as a
plain episodic memory instead of a realized beat."""

_VALID_LAYERS = frozenset(
    {SCENE_LAYER_BEAT, SCENE_LAYER_NEW_SEASON, SCENE_LAYER_SIDE_STORY},
)

# --- Close reasons (SC1-D / SC1-E fill these in) ----------------------
SCENE_CLOSE_RESOLVED = "resolved"
"""The dramatic question was judged answered and the scene wrapped up."""
SCENE_CLOSE_MANUAL = "manual"
"""The player pressed 「結束場景」."""
SCENE_CLOSE_TIMEOUT = "timeout"
"""Idle past the configured window; closed autonomously (§2 #5)."""

_VALID_CLOSE_REASONS = frozenset(
    {SCENE_CLOSE_RESOLVED, SCENE_CLOSE_MANUAL, SCENE_CLOSE_TIMEOUT},
)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


@dataclass(frozen=True, slots=True)
class StorySceneSession:
    """A single 起幕 scene, from the opening beat to its wrap-up."""

    id: str
    character_id: str
    conversation_id: str
    """The conversation the scene is being played inside.

    Pinned at open time rather than re-resolved per turn: 「最新的 web
    對話」 can change under the player's feet, and a scene that drifted
    to another thread mid-performance would strand its own narration."""
    source_layer: str
    status: str = SCENE_OPEN
    arc_id: str | None = None
    beat_id: str | None = None
    """Set for layers 1 and 2, ``None`` for the ad-hoc side story.

    Deliberately *not* a foreign key at the storage layer: the arc
    repository's ``save`` rewrites a whole arc's beat rows, so an FK
    with ``ON DELETE SET NULL`` would silently unlink a live scene from
    its beat on the next unrelated arc edit."""
    title: str = ""
    location: str | None = None
    mood: str | None = None
    """The scene frame the player sees around the thread (§2 #1)."""
    scene_type: str | None = None
    dramatic_question: str | None = None
    """The narrative material later turns need. Copied onto the session
    instead of re-read from the beat so a scene keeps playing coherently
    even if the beat is edited, retired, or realized mid-performance."""
    opened_at: datetime = field(default_factory=_now_utc)
    last_activity_at: datetime | None = None
    """Idle-timeout anchor (SC1-E). Bumped by every in-scene turn.
    Defaults to ``opened_at`` — a scene that has had no turn yet is idle
    from the moment it opened, not from the epoch."""
    closed_at: datetime | None = None
    closed_reason: str | None = None
    # --- Player's place in this scene (OP2-D) -------------------------
    # Copied from the opening ``StorySceneMaterial`` the same way
    # ``scene_type`` / ``dramatic_question`` are: the closer (SC1-D) reads
    # only this session, never the beat, so a scene keeps framing the
    # player coherently even if the beat that spawned it is edited,
    # retired, or realized mid-performance. Kept as the last fields so no
    # positional construction anywhere shifts.
    operator_position: str | None = None
    """One of ``VALID_OPERATOR_POSITIONS`` (see ``story_arc.py``), or
    ``None`` = unjudged. ``None`` for every layer-3 side story on purpose
    — there is no beat to have judged one, and the opener/closer derive a
    framing from the rest of the material instead of a forced value."""
    operator_note: str | None = None
    """Optional prose about how the player figures in this scene, copied
    from the beat that supplied it."""

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("StorySceneSession.id is required")
        if not self.character_id:
            raise ValueError("StorySceneSession.character_id is required")
        if not self.conversation_id:
            raise ValueError(
                "StorySceneSession.conversation_id is required",
            )
        if self.source_layer not in _VALID_LAYERS:
            raise ValueError(
                f"StorySceneSession.source_layer must be one of "
                f"{sorted(_VALID_LAYERS)}, got {self.source_layer!r}",
            )
        if self.status not in _VALID_STATUSES:
            raise ValueError(
                f"StorySceneSession.status must be one of "
                f"{sorted(_VALID_STATUSES)}, got {self.status!r}",
            )
        if (
            self.closed_reason is not None
            and self.closed_reason not in _VALID_CLOSE_REASONS
        ):
            raise ValueError(
                f"StorySceneSession.closed_reason must be one of "
                f"{sorted(_VALID_CLOSE_REASONS)}, got {self.closed_reason!r}",
            )
        if self.status == SCENE_OPEN and self.closed_reason is not None:
            raise ValueError("an open scene session cannot carry a close reason")
        # Timestamps are absolute instants, and every one of them is
        # compared against another (idle window, monotonic activity, close
        # ordering). SQLite hands back naive values for a
        # ``DateTime(timezone=True)`` column where PostgreSQL keeps the
        # offset, so normalising here — rather than in one repository —
        # is what stops self-host and hosted from disagreeing, and stops
        # a naive/aware comparison from raising deep inside a close.
        object.__setattr__(self, "opened_at", ensure_utc(self.opened_at))
        object.__setattr__(
            self,
            "last_activity_at",
            ensure_utc(self.last_activity_at or self.opened_at),
        )
        if self.closed_at is not None:
            object.__setattr__(self, "closed_at", ensure_utc(self.closed_at))
        object.__setattr__(self, "title", (self.title or "").strip())
        object.__setattr__(self, "location", _clean(self.location))
        object.__setattr__(self, "mood", _clean(self.mood))
        object.__setattr__(self, "scene_type", _clean(self.scene_type))
        object.__setattr__(
            self, "dramatic_question", _clean(self.dramatic_question),
        )
        # Same strict-at-the-domain-boundary rule ``StoryArcBeat`` carries:
        # an off-vocabulary position raises rather than degrading, so a
        # bad value cannot enter through a bare constructor either.
        object.__setattr__(
            self,
            "operator_position",
            normalise_operator_position(self.operator_position),
        )
        object.__setattr__(
            self,
            "operator_note",
            normalise_operator_note(self.operator_note),
        )

    @property
    def is_open(self) -> bool:
        return self.status == SCENE_OPEN

    @classmethod
    def open_scene(
        cls,
        *,
        character_id: str,
        conversation_id: str,
        source_layer: str,
        arc_id: str | None = None,
        beat_id: str | None = None,
        title: str = "",
        location: str | None = None,
        mood: str | None = None,
        scene_type: str | None = None,
        dramatic_question: str | None = None,
        operator_position: str | None = None,
        operator_note: str | None = None,
        opened_at: datetime | None = None,
        id: str | None = None,
    ) -> "StorySceneSession":
        moment = opened_at or _now_utc()
        return cls(
            id=id or str(uuid4()),
            character_id=character_id,
            conversation_id=conversation_id,
            source_layer=source_layer,
            status=SCENE_OPEN,
            arc_id=arc_id,
            beat_id=beat_id,
            title=title,
            location=location,
            mood=mood,
            scene_type=scene_type,
            dramatic_question=dramatic_question,
            operator_position=operator_position,
            operator_note=operator_note,
            opened_at=moment,
            last_activity_at=moment,
        )

    def touched(self, at: datetime) -> "StorySceneSession":
        """Record in-scene activity (SC1-C bumps this every turn).

        Monotonic: an out-of-order write from a slower replica must not
        drag the idle clock backwards and make a live scene look stale to
        the timeout closer."""
        moment = ensure_utc(at)
        if moment <= self.last_activity_at:
            return self
        return replace(self, last_activity_at=moment)

    def closed(
        self, *, reason: str, at: datetime | None = None,
    ) -> "StorySceneSession":
        """Terminal transition. Idempotent — closing a closed session
        keeps the ORIGINAL reason and timestamp, so a manual end that
        races the timeout closer cannot rewrite why the scene ended."""
        if self.status == SCENE_CLOSED:
            return self
        moment = ensure_utc(at) if at is not None else _now_utc()
        return replace(
            self,
            status=SCENE_CLOSED,
            closed_at=moment,
            closed_reason=reason,
            last_activity_at=max(self.last_activity_at, moment),
        )
