"""Planning, running and memorialising real character encounters."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone, tzinfo
from typing import Any

from kokoro_link.application.services.character_life_context import (
    CharacterLifeContext,
    CharacterLifeContextBuilder,
)
from kokoro_link.application.services.character_social_knowledge_service import (
    CharacterSocialKnowledgeService,
)
from kokoro_link.application.services.memory_embedding import attach_embeddings
from kokoro_link.infrastructure.memory.deduplicator import deduplicate
from kokoro_link.application.services.character_relationship_service import (
    CharacterRelationshipService,
)
from kokoro_link.application.services.feature_keys import (
    FEATURE_CHARACTER_ENCOUNTER_BEATS,
    FEATURE_CHARACTER_ENCOUNTER_DIALOGUE,
    FEATURE_CHARACTER_ENCOUNTER_PLAN,
    FEATURE_CHARACTER_ENCOUNTER_REFLECT,
)
from kokoro_link.contracts.novelty_gate import (
    NoveltyGateContext,
    NoveltyGatePort,
)
from kokoro_link.contracts.register_profile import (
    RegisterProfile,
    RegisterProfileContext,
    RegisterProfilePort,
)
from kokoro_link.contracts.reply_quality import ReplyDiversityEvidence
from kokoro_link.application.services.schedule_service import ScheduleService
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.character_encounter import CharacterEncounterRepositoryPort
from kokoro_link.contracts.character_encounter_intent import (
    CharacterEncounterIntentRepositoryPort,
)
from kokoro_link.contracts.character_relationship import (
    CharacterRelationshipRepositoryPort,
)
from kokoro_link.contracts.embedder import EmbedderPort
from kokoro_link.contracts.memory import MemoryRepositoryPort
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.contracts.schedule_repository import ScheduleRepositoryPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_encounter import (
    CharacterEncounter,
    EncounterLine,
)
from kokoro_link.domain.entities.character_relationship import CharacterRelationship
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity
from kokoro_link.domain.value_objects.actor import ParticipantRef
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.localization.fallback_texts import (
    localized_fallback_text,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompt.timing_utils import (
    format_relative_past_label,
    render_current_time_fact_lines,
)

_DEFAULT_OPERATOR_LANGUAGE = "zh-TW"


async def _resolve_owner_language(
    character: Character,
    operator_profile_service: Any | None,
) -> str:
    """Resolve the owning operator's content language for a character.

    Falls back to the ship-first ``zh-TW`` when no profile service is
    wired or resolution fails (legacy / tests). Mirrors the resolver in
    ``schedule_memorializer`` so player-visible encounter text follows
    the same language source of truth.
    """
    if operator_profile_service is None:
        return _DEFAULT_OPERATOR_LANGUAGE
    user_id = getattr(character, "user_id", None) or "default"
    try:
        operator = await operator_profile_service.get_for_user(user_id)
    except Exception:  # pragma: no cover - defensive
        return _DEFAULT_OPERATOR_LANGUAGE
    if operator is None:
        return _DEFAULT_OPERATOR_LANGUAGE
    lang = (getattr(operator, "primary_language", "") or "").strip()
    return lang or _DEFAULT_OPERATOR_LANGUAGE

_LOGGER = logging.getLogger(__name__)

_MIN_PAIR_GAP = timedelta(hours=6)
_MIN_SLOT = timedelta(minutes=15)
_MAX_SLOT = timedelta(minutes=45)
_INTENT_PLANNING_HORIZON = timedelta(days=2)
_ENCOUNTER_END_SENTINEL = "<END>"
_DEDUP_POOL_SIZE = 80
"""Existing-memory pool per owner for write-time dedup; mirrors the chat
post-turn pool size so both paths compare against the same window."""
_TOPIC_HISTORY_LIMIT = 3
"""Recent completed encounters injected as "already discussed" negative
examples — into the planner (anti-convergent trigger_reason), the
dialogue prompt, and the reflect/gate context. Small on purpose: the
point is steering away from last time's topic, not re-feeding history."""


@dataclass(frozen=True, slots=True)
class EncounterTickResult:
    planned: int = 0
    completed: int = 0
    failed: int = 0
    planned_ids: tuple[str, ...] = ()
    completed_ids: tuple[str, ...] = ()
    failed_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EncounterBeat:
    """One concrete topic beat for an encounter dialogue.

    ``carrier`` says who naturally brings it up ("a" / "b" / "both") so
    the per-line prompt can nudge the right speaker without hard-coding
    turn logic."""

    topic: str
    carrier: str = "both"
    note: str = ""


@dataclass(frozen=True, slots=True)
class ReflectionMemoryEntry:
    """One memory candidate from the reflect pass.

    ``salience`` is the LLM's own importance score (clamped by the
    writer per memory kind); ``None`` keeps the kind's legacy default.
    ``audience`` follows the chat post-turn contract: ``"private"``
    blocks the entry from public feed material, ``""`` = unclassified
    (legacy shareable)."""

    content: str
    salience: float | None = None
    audience: str = ""


@dataclass(frozen=True, slots=True)
class EncounterReflection:
    summary_for_a: str
    summary_for_b: str
    relationship_label: str | None = None
    how_a_sees_b: str | None = None
    how_b_sees_a: str | None = None
    affection_a_delta: int = 0
    affection_b_delta: int = 0
    trust_a_delta: int = 0
    trust_b_delta: int = 0
    summary_salience_a: float | None = None
    summary_salience_b: float | None = None
    summary_audience_a: str = ""
    summary_audience_b: str = ""
    hearsay_for_a: tuple[ReflectionMemoryEntry, ...] = field(default_factory=tuple)
    hearsay_for_b: tuple[ReflectionMemoryEntry, ...] = field(default_factory=tuple)
    peer_facts_for_a: tuple[ReflectionMemoryEntry, ...] = field(default_factory=tuple)
    peer_facts_for_b: tuple[ReflectionMemoryEntry, ...] = field(default_factory=tuple)


