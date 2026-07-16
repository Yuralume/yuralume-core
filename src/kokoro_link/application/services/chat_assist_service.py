"""Generate player-side chat starter suggestions from current context."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from typing import Any, TYPE_CHECKING

from kokoro_link.application.dto.chat_assist import (
    ChatAssistSuggestion,
    ChatAssistSuggestionsResponse,
)
from kokoro_link.application.services.feature_keys import FEATURE_CHAT_ASSIST
from kokoro_link.application.services.subscription_access_guard import (
    SubscriptionAccessGuard,
)
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.repositories import ConversationRepositoryPort
from kokoro_link.contracts.story_arc import StoryArcRepositoryPort
from kokoro_link.contracts.world_event import WorldEventRepositoryPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.domain.entities.schedule import ScheduleActivity
from kokoro_link.domain.entities.story_arc import StoryArc
from kokoro_link.domain.entities.world_event import WorldEvent
from kokoro_link.domain.value_objects.content_flow import (
    CONTENT_TOLERANCE_FRONTIER,
    sanitize_messages_for_tolerance,
)
from kokoro_link.infrastructure.prompt.character_identity import (
    render_character_identity_lines,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)

if TYPE_CHECKING:  # pragma: no cover
    from kokoro_link.application.services.character_service import CharacterService
    from kokoro_link.application.services.operator_profile_service import (
        OperatorProfileService,
    )
    from kokoro_link.application.services.schedule_service import ScheduleService


_LOGGER = logging.getLogger(__name__)
_MAX_DIALOGUE_MESSAGES = 12
_MAX_DIALOGUE_CHARS = 180
_MAX_WORLD_EVENTS = 4
_MAX_WORLD_EVENT_CHARS = 180
_DEFAULT_COUNT = 4


class ChatAssistCharacterNotFoundError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _ScheduleAssistSnapshot:
    current: ScheduleActivity | None = None
    upcoming: tuple[ScheduleActivity, ...] = ()
    just_finished: ScheduleActivity | None = None
    local_tz: tzinfo = timezone.utc


class ChatAssistService:
    def __init__(
        self,
        *,
        character_service: "CharacterService",
        active_llm_provider: ActiveLLMProviderPort,
        conversation_repository: ConversationRepositoryPort | None = None,
        schedule_service: "ScheduleService | None" = None,
        story_arc_repository: StoryArcRepositoryPort | None = None,
        world_event_repository: WorldEventRepositoryPort | None = None,
        operator_profile_service: "OperatorProfileService | None" = None,
        subscription_access_guard: SubscriptionAccessGuard | None = None,
    ) -> None:
        self._character_service = character_service
        self._active_llm_provider = active_llm_provider
        self._conversation_repository = conversation_repository
        self._schedule_service = schedule_service
        self._story_arc_repository = story_arc_repository
        self._world_event_repository = world_event_repository
        self._operator_profile_service = operator_profile_service
        self._subscription_access_guard = subscription_access_guard

    async def suggest(
        self,
        character_id: str,
        *,
        user_id: str = DEFAULT_OPERATOR_ID,
        count: int = _DEFAULT_COUNT,
    ) -> ChatAssistSuggestionsResponse:
        character = await self._character_service.get_character_entity(
            character_id,
            user_id=user_id,
        )
        if character is None:
            raise ChatAssistCharacterNotFoundError("Character not found")
        if self._subscription_access_guard is not None:
            await self._subscription_access_guard.ensure_character_allowed(character)

        count = max(1, min(5, count))
        try:
            if await self._active_llm_provider.is_fake(
                FEATURE_CHAT_ASSIST,
                character=character,
            ):
                return ChatAssistSuggestionsResponse()
            model = await self._active_llm_provider.resolve(
                FEATURE_CHAT_ASSIST,
                character=character,
            )
            model_id = await self._active_llm_provider.resolve_model_id(
                FEATURE_CHAT_ASSIST,
                character=character,
            )
        except Exception:
            _LOGGER.exception("chat assist model resolution failed")
            return ChatAssistSuggestionsResponse()

        prompt = await self._build_prompt(character, user_id=user_id, count=count)
        try:
            raw = await model.generate(prompt, model=model_id)
        except Exception:
            _LOGGER.exception("chat assist LLM call failed character=%s", character.id)
            return ChatAssistSuggestionsResponse()

        suggestions = _parse_suggestions(raw, limit=count)
        return ChatAssistSuggestionsResponse(suggestions=suggestions)

    async def _build_prompt(
        self,
        character: Character,
        *,
        user_id: str,
        count: int,
    ) -> str:
        operator = await self._load_operator(user_id)
        operator_language = getattr(operator, "primary_language", None)
        schedule = await self._load_schedule(character)
        today = (
            await self._schedule_service.today_for_character(character)
            if self._schedule_service is not None
            else datetime.now(timezone.utc).date()
        )
        story_arc = await self._load_active_story_arc(character.id)
        recent_dialogue = await self._load_recent_dialogue(character)
        world_events = await self._load_world_events(character)

        lines = [
            "你是 Yuralume 的聊天發話輔助器，不是角色本人。",
            "任務：幫玩家想幾句可以對角色說的自然發話，讓玩家挑一句放進輸入框。",
            "不要代替角色回答，也不要直接推進劇情結果；只產生玩家可以說出口的句子。",
            "請根據下列語意上下文產生，不要給固定寒暄模板。",
            "",
            render_operator_language_hint(operator_language),
            "",
            "角色：",
            f"- 名字：{character.name}",
            f"- 摘要：{_trim(character.summary, 260) or '未設定'}",
            *render_character_identity_lines(character),
            f"- 說話風格：{character.speaking_style or '未設定'}",
            f"- 個性：{_join(character.personality) or '未設定'}",
            f"- 興趣：{_join(character.interests) or '未設定'}",
            f"- 界線：{_join(character.boundaries) or '未設定'}",
            "",
            "角色當前狀態：",
            f"- 情緒：{character.state.emotion}",
            f"- 目前意圖：{character.state.current_intent or '未設定'}",
            f"- 精力/疲勞/信任/好感："
            f"{character.state.energy}/{character.state.fatigue}/"
            f"{character.state.trust}/{character.state.affection}",
            "",
            "行程上下文：",
            *_schedule_lines(schedule),
            "",
            "近期對話：",
            *(recent_dialogue or ["- 尚無近期對話"]),
            "",
            "劇情上下文：",
            *_story_lines(story_arc, today=today),
            "",
            "RSS / 世界事件上下文：",
            *(_world_event_lines(world_events) or ["- 沒有可用的近期世界事件"]),
            "",
            "輸出要求：",
            f"- 產生 {count} 個彼此不同的選項。",
            "- 每個選項都必須是玩家口吻的一句話，不要加角色名旁白或舞台指示。",
            "- 可以自然承接近期對話、她現在在做的事、故事節奏或世界事件，但不要暴露「行程 / RSS / 狀態」等內部標籤。",
            "- 句子要可直接送出；避免太長、太正式、太像客服提示。",
            "- 只輸出 JSON 物件，不要 markdown、不要 code fence、不要前言。",
            (
                '{"suggestions":[{"text":"玩家可以說的句子",'
                '"reason":"簡短說明它承接了哪個上下文"}]}'
            ),
        ]
        return "\n".join(line for line in lines if line is not None)

    async def _load_operator(self, user_id: str) -> object | None:
        if self._operator_profile_service is None:
            return None
        try:
            return await self._operator_profile_service.get_for_user(user_id)
        except Exception:
            _LOGGER.exception("chat assist operator profile lookup failed")
            return None

    async def _load_schedule(self, character: Character) -> _ScheduleAssistSnapshot:
        if self._schedule_service is None:
            return _ScheduleAssistSnapshot()
        try:
            local_tz = await self._schedule_service.timezone_for_character(character)
            schedule = await self._schedule_service.ensure_schedule(character)
            current, upcoming, just_finished = self._schedule_service.resolve_current(
                schedule,
                upcoming_limit=3,
            )
            return _ScheduleAssistSnapshot(
                current=current,
                upcoming=tuple(upcoming),
                just_finished=just_finished,
                local_tz=local_tz,
            )
        except Exception:
            _LOGGER.exception("chat assist schedule lookup failed character=%s", character.id)
            return _ScheduleAssistSnapshot()

    async def _load_active_story_arc(self, character_id: str) -> StoryArc | None:
        if self._story_arc_repository is None:
            return None
        try:
            return await self._story_arc_repository.get_active_for_character(
                character_id,
            )
        except Exception:
            _LOGGER.exception("chat assist story arc lookup failed")
            return None

    async def _load_recent_dialogue(self, character: Character) -> list[str]:
        if self._conversation_repository is None:
            return []
        try:
            messages = await self._conversation_repository.recent_messages_for_character(
                character.id,
                limit=_MAX_DIALOGUE_MESSAGES,
                exclude_tool_only=True,
            )
        except Exception:
            _LOGGER.exception("chat assist recent dialogue lookup failed")
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
            role_value = getattr(getattr(message, "role", None), "value", None)
            role = "玩家" if role_value == "user" else character.name
            lines.append(f"- {role}: {_trim(content, _MAX_DIALOGUE_CHARS)}")
        return lines

    async def _load_world_events(self, character: Character) -> list[WorldEvent]:
        if self._world_event_repository is None:
            return []
        if not character.world_awareness_enabled:
            return []
        try:
            events = await self._world_event_repository.query_recent(
                limit=12,
                topic_tags=list(character.world_topics) or None,
                max_age_days=7,
            )
        except Exception:
            _LOGGER.exception("chat assist world events lookup failed")
            return []
        categories = {c.strip().lower() for c in character.subscribed_categories if c.strip()}
        if categories:
            events = [
                event for event in events
                if (event.category or "news").strip().lower() in categories
            ]
        return events[:_MAX_WORLD_EVENTS]


def _schedule_lines(snapshot: _ScheduleAssistSnapshot) -> list[str]:
    lines: list[str] = []
    if snapshot.current is not None:
        lines.append(
            "- 現在："
            + _activity_line(snapshot.current, local_tz=snapshot.local_tz),
        )
    elif snapshot.just_finished is not None:
        lines.append(
            "- 剛結束："
            + _activity_line(snapshot.just_finished, local_tz=snapshot.local_tz),
        )
    if snapshot.upcoming:
        lines.append("- 接下來：")
        lines.extend(
            f"  - {_activity_line(activity, local_tz=snapshot.local_tz)}"
            for activity in snapshot.upcoming
        )
    if not lines:
        lines.append("- 尚無可用行程")
    return lines


def _activity_line(activity: ScheduleActivity, *, local_tz: tzinfo) -> str:
    start = activity.start_at.astimezone(local_tz).strftime("%H:%M")
    end = activity.end_at.astimezone(local_tz).strftime("%H:%M")
    location = f" @ {activity.location}" if activity.location else ""
    affordance = getattr(activity.meeting_affordance, "value", None)
    privacy = getattr(activity.scene_privacy, "value", None)
    cues = ", ".join(cue for cue in (privacy, affordance) if cue)
    cue_text = f" ({cues})" if cues else ""
    return f"{start}-{end} {activity.description}{location}{cue_text}"


def _story_lines(arc: StoryArc | None, *, today) -> list[str]:
    if arc is None:
        return ["- 尚無進行中的劇情弧"]
    lines = [
        f"- 進行中：{arc.title}",
        f"- 前提：{_trim(arc.premise, 260)}",
        f"- 主題/調性：{arc.theme} / {arc.tone}",
    ]
    beats = arc.forward_beats(after=today, limit=3, include_today=True)
    if beats:
        lines.append("- 近期節點：")
        for beat in beats:
            location = f" @ {beat.location}" if beat.location else ""
            lines.append(
                f"  - {beat.scheduled_date}: {beat.title}{location} — "
                f"{_trim(beat.summary, 180)}",
            )
    return lines


def _world_event_lines(events: list[WorldEvent]) -> list[str]:
    lines: list[str] = []
    for event in events:
        published = event.published_at
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        summary = _trim(event.summary, _MAX_WORLD_EVENT_CHARS)
        lines.append(
            f"- [{event.category}] {event.title}: {summary} "
            f"({event.source}, {published.date().isoformat()})",
        )
    return lines


def _parse_suggestions(raw: str, *, limit: int) -> list[ChatAssistSuggestion]:
    payload = _extract_json_object(raw)
    if payload is None:
        return []
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return []
    items = parsed.get("suggestions") if isinstance(parsed, dict) else None
    if not isinstance(items, list):
        return []
    out: list[ChatAssistSuggestion] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        text = _clean_generated_text(item.get("text"))
        if not text or text in seen:
            continue
        reason = _clean_generated_text(item.get("reason"), max_chars=220) or None
        try:
            out.append(ChatAssistSuggestion(text=text, reason=reason))
        except ValueError:
            continue
        seen.add(text)
        if len(out) >= limit:
            break
    return out


def _extract_json_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _clean_generated_text(value: Any, *, max_chars: int = 240) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = " ".join(value.strip().split())
    cleaned = cleaned.strip("「」\"'")
    return cleaned[:max_chars].strip()


def _trim(value: str | None, max_chars: int) -> str:
    text = " ".join((value or "").strip().split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _join(values: list[str] | tuple[str, ...]) -> str:
    return "、".join(value.strip() for value in values if value and value.strip())
