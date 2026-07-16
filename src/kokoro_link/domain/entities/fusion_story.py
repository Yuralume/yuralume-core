"""Fusion short-story domain entity.

A ``FusionStory`` is a multi-character short story (~2~3k 字) composed
by the fusion pipeline. Unlike ``StoryArc``:

- it is not bound to a single character — ``character_ids`` is an
  ordered tuple of every character whose persona / memory the planner
  pulled from;
- it has no calendar — beats are fictional acts, not scheduled events;
- iteration is **versioned**: each regenerate / polish operation snapshots
  the previous content into a sibling ``FusionStoryVersion`` so operators
  can walk back through earlier drafts.

The entity captures a single "head" version (current text + outline +
beats) plus the history chain. Older versions are stored in
``FusionStoryVersion`` rows that point back to the parent story id.

Status values track the async generation lifecycle so the UI can
poll a known set:

- ``planning``  — outline LLM call running
- ``writing``   — per-beat LLM calls running
- ``polishing`` — final pass running
- ``ready``     — full text available; idle until next iterate call
- ``failed``    — pipeline aborted; ``error_message`` non-null
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from uuid import uuid4

from kokoro_link.domain.value_objects.fusion_outline import (
    FusionBeatPlan,
    FusionOutline,
)


STATUS_PLANNING = "planning"
STATUS_WRITING = "writing"
STATUS_POLISHING = "polishing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

_VALID_STATUSES = frozenset(
    {STATUS_PLANNING, STATUS_WRITING, STATUS_POLISHING, STATUS_READY, STATUS_FAILED},
)
_TERMINAL_STATUSES = frozenset({STATUS_READY, STATUS_FAILED})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class FusionStoryBeat:
    """A single act's expanded text within the current head version.

    Mirrors ``FusionBeatPlan`` (the outline-stage value object) plus the
    actual prose. Kept separate from the value object so the writer stage
    can attach metadata (regenerated_at, model_id) the planner doesn't
    have. Frozen — mutations go through ``replace``.
    """

    id: str
    sequence: int
    act: str
    title: str
    hook: str
    dramatic_question: str
    target_chars: int
    content: str
    """Prose for this beat. Empty string while the writer hasn't reached
    this beat yet (status=writing); populated once the call returns."""
    actual_chars: int = 0
    focus_character_ids: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("FusionStoryBeat.id must be non-empty")
        if self.sequence < 0:
            raise ValueError("FusionStoryBeat.sequence must be >= 0")
        if not self.title.strip():
            raise ValueError("FusionStoryBeat.title must be non-empty")
        if not self.act.strip():
            raise ValueError("FusionStoryBeat.act must be non-empty")

    @classmethod
    def from_plan(
        cls,
        plan: FusionBeatPlan,
        *,
        content: str = "",
        id: str | None = None,
    ) -> "FusionStoryBeat":
        return cls(
            id=id or uuid4().hex,
            sequence=plan.sequence,
            act=plan.act,
            title=plan.title,
            hook=plan.hook,
            dramatic_question=plan.dramatic_question,
            target_chars=plan.target_chars,
            content=content,
            actual_chars=len(content),
            focus_character_ids=plan.focus_character_ids,
        )

    def with_content(self, content: str) -> "FusionStoryBeat":
        return replace(self, content=content, actual_chars=len(content))


@dataclass(frozen=True, slots=True)
class FusionStoryVersion:
    """Frozen snapshot of an earlier head — kept for the version chain.

    Stores the same fields as ``FusionStory`` minus the bookkeeping
    (status / error_message / character_ids — the parent story owns
    those). Replaying / restoring is a service-layer operation; the
    entity just remembers what was there.
    """

    id: str
    story_id: str
    version_number: int
    """1-based; matches the order operators see in the UI history."""
    title: str
    premise: str
    theme: str
    full_text: str
    outline_json: str
    """Serialized snapshot of the outline + beats at this version. Kept
    as JSON text so restoring is a single-shot decode rather than a
    relational join across two more tables."""
    iteration_label: str
    """Free-text breadcrumb describing what produced this version
    (``"initial"`` / ``"polish"`` / ``"beat 2 regenerated"`` / ...)."""
    created_at: datetime = field(default_factory=_utcnow)
    beats_json: str = "[]"
    """Serialized per-beat prose at this version (C0-6 restore
    fidelity). ``"[]"`` on rows snapshotted before the column existed —
    restoring those recovers the full text but not per-beat iteration
    material."""

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("FusionStoryVersion.id must be non-empty")
        if not self.story_id:
            raise ValueError("FusionStoryVersion.story_id must be non-empty")
        if self.version_number < 1:
            raise ValueError(
                "FusionStoryVersion.version_number must be >= 1",
            )


@dataclass(frozen=True, slots=True)
class FusionStory:
    """Top-level entity for a fusion short story.

    ``character_ids`` is the ordered tuple of characters the operator
    selected at creation time. Adding / removing characters mid-iterate
    is intentionally not supported — the persona briefs would diverge
    from the existing text. Operators who want a different cast start
    a new fusion story.

    ``head_version`` (1-based) is the version_number that matches the
    current ``full_text`` / ``outline`` / ``beats`` — every iterate call
    that produces new prose snapshots the prior head into ``versions``
    and increments this counter.
    """

    id: str
    character_ids: tuple[str, ...]
    prompt: str
    """Operator-supplied direction ("提示方向"). Echoed to every LLM
    call so iterations stay anchored even after multiple regenerates."""
    title: str
    premise: str
    theme: str
    outline: FusionOutline | None
    """``None`` while status is still ``planning`` and the outline call
    hasn't returned yet. Populated for every other status."""
    beats: tuple[FusionStoryBeat, ...]
    full_text: str
    """Concatenated prose after the polish stage. Empty while status
    is below ``ready``; readers should fall back to joining beats."""
    status: str
    head_version: int
    """1-based monotonic version counter — see class docstring."""
    versions: tuple[FusionStoryVersion, ...] = field(default_factory=tuple)
    error_message: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("FusionStory.id must be non-empty")
        if not self.character_ids:
            raise ValueError(
                "FusionStory.character_ids must contain at least one id",
            )
        if not self.prompt.strip():
            raise ValueError("FusionStory.prompt must be non-empty")
        if self.status not in _VALID_STATUSES:
            raise ValueError(
                f"FusionStory.status {self.status!r} must be one of "
                f"{sorted(_VALID_STATUSES)}",
            )
        if self.head_version < 1:
            raise ValueError("FusionStory.head_version must be >= 1")

    # --- factories ---------------------------------------------------

    @classmethod
    def create_pending(
        cls,
        *,
        character_ids: Iterable[str],
        prompt: str,
        id: str | None = None,
    ) -> "FusionStory":
        """Build the row that gets written *before* the planner stage.

        Status is ``planning`` and most narrative fields are blank — the
        orchestrator fills them in via ``with_outline`` /
        ``with_beat_content`` / ``with_full_text`` as each LLM stage
        returns.
        """
        cleaned_ids = tuple(
            cid.strip() for cid in character_ids
            if isinstance(cid, str) and cid.strip()
        )
        if not cleaned_ids:
            raise ValueError(
                "FusionStory.create_pending requires at least one character_id",
            )
        # Dedupe while preserving operator-supplied order.
        seen: set[str] = set()
        deduped: list[str] = []
        for cid in cleaned_ids:
            if cid in seen:
                continue
            seen.add(cid)
            deduped.append(cid)
        cleaned_prompt = prompt.strip()
        if not cleaned_prompt:
            raise ValueError("FusionStory.prompt must be non-empty")
        now = _utcnow()
        return cls(
            id=id or uuid4().hex,
            character_ids=tuple(deduped),
            prompt=cleaned_prompt,
            title="(planning…)",
            premise="(planning…)",
            theme="custom",
            outline=None,
            beats=(),
            full_text="",
            status=STATUS_PLANNING,
            head_version=1,
            versions=(),
            error_message=None,
            created_at=now,
            updated_at=now,
        )

    # --- transitions -------------------------------------------------

    def with_outline(self, outline: FusionOutline) -> "FusionStory":
        """Apply the outline-stage result; transition to ``writing``.

        Pre-fills empty beat shells matching the outline so the writer
        stage can update them in place via ``with_beat_content``.
        """
        beats = tuple(FusionStoryBeat.from_plan(p) for p in outline.beats)
        return replace(
            self,
            title=outline.title,
            premise=outline.premise,
            theme=outline.theme,
            outline=outline,
            beats=beats,
            status=STATUS_WRITING,
            updated_at=_utcnow(),
        )

    def with_beat_content(
        self, *, beat_id: str, content: str,
    ) -> "FusionStory":
        new_beats: list[FusionStoryBeat] = []
        for beat in self.beats:
            if beat.id == beat_id:
                new_beats.append(beat.with_content(content))
            else:
                new_beats.append(beat)
        return replace(
            self,
            beats=tuple(new_beats),
            updated_at=_utcnow(),
        )

    def with_status(
        self,
        status: str,
        *,
        error_message: str | None = None,
    ) -> "FusionStory":
        if status not in _VALID_STATUSES:
            raise ValueError(
                f"with_status: {status!r} not in {sorted(_VALID_STATUSES)}",
            )
        return replace(
            self,
            status=status,
            error_message=error_message,
            updated_at=_utcnow(),
        )

    def with_full_text(self, full_text: str) -> "FusionStory":
        """Apply the polish-stage result and flip status to ``ready``."""
        return replace(
            self,
            full_text=full_text,
            status=STATUS_READY,
            updated_at=_utcnow(),
        )

    def snapshot_version(self, *, label: str) -> "FusionStory":
        """Append the current head to the version chain.

        Called *before* mutating fields for an iterate operation so the
        prior text is preserved. Increments ``head_version`` so the next
        write is tagged with the new number.
        """
        outline_payload = (
            _serialize_outline(self.outline) if self.outline is not None else "{}"
        )
        snapshot = FusionStoryVersion(
            id=uuid4().hex,
            story_id=self.id,
            version_number=self.head_version,
            title=self.title,
            premise=self.premise,
            theme=self.theme,
            full_text=self.full_text,
            outline_json=outline_payload,
            iteration_label=label.strip() or "iterate",
            created_at=_utcnow(),
            beats_json=serialize_beats(self.beats),
        )
        return replace(
            self,
            versions=(*self.versions, snapshot),
            head_version=self.head_version + 1,
            updated_at=_utcnow(),
        )

    def restored_from(
        self,
        version: "FusionStoryVersion",
        *,
        outline: FusionOutline | None,
        beats: tuple[FusionStoryBeat, ...],
    ) -> "FusionStory":
        """Point the head back at *version* (C0-6 一鍵還原).

        Pure data transition — no LLM. The caller is expected to have
        snapshotted the current head first so the chain keeps both
        directions. Lands in ``ready`` because a restored version is a
        finished reading artifact."""
        return replace(
            self,
            title=version.title,
            premise=version.premise,
            theme=version.theme,
            full_text=version.full_text,
            outline=outline,
            beats=beats,
            status=STATUS_READY,
            error_message=None,
            updated_at=_utcnow(),
        )

    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES

    def joined_text(self) -> str:
        """Fallback rendering when ``full_text`` is empty.

        Used by the API while polish hasn't run yet so the UI can still
        show the per-beat prose stitched together.
        """
        if self.full_text.strip():
            return self.full_text
        chunks: list[str] = []
        for beat in self.beats:
            if not beat.content.strip():
                continue
            chunks.append(beat.content.strip())
        return "\n\n".join(chunks)