class CharacterEncounterPlanner:
    def __init__(
        self,
        *,
        relationship_repository: CharacterRelationshipRepositoryPort,
        encounter_repository: CharacterEncounterRepositoryPort,
        character_repository: CharacterRepositoryPort,
        schedule_service: ScheduleService,
        schedule_repository: ScheduleRepositoryPort,
        provider: ActiveLLMProviderPort,
        local_tz: tzinfo,
        intent_repository: CharacterEncounterIntentRepositoryPort | None = None,
        operator_profile_service: Any | None = None,
    ) -> None:
        self._relationships = relationship_repository
        self._encounters = encounter_repository
        self._characters = character_repository
        self._schedule_service = schedule_service
        self._schedule_repository = schedule_repository
        self._provider = provider
        self._local_tz = local_tz
        self._intents = intent_repository
        self._operator_profile_service = operator_profile_service

    async def plan_due(self, *, now: datetime | None = None) -> list[CharacterEncounter]:
        moment = _as_utc(now or datetime.now(timezone.utc))
        planned: list[CharacterEncounter] = []
        for relationship in await self._relationships.list_enabled():
            try:
                encounter = await self._maybe_plan_pair(relationship, now=moment)
            except Exception:
                _LOGGER.exception(
                    "encounter planner failed relationship=%s", relationship.id,
                )
                continue
            if encounter is not None:
                planned.append(encounter)
        return planned

    async def _maybe_plan_pair(
        self,
        relationship: CharacterRelationship,
        *,
        now: datetime,
    ) -> CharacterEncounter | None:
        if await self._encounters.has_pending_for_relationship(relationship.id):
            return None
        if relationship.last_interaction_at is not None:
            if now - _as_utc(relationship.last_interaction_at) < _MIN_PAIR_GAP:
                return None

        char_a = await self._characters.get(relationship.character_a_id)
        char_b = await self._characters.get(relationship.character_b_id)
        if char_a is None or char_b is None:
            return None
        # A frozen character halts all background activity (CHARACTER_FREEZE_PLAN):
        # never plan a new encounter that would involve one.
        if char_a.frozen or char_b.frozen:
            return None
        language = await _resolve_owner_language(
            char_a, self._operator_profile_service,
        )
        pending_intent = await self._pending_intent_for_pair(
            relationship,
            now=now,
        )

        local_tz = await self._timezone_for_character(char_a)
        search_floor = now
        if pending_intent is not None:
            search_floor = max(now, pending_intent.desired_after - timedelta(minutes=10))
        start_date = search_floor.astimezone(local_tz).date()
        schedules_a = await self._schedule_service.ensure_window(
            char_a, start=start_date, days=2,
        )
        schedules_b = await self._schedule_service.ensure_window(
            char_b, start=start_date, days=2,
        )
        slot = _first_shared_low_busy_slot(
            schedules_a,
            schedules_b,
            now=search_floor,
            local_tz=local_tz,
        )
        if slot is None:
            return None
        start_at, end_at, hint_location = slot
        recent_topic_lines = await self._recent_pair_topic_lines(
            relationship, now=now,
        )
        decision = await self._ask_llm_for_plan(
            relationship=relationship,
            char_a=char_a,
            char_b=char_b,
            start_at=start_at,
            end_at=end_at,
            hint_location=hint_location,
            pending_intent=pending_intent,
            language=language,
            recent_topic_lines=recent_topic_lines,
        )
        if decision is None or not decision.should_plan:
            return None
        scheduled_for = decision.scheduled_for or start_at
        if scheduled_for < now:
            return None
        if not _slot_is_valid(schedules_a, scheduled_for, decision.end_at):
            return None
        if not _slot_is_valid(schedules_b, scheduled_for, decision.end_at):
            return None

        encounter = CharacterEncounter.plan(
            relationship_id=relationship.id,
            character_a_id=relationship.character_a_id,
            character_b_id=relationship.character_b_id,
            scheduled_for=scheduled_for,
            location=(
                decision.location
                or hint_location
                or localized_fallback_text("encounter.default_location", language)
            ),
            trigger_reason=(
                decision.reason
                or localized_fallback_text(
                    "encounter.default_trigger_reason", language,
                )
            ),
            max_turns=decision.max_turns,
        )
        await self._encounters.save(encounter)
        if pending_intent is not None and self._intents is not None:
            await self._intents.save(pending_intent.mark_consumed(at=now))
        await self._write_schedule_activity(
            schedule=_schedule_for(schedules_a, scheduled_for, local_tz) or schedules_a[0],
            actor=char_a,
            other=char_b,
            encounter=encounter,
            end_at=decision.end_at,
            language=language,
        )
        await self._write_schedule_activity(
            schedule=_schedule_for(schedules_b, scheduled_for, local_tz) or schedules_b[0],
            actor=char_b,
            other=char_a,
            encounter=encounter,
            end_at=decision.end_at,
            language=language,
        )
        return encounter

    async def _pending_intent_for_pair(
        self,
        relationship: CharacterRelationship,
        *,
        now: datetime,
    ):
        if self._intents is None:
            return None
        return await self._intents.find_pending_for_pair(
            relationship.character_a_id,
            relationship.character_b_id,
            now=now,
            horizon=now + _INTENT_PLANNING_HORIZON,
        )

    async def _recent_pair_topic_lines(
        self,
        relationship: CharacterRelationship,
        *,
        now: datetime,
    ) -> list[str]:
        """"Already discussed" lines for the plan prompt.

        Static planner inputs converge on the same trigger_reason every
        time; feeding back what the pair actually talked about recently
        lets the LLM either skip a contentless meetup or steer the
        reason somewhere new."""
        try:
            recent = await self._encounters.list_for_relationship(
                relationship.id, limit=_TOPIC_HISTORY_LIMIT + 5,
            )
        except Exception:
            _LOGGER.exception(
                "encounter plan history lookup failed relationship=%s",
                relationship.id,
            )
            return []
        completed = [
            item for item in recent if item.status == "completed"
        ][:_TOPIC_HISTORY_LIMIT]
        return _render_topic_history_lines(completed, speaker_is_a=True, now=now)

    async def _timezone_for_character(self, character: Character) -> tzinfo:
        resolver = getattr(self._schedule_service, "timezone_for_character", None)
        if resolver is None:
            return self._local_tz
        try:
            return await resolver(character)
        except Exception:
            _LOGGER.exception(
                "encounter planner: timezone lookup failed character=%s",
                character.id,
            )
            return self._local_tz

    async def _ask_llm_for_plan(
        self,
        *,
        relationship: CharacterRelationship,
        char_a: Character,
        char_b: Character,
        start_at: datetime,
        end_at: datetime,
        hint_location: str | None,
        pending_intent: Any | None = None,
        language: str = _DEFAULT_OPERATOR_LANGUAGE,
        recent_topic_lines: list[str] | None = None,
    ) -> "_PlanDecision | None":
        if await self._provider.is_fake(
            FEATURE_CHARACTER_ENCOUNTER_PLAN, character=char_a,
        ):
            return _PlanDecision(
                should_plan=True,
                scheduled_for=start_at,
                end_at=end_at,
                location=hint_location or localized_fallback_text(
                    "encounter.default_location", language,
                ),
                reason=(
                    f"聊天約定：{pending_intent.topic}"
                    if pending_intent is not None
                    else localized_fallback_text(
                        "encounter.fake_reason_planned", language,
                    )
                ),
                max_turns=6 if pending_intent is not None else 4,
            )
        perspective_a = relationship.perspective_for(char_a.id)
        perspective_b = relationship.perspective_for(char_b.id)
        last_seen = (
            _as_utc(relationship.last_interaction_at).isoformat()
            if relationship.last_interaction_at is not None
            else "尚無"
        )
        intent_lines = ""
        if pending_intent is not None:
            intent_lines = (
                "待辦聊天約定：\n"
                f"- 最早時間：{pending_intent.desired_after.isoformat()}\n"
                f"- 話題：{pending_intent.topic}\n"
                f"- 原話：{pending_intent.source_text or '未提供'}\n"
                "這類明確約定應高度傾向安排，但仍不可打斷高忙碌行程。\n"
            )
        history_lines = ""
        if recent_topic_lines:
            history_lines = (
                "這對角色最近碰面聊過的內容（時間已標註）：\n"
                + "\n".join(recent_topic_lines)
                + "\n若沒有新話題或新進展，should_plan 應保守給 false；"
                "若仍值得碰面，reason 必須與上述明顯不同，不要重複同一件事。\n"
            )
        model = await self._provider.resolve(
            FEATURE_CHARACTER_ENCOUNTER_PLAN, character=char_a,
        )
        model_id = await self._provider.resolve_model_id(
            FEATURE_CHARACTER_ENCOUNTER_PLAN, character=char_a,
        )
        language_hint = render_operator_language_hint(language)
        language_line = f"{language_hint}\n" if language_hint else ""
        prompt = (
            f"{language_line}"
            "你是角色生活行程協調器。只判斷這對已白名單角色是否應在既有行程中自然短暫碰面。\n"
            "不要創造重大事件，不要打斷高忙碌活動。頻率請由語意判斷："
            "個性外向/熟識/有明確約定可較常見面；疏離或無話題則保守。\n"
            "max_turns 請依關係親密度、實質話題或約定決定 4-8；普通寒暄用 4。\n"
            "輸出 JSON："
            '{"should_plan": boolean, "start_iso": "...", "end_iso": "...", '
            '"location": "...", "reason": "...", "max_turns": 4-8}\n\n'
            f"角色A：{char_a.name}\n摘要：{char_a.summary}\n"
            f"個性：{', '.join(char_a.personality) or '未設定'}\n"
            f"說話風格：{char_a.speaking_style or '未設定'}\n"
            f"興趣：{', '.join(char_a.interests) or '未設定'}\n"
            f"角色B：{char_b.name}\n摘要：{char_b.summary}\n"
            f"個性：{', '.join(char_b.personality) or '未設定'}\n"
            f"說話風格：{char_b.speaking_style or '未設定'}\n"
            f"興趣：{', '.join(char_b.interests) or '未設定'}\n"
            f"關係：{relationship.relationship_label or '未標註'}\n"
            f"A看B：{relationship.how_a_sees_b or '未整理'}\n"
            f"B看A：{relationship.how_b_sees_a or '未整理'}\n"
            f"A對B親近/信任：affection={perspective_a.affection_self_to_peer}, "
            f"trust={perspective_a.trust_self_to_peer}\n"
            f"B對A親近/信任：affection={perspective_b.affection_self_to_peer}, "
            f"trust={perspective_b.trust_self_to_peer}\n"
            f"上次碰面：{last_seen}\n"
            f"{intent_lines}"
            f"{history_lines}"
            f"可用低忙碌時段：{start_at.isoformat()} 到 {end_at.isoformat()}\n"
            f"地點線索：{hint_location or '無'}\n"
        )
        raw = await model.generate(prompt, model=model_id)
        payload = _json_object(raw)
        if payload is None:
            return None
        return _PlanDecision.from_payload(
            payload, fallback_start=start_at, fallback_end=end_at,
        )

    async def _write_schedule_activity(
        self,
        *,
        schedule: DailySchedule,
        actor: Character,
        other: Character,
        encounter: CharacterEncounter,
        end_at: datetime,
        language: str = _DEFAULT_OPERATOR_LANGUAGE,
    ) -> None:
        activity = ScheduleActivity.create(
            start_at=encounter.scheduled_for,
            end_at=end_at,
            description=localized_fallback_text(
                "encounter.schedule_activity", language, name=other.name,
            ),
            category="social",
            location=encounter.location,
            busy_score=0.25,
            participant_refs=(
                ParticipantRef(
                    actor_kind="character",
                    actor_id=other.id,
                    display_name=other.name,
                    role="encounter_partner",
                ),
            ),
        )
        updated = schedule.with_activities([*schedule.activities, activity])
        await self._schedule_repository.save(updated)


