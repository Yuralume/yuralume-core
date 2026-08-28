"""Ports for the story-seed / story-event pipeline."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Protocol

from kokoro_link.domain.entities.story_event import StoryEvent
from kokoro_link.domain.entities.story_seed import StorySeed

if TYPE_CHECKING:
    from kokoro_link.domain.entities.character import Character


@dataclass(frozen=True, slots=True)
class SceneContext:
    """Optional scene-structure hints handed to the expander.

    Set when the seed being expanded is actually a story-arc beat
    rather than a gacha seed — the expander uses these fields to
    compose a "play this scene" prompt (location, NPCs, dramatic
    question) instead of the generic "private journal entry" prompt.
    All fields optional so a partial template still works; everything
    ``None`` / empty (and tone matching default) makes the expander
    fall back to the seed-style output.
    """

    scene_type: str = "encounter"
    location: str | None = None
    scene_characters: tuple[str, ...] = ()
    dramatic_question: str | None = None
    required: bool = True
    tone: str = "daily"
    """Tonal register of the parent arc; routes the expander's prompt
    selection (daily / dramatic / mature / dark / lighthearted) so the
    same scene structure can read as gentle slice-of-life or grim
    drama. Unknown tones fall back to ``daily`` framing in the
    expander.

    Hosted (cloud mode) the expander runs this through
    ``domain.services.story_tone_policy.resolve_prompt_tone`` first —
    ``mature`` renders as ``dramatic`` and off-catalogue labels as
    ``daily`` (GF6). Self-host: unchanged."""

    today: date | None = None
    """Operator-local civil day this scene is being played on.

    Not scene *structure* — calendar plumbing, so it is deliberately
    excluded from :meth:`is_meaningful`. The expander turns it into the
    absolute-date anchors its prompt needs: the narrative it writes is
    persisted as a StoryEvent and re-read for days, so a relative time
    word frozen into it never resolves (CF1b)."""

    operator_position: str | None = None
    """Where the player stands in this scene (OP0-A's closed vocabulary),
    or ``None`` for *unjudged* — carried through from the beat so the
    expander frames the player the same way the autonomous beat scene
    writer would for the same beat (OP2-C). Not re-validated here: the
    beat entity already rejected anything outside the vocabulary, and
    the renderer degrades an unexpected value to *unjudged* rather than
    failing a realization."""

    operator_note: str | None = None
    """Optional free-text note about the player's dramatic position."""

    def is_meaningful(self) -> bool:
        """``True`` when at least one structured field is populated.

        Lets the expander cheaply decide whether to switch prompt
        modes — purely-empty contexts are treated identically to
        ``scene=None``.

        The player-position fields count as structure (OP2-C): a beat
        that says the scene is *about the player* but names no location,
        cast or question is exactly a beat whose framing must not be
        dropped, and falling through to the seed-style journal prompt
        would drop it. ``today`` still does not count — it is plumbing,
        not structure.
        """
        return bool(
            self.location
            or self.scene_characters
            or self.dramatic_question
            or self.operator_position
            or self.operator_note
        )


class StorySeedRepositoryPort(Protocol):
    async def upsert_by_external_id(
        self, seed: StorySeed,
    ) -> StorySeed:
        """Insert-or-update a seed keyed on its ``external_id``.

        Used by the YAML import CLI. When the row exists and content
        matches, this should be a no-op; when content differs, update
        the mutable fields (everything except ``id`` / ``created_at``).
        ``seed.external_id`` must be non-None.
        """

    async def add(self, seed: StorySeed) -> StorySeed:
        """Persist a seed created from the UI (no ``external_id``)."""

    async def get(self, seed_id: str) -> StorySeed | None: ...

    async def list_for_character(
        self,
        character_id: str,
        *,
        include_global: bool = True,
        enabled_only: bool = True,
    ) -> list[StorySeed]:
        """Seeds this character can draw from.

        ``include_global=True`` means global seeds
        (``character_id IS NULL``) come back alongside the character's
        private ones. ``enabled_only=True`` drops soft-disabled rows.
        """

    async def list_by_pack(self, pack_id: str) -> list[StorySeed]: ...

    async def update(self, seed: StorySeed) -> StorySeed: ...

    async def delete(self, seed_id: str) -> bool: ...


class StoryEventRepositoryPort(Protocol):
    async def add(self, event: StoryEvent) -> StoryEvent: ...

    async def get_for_day(
        self, character_id: str, date: str,
    ) -> list[StoryEvent]:
        """Events rolled for this character on this civil day."""

    async def list_recent(
        self, character_id: str, *, limit: int = 10,
    ) -> list[StoryEvent]:
        """Newest-first listing for prompt / UI display."""

    async def last_roll_dates(
        self, character_id: str,
    ) -> dict[str, str]:
        """Map of ``seed_id → YYYY-MM-DD of most recent roll``.

        The gacha service uses this to enforce cooldowns without
        making N queries per roll attempt.
        """

    async def mark_memorialized(self, event_id: str) -> None: ...

    async def delete_for_character(self, character_id: str) -> int: ...

    async def delete_arc_beat_realizations_since(
        self, character_id: str, since: datetime,
    ) -> int:
        """Delete arc-beat-realization events for ``character_id`` created
        at-or-after ``since``. Returns the number removed.

        Scoped to ``arc_beat_id IS NOT NULL`` only — gacha-rolled events
        (``seed_id`` set) regenerate daily via ``ensure_today`` and are
        deliberately out of scope for undo. An arc-beat realization is a
        one-shot record of a beat having been played; nothing regenerates
        it, so turn-undo is the only thing that can put it back."""


class StoryEventExpanderPort(Protocol):
    async def expand(
        self,
        *,
        seed: StorySeed,
        character_name: str,
        character_summary: str,
        speaking_style: str,
        world_frame: str,
        scene: SceneContext | None = None,
        character: "Character | None" = None,
        operator_primary_language: str = "zh-TW",
    ) -> tuple[str, str | None]:
        """Turn a seed into (narrative, emotional_tone).

        ``narrative`` is 2–3 sentences in the character's voice.
        ``emotional_tone`` is optional (may be ``None`` when the
        expander can't infer one).

        ``scene`` is set when ``seed`` is actually a story-arc beat:
        the expander should produce a "play this scene" narrative
        (location, NPC interactions, the dramatic question's
        beat) rather than a generic journal entry. Adapters built
        before Phase 1 that ignore ``scene`` continue to work — they
        just produce flatter narratives for arc beats.
        """
