"""Character brief assembler for the fusion-story pipeline.

Pulls the bits the planner and writer stages actually use from a
``Character`` + a small, salience-ranked slice of its memory pool, and
formats them into a stable prompt fragment.

Why a dedicated service:

- Keeps the LLM stages (planner / writer / polisher) free of repository
  dependencies — they only see strings.
- Per-character memory budget enforcement happens in one place, so
  prompt size stays predictable when the operator selects 5 characters.
- The same brief is reused across every regenerate iteration so the
  story stays anchored to the same persona facts.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from kokoro_link.contracts.memory import MemoryRepositoryPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.infrastructure.prompt.character_identity import (
    render_character_identity_lines,
)


_LOGGER = logging.getLogger(__name__)

_DEFAULT_MEMORY_LIMIT = 10
"""Top-K salience-ranked memories per character."""

_DEFAULT_MEMORY_CHAR_BUDGET = 1500
"""Max total characters of memory text included per character.

Combined with ``_DEFAULT_MEMORY_LIMIT`` this keeps each brief under
~2k chars even when several memories are long. Selecting 5 characters
caps the joined-brief block at ~10k chars before the planner template
is added.
"""

_DEFAULT_MIN_SALIENCE = 0.3
"""Floor on memory salience when ranking. Below this we treat the row
as noise — fusion stories should pull on what *matters* to the
character, not whatever happens to be the freshest recall."""


@dataclass(frozen=True, slots=True)
class CharacterBrief:
    """Prompt-ready block for a single character.

    ``text`` is the formatted multi-line block consumed by the planner
    and writer prompts. The structured fields are kept for callers that
    want to render them differently (e.g. the polish stage cites the
    character names without the full memory dump).
    """

    character_id: str
    name: str
    summary: str
    text: str

    def short_label(self) -> str:
        return self.name or self.character_id


class FusionCharacterBriefBuilder:
    """Builds character briefs from a memory repository + the entity.

    The repo lookup degrades gracefully — when none is wired (unit test
    container) we still return a brief with persona fields only.
    """

    def __init__(
        self,
        *,
        memory_repository: MemoryRepositoryPort | None,
        memory_limit: int = _DEFAULT_MEMORY_LIMIT,
        memory_char_budget: int = _DEFAULT_MEMORY_CHAR_BUDGET,
        min_salience: float = _DEFAULT_MIN_SALIENCE,
    ) -> None:
        self._memory_repository = memory_repository
        self._memory_limit = max(1, memory_limit)
        self._memory_char_budget = max(200, memory_char_budget)
        self._min_salience = max(0.0, min(1.0, min_salience))

    async def build(self, character: Character) -> CharacterBrief:
        memories = await self._top_memories(character.id)
        text = _format_brief(character, memories)
        return CharacterBrief(
            character_id=character.id,
            name=character.name,
            summary=character.summary or "",
            text=text,
        )

    async def build_many(
        self, characters: Sequence[Character],
    ) -> list[CharacterBrief]:
        return [await self.build(c) for c in characters]

    def build_persona_only(self, character: Character) -> CharacterBrief:
        """Persona-only brief — skip the memory pool entirely.

        Used by surfaces that should stay isolated from the character's
        chat history, like branching drama: a drama is a what-if story,
        and bleeding chat memories into the prompt makes the character
        drift toward chat events that have nothing to do with the drama
        scenario. After several plays this manifests as the persona
        going off-rails. Synchronous because no IO is needed."""
        text = _format_brief(character, ())
        return CharacterBrief(
            character_id=character.id,
            name=character.name,
            summary=character.summary or "",
            text=text,
        )

    def build_persona_only_many(
        self, characters: Sequence[Character],
    ) -> list[CharacterBrief]:
        return [self.build_persona_only(c) for c in characters]

    async def _top_memories(self, character_id: str) -> list[MemoryItem]:
        return await select_brief_memories(
            self._memory_repository,
            character_id,
            memory_limit=self._memory_limit,
            memory_char_budget=self._memory_char_budget,
            min_salience=self._min_salience,
        )


async def select_brief_memories(
    memory_repository: MemoryRepositoryPort | None,
    character_id: str,
    *,
    memory_limit: int = _DEFAULT_MEMORY_LIMIT,
    memory_char_budget: int = _DEFAULT_MEMORY_CHAR_BUDGET,
    min_salience: float = _DEFAULT_MIN_SALIENCE,
) -> list[MemoryItem]:
    """Salience-ranked, char-budgeted memory slice for one character.

    Single source of truth for *which* memories count as a character's
    fusion-usable material. :class:`FusionCharacterBriefBuilder` formats
    these into the planner / writer prompt; the material-richness stats
    service counts them for the picker badge. Sharing one implementation
    stops the badge from drifting away from what a fusion story would
    actually pull.

    Fail-soft: a missing repository or a query error yields ``[]`` (the
    caller degrades to a persona-only brief / a ``sparse`` badge) rather
    than propagating.
    """
    if memory_repository is None:
        return []
    try:
        recent = await memory_repository.query(
            character_id,
            limit=max(memory_limit * 3, 30),
            min_salience=min_salience,
        )
    except Exception:
        _LOGGER.exception(
            "fusion memory selection: query failed character=%s",
            character_id,
        )
        return []
    # Salience desc → recency desc as the tie-breaker. ``query`` already
    # returns recent-first; salience sort is stable so older high-salience
    # memories still rank above newer low-salience.
    ranked = sorted(recent, key=lambda m: m.salience, reverse=True)
    # Apply char budget across the top-K.
    budget = memory_char_budget
    chosen: list[MemoryItem] = []
    for item in ranked:
        if len(chosen) >= memory_limit:
            break
        length = len(item.content)
        if length > budget and chosen:
            # Already have something — stop instead of partial.
            break
        chosen.append(item)
        budget -= length
        if budget <= 0:
            break
    return chosen


def _format_brief(
    character: Character, memories: Sequence[MemoryItem],
) -> str:
    personality = "、".join(character.personality) or "（未設定）"
    interests = "、".join(character.interests) or "（未設定）"
    speaking_style = (character.speaking_style or "").strip() or "（未設定）"
    aspirations = (
        "、".join(character.aspirations)
        if character.aspirations else "（未設定）"
    )
    appearance = (character.appearance or "").strip() or "（未設定）"
    summary = (character.summary or "").strip() or "（未設定）"

    lines = [
        f"## 角色：{character.name}（id={character.id}）",
        f"- 簡介：{summary}",
        *render_character_identity_lines(character),
        f"- 性格：{personality}",
        f"- 興趣：{interests}",
        f"- 說話風格：{speaking_style}",
        f"- 長期追求：{aspirations}",
        f"- 外觀：{appearance}",
    ]
    if memories:
        lines.append("- 重要記憶（依重要度排序）：")
        for item in memories:
            tag_label = "/".join(item.tags) if item.tags else item.kind.value
            content = item.content.strip().replace("\n", " ")
            lines.append(
                f"  · [{tag_label}|salience={item.salience:.2f}] {content}",
            )
    else:
        lines.append("- 重要記憶：（暫無）")
    return "\n".join(lines)
