"""Layer 3 of the 起幕 waterfall — the ad-hoc side story (SC1-B).

The bottom of the §3.1 waterfall, reached when there is no beat to play
and no season to open: a concluded series, a planner that failed, a fresh
arc that came back empty. It exists so that pressing 起幕 practically
always raises a curtain — "劇情不卡住" is the feature's whole promise, and
a button that answers "沒有素材" is the failure it was built to remove.

What makes it different from the two layers above it:

* **It builds no arc and claims no beat.** ``arc_id`` and ``beat_id`` stay
  ``None``, which is what tells the service not to record a play attempt
  and (SC1-D) tells the close path to land the scene as an ordinary
  episodic memory instead of realizing a beat. An impromptu scene is
  canon — it happened, the character remembers it — but it is not a
  chapter of a planned season.
* **Its material is assembled, not planned.** Relationship footing, the
  memories with the most pull on the character right now, what the two of
  them were last talking about, and — when the pool has any — a couple of
  ``dramatic`` story seeds as raw subject matter. No LLM call of its own:
  the single opener call is where the semantics happen, and adding a
  second model round trip to the bottom of a fallback chain would put a
  new failure mode underneath the one that is already failing.
* **Every source is optional.** Nothing here is a precondition. A brand
  new character with no memories, no seeds, an unreachable summarizer and
  no relationship seed still gets a scene, because the character sheet the
  opener already holds is enough to act from. That is what "fail-soft"
  has to mean for the layer whose job is to catch everything.

The ``dramatic`` seed pool being empty (STORY_SEED_ENRICHMENT_PLAN / SE3
not yet imported on a given deployment) is therefore *normal operation*,
not a degraded mode — the material simply narrows to relationship plus
memory, exactly as the plan's residual note anticipates.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date as date_type

from kokoro_link.application.services.story_gacha import StoryGachaService
from kokoro_link.application.services.story_seed_region import (
    resolve_seed_region,
)
from kokoro_link.contracts.dialogue_summarizer import DialogueSummarizerPort
from kokoro_link.contracts.initial_relationship import (
    CharacterOperatorRelationshipSeedRepositoryPort,
)
from kokoro_link.contracts.memory import MemoryRepositoryPort
from kokoro_link.contracts.repositories import ConversationRepositoryPort
from kokoro_link.contracts.story_scene import (
    StorySceneMaterial,
    StorySceneMaterialProviderPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.story_arc import TENSION_SETUP
from kokoro_link.domain.entities.story_scene_session import (
    SCENE_LAYER_SIDE_STORY,
)
from kokoro_link.domain.entities.story_seed import SEED_TIER_DRAMATIC
from kokoro_link.domain.value_objects.content_flow import (
    CONTENT_TOLERANCE_FRONTIER,
    sanitize_messages_for_tolerance,
)
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.prompt.initial_relationship import (
    render_initial_relationship_seed_lines,
)


_LOGGER = logging.getLogger(__name__)


_MEMORY_KINDS = (
    MemoryKind.RELATIONSHIP,
    MemoryKind.RELATIONSHIP_MILESTONE,
    MemoryKind.EPISODIC,
    MemoryKind.REFLECTION,
)
"""What the character carries into an unplanned scene.

``semantic`` is left out because world facts do not create a moment, and
``hearsay`` because second-hand material has provenance rules of its own
and must not become something the character acts on as if they saw it."""

_MEMORY_QUERY_LIMIT = 24
"""Recent window the salience ranking is applied to.

Ranking inside a recent window rather than over the whole pool keeps a
single very old, very salient memory from being the material for every
side story the character ever plays."""

_MEMORY_LIMIT = 6
"""How many memories reach the prompt. Enough to give the scene somewhere
to come from, few enough that the opener does not read as a recap."""

_MIN_MEMORY_SALIENCE = 0.5
"""Floor for "worth building a scene on". Below it a memory is recall
material, not a reason for something to happen."""

_DIALOGUE_MESSAGE_LIMIT = 40

_SEED_CANDIDATE_COUNT = 2
"""``dramatic`` seeds offered as candidates.