class CharacterEncounterRunner:
    def __init__(
        self,
        *,
        encounter_repository: CharacterEncounterRepositoryPort,
        character_repository: CharacterRepositoryPort,
        memory_writer: "CharacterEncounterMemoryWriter",
        relationship_service: CharacterRelationshipService,
        provider: ActiveLLMProviderPort,
        social_knowledge_service: CharacterSocialKnowledgeService | None = None,
        schedule_service: ScheduleService | None = None,
        local_tz: tzinfo = timezone.utc,
        operator_profile_service: Any | None = None,
        life_context_builder: CharacterLifeContextBuilder | None = None,
        register_profiler: RegisterProfilePort | None = None,
        novelty_gate: NoveltyGatePort | None = None,
    ) -> None:
        self._encounters = encounter_repository
        self._characters = character_repository
        self._memory_writer = memory_writer
        self._relationships = relationship_service
        self._provider = provider
        self._social_knowledge = social_knowledge_service
        self._schedule_service = schedule_service
        self._local_tz = local_tz
        self._operator_profile_service = operator_profile_service
        self._life_context_builder = life_context_builder
        self._register_profiler = register_profiler
        self._novelty_gate = novelty_gate

    async def run_due(
        self, *, now: datetime | None = None,
    ) -> tuple[list[str], list[str]]:
        moment = _as_utc(now or datetime.now(timezone.utc))
        completed: list[str] = []
        failed: list[str] = []
        for encounter in await self._encounters.list_runnable(moment):
            result = await self.run(encounter.id, now=moment)
            if result.status == "completed":
                completed.append(result.id)
            elif result.status == "failed":
                failed.append(result.id)
        return completed, failed

    async def run(
        self,
        encounter_id: str,
        *,
        now: datetime | None = None,
    ) -> CharacterEncounter:
        moment = _as_utc(now or datetime.now(timezone.utc))
        encounter = await self._encounters.get(encounter_id)
        if encounter is None:
            raise ValueError("Encounter not found")
        if encounter.status not in {"planned", "running"}:
            return encounter
        running = encounter.mark_running(at=moment)
        await self._encounters.save(running)
        char_a = await self._characters.get(running.character_a_id)
        char_b = await self._characters.get(running.character_b_id)
        if char_a is None or char_b is None:
            failed = running.fail("character not found", at=moment)
            await self._encounters.save(failed)
            return failed
        # A character frozen after this encounter was planned must not incur
        # the transcript/reflection LLM cost (CHARACTER_FREEZE_PLAN). Fail it
        # gracefully; a fresh encounter can be planned once it unfreezes.
        if char_a.frozen or char_b.frozen:
            failed = running.fail("character frozen", at=moment)
            await self._encounters.save(failed)
            return failed
        language = await _resolve_owner_language(
            char_a, self._operator_profile_service,
        )
        try:
            # The completed-pair history is read-only for the whole run
            # (the current encounter only completes at the very end), so
            # fetch it once and thread it through context assembly and
            # both quality gates instead of re-querying three times.
            pair_history = await self._completed_pair_history(running)
            speaker_contexts = await self._speaker_contexts(
                char_a, char_b, now=moment, encounter=running,
                history=pair_history,
            )
            beats = await self._plan_topic_beats(
                running,
                char_a,
                char_b,
                speaker_contexts=speaker_contexts,
                language=language,
            )
            register_profile = await self._profile_register(
                running, char_a, char_b, beats=beats,
            )
            transcript = await self._generate_transcript(
                running,
                char_a,
                char_b,
                language=language,
                speaker_contexts=speaker_contexts,
                beats=beats,
                register_profile=register_profile,
            )
            transcript = await self._gate_transcript(
                running,
                char_a,
                char_b,
                transcript,
                speaker_contexts=speaker_contexts,
                beats=beats,
                register_profile=register_profile,
                language=language,
                now=moment,
                history=pair_history,
            )
            reflection = await self._reflect(
                running,
                char_a,
                char_b,
                transcript,
                language=language,
                speaker_contexts=speaker_contexts,
            )
            reflection = await self._gate_reflection(
                running,
                char_a,
                char_b,
                transcript,
                reflection,
                speaker_contexts=speaker_contexts,
                language=language,
                now=moment,
                history=pair_history,
            )
            memory_ids = await self._memory_writer.write(
                encounter=running,
                char_a=char_a,
                char_b=char_b,
                transcript=transcript,
                reflection=reflection,
            )
            await self._relationships.apply_reflection(
                running.relationship_id,
                affection_a_delta=reflection.affection_a_delta,
                affection_b_delta=reflection.affection_b_delta,
                trust_a_delta=reflection.trust_a_delta,
                trust_b_delta=reflection.trust_b_delta,
                how_a_sees_b=reflection.how_a_sees_b,
                how_b_sees_a=reflection.how_b_sees_a,
                interacted_at=moment,
            )
            completed = running.complete(
                transcript=transcript,
                summary_for_a=reflection.summary_for_a,
                summary_for_b=reflection.summary_for_b,
                memory_ids=memory_ids,
                at=moment,
            )
        except Exception as exc:
            failed = running.fail(str(exc), at=moment)
            await self._encounters.save(failed)
            return failed
        await self._encounters.save(completed)
        return completed

    async def _plan_topic_beats(
        self,
        encounter: CharacterEncounter,
        char_a: Character,
        char_b: Character,
        *,
        speaker_contexts: dict[str, list[str]],
        language: str = _DEFAULT_OPERATOR_LANGUAGE,
    ) -> tuple[EncounterBeat, ...]:
        """Pick 1-3 concrete topic beats before the dialogue starts.

        Without a beat plan the per-line model orbits the trigger reason
        for the whole meetup; with one, the dialogue has a direction
        grounded in whichever fresh material (own life, operator news,
        world events) the speakers actually have. Fail-soft: any error
        falls back to a single trigger-reason beat, which is exactly the
        pre-beats behaviour."""
        fallback = EncounterBeat(
            topic=(
                encounter.trigger_reason
                or localized_fallback_text(
                    "encounter.default_trigger_reason", language,
                )
            ),
        )
        if await self._provider.is_fake(
            FEATURE_CHARACTER_ENCOUNTER_BEATS, character=char_a,
        ):
            return (fallback,)
        try:
            model = await self._provider.resolve(
                FEATURE_CHARACTER_ENCOUNTER_BEATS, character=char_a,
            )
            model_id = await self._provider.resolve_model_id(
                FEATURE_CHARACTER_ENCOUNTER_BEATS, character=char_a,
            )
            language_hint = render_operator_language_hint(language)
            language_line = f"{language_hint}\n" if language_hint else ""
            prompt = (
                f"{language_line}"
                "你是碰面話題設計器。根據兩位角色各自的近況與關係，"
                "為這次短暫碰面挑 1-3 個具體話題節拍。\n"
                "規則：\n"
                "- 優先從各自「自己最近的生活」與「主人相關」中挑新鮮、具體、"
                "兩人聊得起來的事；\n"
                "- 「最近幾次碰面已聊過」清單中的話題，除非有明確新進展，"
                "否則不要再選；\n"
                "- 話題要符合兩人關係深淺與此刻情境，不要創造重大事件。\n"
                "輸出 JSON："
                '{"beats": [{"topic": "...", "carrier": "a|b|both", '
                '"note": "一句話說明怎麼自然展開"}]}\n\n'
                f"碰面情境：地點={encounter.location}；"
                f"原因={encounter.trigger_reason}\n"
                f"{_character_profile_block('A', char_a)}\n"
                f"{_character_profile_block('B', char_b)}\n"
                "A 的脈絡：\n"
                + "\n".join(speaker_contexts.get(char_a.id, []) or ["- （無）"])
                + "\nB 的脈絡：\n"
                + "\n".join(speaker_contexts.get(char_b.id, []) or ["- （無）"])
            )
            payload = _json_object(await model.generate(prompt, model=model_id))
        except Exception:
            _LOGGER.exception(
                "encounter beats planning failed encounter=%s",
                getattr(encounter, "id", None),
            )
            return (fallback,)
        if payload is None:
            return (fallback,)
        beats: list[EncounterBeat] = []
        raw_beats = payload.get("beats")
        if isinstance(raw_beats, list):
            for raw in raw_beats[:3]:
                if not isinstance(raw, dict):
                    continue
                topic = _str(raw.get("topic"))
                if not topic:
                    continue
                carrier = _str(raw.get("carrier")).lower()
                beats.append(
                    EncounterBeat(
                        topic=topic,
                        carrier=carrier if carrier in {"a", "b", "both"} else "both",
                        note=_str(raw.get("note")),
                    ),
                )
        return tuple(beats) or (fallback,)

    async def _profile_register(
        self,
        encounter: CharacterEncounter,
        char_a: Character,
        char_b: Character,
        *,
        beats: tuple[EncounterBeat, ...] = (),
    ) -> RegisterProfile | None:
        """Per-encounter register profile (one call, not per line).

        Same pattern as the proactive dispatcher: no user message
        exists, so the meetup situation is the context anchor. Fail-soft
        to ``None`` = neutral register."""
        if self._register_profiler is None:
            return None
        anchor = (
            f"{char_a.name} 與 {char_b.name} 在 {encounter.location} 碰面："
            f"{encounter.trigger_reason}"
        )
        context = RegisterProfileContext(
            character_id=char_a.id,
            operator_id=getattr(char_a, "user_id", None) or "default",
            latest_user_message=anchor,
            recent_dialogue_summary="；".join(beat.topic for beat in beats),
        )
        try:
            return await self._register_profiler.profile(
                context, character=char_a,
            )
        except Exception:
            _LOGGER.exception(
                "encounter register profiling failed encounter=%s",
                getattr(encounter, "id", None),
            )
            return None

    async def _generate_transcript(
        self,
        encounter: CharacterEncounter,
        char_a: Character,
        char_b: Character,
        *,
        language: str = _DEFAULT_OPERATOR_LANGUAGE,
        speaker_contexts: dict[str, list[str]] | None = None,
        beats: tuple[EncounterBeat, ...] = (),
        register_profile: RegisterProfile | None = None,
        retry_directive: str = "",
    ) -> tuple[EncounterLine, ...]:
        turns = max(2, min(8, encounter.max_turns))
        if await self._provider.is_fake(
            FEATURE_CHARACTER_ENCOUNTER_DIALOGUE, character=char_a,
        ):
            return tuple(
                EncounterLine(
                    speaker_character_id=char_a.id if idx % 2 == 0 else char_b.id,
                    text=localized_fallback_text(
                        "encounter.fake_line",
                        language,
                        speaker=char_a.name if idx % 2 == 0 else char_b.name,
                        location=encounter.location,
                    ),
                )
                for idx in range(turns)
            )
        model = await self._provider.resolve(
            FEATURE_CHARACTER_ENCOUNTER_DIALOGUE, character=char_a,
        )
        model_id = await self._provider.resolve_model_id(
            FEATURE_CHARACTER_ENCOUNTER_DIALOGUE, character=char_a,
        )
        lines: list[EncounterLine] = []
        if speaker_contexts is None:
            speaker_contexts = await self._speaker_contexts(
                char_a, char_b, encounter=encounter,
            )
        local_tz = await self._timezone_for_character(char_a)
        time_context = _encounter_time_context(encounter, local_tz)
        language_hint = render_operator_language_hint(language)
        language_line = f"{language_hint}\n" if language_hint else ""
        beats_block = _render_beats_block(beats, char_a=char_a, char_b=char_b)
        register_block = "\n".join(_encounter_register_lines(register_profile))
        retry_block = (
            f"上一次整段對話被品質檢查退回：{retry_directive}\n"
            "這次務必避開上述問題，帶出具體的新內容，不要換句話重講。\n"
            if retry_directive.strip()
            else ""
        )
        for idx in range(turns):
            speaker = char_a if idx % 2 == 0 else char_b
            other = char_b if speaker.id == char_a.id else char_a
            prompt = (
                f"{language_line}"
                "你是角色短對話器。只輸出當前 speaker 的一句自然台詞，不要旁白，不要 JSON。\n"
                "用 speaker 自己的個性、關係史與已知資訊自然回應；不要背誦設定。\n"
                f"如果這一句自然結束整段碰面，請在句末附上 {_ENCOUNTER_END_SENTINEL}；"
                "否則不要輸出這個標記。標記不是台詞內容。\n"
                f"{retry_block}"
                f"{register_block}\n"
                f"地點：{encounter.location}\n原因：{encounter.trigger_reason}\n"
                f"{time_context}"
                f"{beats_block}"
                f"{_character_profile_block('speaker', speaker)}\n"
                f"{_character_profile_block('對方', other)}\n"
                "speaker 對這次碰面可用的脈絡：\n"
                + "\n".join(speaker_contexts.get(speaker.id, []) or ["- （無額外脈絡）"])
                + "\n"
                "已說過：\n"
                + "\n".join(
                    f"{char_a.name if line.speaker_character_id == char_a.id else char_b.name}: {line.text}"
                    for line in lines
                )
            )
            raw = await model.generate(prompt, model=model_id)
            text, should_end = _clean_generated_line(raw)
            lines.append(
                EncounterLine(
                    speaker_character_id=speaker.id,
                    text=text,
                ),
            )
            if len(lines) >= 2 and should_end:
                break
        return tuple(lines)

    async def _gate_transcript(
        self,
        encounter: CharacterEncounter,
        char_a: Character,
        char_b: Character,
        transcript: tuple[EncounterLine, ...],
        *,
        speaker_contexts: dict[str, list[str]],
        beats: tuple[EncounterBeat, ...],
        register_profile: RegisterProfile | None,
        language: str,
        now: datetime,
        history: list[CharacterEncounter] | None = None,
    ) -> tuple[EncounterLine, ...]:
        """Player-visible quality gate over the whole transcript.

        Same shape as the proactive/feed gates: evaluate once after
        composition, on failure regenerate once with the verdict
        feedback, keep the original on a second failure. Always
        fail-open — a broken judge never blocks a background encounter.
        """
        if self._novelty_gate is None or not transcript:
            return transcript
        if history is None:
            history = await self._completed_pair_history(encounter)
        rendered = _render_transcript(transcript, char_a=char_a, char_b=char_b)
        context = NoveltyGateContext(
            character_id=char_a.id,
            operator_id=getattr(char_a, "user_id", None) or "default",
            response_text=rendered,
            known_material=tuple(
                speaker_contexts.get(char_a.id, [])
                + speaker_contexts.get(char_b.id, []),
            )[:40],
            recent_self_lines=tuple(
                line for line in _render_topic_history_lines(
                    history, speaker_is_a=True, now=now,
                )
            ),
            latest_user_message=(
                f"{char_a.name} 與 {char_b.name} 在 {encounter.location} 碰面："
                f"{encounter.trigger_reason}"
            ),
            register_profile=register_profile,
            diversity_evidence=_encounter_diversity_evidence(history, now=now),
            persona_context=(
                _character_profile_block("A", char_a),
                _character_profile_block("B", char_b),
            ),
        )
        try:
            verdict = await self._novelty_gate.evaluate(
                context, character=char_a,
            )
        except Exception:
            _LOGGER.exception(
                "encounter transcript gate failed encounter=%s",
                getattr(encounter, "id", None),
            )
            return transcript
        if verdict is None or verdict.passes:
            return transcript
        feedback = verdict.feedback or "內容與近期碰面重複或流於形式"
        _LOGGER.info(
            "encounter transcript gated encounter=%s feedback=%s",
            getattr(encounter, "id", None),
            feedback,
        )
        try:
            retry = await self._generate_transcript(
                encounter,
                char_a,
                char_b,
                language=language,
                speaker_contexts=speaker_contexts,
                beats=beats,
                register_profile=register_profile,
                retry_directive=feedback,
            )
        except Exception:
            _LOGGER.exception(
                "encounter transcript regen failed encounter=%s",
                getattr(encounter, "id", None),
            )
            return transcript
        return retry or transcript

    async def _gate_reflection(
        self,
        encounter: CharacterEncounter,
        char_a: Character,
        char_b: Character,
        transcript: tuple[EncounterLine, ...],
        reflection: EncounterReflection,
        *,
        speaker_contexts: dict[str, list[str]] | None,
        language: str,
        now: datetime,
        history: list[CharacterEncounter] | None = None,
    ) -> EncounterReflection:
        """Gate the player-visible summaries against recent-encounter
        repetition — the direct counter to "聊到亮亮的東西" five times in
        a row. On failure, re-reflect once with the feedback; keep the
        original on a second failure. Always fail-open."""
        if self._novelty_gate is None:
            return reflection
        summary_text = "\n".join(
            part for part in (
                reflection.summary_for_a.strip(),
                reflection.summary_for_b.strip(),
            ) if part
        )
        if not summary_text:
            return reflection
        if history is None:
            history = await self._completed_pair_history(encounter)
        if not history:
            # Nothing to repeat against — first meetups always pass.
            return reflection
        context = NoveltyGateContext(
            character_id=char_a.id,
            operator_id=getattr(char_a, "user_id", None) or "default",
            response_text=summary_text,
            known_material=tuple(
                _render_topic_history_lines(history, speaker_is_a=True, now=now)
                + _render_topic_history_lines(history, speaker_is_a=False, now=now),
            ),
            latest_user_message=(
                f"這是 {char_a.name} 與 {char_b.name} 本次碰面的記憶摘要；"
                "與 known_material 中近期碰面摘要高度雷同即視為缺乏新意。"
            ),
            diversity_evidence=_encounter_diversity_evidence(history, now=now),
        )
        try:
            verdict = await self._novelty_gate.evaluate(
                context, character=char_a,
            )
        except Exception:
            _LOGGER.exception(
                "encounter summary gate failed encounter=%s",
                getattr(encounter, "id", None),
            )
            return reflection
        if verdict is None or verdict.passes:
            return reflection
        feedback = verdict.feedback or "摘要與近期碰面重複"
        _LOGGER.info(
            "encounter summary gated encounter=%s feedback=%s",
            getattr(encounter, "id", None),
            feedback,
        )
        try:
            retry = await self._reflect(
                encounter,
                char_a,
                char_b,
                transcript,
                language=language,
                speaker_contexts=speaker_contexts,
                retry_directive=feedback,
            )
        except Exception:
            _LOGGER.exception(
                "encounter re-reflect failed encounter=%s",
                getattr(encounter, "id", None),
            )
            return reflection
        return retry

    async def _reflect(
        self,
        encounter: CharacterEncounter,
        char_a: Character,
        char_b: Character,
        transcript: tuple[EncounterLine, ...],
        *,
        language: str = _DEFAULT_OPERATOR_LANGUAGE,
        speaker_contexts: dict[str, list[str]] | None = None,
        retry_directive: str = "",
    ) -> EncounterReflection:
        if await self._provider.is_fake(
            FEATURE_CHARACTER_ENCOUNTER_REFLECT, character=char_a,
        ):
            return EncounterReflection(
                summary_for_a=localized_fallback_text(
                    "encounter.summary_met", language,
                    location=encounter.location, name=char_b.name,
                ),
                summary_for_b=localized_fallback_text(
                    "encounter.summary_met", language,
                    location=encounter.location, name=char_a.name,
                ),
                trust_a_delta=1,
                trust_b_delta=1,
                peer_facts_for_a=(
                    ReflectionMemoryEntry(
                        content=localized_fallback_text(
                            "encounter.peer_fact_seen_here", language,
                            name=char_b.name, location=encounter.location,
                        ),
                    ),
                ),
                peer_facts_for_b=(
                    ReflectionMemoryEntry(
                        content=localized_fallback_text(
                            "encounter.peer_fact_seen_here", language,
                            name=char_a.name, location=encounter.location,
                        ),
                    ),
                ),
            )
        model = await self._provider.resolve(
            FEATURE_CHARACTER_ENCOUNTER_REFLECT, character=char_a,
        )
        model_id = await self._provider.resolve_model_id(
            FEATURE_CHARACTER_ENCOUNTER_REFLECT, character=char_a,
        )
        rendered = "\n".join(
            f"{char_a.name if line.speaker_character_id == char_a.id else char_b.name}: {line.text}"
            for line in transcript
        )
        if speaker_contexts is None:
            speaker_contexts = await self._speaker_contexts(
                char_a, char_b, encounter=encounter,
            )
        language_hint = render_operator_language_hint(language)
        language_line = f"{language_hint}\n" if language_hint else ""
        reflect_retry_block = (
            f"上一次的摘要被品質檢查退回：{retry_directive}\n"
            "這次請從不同角度整理，聚焦這場對話中真正新的資訊與進展。\n"
            if retry_directive.strip()
            else ""
        )
        prompt = (
            f"{language_line}"
            f"{reflect_retry_block}"
            "把角色短對話整理成結構化反思。共同親見的事寫進 summary；"
            "談到使用者或第三角色的主觀看法只放 hearsay，不可改寫成事實。\n"
            "輸出 JSON："
            '{"summary_for_a": {"content": "...", "salience": 0.5, "audience": "shareable"}, '
            '"summary_for_b": {"content": "...", "salience": 0.5, "audience": "shareable"}, '
            '"how_a_sees_b": "", "how_b_sees_a": "", '
            '"affection_a_delta": 0, "affection_b_delta": 0, '
            '"trust_a_delta": 0, "trust_b_delta": 0, '
            '"hearsay_for_a": [{"content": "...", "salience": 0.4, "audience": "private"}], '
            '"hearsay_for_b": [...], '
            '"peer_facts_for_a": [{"content": "...", "salience": 0.55, "audience": "shareable"}], '
            '"peer_facts_for_b": [...]}\n\n'
            "記憶品質規則（所有 content 都適用）：\n"
            "- 直接、直述、白話：寫發生了什麼、誰說了什麼、自己感受到什麼。"
            "禁止隱喻、比喻、文學修飾、含糊措辭——記憶是給未來檢索用的，越白話越有效。"
            "範例（好）：「小英說她下週要去東京出差」；"
            "範例（壞）：「在夕陽下我們的心靠得更近了」。\n"
            "- 時間措辭必須中性：不要寫「剛剛」「今天早上」「昨天」這類相對時間——"
            "事件發生時點由系統另行標註，寫進 content 日後注入會過期失真。\n"
            "- 主詞用具體名字：一律寫角色的名字，不要寫「對方」「他」——"
            "未來其他情境檢索到這筆記憶時才不會搞錯是誰。\n"
            "- salience 是 0.0-1.0 的重要性：普通寒暄 0.3-0.5、"
            "有實質新資訊 0.5-0.7、關係轉折或重要承諾 0.75 以上。\n"
            '- audience 判斷「這筆內容若被角色公開分享到社群動態是否得體」：'
            '涉及主人隱私、他人私事、脆弱情緒的一律 "private"，'
            '一般日常可 "shareable"。salience 高不代表可公開。\n'
            "peer_facts_for_a/b 只寫 A/B 對對方可穩定累積的事實，"
            "例如職業、常去地點、習慣、彼此如何相處；"
            "不要把一時情緒或第三方主觀看法寫成 peer fact。\n"
            f"A={char_a.name}, B={char_b.name}, 地點={encounter.location}\n"
            f"{_character_profile_block('A', char_a)}\n"
            f"{_character_profile_block('B', char_b)}\n"
            "A 的 encounter context：\n"
            + "\n".join(speaker_contexts.get(char_a.id, []) or ["- （無額外脈絡）"])
            + "\nB 的 encounter context：\n"
            + "\n".join(speaker_contexts.get(char_b.id, []) or ["- （無額外脈絡）"])
            + "\n"
            f"transcript:\n{rendered}\n"
        )
        payload = _json_object(await model.generate(prompt, model=model_id)) or {}
        summary_a = _reflection_entry(payload.get("summary_for_a"))
        summary_b = _reflection_entry(payload.get("summary_for_b"))
        return EncounterReflection(
            summary_for_a=(summary_a.content if summary_a else "")
            or localized_fallback_text(
                "encounter.summary_met_short", language,
                location=encounter.location, name=char_b.name,
            ),
            summary_for_b=(summary_b.content if summary_b else "")
            or localized_fallback_text(
                "encounter.summary_met_short", language,
                location=encounter.location, name=char_a.name,
            ),
            how_a_sees_b=_str(payload.get("how_a_sees_b")) or None,
            how_b_sees_a=_str(payload.get("how_b_sees_a")) or None,
            affection_a_delta=_int_delta(payload.get("affection_a_delta")),
            affection_b_delta=_int_delta(payload.get("affection_b_delta")),
            trust_a_delta=_int_delta(payload.get("trust_a_delta")),
            trust_b_delta=_int_delta(payload.get("trust_b_delta")),
            summary_salience_a=summary_a.salience if summary_a else None,
            summary_salience_b=summary_b.salience if summary_b else None,
            summary_audience_a=summary_a.audience if summary_a else "",
            summary_audience_b=summary_b.audience if summary_b else "",
            hearsay_for_a=_entry_tuple(payload.get("hearsay_for_a")),
            hearsay_for_b=_entry_tuple(payload.get("hearsay_for_b")),
            peer_facts_for_a=_entry_tuple(payload.get("peer_facts_for_a")),
            peer_facts_for_b=_entry_tuple(payload.get("peer_facts_for_b")),
        )

    async def _speaker_contexts(
        self,
        char_a: Character,
        char_b: Character,
        *,
        now: datetime | None = None,
        encounter: CharacterEncounter | None = None,
        history: list[CharacterEncounter] | None = None,
    ) -> dict[str, list[str]]:
        """Assemble per-speaker bucketed context once per encounter.

        Buckets (ENCOUNTER_CHAT_PARITY_PLAN §Phase 1):
        1. 對方與關係 — social knowledge (peer profile, time-anchored
           memories, tier-gated operator gossip).
        2. 自己最近的生活 — CharacterLifeContextBuilder material, the
           fresh-topic supply that breaks the echo chamber.
        3. 已聊過 — recent completed encounters of the same pair as
           "already discussed" negative examples.

        Built once before the per-line loop; each generated line reuses
        the same dict, so material cost does not scale with turns.
        """
        life_contexts: dict[str, CharacterLifeContext | None] = {}
        for speaker in (char_a, char_b):
            life_contexts[speaker.id] = await self._life_context(speaker, now=now)
        if history is None:
            history = await self._completed_pair_history(encounter)
        contexts: dict[str, list[str]] = {}
        for speaker, peer in ((char_a, char_b), (char_b, char_a)):
            life = life_contexts.get(speaker.id)
            peer_lines: list[str] = []
            if self._social_knowledge is not None:
                try:
                    peer_lines = await self._social_knowledge.render_encounter_context(
                        speaker.id,
                        peer.id,
                        now=now,
                        operator_dialogue_summary=(
                            life.operator_dialogue_summary if life else ""
                        ),
                    )
                except Exception:
                    _LOGGER.exception(
                        "encounter context lookup failed speaker=%s peer=%s",
                        speaker.id,
                        peer.id,
                    )
            life_lines = (
                life.prompt_lines()
                if life is not None and life.has_material()
                else []
            )
            topic_lines = _render_topic_history_lines(
                history, speaker_is_a=speaker.id == char_a.id, now=now,
            )
            contexts[speaker.id] = _assemble_speaker_lines(
                peer_lines=peer_lines,
                life_lines=life_lines,
                topic_lines=topic_lines,
            )
        return contexts

    async def _life_context(
        self,
        character: Character,
        *,
        now: datetime | None = None,
    ) -> CharacterLifeContext | None:
        if self._life_context_builder is None:
            return None
        moment = _as_utc(now or datetime.now(timezone.utc))
        try:
            return await self._life_context_builder.build(character, now=moment)
        except Exception:
            _LOGGER.exception(
                "encounter life context failed character=%s", character.id,
            )
            return None

    async def _completed_pair_history(
        self,
        encounter: CharacterEncounter | None,
        *,
        limit: int = _TOPIC_HISTORY_LIMIT,
    ) -> list[CharacterEncounter]:
        if encounter is None:
            return []
        relationship_id = getattr(encounter, "relationship_id", None)
        if not relationship_id:
            return []
        try:
            recent = await self._encounters.list_for_relationship(
                relationship_id, limit=limit + 5,
            )
        except Exception:
            _LOGGER.exception(
                "encounter history lookup failed relationship=%s",
                relationship_id,
            )
            return []
        exclude_id = getattr(encounter, "id", None)
        completed = [
            item for item in recent
            if item.status == "completed" and item.id != exclude_id
        ]
        return completed[:limit]

    async def _timezone_for_character(self, character: Character) -> tzinfo:
        if self._schedule_service is None:
            return self._local_tz
        resolver = getattr(self._schedule_service, "timezone_for_character", None)
        if resolver is None:
            return self._local_tz
        try:
            return await resolver(character)
        except Exception:
            _LOGGER.exception(
                "encounter runner: timezone lookup failed character=%s",
                character.id,
            )
            return self._local_tz