def _serialize_outline(outline: FusionOutline) -> str:
    """JSON-serialize an outline into the shape used by version snapshots.

    Kept as a free function so the entity stays free of ``json`` import
    leakage at the class level — only the snapshot path needs it.
    """
    import json

    return json.dumps(
        {
            "title": outline.title,
            "premise": outline.premise,
            "theme": outline.theme,
            "beats": [
                {
                    "sequence": b.sequence,
                    "act": b.act,
                    "title": b.title,
                    "hook": b.hook,
                    "dramatic_question": b.dramatic_question,
                    "target_chars": b.target_chars,
                    "focus_character_ids": list(b.focus_character_ids),
                    "entry_state": b.entry_state,
                    "exit_state": b.exit_state,
                    "transition_from_previous": b.transition_from_previous,
                }
                for b in outline.beats
            ],
        },
        ensure_ascii=False,
    )


def outline_from_snapshot_json(raw: str | None) -> FusionOutline | None:
    """Decode a version snapshot's ``outline_json`` back into an
    outline. Forgiving: any malformed payload returns ``None`` so a
    single bad row can't block a restore of the text itself."""
    import json

    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not data:
        return None
    raw_beats = data.get("beats")
    if not isinstance(raw_beats, list) or not raw_beats:
        return None
    plans: list[FusionBeatPlan] = []
    for entry in raw_beats:
        if not isinstance(entry, dict):
            continue
        try:
            plans.append(FusionBeatPlan.create(
                sequence=int(entry.get("sequence") or 0),
                act=str(entry.get("act") or "opening"),
                title=str(entry.get("title") or "（未命名）"),
                hook=str(entry.get("hook") or "（無）"),
                dramatic_question=str(entry.get("dramatic_question") or ""),
                target_chars=int(entry.get("target_chars") or 0),
                focus_character_ids=tuple(
                    str(cid) for cid in (entry.get("focus_character_ids") or [])
                ),
                entry_state=str(entry.get("entry_state") or ""),
                exit_state=str(entry.get("exit_state") or ""),
                transition_from_previous=str(
                    entry.get("transition_from_previous") or "",
                ),
            ))
        except (TypeError, ValueError):
            continue
    if not plans:
        return None
    title = str(data.get("title") or "").strip()
    premise = str(data.get("premise") or "").strip()
    if not title or not premise:
        return None
    try:
        return FusionOutline.create(
            title=title,
            premise=premise,
            theme=str(data.get("theme") or "custom"),
            beats=plans,
        )
    except ValueError:
        return None