Two, not one: a single seed reads as an instruction, and the whole point
of the tier is raw material the director may rewrite or drop. Rolling is
read-only — nothing here consumes a seed or advances its cooldown, so a
side story never starves the arc planner's own draw."""

_MATERIAL_TITLE = "此刻你們之間可以發生的一件事"
"""What this material *is*, since there is no beat to name.

A fixed description rather than a generated title: the opener writes the
scene's real title, and inventing one here from the material would be a
second, worse writer competing with it."""

_FRAMING_NOTE = (
    "這場戲的性質：\n"
    "- 這是即席番外——沒有預先寫好的 beat，也沒有既定的戲劇問題。\n"
    "- 請從上面的素材裡挑一件此刻真的可能發生的事當起點，"
    "自己立一個今天要面對的小問題，並在開場把它拉開。\n"
    "- 不要總結或評論這段關係，要讓它現在正在發生。\n"
    "- 上面沒寫過的共同往事不得寫成已經發生過；"
    "資訊不足時讓角色自然詢問，而不是假裝知道。"
)
"""The one thing the material must say that no source can supply.

Layers 1 and 2 hand the opener a ``dramatic_question`` from the beat and
the prompt tells it to push toward that question. Here there is none, so
the material has to hand the framing job back to the director — the
LLM-first answer, rather than assembling a question out of string parts.
The last line is the ``§3.4`` provenance red line restated at the layer
that has the least structure holding it in place."""