class CharacterEncounterMemoryWriter:
    def __init__(
        self,
        *,
        repository: MemoryRepositoryPort,
        embedder: EmbedderPort | None = None,
    ) -> None:
        self._repository = repository
        self._embedder = embedder

    async def write(
        self,
        *,
        encounter: CharacterEncounter,
        char_a: Character,
        char_b: Character,
        transcript: tuple[EncounterLine, ...],
        reflection: EncounterReflection,
    ) -> tuple[str, ...]:
        _ = transcript
        transcript_tag = f"encounter:{encounter.id}"
        memories = [
            MemoryItem.create(
                character_id=char_a.id,
                kind=MemoryKind.EPISODIC,
                content=reflection.summary_for_a,
                salience=_clamped_salience(
                    reflection.summary_salience_a,
                    default=0.62, lo=0.25, hi=0.9,
                ),
                tags=("encounter", "character_interaction", transcript_tag),
                created_at=encounter.scheduled_for,
                participants=(
                    ParticipantRef(
                        actor_kind="character",
                        actor_id=char_b.id,
                        display_name=char_b.name,
                        role="encounter_partner",
                    ),
                ),
                location=encounter.location,
                audience=reflection.summary_audience_a,
            ),
            MemoryItem.create(
                character_id=char_b.id,
                kind=MemoryKind.EPISODIC,
                content=reflection.summary_for_b,
                salience=_clamped_salience(
                    reflection.summary_salience_b,
                    default=0.62, lo=0.25, hi=0.9,
                ),
                tags=("encounter", "character_interaction", transcript_tag),
                created_at=encounter.scheduled_for,
                participants=(
                    ParticipantRef(
                        actor_kind="character",
                        actor_id=char_a.id,
                        display_name=char_a.name,
                        role="encounter_partner",
                    ),
                ),
                location=encounter.location,
                audience=reflection.summary_audience_b,
            ),
        ]
        for entry in reflection.hearsay_for_a:
            memories.append(_hearsay_memory(char_a, char_b, encounter, entry))
        for entry in reflection.hearsay_for_b:
            memories.append(_hearsay_memory(char_b, char_a, encounter, entry))
        for entry in reflection.peer_facts_for_a:
            memories.append(_peer_fact_memory(char_a, char_b, encounter, entry))
        for entry in reflection.peer_facts_for_b:
            memories.append(_peer_fact_memory(char_b, char_a, encounter, entry))
        unique = await self._deduplicate_per_owner(memories)
        if not unique:
            return ()
        embedded = await attach_embeddings(unique, self._embedder)
        stored = await self._repository.add_many(embedded)
        return tuple(item.id for item in stored)

    async def _deduplicate_per_owner(
        self,
        memories: list[MemoryItem],
    ) -> list[MemoryItem]:
        """Same-kind bigram-Jaccard dedup against each owner's existing
        memories — mirrors the chat write path (chat_service post-turn).

        Without this, every meetup unconditionally appends 2 summaries +
        N hearsay + M peer facts, and near-identical entries pile up
        into the echo chamber the context builder then re-reads.
        Fail-soft: if the existing-pool query breaks we keep the new
        batch rather than dropping fresh memories."""
        unique: list[MemoryItem] = []
        owner_ids = dict.fromkeys(item.character_id for item in memories)
        for owner_id in owner_ids:
            own_new = [item for item in memories if item.character_id == owner_id]
            try:
                existing = await self._repository.query(
                    owner_id,
                    limit=_DEDUP_POOL_SIZE,
                    world_scope=None,
                )
            except Exception:
                _LOGGER.exception(
                    "encounter memory dedup pool query failed character=%s",
                    owner_id,
                )
                unique.extend(own_new)
                continue
            unique.extend(deduplicate(own_new, existing))
        return unique


