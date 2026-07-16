"""Memoir view-model entities (player-facing recollection page).

The memoir page is the player's view onto what the character has
remembered. It aggregates three already-existing sources — high-salience
``MemoryItem`` rows (including ``relationship_milestone`` anchors),
``SelfReflection`` (HUMANIZATION_ROADMAP §3.2) chapters, and high-
intensity ``EmotionEvent`` rows — into a single read-only timeline plus
chapter pair.

The memoir does not spawn a new LLM job to decide or author memoir
content: chapter narratives come from the latest week / month
``SelfReflection`` snapshots; timeline entries are pure structural
projections of the underlying rows. A read-side localizer may translate
already-selected player-visible text for the current operator language,
but it must preserve ids, scores, pin state, and source boundaries.
Players can pin entries to influence ordering but cannot edit or delete
(the character's memory belongs to the character, not the player).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Final, Mapping

ENTRY_MEMORY: Final = "memory"
"""Timeline entry projected from a ``MemoryItem`` row."""

ENTRY_EMOTION: Final = "emotion"
"""Timeline entry projected from an ``EmotionEvent`` row."""

ENTRY_MILESTONE: Final = "milestone"
"""Timeline entry projected from a ``MemoryItem`` whose
``kind == MemoryKind.RELATIONSHIP_MILESTONE``. Rendered with a distinct
badge in the UI so anchor events don't get lost among regular memories.
"""

ENTRY_KINDS: Final = frozenset({ENTRY_MEMORY, ENTRY_EMOTION, ENTRY_MILESTONE})


@dataclass(frozen=True, slots=True)
class MemoirEntry:
    """One row on the memoir timeline.

    Shape is deliberately flat and identical across the three source
    kinds — the UI distinguishes purely on ``kind`` + ``extras`` and never
    needs to know which backing table produced the row.
    """

    kind: str
    """One of :data:`ENTRY_MEMORY`, :data:`ENTRY_EMOTION`,
    :data:`ENTRY_MILESTONE`."""
    entry_id: str
    """The source row's ``id`` (MemoryItem.id or EmotionEvent.id).
    Combined with ``kind`` it forms the unique pin key."""
    occurred_at: datetime
    """When the underlying event happened (``created_at`` of the source
    row). Sort key for the chronological view."""
    summary: str
    """Display text: memory content or emotion label. The service
    truncates / strips before assembling; the entity stores the final
    form."""
    score: float
    """``salience`` for memory/milestone entries, ``intensity`` for
    emotion entries. Both share the [0, 1] range so the UI can render
    a single visual scale."""
    pinned: bool = False
    """Whether this entry is pinned by the *current* operator. The
    service hydrates this per-request — entries are not pinned in the
    abstract, only relative to one (character, operator) pair."""
    extras: Mapping[str, str] = field(default_factory=dict)
    """Kind-specific metadata. Examples:

    * memory / milestone: ``{"memory_kind": "episodic"}``,
      ``{"tags": "travel,coffee"}``
    * emotion: ``{"emotion_label": "被理解了", "valence": "0.62",
      "cause_ref_kind": "turn"}``

    Always strings so the UI / DTO layer never has to guess types.
    """

    def __post_init__(self) -> None:
        if self.kind not in ENTRY_KINDS:
            raise ValueError(
                f"MemoirEntry.kind must be one of {sorted(ENTRY_KINDS)}, "
                f"got {self.kind!r}",
            )
        if not self.entry_id.strip():
            raise ValueError("MemoirEntry.entry_id must be non-empty")
        if not self.summary.strip():
            raise ValueError("MemoirEntry.summary must be non-empty")


@dataclass(frozen=True, slots=True)
class MemoirChapter:
    """A self-reflection narrative rendered as a memoir chapter.

    Mirrors :class:`SelfReflection` one-to-one — we keep a separate type
    so the memoir view stays decoupled from the reflection module's
    internals (e.g. if future history rollup changes the source shape).
    """

    period: str
    """``"week"`` or ``"month"`` — propagated from the underlying
    ``SelfReflection.period``."""
    period_start: date
    period_end: date
    narrative: str
    """The LLM-written first-person passage. Memoir content selection
    surfaces this verbatim; do **not** template-format chapter content
    here. Read-side localization, if applied, must be a faithful
    language projection rather than new memoir writing."""
    dominant_themes: tuple[str, ...] = field(default_factory=tuple)
    evidence_quotes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.period not in {"week", "month"}:
            raise ValueError(
                f"MemoirChapter.period must be 'week' or 'month', "
                f"got {self.period!r}",
            )
        if not self.narrative.strip():
            raise ValueError("MemoirChapter.narrative must be non-empty")


@dataclass(frozen=True, slots=True)
class MemoirView:
    """Aggregated read-only view returned by ``MemoirService.build_view``.

    ``chapters`` carries at most two entries (the latest week + the
    latest month) since :class:`SelfReflectionRepositoryPort` keeps only
    the most recent snapshot per period. ``timeline`` is already sorted
    (pinned first, then most-recent first) so the UI can render it
    straight.
    """

    chapters: tuple[MemoirChapter, ...]
    timeline: tuple[MemoirEntry, ...]
    pin_count: int
    """Current pin count for this (character, operator) pair."""
    pin_limit: int
    """Hard limit from ``MemoirSettings.pin_max_per_pair``. The UI shows
    ``pin_count / pin_limit`` so the player knows when they're close to
    the ceiling before the API returns 409."""