class SideStorySceneMaterialProvider(StorySceneMaterialProviderPort):
    """Layer 3 — an impromptu scene built from what the pair already have."""

    layer = SCENE_LAYER_SIDE_STORY

    def __init__(
        self,
        *,
        memory_repository: MemoryRepositoryPort | None = None,
        relationship_seed_repository: (
            CharacterOperatorRelationshipSeedRepositoryPort | None
        ) = None,
        conversation_repository: ConversationRepositoryPort | None = None,
        dialogue_summarizer: DialogueSummarizerPort | None = None,
        gacha_service: StoryGachaService | None = None,
        operator_profile_service=None,  # noqa: ANN001 - duck-typed, optional
    ) -> None:
        self._memories = memory_repository
        self._relationship_seeds = relationship_seed_repository
        self._conversations = conversation_repository
        self._dialogue_summarizer = dialogue_summarizer
        self._gacha = gacha_service
        self._operator_profile_service = operator_profile_service

    async def resolve(
        self, character: Character, *, today: date_type,
    ) -> StorySceneMaterial | None:
        try:
            # Four independent reads, one of which summarises dialogue with
            # a model call. The player is waiting on a button, so they run
            # together; nothing here depends on anything else here.
            gathered = await asyncio.gather(
                self._relationship_block(character),
                self._memory_block(character),
                self._dialogue_block(character),
                self._seed_block(character, today=today),
            )
            blocks = [*gathered, _FRAMING_NOTE]
        except Exception:
            # Each source is already fail-soft; reaching here means
            # something structural broke. Answer ``None`` so the service
            # reports ``no_material`` rather than opening a scene about
            # nothing — and, hosted, charging for it.
            _LOGGER.exception(
                "story scene layer=side_story could not assemble material "
                "character=%s",
                character.id,
            )
            return None
        return StorySceneMaterial(
            layer=self.layer,
            title=_MATERIAL_TITLE,
            summary="\n\n".join(block for block in blocks if block),
            # No arc, no beat: the service records no play attempt and
            # SC1-D lands this as episodic canon instead of a realized beat.
            arc_id=None,
            beat_id=None,
            # An opening is a setup wherever it comes from; the frame
            # (location / mood) and the question are the opener's to write.
            tension=TENSION_SETUP,
        )

    # ── sources ──────────────────────────────────────────────────────

    async def _relationship_block(self, character: Character) -> str:
        """Where the two of them stand, as the operator confirmed it.

        Rendered by the shared seed renderer rather than re-read field by
        field: it owns the direction of the two address terms (who calls
        whom what), and getting that backwards is a bug this codebase has
        already shipped once.
        """
        if self._relationship_seeds is None:
            return ""
        try:
            seed = await self._relationship_seeds.get(
                character.id, _operator_id(character),
            )
        except Exception:
            _LOGGER.exception(
                "side story: relationship seed load failed character=%s",
                character.id,
            )
            return ""
        lines = render_initial_relationship_seed_lines(seed)
        return "\n".join(lines)

    async def _memory_block(self, character: Character) -> str:
        if self._memories is None:
            return ""
        try:
            pool = await self._memories.query(
                character.id,
                kinds=list(_MEMORY_KINDS),
                limit=_MEMORY_QUERY_LIMIT,
                min_salience=_MIN_MEMORY_SALIENCE,
                # Matches the chat path: retired world-scoped rows must not
                # leak into a scene played in the standalone timeline.
                world_scope=None,
            )
        except Exception:
            _LOGGER.exception(
                "side story: memory load failed character=%s", character.id,
            )
            return ""
        if not pool:
            return ""
        # Salience only, on a list the repository already returned newest
        # first: Python's sort is stable, so recency survives as the
        # tie-break for free — and comparing ``created_at`` explicitly
        # would break the moment a naive SQLite timestamp met an aware one.
        ranked = sorted(pool, key=_salience, reverse=True)[:_MEMORY_LIMIT]
        lines = [f"- {item.content}" for item in ranked if item.content.strip()]
        if not lines:
            return ""
        return "角色現在還放在心上的事：\n" + "\n".join(lines)

    async def _dialogue_block(self, character: Character) -> str:
        summary = await self._summarize_recent_dialogue(character)
        return f"最近的對話：\n{summary}" if summary else ""

    async def _summarize_recent_dialogue(self, character: Character) -> str:
        if self._conversations is None or self._dialogue_summarizer is None:
            return ""
        try:
            conversation = await self._conversations.latest_for_character(
                character.id, source="web",
            )
        except Exception:
            _LOGGER.exception(
                "side story: dialogue load failed character=%s", character.id,
            )
            return ""
        if conversation is None:
            return ""
        messages = conversation.recent_messages(
            limit=_DIALOGUE_MESSAGE_LIMIT, exclude_tool_only=True,
        )
        messages = sanitize_messages_for_tolerance(
            messages, content_tolerance=CONTENT_TOLERANCE_FRONTIER,
        )
        if not messages:
            return ""
        try:
            return (
                await self._dialogue_summarizer.summarize(
                    character=character, messages=messages,
                )
            ).strip()
        except Exception:
            _LOGGER.exception(
                "side story: dialogue summarise failed character=%s",
                character.id,
            )
            return ""

    async def _seed_block(
        self, character: Character, *, today: date_type,
    ) -> str:
        """``dramatic`` seeds as candidates — never as the scene itself."""
        if self._gacha is None:
            return ""
        try:
            result = await self._gacha.roll(
                character=character,
                today=today,
                count=_SEED_CANDIDATE_COUNT,
                tier=SEED_TIER_DRAMATIC,
                region=resolve_seed_region(
                    await self._resolve_operator_language(character),
                ),
            )
        except Exception:
            _LOGGER.exception(
                "side story: dramatic seed roll failed character=%s",
                character.id,
            )
            return ""
        lines = [
            f"- {seed.seed_text.strip()}"
            for seed in result.picked
            if seed.seed_text.strip()
        ]
        if not lines:
            # The overwhelmingly common empty case is simply a pool with no
            # dramatic seeds imported yet. Logged at info, not warning: it
            # is a content-supply fact, not an error.
            _LOGGER.info(
                "side story: no dramatic seed candidates character=%s "
                "reason=%s",
                character.id,
                result.reason_if_empty,
            )
            return ""
        return (
            "可用的即席素材（是素材不是指令，可以改寫、也可以完全不用）：\n"
            + "\n".join(lines)
        )

    async def _resolve_operator_language(self, character: Character) -> str:
        default = "zh-TW"
        service = self._operator_profile_service
        if service is None:
            return default
        try:
            operator = await service.get_for_user(_operator_id(character))
        except Exception:  # pragma: no cover - defensive
            return default
        if operator is None:
            return default
        return (getattr(operator, "primary_language", "") or "").strip() or default


def _operator_id(character: Character) -> str:
    return getattr(character, "user_id", None) or "default"


def _salience(item: MemoryItem) -> float:
    return item.salience