class CharacterEncounterService:
    def __init__(
        self,
        *,
        planner: CharacterEncounterPlanner,
        runner: CharacterEncounterRunner,
        encounter_repository: CharacterEncounterRepositoryPort,
    ) -> None:
        self._planner = planner
        self._runner = runner
        self._encounters = encounter_repository

    async def list_for_character(
        self, character_id: str, *, limit: int = 30,
    ) -> list[CharacterEncounter]:
        return await self._encounters.list_for_character(character_id, limit=limit)

    async def tick(self, *, now: datetime | None = None) -> EncounterTickResult:
        planned_result = await self.plan_pending(now=now)
        run_result = await self.run_pending(now=now)
        return EncounterTickResult(
            planned=planned_result.planned,
            completed=run_result.completed,
            failed=run_result.failed,
            planned_ids=planned_result.planned_ids,
            completed_ids=run_result.completed_ids,
            failed_ids=run_result.failed_ids,
        )

    async def plan_pending(
        self, *, now: datetime | None = None,
    ) -> EncounterTickResult:
        planned = await self._planner.plan_due(now=now)
        return EncounterTickResult(
            planned=len(planned),
            planned_ids=tuple(item.id for item in planned),
        )

    async def run_pending(
        self, *, now: datetime | None = None,
    ) -> EncounterTickResult:
        completed, failed = await self._runner.run_due(now=now)
        return EncounterTickResult(
            completed=len(completed),
            failed=len(failed),
            completed_ids=tuple(completed),
            failed_ids=tuple(failed),
        )


