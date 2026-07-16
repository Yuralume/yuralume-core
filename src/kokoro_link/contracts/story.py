"""Ports for the story-seed / story-event pipeline."""

from collections.abc import Sequence
from dataclasses import dataclass
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
    expander."""

    def is_meaningful(self) -> bool:
        """``True`` when at least one structured field is populated.

        Lets the expander cheaply decide whether to switch prompt
        modes — purely-empty contexts are treated identically to
        ``scene=None``.
        """
        return bool(
            self.location
            or self.scene_characters
            or self.dramatic_question
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
