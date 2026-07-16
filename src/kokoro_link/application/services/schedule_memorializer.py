"""Turn completed schedule activities into episodic memories.

Once an activity's ``end_at`` is in the past, we write a one-line
episodic memory so the character can later recall what they did —
"我今天下午在咖啡店寫了劇本大綱" — instead of only knowing their
current schedule block.

Design:

- Idempotent via the ``memorialized`` flag on ``ScheduleActivity``.
  Every run only processes activities that haven't been memorialized
  yet, so calling this after every turn is cheap.
- Yesterday's schedule is scanned too, to cover the case where the
  user didn't chat after an activity completed (e.g. overnight gap).
- Memories pass through the existing ``deduplicate`` filter before
  being persisted, preventing near-duplicates when the user edits a
  schedule and re-runs.
- Salience is derived from ``busy_score`` with a small floor so even
  very idle blocks ("睡覺") are still remembered — they carry
  relational value even if they're not exciting.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone, tzinfo

from kokoro_link.application.services.memory_embedding import attach_embeddings
from kokoro_link.contracts.activity_aftermath import (
    ActivityAftermath,
    ActivityAftermathPort,
)
from kokoro_link.contracts.embedder import EmbedderError, EmbedderPort
from kokoro_link.contracts.memory import MemoryRepositoryPort
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.contracts.schedule_repository import ScheduleRepositoryPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.schedule import (
    DailySchedule,
    OPERATOR_CONFIRMED_SHARED_ROLE,
    OPERATOR_INVITE_PENDING_ROLE,
    OPERATOR_WISH_ROLE,
    ScheduleActivity,
)
from kokoro_link.domain.value_objects.actor import ParticipantRef
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.domain.value_objects.timezone import timezone_for_id
from kokoro_link.infrastructure.localization.fallback_texts import (
    localized_fallback_text,
    localized_weekday_label,
)
from kokoro_link.infrastructure.memory.deduplicator import deduplicate_with_matches

_LOGGER = logging.getLogger(__name__)

_MEMORY_RECENT_POOL = 60


class ScheduleMemorializer:
    def __init__(
        self,
        *,
        schedule_repository: ScheduleRepositoryPort,
        memory_repository: MemoryRepositoryPort,
        local_tz: tzinfo,
        embedder: EmbedderPort | None = None,
        aftermath_port: ActivityAftermathPort | None = None,
        character_repository: CharacterRepositoryPort | None = None,
        operator_profile_service=None,  # noqa: ANN001 - optional owner timezone resolver
    ) -> None:
        self._schedule_repository = schedule_repository
        self._memory_repository = memory_repository
        self._local_tz = local_tz
        self._embedder = embedder
        # Aftermath is opt-in: a container missing either the port or
        # the character repo falls back to the bare-activity memory
        # path. The port speaks persona, so we need the character entity
        # — and we fetch it once per memorialize call (not per activity)
        # to keep token / DB cost bounded.
        self._aftermath_port = aftermath_port
        self._character_repository = character_repository
        self._operator_profile_service = operator_profile_service

    async def memorialize(
        self,
        *,
        character_id: str,
        now: datetime | None = None,
    ) -> int:
        """Scan today + yesterday for completed activities and write memories.

        Returns the number of memories newly written.
        """
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        character = await self._load_character(character_id)
        local_tz = await self._resolve_operator_timezone(character)
        local_today = moment.astimezone(local_tz).date()
        local_yesterday = local_today - timedelta(days=1)

        targets = []
        for civil in (local_today, local_yesterday):
            schedule = await self._schedule_repository.get(character_id, civil)
            if schedule is not None:
                targets.append(schedule)

        completed: list[tuple[DailySchedule, ScheduleActivity]] = []
        encounter_completed: list[tuple[DailySchedule, ScheduleActivity]] = []
        generic_completed: list[tuple[DailySchedule, ScheduleActivity]] = []
        for schedule in targets:
            for activity in schedule.activities:
                if activity.memorialized:
                    continue
                if activity.end_at >= moment:
                    continue
                completed.append((schedule, activity))
                if _is_encounter_activity(activity):
                    encounter_completed.append((schedule, activity))
                else:
                    generic_completed.append((schedule, activity))

        if not completed:
            return 0

        if not generic_completed:
            await self._mark_memorialized(encounter_completed)
            return 0

        memory_completed: list[tuple[DailySchedule, ScheduleActivity]] = []
        for schedule, activity in generic_completed:
            operator_role = _operator_involvement_role(activity)
            if operator_role in {OPERATOR_INVITE_PENDING_ROLE, OPERATOR_WISH_ROLE}:
                continue
            memory_completed.append((schedule, activity))

        if not memory_completed:
            await self._mark_memorialized(completed)
            return 0

        # Aftermath is per-activity but the character entity is shared,
        # so we load it once. Empty mapping (no port / no character / no
        # repo) is a valid signal: every activity falls back to the
        # bare-content memory below.
        aftermaths = await self._collect_aftermaths(
            character_id=character_id,
            activities=[activity for _, activity in memory_completed],
            character=character,
        )

        # Player-visible memory content follows the operator's content
        # language (plan #14): weekday label + location/companion wrappers
        # + residue parenthetical are all localised off this tag.
        language = await self._resolve_operator_language(character)

        candidate_pairs = [
            (
                activity,
                _activity_to_memory(
                    character_id=character_id,
                    activity=activity,
                    local_tz=local_tz,
                    aftermath=aftermaths.get(activity.id, ActivityAftermath()),
                    language=language,
                ),
            )
            for _, activity in memory_completed
        ]
        candidates = [candidate for _, candidate in candidate_pairs]

        activity_ids_by_memory_id = {
            candidate.id: activity.id for activity, candidate in candidate_pairs
        }

        try:
            existing = await self._memory_repository.query(
                character_id=character_id,
                limit=_MEMORY_RECENT_POOL,
            )
        except Exception:
            _LOGGER.exception("Failed to query existing memories for dedup")
            existing = []

        deduped = deduplicate_with_matches(candidates, existing)
        unique = deduped.kept
        has_memory_activity_ids = {
            activity_ids_by_memory_id[memory_id]
            for memory_id in deduped.duplicate_ids
            if memory_id in activity_ids_by_memory_id
        }

        if unique:
            try:
                embedded = await attach_embeddings(unique, self._embedder)
            except EmbedderError:
                # Fail-loud: do NOT write memorialised memories without
                # embeddings, and do NOT mark the activities as done —
                # the next turn will retry once the embedder is back.
                _LOGGER.exception(
                    "Embedder unavailable; deferring memorialisation of %d activit(y|ies)",
                    len(generic_completed),
                )
                await self._mark_memorialized(encounter_completed)
                return 0
            try:
                await self._memory_repository.add_many(embedded)
            except Exception:
                _LOGGER.exception("Failed to persist memorialised activities")
                await self._mark_memorialized(encounter_completed)
                return 0
            has_memory_activity_ids.update(
                activity_ids_by_memory_id[item.id]
                for item in unique
                if item.id in activity_ids_by_memory_id
            )

        # Mark activities as memorialized only after successful persist
        # so an embedder outage leaves them eligible for the next pass.
        await self._mark_memorialized(
            completed,
            has_memory_activity_ids=frozenset(has_memory_activity_ids),
        )

        return len(unique)

    async def _mark_memorialized(
        self,
        items: list[tuple[DailySchedule, ScheduleActivity]],
        *,
        has_memory_activity_ids: frozenset[str] = frozenset(),
    ) -> None:
        if not items:
            return
        dirty_schedules: dict[str, DailySchedule] = {}
        for schedule, activity in items:
            updated_activities = tuple(
                a.with_memory_state(
                    memorialized=True,
                    has_memory=a.id in has_memory_activity_ids,
                )
                if a.id == activity.id else a
                for a in dirty_schedules.get(schedule.id, schedule).activities
            )
            dirty_schedules[schedule.id] = schedule.with_activities(list(updated_activities))

        for schedule in dirty_schedules.values():
            try:
                await self._schedule_repository.save(schedule)
            except Exception:
                _LOGGER.exception("Failed to persist memorialised flag")

    async def _collect_aftermaths(
        self,
        *,
        character_id: str,
        activities: list[ScheduleActivity],
        character: Character | None = None,
    ) -> dict[str, ActivityAftermath]:
        """Run the aftermath port over each completed activity.

        Returns ``{activity_id: aftermath}`` for activities that produced
        a non-empty residue. Activities the port returned blank for, or
        that raised, are absent from the dict so the caller falls back
        to the bare-activity memory. Fail-soft at every layer — a flaky
        LLM must never block schedule history from being written."""
        if self._aftermath_port is None or self._character_repository is None:
            return {}
        character = character or await self._load_character(character_id)
        if character is None:
            return {}
        # Resolve the operator language once per memorialize call — the
        # residue is folded into player-visible memory content, so it must
        # follow the operator's content language (bug B2 class).
        operator_language = await self._resolve_operator_language(character)
        results: dict[str, ActivityAftermath] = {}
        for activity in activities:
            aftermath = await self._judge_aftermath(
                character=character,
                activity=activity,
                operator_primary_language=operator_language,
            )
            if aftermath is not None and not aftermath.is_empty:
                results[activity.id] = aftermath
        return results

    async def _judge_aftermath(
        self,
        *,
        character: Character,
        activity: ScheduleActivity,
        operator_primary_language: str = "zh-TW",
    ) -> ActivityAftermath | None:
        """Fail-soft single-activity wrapper around the port."""
        if self._aftermath_port is None:
            return None
        try:
            return await self._aftermath_port.judge(
                character=character,
                activity=activity,
                operator_primary_language=operator_primary_language,
            )
        except Exception:
            _LOGGER.exception(
                "Aftermath port crashed for activity %s; using bare memory",
                activity.id,
            )
            return None

    async def _load_character(self, character_id: str) -> Character | None:
        if self._character_repository is None:
            return None
        try:
            return await self._character_repository.get(character_id)
        except Exception:
            _LOGGER.exception("Failed to load character for schedule memorializer")
            return None

    async def _resolve_operator_timezone(self, character: Character | None) -> tzinfo:
        if character is None or self._operator_profile_service is None:
            return self._local_tz
        user_id = getattr(character, "user_id", None) or "default"
        try:
            operator = await self._operator_profile_service.get_for_user(user_id)
            return timezone_for_id(getattr(operator, "timezone_id", None))
        except Exception:  # pragma: no cover - defensive
            return self._local_tz

    async def _resolve_operator_language(self, character: Character | None) -> str:
        """Resolve the operator's content language for player-visible
        aftermath residue. Falls back to the ship-first ``zh-TW`` when no
        profile service is wired or resolution fails (legacy / tests)."""
        default = "zh-TW"
        if character is None or self._operator_profile_service is None:
            return default
        user_id = getattr(character, "user_id", None) or "default"
        try:
            operator = await self._operator_profile_service.get_for_user(user_id)
        except Exception:  # pragma: no cover - defensive
            return default
        if operator is None:
            return default
        lang = (getattr(operator, "primary_language", "") or "").strip()
        return lang or default


def _activity_to_memory(
    *,
    character_id: str,
    activity: ScheduleActivity,
    local_tz: tzinfo,
    aftermath: ActivityAftermath | None = None,
    language: str = "zh-TW",
) -> MemoryItem:
    local_start = activity.start_at.astimezone(local_tz)
    local_end = activity.end_at.astimezone(local_tz)
    weekday = localized_weekday_label(local_start.weekday(), language)
    time_range = f"{local_start.strftime('%H:%M')}-{local_end.strftime('%H:%M')}"
    location_part = (
        localized_fallback_text(
            "memory.schedule_location_prefix", language,
            location=activity.location,
        )
        if activity.location else ""
    )
    character_names = [
        ref.display_name
        for ref in activity.participant_refs
        if ref.actor_kind == "character" and ref.display_name
    ]
    companion_names = character_names or list(activity.companion_names)
    companions_part = (
        localized_fallback_text(
            "memory.schedule_companions", language,
            names="、".join(companion_names),
        )
        if companion_names else ""
    )
    semantic_part = f"{location_part}{activity.description}{companions_part}"
    content = localized_fallback_text(
        "memory.schedule_content", language,
        body=semantic_part,
        date=local_start.strftime("%Y-%m-%d"),
        weekday=weekday,
        time_range=time_range,
    )
    # Busy score influences how "memorable" the block feels — intense
    # activities earn higher salience. Clamp to [0.35, 0.8] so even
    # lazy moments have non-trivial recall but a deadline doesn't
    # crowd out everything else.
    salience = 0.35 + 0.45 * activity.busy_score
    tags: list[str] = ["schedule", activity.category]
    # Fold the LLM-judged emotional residue into the memory: residue
    # text gets appended in a "（情緒尾韻：…）" parenthetical so the
    # next chat's memory recall surfaces both the fact ("開會") and the
    # feeling ("被同事煩到頭痛") in one item. ``aftermath`` tag lets the
    # prompt builder promote fresh residues into a dedicated block.
    if aftermath is not None and not aftermath.is_empty:
        if aftermath.residue_summary.strip():
            content = localized_fallback_text(
                "memory.schedule_residue", language,
                content=content, residue=aftermath.residue_summary.strip(),
            )
            # Salience bump: residue means the LLM found this emotionally
            # notable. Capped by the same 0.8 ceiling so it doesn't
            # crowd out genuinely climactic memories.
            salience = min(0.85, salience + 0.1)
        tags.append("aftermath")
        emotion_tag = aftermath.emotion_tag.strip()
        if emotion_tag:
            tags.append(emotion_tag)
    npc_participants = tuple(
        ParticipantRef(
            actor_kind="npc",
            actor_id=None,
            display_name=name,
            role=None,
        )
        for name in activity.companion_names
    )
    participants = activity.participant_refs or npc_participants
    return MemoryItem.create(
        character_id=character_id,
        kind=MemoryKind.EPISODIC,
        content=content,
        salience=min(0.85, max(0.35, salience)),
        tags=tuple(tags),
        participants=participants,
        location=activity.location,
    )


def _is_encounter_activity(activity: ScheduleActivity) -> bool:
    return any(ref.role == "encounter_partner" for ref in activity.participant_refs)


def _operator_involvement_role(activity: ScheduleActivity) -> str | None:
    for ref in activity.participant_refs:
        if ref.actor_kind != "operator":
            continue
        if ref.role in {
            OPERATOR_CONFIRMED_SHARED_ROLE,
            OPERATOR_INVITE_PENDING_ROLE,
            OPERATOR_WISH_ROLE,
        }:
            return ref.role
    return None