_SPEAKER_CONTEXT_MAX_CHARS = 2600
"""Per-speaker total context budget (plan §6). The assembled context is
re-injected verbatim into every per-line prompt (up to 8 turns, plus a
full regeneration on gate failure), so an unbounded assembly multiplies
its excess across the whole encounter."""


def _cap_bucket(header: str, lines: list[str], max_chars: int) -> list[str]:
    """Cap one bucket to a char budget; drop it entirely when there is
    no room for even a single content line (a bare header is noise)."""
    if not lines:
        return []
    kept = [header]
    total = len(header)
    for line in lines:
        next_total = total + len(line)
        if next_total > max_chars:
            break
        kept.append(line)
        total = next_total
    return kept if len(kept) > 1 else []


def _assemble_speaker_lines(
    *,
    peer_lines: list[str],
    life_lines: list[str],
    topic_lines: list[str],
) -> list[str]:
    """Assemble one speaker's bucketed context under the total budget.

    Budget is reserved back to front: the topic-history bucket (the
    anti-repeat negative examples) is kept whole, the life bucket (the
    fresh-topic supply) gets what remains, and the peer bucket — already
    capped upstream by the social-knowledge renderer — absorbs the
    squeeze. Rendering order stays peer → life → history."""
    budget = _SPEAKER_CONTEXT_MAX_CHARS
    topic_kept = _cap_bucket(
        "【最近幾次碰面已聊過（可延續進展，但不要原樣重講同一件事）】",
        topic_lines,
        budget,
    )
    budget -= sum(len(line) for line in topic_kept)
    life_kept = _cap_bucket(
        "【自己最近的生活（可自然聊起的近況）】",
        life_lines,
        max(0, budget),
    )
    budget -= sum(len(line) for line in life_kept)
    peer_kept = _cap_bucket(
        "【對方與你們的關係】",
        peer_lines,
        max(0, budget),
    )
    return [*peer_kept, *life_kept, *topic_kept]


