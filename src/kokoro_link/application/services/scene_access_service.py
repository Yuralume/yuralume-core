from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from typing import TYPE_CHECKING

from kokoro_link.contracts.memory import MemoryRepositoryPort
from kokoro_link.contracts.pending_follow_up import PendingFollowUpRepositoryPort
from kokoro_link.contracts.initial_relationship import (
    CharacterOperatorRelationshipSeedRepositoryPort,
)
from kokoro_link.contracts.repositories import (
    CharacterRepositoryPort,
    ConversationRepositoryPort,
)
from kokoro_link.contracts.scene_access import (
    SceneAccessContext,
    SceneAccessJudgePort,
    StageAccessAction,
    StageAccessDecision,
    StageAccessVerdict,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.domain.entities.schedule import ScheduleActivity
from kokoro_link.domain.value_objects.content_flow import (
    CONTENT_TOLERANCE_FRONTIER,
    sanitize_messages_for_tolerance,
)
from kokoro_link.domain.value_objects.presence_frame import AccessContext, ChatSurface
from kokoro_link.domain.value_objects.timezone import timezone_for_id, to_timezone
from kokoro_link.infrastructure.prompt.initial_relationship import (
    render_initial_relationship_seed_lines,
)

if TYPE_CHECKING:  # pragma: no cover
    from kokoro_link.application.services.operator_persona_service import (
        OperatorPersonaService,
    )
    from kokoro_link.application.services.operator_profile_service import (
        OperatorProfileService,
    )
    from kokoro_link.application.services.schedule_service import ScheduleService


_LOGGER = logging.getLogger(__name__)
_MAX_EVIDENCE_ITEMS = 8
_MAX_EVIDENCE_CHARS = 220
_MAX_DIALOGUE_MESSAGES = 12
_MAX_DIALOGUE_CHARS = 180


@dataclass(frozen=True, slots=True)
class _ScheduleAccessSnapshot:
    current: ScheduleActivity | None = None
    upcoming: tuple[ScheduleActivity, ...] = ()
    just_finished: ScheduleActivity | None = None


class CharacterNotFoundError(ValueError):
    pass


class SceneAccessService:
    def __init__(
        self,
        *,
        character_repository: CharacterRepositoryPort,
        judge: SceneAccessJudgePort,
        conversation_repository: ConversationRepositoryPort | None = None,
        schedule_service: "ScheduleService | None" = None,
        memory_repository: MemoryRepositoryPort | None = None,
        pending_follow_up_repository: PendingFollowUpRepositoryPort | None = None,
        relationship_seed_repository: (
            CharacterOperatorRelationshipSeedRepositoryPort | None
        ) = None,
        operator_profile_service: "OperatorProfileService | None" = None,
        operator_persona_service: "OperatorPersonaService | None" = None,
    ) -> None:
        self._character_repository = character_repository
        self._judge = judge
        self._conversation_repository = conversation_repository
        self._schedule_service = schedule_service
        self._memory_repository = memory_repository
        self._pending_follow_up_repository = pending_follow_up_repository
        self._relationship_seed_repository = relationship_seed_repository
        self._operator_profile_service = operator_profile_service
        self._operator_persona_service = operator_persona_service

    async def evaluate(
        self,
        character_id: str,
        *,
        operator_id: str = DEFAULT_OPERATOR_ID,
        requested_surface: ChatSurface = ChatSurface.WEB_STAGE,
        current_user_id: str | None = None,
    ) -> StageAccessVerdict:
        character = await self._character_repository.get(character_id)
        if character is None:
            raise CharacterNotFoundError("Character not found")
        if current_user_id is not None and character.user_id != current_user_id:
            raise CharacterNotFoundError("Character not found")

        surface = ChatSurface(requested_surface)
        if surface is not ChatSurface.WEB_STAGE:
            return StageAccessVerdict(
                decision=StageAccessDecision.ALLOW,
                recommended_action=StageAccessAction.USE_PHONE,
                access_context=AccessContext.TEXT_MESSAGE_ONLY,
                reason_for_user="這個介面是文字訊息，不需要同場可抵達性判斷。",
                prompt_fact="本輪是文字訊息互動；不要描寫成面對面同場。",
                suggested_opener=None,
            )

        context = await self._build_context(
            character,
            operator_id=operator_id,
            requested_surface=surface,
        )
        try:
            verdict = await self._judge.judge(context)
        except Exception:  # noqa: BLE001 - gate must fail closed
            _LOGGER.exception(
                "scene access judge failed character=%s operator=%s",
                character.id,
                operator_id,
            )
            return _fallback_verdict(character)
        return _normalise_verdict(verdict, character)

    async def _build_context(
        self,
        character: Character,
        *,
        operator_id: str,
        requested_surface: ChatSurface,
    ) -> SceneAccessContext:
        operator = await self._load_operator(operator_id)
        local_tz = timezone.utc
        if operator is not None:
            try:
                local_tz = timezone_for_id(getattr(operator, "timezone_id", None))
            except ValueError:
                local_tz = timezone.utc
        now_utc = datetime.now(timezone.utc)
        now_local = to_timezone(now_utc, local_tz)
        schedule_snapshot = await self._load_schedule_snapshot(character)
        current_activity = schedule_snapshot.current
        persona, persona_lines = await self._load_operator_persona(
            character.id, operator_id,
        )
        initial_relationship_lines = await self._load_initial_relationship_lines(
            character.id,
            operator_id,
        )
        evidence = await self._load_evidence(character.id)
        recent_dialogue = await self._load_recent_dialogue(character.id)
        return SceneAccessContext(
            character_id=character.id,
            operator_id=operator_id,
            character_name=character.name,
            character_summary=character.summary,
            character_boundaries=tuple(character.boundaries),
            familiarity_band=_familiarity_band(persona),
            trust_band=_trust_band(character.state.trust),
            current_activity_summary=_activity_summary(current_activity),
            current_activity_location=(
                current_activity.location if current_activity is not None else None
            ),
            current_activity_category=(
                current_activity.category if current_activity is not None else None
            ),
            current_activity_busy_score=(
                current_activity.busy_score if current_activity is not None else None
            ),
            current_activity_scene_privacy=_enum_value(
                getattr(current_activity, "scene_privacy", None),
            ),
            current_activity_meeting_affordance=_enum_value(
                getattr(current_activity, "meeting_affordance", None),
            ),
            schedule_context_summary=_schedule_context_summary(
                schedule_snapshot,
                local_tz=local_tz,
            ),
            recent_dialogue=tuple(recent_dialogue),
            operator_primary_language=(
                getattr(operator, "primary_language", None) or "zh-TW"
            ),
            operator_current_status=(
                getattr(operator, "current_status", None)
                if operator is not None else None
            ),
            operator_current_status_set_at=(
                getattr(operator, "current_status_set_at", None)
                if operator is not None else None
            ),
            initial_relationship_lines=tuple(initial_relationship_lines),
            recent_invitation_or_meetup_evidence=tuple(evidence),
            operator_persona_lines=tuple(persona_lines),
            requested_surface=requested_surface,
            now_local=now_local,
        )

    async def _load_operator(self, operator_id: str):
        if self._operator_profile_service is None:
            return None
        try:
            return await self._operator_profile_service.get_for_user(operator_id)
        except Exception:
            _LOGGER.exception("scene access operator profile lookup failed")
            return None

    async def _load_schedule_snapshot(
        self,
        character: Character,
    ) -> _ScheduleAccessSnapshot:
        if self._schedule_service is None:
            return _ScheduleAccessSnapshot()
        try:
            schedule = await self._schedule_service.ensure_schedule(character)
            current, upcoming, just_finished = self._schedule_service.resolve_current(
                schedule,
            )
            return _ScheduleAccessSnapshot(
                current=current,
                upcoming=tuple(upcoming),
                just_finished=just_finished,
            )
        except Exception:
            _LOGGER.exception(
                "scene access schedule lookup failed character=%s",
                character.id,
            )
            return _ScheduleAccessSnapshot()

    async def _load_operator_persona(
        self,
        character_id: str,
        operator_id: str,
    ) -> tuple[object | None, list[str]]:
        if self._operator_persona_service is None:
            return None, []
        try:
            persona = await self._operator_persona_service.get_current(
                character_id, operator_id,
            )
            lines = self._operator_persona_service.render_for_prompt(persona)
            return persona, lines
        except Exception:
            _LOGGER.exception("scene access persona lookup failed")
            return None, []

    async def _load_initial_relationship_lines(
        self,
        character_id: str,
        operator_id: str,
    ) -> list[str]:
        if self._relationship_seed_repository is None:
            return []
        try:
            seed = await self._relationship_seed_repository.get(
                character_id,
                operator_id,
            )
            return render_initial_relationship_seed_lines(seed)
        except Exception:
            _LOGGER.exception("scene access relationship seed lookup failed")
            return []

    async def _load_evidence(self, character_id: str) -> list[str]:
        evidence: list[str] = []
        if self._memory_repository is not None:
            try:
                memories = await self._memory_repository.query(
                    character_id,
                    limit=_MAX_EVIDENCE_ITEMS,
                    min_salience=0.4,
                    world_scope=None,
                )
                for memory in memories:
                    text = (memory.content or "").strip()
                    if text:
                        evidence.append(_trim(text, _MAX_EVIDENCE_CHARS))
            except Exception:
                _LOGGER.exception("scene access memory evidence lookup failed")
        if self._pending_follow_up_repository is not None:
            try:
                rows = await self._pending_follow_up_repository.list_open_for_character(
                    character_id,
                )
                for row in rows[:_MAX_EVIDENCE_ITEMS]:
                    if getattr(row, "promise_intent", ""):
                        evidence.append(
                            _trim(
                                "pending promise: " + row.promise_intent,
                                _MAX_EVIDENCE_CHARS,
                            ),
                        )
            except Exception:
                _LOGGER.exception("scene access pending evidence lookup failed")
        return evidence[:_MAX_EVIDENCE_ITEMS]

    async def _load_recent_dialogue(self, character_id: str) -> list[str]:
        if self._conversation_repository is None:
            return []
        try:
            messages = await self._conversation_repository.recent_messages_for_character(
                character_id,
                limit=_MAX_DIALOGUE_MESSAGES,
                exclude_tool_only=True,
            )
        except Exception:
            _LOGGER.exception("scene access recent dialogue lookup failed")
            return []
        messages = sanitize_messages_for_tolerance(
            messages,
            content_tolerance=CONTENT_TOLERANCE_FRONTIER,
        )
        lines: list[str] = []
        for message in messages:
            content = (message.content or "").strip()
            if not content:
                continue
            role = getattr(getattr(message, "role", None), "value", None)
            if not isinstance(role, str) or not role:
                role = str(getattr(message, "role", "message"))
            lines.append(f"{role}: {_trim(content, _MAX_DIALOGUE_CHARS)}")
        return lines


def _normalise_verdict(
    verdict: StageAccessVerdict,
    character: Character,
) -> StageAccessVerdict:
    if verdict.access_context is AccessContext.REMOTE_STAGE:
        action = verdict.recommended_action
        if action is StageAccessAction.USE_STAGE:
            action = StageAccessAction.USE_PHONE
        return StageAccessVerdict(
            decision=StageAccessDecision.BLOCK,
            recommended_action=action,
            access_context=AccessContext.TEXT_MESSAGE_ONLY,
            reason_for_user=(
                verdict.reason_for_user
                or "目前沒有現實世界可解釋的同場理由，先用手機訊息比較自然。"
            ),
            prompt_fact=(
                "本輪是文字訊息互動；不要描寫成面對面同場、共享舞台，"
                "也不要假設使用者已到角色所在地。"
            ),
            suggested_opener=verdict.suggested_opener or _default_opener(character),
        )
    if verdict.decision is StageAccessDecision.BLOCK:
        access_context = verdict.access_context
        if access_context not in {
            AccessContext.TEXT_MESSAGE_ONLY,
            AccessContext.NOT_PLAUSIBLE,
        }:
            access_context = AccessContext.TEXT_MESSAGE_ONLY
        action = verdict.recommended_action
        if action is StageAccessAction.USE_STAGE:
            action = StageAccessAction.USE_PHONE
        return StageAccessVerdict(
            decision=verdict.decision,
            recommended_action=action,
            access_context=access_context,
            reason_for_user=verdict.reason_for_user or "現在不適合直接同場。",
            prompt_fact=verdict.prompt_fact
            or "使用者目前不合理出現在角色當前場景；不要演成面對面同場。",
            suggested_opener=verdict.suggested_opener
            or _default_opener(character),
        )
    return StageAccessVerdict(
        decision=verdict.decision,
        recommended_action=verdict.recommended_action,
        access_context=verdict.access_context,
        reason_for_user=verdict.reason_for_user or "可依目前語境互動。",
        prompt_fact=verdict.prompt_fact or "請依 Scene Access verdict 維持互動邊界。",
        suggested_opener=verdict.suggested_opener,
    )


def _fallback_verdict(character: Character) -> StageAccessVerdict:
    return StageAccessVerdict(
        decision=StageAccessDecision.BLOCK,
        recommended_action=StageAccessAction.USE_PHONE,
        access_context=AccessContext.TEXT_MESSAGE_ONLY,
        reason_for_user="目前無法可靠判斷同場可抵達性，先用手機訊息比較自然。",
        prompt_fact=(
            "Scene Access 判斷不可用；本輪不得假設使用者已實際進入角色"
            "所在位置，請改以文字訊息或先約定見面處理。"
        ),
        suggested_opener=_default_opener(character),
    )


def _default_opener(character: Character) -> str:
    return f"{character.name}，你現在方便聊一下嗎？"


def _activity_summary(activity: ScheduleActivity | None) -> str | None:
    if activity is None:
        return None
    return (activity.description or activity.category or "").strip() or None


def _schedule_context_summary(
    snapshot: _ScheduleAccessSnapshot,
    *,
    local_tz: tzinfo,
) -> str | None:
    if snapshot.current is not None:
        return "目前落在已規劃活動段；請優先使用 current activity 欄位判斷同場可抵達性。"

    lines = [
        "目前不在任何已規劃活動段，這代表行程空檔、轉場或未明確狀態；"
        "不能自動視為公共可抵達場景。",
    ]
    if snapshot.just_finished is not None:
        lines.append(
            "上一段剛結束："
            + _format_activity_brief(snapshot.just_finished, local_tz=local_tz),
        )
    next_activity = snapshot.upcoming[0] if snapshot.upcoming else None
    if next_activity is not None:
        lines.append(
            "下一段預定："
            + _format_activity_brief(next_activity, local_tz=local_tz),
        )
    if snapshot.just_finished is None and next_activity is None:
        lines.append("沒有可用的鄰近行程段。")
    return "\n".join(lines)


def _format_activity_brief(
    activity: ScheduleActivity,
    *,
    local_tz: tzinfo,
) -> str:
    start = activity.start_at.astimezone(local_tz).strftime("%H:%M")
    end = activity.end_at.astimezone(local_tz).strftime("%H:%M")
    parts = [f"{start}-{end}", activity.description]
    if activity.location:
        parts.append(f"location={activity.location}")
    parts.append(f"category={activity.category}")
    parts.append(f"busy_score={activity.busy_score:.2f}")
    scene_privacy = _enum_value(getattr(activity, "scene_privacy", None))
    if scene_privacy:
        parts.append(f"scene_privacy={scene_privacy}")
    meeting_affordance = _enum_value(
        getattr(activity, "meeting_affordance", None),
    )
    if meeting_affordance:
        parts.append(f"meeting_affordance={meeting_affordance}")
    return "｜".join(parts)


def _familiarity_band(persona: object | None) -> str:
    interaction = getattr(persona, "layer4_interaction", None)
    band = getattr(interaction, "familiarity_band", None)
    value = getattr(band, "value", None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "stranger"


def _trust_band(score: int) -> str:
    if score >= 70:
        return "high"
    if score >= 35:
        return "medium"
    return "low"


def _trim(text: str, limit: int) -> str:
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip() + "…"


def _enum_value(value: object | None) -> str | None:
    raw = getattr(value, "value", value)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None
