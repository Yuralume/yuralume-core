"""Orchestration: roll seeds → expand via LLM → persist + memorialise.

The entry point is :meth:`StoryEventService.ensure_today` — idempotent,
called lazily by the chat path on every turn (same pattern as
``ScheduleService.ensure_schedule``). On the first call of a civil day,
it rolls, expands, persists, and fires a matching episodic memory. On
subsequent calls the same day it's a no-op because the roll finds
today's seed already picked.

**Arc integration**: when a ``StoryArcService`` is wired, ``ensure_today``
first checks whether the character's active arc has a beat due today.
If yes, the beat wins the daily slot but is **not** immediately expanded
into a diary entry. Instead it records a play attempt so prompt builders
can stage the scene and post-turn can later persist the actual performed
moment as a ``StoryEvent``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date as date_type, datetime, timezone, tzinfo
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kokoro_link.application.services.story_arc_service import (
        StoryArcService,
    )

from kokoro_link.application.services.memory_embedding import attach_embeddings
from kokoro_link.application.services.story_gacha import (
    GachaResult,
    StoryGachaService,
)
from kokoro_link.contracts.embedder import EmbedderPort
from kokoro_link.contracts.memory import MemoryRepositoryPort
from kokoro_link.contracts.story import (
    SceneContext,
    StoryEventExpanderPort,
    StoryEventRepositoryPort,
)
from kokoro_link.contracts.story_arc import (
    ArcCompletionMemoryContext,
    ArcCompletionMemoryWriterPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.memory_item import (
    MEMORY_AUDIENCE_PRIVATE,
    MemoryItem,
)
from kokoro_link.domain.entities.story_arc import (
    ARC_COMPLETED,
    TENSION_CLIMAX,
    TENSION_FALLING,
    TENSION_RESOLUTION,
    TENSION_RISING,
    TENSION_SETUP,
    StoryArc,
    StoryArcBeat,
)
from kokoro_link.domain.entities.story_event import StoryEvent
from kokoro_link.domain.entities.story_seed import StorySeed
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.domain.value_objects.timezone import timezone_for_id


_LOGGER = logging.getLogger(__name__)
_DEFAULT_DAILY_COUNT = 1


class _BeatAsSeed:
    """Duck-typed view of a ``StoryArcBeat`` that looks like a
    ``StorySeed`` to the expander.

    The expander reads ``seed.id`` (for logging only) and
    ``seed.seed_text`` (as the narrative prompt). We give it the beat's
    summary as the seed_text so the expander treats the beat's
    paragraph as the thing to expand in the character's voice.
    """

    __slots__ = ("_beat",)

    def __init__(self, beat):  # type: ignore[no-untyped-def]
        self._beat = beat

    @property
    def id(self) -> str:
        return f"arc-beat:{self._beat.id}"

    @property
    def seed_text(self) -> str:
        return self._beat.summary


@dataclass(frozen=True, slots=True)
class EnsureReport:
    events: tuple[StoryEvent, ...]
    newly_rolled: int
    """How many events are brand new this call (0 = cached from an
    earlier call today)."""


class StoryEventService:
    def __init__(
        self,
        *,
        gacha: StoryGachaService,
        expander: StoryEventExpanderPort,
        event_repository: StoryEventRepositoryPort,
        memory_repository: MemoryRepositoryPort,
        embedder: EmbedderPort | None = None,
        local_tz: tzinfo | None = None,
        daily_count: int = _DEFAULT_DAILY_COUNT,
        arc_service: "StoryArcService | None" = None,
        arc_completion_memory_writer: ArcCompletionMemoryWriterPort | None = None,
        operator_profile_service=None,  # noqa: ANN001 - optional; resolves primary_language
    ) -> None:
        self._gacha = gacha
        self._expander = expander
        self._events = event_repository
        self._memories = memory_repository
        self._embedder = embedder
        self._local_tz = local_tz
        self._daily_count = max(1, daily_count)
        self._arc_service = arc_service
        self._arc_completion_memory_writer = arc_completion_memory_writer
        self._operator_profile_service = operator_profile_service
        # Per-(character, day) lock — prevents chat + proactive
        # scheduler + schedule-panel poll from all triggering the
        # gacha roll in parallel when the day's first event hasn't
        # been persisted yet.
        self._roll_locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def ensure_today(
        self,
        character: Character,
        *,
        now: datetime | None = None,
    ) -> EnsureReport:
        """Roll + expand + persist today's event if not already done.

        Order of precedence:
        1. If the character has an active story arc with a beat due
           today (or an overdue pending beat), surface **that** as
           today's playable scene and do not roll gacha. Arc beats
           always win the daily slot so the narrative spine doesn't get
           hijacked by a random diary entry.
        2. Otherwise fall back to the gacha. A day with no arc beat is
           free narrative territory — the gacha fills the gap so the
           character still has **something** happening.
        """
        today = await self._today_for_character(character, now)
        existing = await self._events.get_for_day(character.id, today.isoformat())
        if len(existing) >= self._daily_count:
            return EnsureReport(events=tuple(existing), newly_rolled=0)
        lock = self._roll_locks.setdefault(
            (character.id, today.isoformat()), asyncio.Lock(),
        )
        async with lock:
            # Re-check under the lock — another caller may have just
            # filled the daily slot while we waited.
            existing = await self._events.get_for_day(
                character.id, today.isoformat(),
            )
            if len(existing) >= self._daily_count:
                return EnsureReport(events=tuple(existing), newly_rolled=0)
            return await self._do_ensure_today(character, today, existing)

    async def _do_ensure_today(
        self,
        character: Character,
        today: date_type,
        existing: list[StoryEvent] | tuple[StoryEvent, ...],
    ) -> EnsureReport:
        """Inner worker — caller holds the per-day lock."""
        newly_added: list[StoryEvent] = []
        remaining = self._daily_count - len(existing)

        # --- Arc path (takes the first slot) ---
        if self._arc_service is not None and remaining > 0:
            due = await self._arc_service.next_beat_due(character.id, today=today)
            if due is not None:
                _arc, beat = due
                adjustment = None
                try:
                    await self._arc_service.mark_beat_play_attempted(
                        beat_id=beat.id,
                        attempted_at=datetime.now(timezone.utc),
                        source="chat_scene_directive",
                        result="prompted",
                        push_intensity=(
                            "scene_directive" if beat.required
                            else "background_hint"
                        ),
                    )
                    adjustment = await (
                        self._arc_service.recheck_due_beat_after_attempt(
                            character,
                            beat_id=beat.id,
                            today=today,
                        )
                    )
                except Exception:
                    _LOGGER.exception(
                        "arc beat play-attempt record failed beat=%s",
                        beat.id,
                    )
                if (
                    adjustment is not None
                    and adjustment.action == "mark_realized"
                    and adjustment.narrative
                ):
                    event = await self.record_arc_beat_realization(
                        character,
                        beat_id=beat.id,
                        narrative=adjustment.narrative,
                        now=datetime.combine(
                            today,
                            datetime.min.time(),
                            tzinfo=self._local_tz or timezone.utc,
                        ),
                    )
                    if event is not None:
                        return EnsureReport(
                            events=tuple([*existing, event]),
                            newly_rolled=1,
                        )
                if (
                    adjustment is None
                    or adjustment.action not in {"delay_beat", "skip_beat"}
                ):
                    return EnsureReport(
                        events=tuple(existing),
                        newly_rolled=0,
                    )

        # --- Gacha fallback (remaining slots) ---
        if remaining > 0:
            result: GachaResult = await self._gacha.roll(
                character=character, today=today, count=remaining,
            )
            if result.picked:
                for seed in result.picked:
                    event = await self._build_and_persist(character, today, seed)
                    if event is not None:
                        newly_added.append(event)
            else:
                _LOGGER.info(
                    "story gacha: nothing rolled for character=%s reason=%s",
                    character.id, result.reason_if_empty,
                )

        all_events = list(existing) + newly_added
        return EnsureReport(events=tuple(all_events), newly_rolled=len(newly_added))

    async def list_recent(
        self, character_id: str, *, limit: int = 10,
    ) -> list[StoryEvent]:
        return await self._events.list_recent(character_id, limit=limit)

    async def record_arc_beat_realization(
        self,
        character: Character,
        *,
        beat_id: str,
        narrative: str,
        now: datetime | None = None,
        emotional_tone: str | None = None,
    ) -> StoryEvent | None:
        """Persist the event that actually happened in chat/proactive.

        Direction B moves arc realization from calendar time to
        interaction time. This method is called after post-turn LLM
        emits ``mark_realized`` with a narrative of what happened.
        """
        if self._arc_service is None:
            return None
        final_narrative = (narrative or "").strip()
        if not final_narrative:
            return None
        arc = await self._arc_service.get_arc_by_beat(beat_id)
        if arc is None:
            return None
        beat = arc.find_beat(beat_id)
        if beat is None or beat.status != "pending":
            return None
        today = await self._today_for_character(character, now)
        existing = await self._events.get_for_day(
            character.id, today.isoformat(),
        )
        for event in existing:
            if event.arc_beat_id == beat_id:
                try:
                    updated_arc = await self._arc_service.realize_beat(
                        beat_id=beat_id, event_id=event.id,
                    )
                    if updated_arc is not None and updated_arc.status == ARC_COMPLETED:
                        await self._write_arc_completion_milestone(
                            character,
                            updated_arc,
                        )
                except Exception:
                    _LOGGER.exception(
                        "arc beat realize_beat failed beat=%s", beat_id,
                    )
                return event

        event = StoryEvent.create(
            character_id=character.id,
            date=today.isoformat(),
            arc_beat_id=beat_id,
            narrative=final_narrative,
            emotional_tone=emotional_tone,
        )
        try:
            event = await self._events.add(event)
        except Exception:
            _LOGGER.exception(
                "arc beat performed event persist failed beat=%s character=%s",
                beat_id, character.id,
            )
            return None

        await self._memorialize(event)
        try:
            updated_arc = await self._arc_service.realize_beat(
                beat_id=beat_id, event_id=event.id,
            )
            if updated_arc is not None and updated_arc.status == ARC_COMPLETED:
                await self._write_arc_completion_milestone(
                    character,
                    updated_arc,
                )
        except Exception:
            _LOGGER.exception(
                "arc beat realize_beat failed beat=%s", beat_id,
            )
        return event

    async def _build_and_persist_from_beat(
        self,
        character: Character,
        today: date_type,
        beat: StoryArcBeat,
        *,
        arc: StoryArc | None = None,
    ) -> StoryEvent | None:
        """Expand a beat's summary into a StoryEvent narrative.

        Uses the same ``StoryEventExpanderPort`` as the gacha path so
        the resulting narrative has the same tone / length / voice
        handling — just with a richer seed (the beat's paragraph-length
        summary vs. a one-line gacha seed). A duck-typed ``_BeatAsSeed``
        satisfies the expander's interface without building a real
        ``StorySeed`` (which does strict validation we don't need here);
        the persisted event has ``seed_id=NULL`` and ``arc_beat_id`` set.
        """
        # Tone comes from the parent arc, threaded down by the caller
        # so we don't need a separate lookup. Falls back to "daily"
        # when the caller didn't pass an arc (legacy paths or tests).
        arc_tone = arc.tone if arc is not None else "daily"
        scene = SceneContext(
            scene_type=beat.scene_type,
            location=beat.location,
            scene_characters=beat.scene_characters,
            dramatic_question=beat.dramatic_question,
            required=beat.required,
            tone=arc_tone,
        )
        try:
            narrative, tone = await self._expand_with_language(
                seed=_BeatAsSeed(beat),  # duck-typed StorySeed
                character_name=character.name,
                character_summary=character.summary,
                speaking_style=character.speaking_style,
                world_frame=character.world_frame or "modern",
                scene=scene,
                character=character,
            )
        except Exception:
            _LOGGER.exception(
                "arc beat expander failed beat=%s character=%s",
                beat.id, character.id,
            )
            # Fall back to using the beat summary directly — the arc
            # path should always produce *something* so the narrative
            # spine doesn't silently drop.
            narrative = beat.summary
            tone = None

        final_narrative = (narrative or "").strip() or beat.summary.strip()
        if not final_narrative:
            return None

        event = StoryEvent.create(
            character_id=character.id,
            date=today.isoformat(),
            arc_beat_id=beat.id,
            narrative=final_narrative,
            emotional_tone=tone,
        )
        try:
            await self._events.add(event)
        except Exception:
            _LOGGER.exception(
                "arc beat event persist failed beat=%s character=%s",
                beat.id, character.id,
            )
            return None

        await self._memorialize(event)
        return event

    async def _build_and_persist(
        self,
        character: Character,
        today: date_type,
        seed: StorySeed,
    ) -> StoryEvent | None:
        try:
            narrative, tone = await self._expand_with_language(
                seed=seed,
                character_name=character.name,
                character_summary=character.summary,
                speaking_style=character.speaking_style,
                world_frame=character.world_frame or "modern",
                character=character,
            )
        except Exception:
            _LOGGER.exception(
                "story expander crashed for seed=%s character=%s",
                seed.id, character.id,
            )
            return None

        if not narrative.strip():
            return None

        event = StoryEvent.create(
            character_id=character.id,
            date=today.isoformat(),
            seed_id=seed.id,
            narrative=narrative,
            emotional_tone=tone,
        )
        try:
            await self._events.add(event)
        except Exception:
            _LOGGER.exception(
                "story event persist failed seed=%s character=%s",
                seed.id, character.id,
            )
            return None

        await self._memorialize(event)
        return event

    async def _expand_with_language(
        self,
        *,
        seed,
        character_name: str,
        character_summary: str,
        speaking_style: str,
        world_frame: str,
        scene: SceneContext | None = None,
        character: Character | None = None,
    ) -> tuple[str, str | None]:
        language = (
            await self._resolve_operator_language(character)
            if character is not None else "zh-TW"
        )
        try:
            return await self._expander.expand(
                seed=seed,
                character_name=character_name,
                character_summary=character_summary,
                speaking_style=speaking_style,
                world_frame=world_frame,
                scene=scene,
                character=character,
                operator_primary_language=language,
            )
        except TypeError as exc:
            if "operator_primary_language" not in str(exc):
                raise
            return await self._expander.expand(
                seed=seed,
                character_name=character_name,
                character_summary=character_summary,
                speaking_style=speaking_style,
                world_frame=world_frame,
                scene=scene,
                character=character,
            )

    async def _resolve_operator_language(self, character) -> str:  # noqa: ANN001
        default = "zh-TW"
        service = self._operator_profile_service
        if service is None:
            return default
        user_id = getattr(character, "user_id", None) or "default"
        try:
            operator = await service.get_for_user(user_id)
        except Exception:  # pragma: no cover - defensive
            return default
        if operator is None:
            return default
        lang = getattr(operator, "primary_language", "") or ""
        return lang.strip() or default

    async def _resolve_operator_timezone(self, character) -> tzinfo:  # noqa: ANN001
        default = self._local_tz or timezone.utc
        service = self._operator_profile_service
        if service is None:
            return default
        user_id = getattr(character, "user_id", None) or "default"
        try:
            operator = await service.get_for_user(user_id)
            return timezone_for_id(getattr(operator, "timezone_id", None))
        except Exception:  # pragma: no cover - defensive
            return default

    async def _memorialize(self, event: StoryEvent) -> None:
        """Fire-and-forget episodic memory write for the event.

        Failure here must not abort the caller — the event is persisted
        and the character still gets the narrative in prompts via
        ``list_recent``. Worst case, it's not in hybrid-ranker pool.
        """
        try:
            kind = MemoryKind.EPISODIC
            salience = 0.45
            tags = ["story_event"]
            if event.arc_beat_id and self._arc_service is not None:
                arc = await self._arc_service.get_arc_by_beat(event.arc_beat_id)
                beat = arc.find_beat(event.arc_beat_id) if arc is not None else None
                if beat is not None:
                    kind, salience, tags = _arc_memory_shape(beat)
            item = MemoryItem.create(
                character_id=event.character_id,
                kind=kind,
                content=event.narrative,
                salience=salience,
                tags=tags,
                created_at=event.created_at,
            )
            embedded = await attach_embeddings([item], self._embedder)
            await self._memories.add_many(embedded)
            await self._events.mark_memorialized(event.id)
        except Exception:
            _LOGGER.exception(
                "story event memorialization failed event=%s", event.id,
            )

    async def _write_arc_completion_milestone(
        self,
        character: Character,
        arc: StoryArc,
    ) -> None:
        tag = f"arc_completion:{arc.id}"
        try:
            existing = await self._memories.query(arc.character_id, limit=80)
            if any(tag in memory.tags for memory in existing):
                return
            realized = arc.realized_history_beats(limit=5)
            if not realized:
                return
            content = await self._compose_arc_completion_memory(
                character=character,
                arc=arc,
                realized=tuple(realized),
            )
            if not content:
                return
            item = MemoryItem.create(
                character_id=arc.character_id,
                kind=MemoryKind.RELATIONSHIP_MILESTONE,
                content=content,
                salience=0.95,
                tags=["story_event", "arc_completion", tag],
                # Relationship-progression book-keeping — not a public post.
                audience=MEMORY_AUDIENCE_PRIVATE,
            )
            embedded = await attach_embeddings([item], self._embedder)
            await self._memories.add_many(embedded)
        except Exception:
            _LOGGER.exception("arc completion milestone write failed arc=%s", arc.id)

    async def _compose_arc_completion_memory(
        self,
        *,
        character: Character,
        arc: StoryArc,
        realized: tuple[StoryArcBeat, ...],
    ) -> str:
        writer = self._arc_completion_memory_writer
        if writer is None:
            return _fallback_arc_completion_memory(arc, realized)
        try:
            draft = await writer.write_memory(
                ArcCompletionMemoryContext(
                    character=character,
                    arc=arc,
                    realized_beats=realized,
                    operator_primary_language=await self._resolve_operator_language(
                        character,
                    ),
                ),
            )
        except Exception:
            _LOGGER.exception(
                "arc completion memory writer failed arc=%s",
                arc.id,
            )
            return _fallback_arc_completion_memory(arc, realized)
        content = (draft.content or "").strip()
        return content[:1200] if content else _fallback_arc_completion_memory(
            arc,
            realized,
        )

    async def _today_for_character(
        self, character: Character, now: datetime | None,
    ) -> date_type:
        when = now or datetime.now(timezone.utc)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.astimezone(
            await self._resolve_operator_timezone(character),
        ).date()


def _arc_memory_shape(beat: StoryArcBeat) -> tuple[MemoryKind, float, list[str]]:
    tags = ["story_event", "arc_beat", beat.tension]
    if beat.tension in {TENSION_CLIMAX, TENSION_RESOLUTION}:
        return MemoryKind.RELATIONSHIP_MILESTONE, 0.9, tags + ["arc_milestone"]
    salience_by_tension = {
        TENSION_SETUP: 0.5,
        TENSION_RISING: 0.65,
        TENSION_FALLING: 0.6,
    }
    return (
        MemoryKind.EPISODIC,
        salience_by_tension.get(beat.tension, 0.55),
        tags,
    )


def _fallback_arc_completion_memory(
    arc: StoryArc,
    realized: tuple[StoryArcBeat, ...],
) -> str:
    summary = "；".join(
        f"{beat.title}：{beat.summary}" for beat in realized[-3:]
    )
    return f"我們一起走完了《{arc.title}》：{summary}"