def _render_transcript(
    transcript: tuple[EncounterLine, ...],
    *,
    char_a: Character,
    char_b: Character,
) -> str:
    return "\n".join(
        f"{char_a.name if line.speaker_character_id == char_a.id else char_b.name}: "
        f"{line.text}"
        for line in transcript
    )


def _encounter_diversity_evidence(
    history: list[CharacterEncounter],
    *,
    now: datetime | None,
) -> ReplyDiversityEvidence | None:
    """Manual diversity evidence from recent pair history.

    Mirrors the feed composer's approach (hand-built frequency lines,
    no Message conversion): the judge sees what this pair talked about
    recently as statistical evidence, never as a mechanical filter."""
    if not history:
        return None
    topic_lines = _render_topic_history_lines(history, speaker_is_a=True, now=now)
    return ReplyDiversityEvidence(
        assistant_line_count=len(history),
        phrase_frequency_lines=tuple(
            f"近期碰面話題：{line[2:] if line.startswith('- ') else line}"
            for line in topic_lines[:4]
        ),
    )


def _render_beats_block(
    beats: tuple[EncounterBeat, ...],
    *,
    char_a: Character,
    char_b: Character,
) -> str:
    if not beats:
        return ""
    rendered: list[str] = [
        "本次碰面話題節拍（依序自然推進，不必硬講完；話題聊完就自然收尾）：",
    ]
    for index, beat in enumerate(beats, start=1):
        if beat.carrier == "a":
            who = char_a.name
        elif beat.carrier == "b":
            who = char_b.name
        else:
            who = "誰先都行"
        line = f"{index}. {beat.topic}（自然帶起：{who}）"
        if beat.note:
            line += f"——{beat.note}"
        rendered.append(line)
    return "\n".join(rendered) + "\n"


def _encounter_register_lines(profile: RegisterProfile | None) -> list[str]:
    """Encounter-flavoured register rail.

    The chat pack's register guidance assumes texting with the player;
    encounters are two characters talking face to face, so the rail is
    restated for that situation instead of reusing the chat wording.
    Base lines are unconditional (anti-counsellor / concrete-first);
    the increment follows the same warmth-earned rule as chat."""
    lines = [
        "語氣要求：像真人面對面閒聊——口語、具體、白描；"
        "不要諮商師或客服腔，不要旁白式抒情；"
        "溫柔與深度要由話題和關係賺得，不要預設溫柔收尾。",
    ]
    if profile is None:
        lines.append("語域：中性日常——優先回應具體內容，不把小事升格成療癒場景。")
        return lines
    warmth_earned = (
        profile.vulnerable_disclosure
        or profile.emotional_intensity >= 0.65
        or profile.help_seeking >= 0.65
        or profile.seriousness >= 0.75
    )
    if warmth_earned:
        lines.append(
            "語域：本場情緒較重——可以放慢、先承接對方的感受再接話，"
            "但不要逐句安撫或把對話變成開導。",
        )
    else:
        lines.append("語域：中性日常——優先回應具體內容，不把小事升格成療癒場景。")
    lines.append(
        f"語域剖面（內部參考）：情緒強度 {profile.emotional_intensity:.2f}；"
        f"嚴肅度 {profile.seriousness:.2f}；"
        f"幽默容許 {profile.humor_latitude:.2f}",
    )
    return lines


def _render_topic_history_lines(
    history: list[CharacterEncounter],
    *,
    speaker_is_a: bool,
    now: datetime | None,
) -> list[str]:
    """Render "already discussed" lines from the speaker's own side.

    Each completed encounter stores per-side summaries; quoting the
    speaker's own perspective keeps the negative example in first
    person. Falls back to the trigger reason for legacy rows without a
    summary. A relative-time anchor stops "上次" from reading as "剛剛"."""
    lines: list[str] = []
    for item in history:
        summary = item.summary_for_a if speaker_is_a else item.summary_for_b
        text = (summary or item.trigger_reason or "").strip()
        if not text:
            continue
        when = ""
        if now is not None and isinstance(item.scheduled_for, datetime):
            elapsed_min = (
                _as_utc(now) - _as_utc(item.scheduled_for)
            ).total_seconds() / 60.0
            if elapsed_min >= 0:
                when = f"（{format_relative_past_label(elapsed_min)}）"
        lines.append(f"- {when}{text}")
    return lines