def serialize_beats(beats: Iterable[FusionStoryBeat]) -> str:
    """JSON-serialize per-beat prose for version snapshots (C0-6)."""
    import json

    return json.dumps(
        [
            {
                "id": b.id,
                "sequence": b.sequence,
                "act": b.act,
                "title": b.title,
                "hook": b.hook,
                "dramatic_question": b.dramatic_question,
                "target_chars": b.target_chars,
                "content": b.content,
                "focus_character_ids": list(b.focus_character_ids),
            }
            for b in beats
        ],
        ensure_ascii=False,
    )


def beats_from_snapshot_json(raw: str | None) -> tuple[FusionStoryBeat, ...]:
    """Decode a version snapshot's ``beats_json``. Forgiving — pre-C0-6
    rows carry ``"[]"`` and decode to an empty tuple."""
    import json

    if not raw:
        return ()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(data, list):
        return ()
    beats: list[FusionStoryBeat] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        try:
            content = str(entry.get("content") or "")
            beats.append(FusionStoryBeat(
                id=str(entry.get("id") or uuid4().hex),
                sequence=int(entry.get("sequence") or 0),
                act=str(entry.get("act") or "opening"),
                title=str(entry.get("title") or "（未命名）"),
                hook=str(entry.get("hook") or ""),
                dramatic_question=str(entry.get("dramatic_question") or ""),
                target_chars=int(entry.get("target_chars") or 0),
                content=content,
                actual_chars=len(content),
                focus_character_ids=tuple(
                    str(cid) for cid in (entry.get("focus_character_ids") or [])
                ),
            ))
        except (TypeError, ValueError):
            continue
    return tuple(beats)