def _encounter_time_context(encounter: CharacterEncounter, local_tz: tzinfo) -> str:
    scheduled_for = getattr(encounter, "scheduled_for", None)
    if not isinstance(scheduled_for, datetime):
        return ""
    lines = render_current_time_fact_lines(
        scheduled_for,
        local_tz,
        heading="碰面時間（使用者本地時區，僅供內部參考）：",
        label="碰面時間",
    )
    return "\n".join(lines) + "\n"


@dataclass(frozen=True, slots=True)
class _PlanDecision:
    should_plan: bool
    scheduled_for: datetime | None
    end_at: datetime
    location: str
    reason: str
    max_turns: int

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        fallback_start: datetime,
        fallback_end: datetime,
    ) -> "_PlanDecision":
        start = _parse_dt(payload.get("start_iso")) or fallback_start
        end = _parse_dt(payload.get("end_iso")) or fallback_end
        if end <= start:
            end = min(start + _MAX_SLOT, fallback_end)
        return cls(
            should_plan=bool(payload.get("should_plan")),
            scheduled_for=start,
            end_at=end,
            location=_str(payload.get("location")),
            reason=_str(payload.get("reason")),
            max_turns=max(2, min(8, int(payload.get("max_turns") or 4))),
        )


def _default_closed_audience(entry: ReflectionMemoryEntry) -> str:
    """Encounter gossip is private unless the LLM explicitly says otherwise.

    The reflect model classifies ``audience`` per entry, but an omitted
    tag must fail CLOSED for second-hand / about-the-peer material — an
    empty audience reads as shareable at the feed chokepoint, which
    would let another person's business default onto a public wall."""
    return "shareable" if entry.audience == "shareable" else "private"


def _hearsay_memory(
    owner: Character,
    source: Character,
    encounter: CharacterEncounter,
    entry: ReflectionMemoryEntry | str,
) -> MemoryItem:
    normalized = _reflection_entry(entry) or ReflectionMemoryEntry(content=str(entry))
    return MemoryItem.create(
        character_id=owner.id,
        kind=MemoryKind.HEARSAY,
        content=normalized.content,
        salience=_clamped_salience(
            normalized.salience, default=0.48, lo=0.2, hi=0.7,
        ),
        tags=("hearsay", "encounter", f"encounter:{encounter.id}"),
        created_at=encounter.scheduled_for,
        participants=(
            ParticipantRef(
                actor_kind="character",
                actor_id=source.id,
                display_name=source.name,
                role="source",
            ),
        ),
        location=encounter.location,
        audience=_default_closed_audience(normalized),
    )


def _peer_fact_memory(
    owner: Character,
    peer: Character,
    encounter: CharacterEncounter,
    entry: ReflectionMemoryEntry | str,
) -> MemoryItem:
    normalized = _reflection_entry(entry) or ReflectionMemoryEntry(content=str(entry))
    return MemoryItem.create(
        character_id=owner.id,
        kind=MemoryKind.RELATIONSHIP,
        content=normalized.content,
        salience=_clamped_salience(
            normalized.salience, default=0.58, lo=0.3, hi=0.8,
        ),
        tags=(
            "peer_fact",
            "relationship",
            "encounter",
            f"peer:{peer.id}",
            f"encounter:{encounter.id}",
        ),
        created_at=encounter.scheduled_for,
        participants=(
            ParticipantRef(
                actor_kind="character",
                actor_id=peer.id,
                display_name=peer.name,
                role="peer",
            ),
        ),
        location=encounter.location,
        audience=_default_closed_audience(normalized),
    )


def _first_shared_low_busy_slot(
    schedules_a: list[DailySchedule],
    schedules_b: list[DailySchedule],
    *,
    now: datetime,
    local_tz: tzinfo,
) -> tuple[datetime, datetime, str | None] | None:
    for schedule_a in schedules_a:
        schedule_b = next((s for s in schedules_b if s.date == schedule_a.date), None)
        if schedule_b is None:
            continue
        day_start = datetime.combine(
            schedule_a.date, time(8, 0), tzinfo=local_tz,
        ).astimezone(timezone.utc)
        day_end = datetime.combine(
            schedule_a.date, time(23, 0), tzinfo=local_tz,
        ).astimezone(timezone.utc)
        cursor = max(now + timedelta(minutes=10), day_start)
        while cursor + _MIN_SLOT <= day_end:
            end = min(cursor + _MAX_SLOT, day_end)
            if _slot_is_valid([schedule_a], cursor, end) and _slot_is_valid([schedule_b], cursor, end):
                location = _location_at(schedule_a, cursor) or _location_at(schedule_b, cursor)
                return cursor, end, location
            cursor += timedelta(minutes=15)
    return None


def _schedule_for(
    schedules: list[DailySchedule],
    moment: datetime,
    local_tz: tzinfo,
) -> DailySchedule | None:
    target = moment.astimezone(local_tz).date()
    return next((schedule for schedule in schedules if schedule.date == target), None)


def _slot_is_valid(
    schedules: list[DailySchedule],
    start_at: datetime,
    end_at: datetime,
) -> bool:
    if end_at <= start_at:
        return False
    for schedule in schedules:
        if schedule.date != start_at.date():
            continue
        for activity in schedule.activities:
            if activity.end_at <= start_at or activity.start_at >= end_at:
                continue
            if activity.busy_score >= 0.75 or activity.memorialized:
                return False
    return True


def _location_at(schedule: DailySchedule, moment: datetime) -> str | None:
    current = schedule.activity_at(moment)
    if current is not None and current.location:
        return current.location
    return None


def _json_object(raw: str) -> dict[str, Any] | None:
    text = raw.strip()
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end >= start:
        text = text[start:end + 1]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _clean_line(raw: str) -> str:
    return _clean_generated_line(raw)[0]


def _clean_generated_line(raw: str) -> tuple[str, bool]:
    text = raw.strip().strip('"').strip()
    if "\n" in text:
        text = text.splitlines()[0].strip()
    should_end = _ENCOUNTER_END_SENTINEL in text
    text = text.replace(_ENCOUNTER_END_SENTINEL, "").strip()
    return text[:240] or "嗯，我剛好也在這裡。", should_end


def _character_profile_block(label: str, character: Character) -> str:
    return (
        f"{label}：{character.name}\n"
        f"摘要：{character.summary}\n"
        f"個性：{', '.join(character.personality) or '未設定'}\n"
        f"說話風格：{character.speaking_style or '未設定'}\n"
        f"興趣：{', '.join(character.interests) or '未設定'}\n"
        f"界線：{', '.join(character.boundaries) or '未設定'}"
    )


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parse_dt(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    return _as_utc(parsed)


def _str(raw: object) -> str:
    return raw.strip() if isinstance(raw, str) else ""


def _str_tuple(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(item.strip() for item in raw if isinstance(item, str) and item.strip())


def _reflection_entry(raw: object) -> "ReflectionMemoryEntry | None":
    """Parse one reflect-output entry; tolerates both the new object
    shape and the legacy bare-string shape."""
    if isinstance(raw, ReflectionMemoryEntry):
        return raw if raw.content.strip() else None
    if isinstance(raw, str):
        text = raw.strip()
        return ReflectionMemoryEntry(content=text) if text else None
    if isinstance(raw, dict):
        content = _str(raw.get("content"))
        if not content:
            return None
        return ReflectionMemoryEntry(
            content=content,
            salience=_salience_or_none(raw.get("salience")),
            audience=_str(raw.get("audience")).lower(),
        )
    return None


def _entry_tuple(raw: object) -> tuple["ReflectionMemoryEntry", ...]:
    if not isinstance(raw, list):
        return ()
    entries = (_reflection_entry(item) for item in raw)
    return tuple(entry for entry in entries if entry is not None)


def _salience_or_none(raw: object) -> float | None:
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value


def _clamped_salience(
    value: float | None, *, default: float, lo: float, hi: float,
) -> float:
    if value is None:
        return default
    return max(lo, min(hi, value))


def _int_delta(raw: object) -> int:
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(-10, min(10, value))
