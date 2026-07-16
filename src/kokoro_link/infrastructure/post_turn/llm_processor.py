"""LLM-backed post-turn processor.

Sends a single prompt that asks the model to output both structured
memory items **and** character-state delta suggestions. This avoids a
separate inference call for the state engine while keeping the memory
extraction quality.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from datetime import date, datetime, timezone, tzinfo

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.application.services.nsfw_mode import CONTENT_MODE_NSFW
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.post_turn import (
    ArcAdjustmentSignal,
    EmotionEventCandidate,
    PeerMeetIntent,
    PostTurnProcessorPort,
    PostTurnResult,
    ScheduleAdjustment,
    StateSuggestion,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Message, MessageRole
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.entities.schedule import (
    DailySchedule,
    OPERATOR_CONFIRMED_SHARED_ROLE,
    OPERATOR_INVITE_PENDING_ROLE,
    OPERATOR_WISH_ROLE,
)
from kokoro_link.domain.entities.story_arc import StoryArc
from kokoro_link.domain.value_objects.actor import ParticipantRef
from kokoro_link.domain.value_objects.memory_kind import CANONICAL_KINDS, MemoryKind
from kokoro_link.domain.value_objects.resolved_address import ResolvedAddress
from kokoro_link.domain.value_objects.timezone import timezone_for_id, to_timezone
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_lines,
)
from kokoro_link.infrastructure.prompt.timing_utils import (
    render_current_time_fact_lines,
)
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)

_MAX_CONTENT_CHARS = 400
_MAX_TAGS = 5
_MAX_TAG_CHARS = 40
_MAX_ITEMS = 6
_MAX_INTENT_CHARS = 140
_MAX_PARTICIPANTS = 6
_MAX_PARTICIPANT_NAME_CHARS = 60
_MAX_PARTICIPANT_ROLE_CHARS = 40
_MAX_LOCATION_CHARS_MEMORY = 120
_ALLOWED_PARTICIPANT_KINDS = frozenset({"operator", "character", "npc"})

_ALLOWED_KINDS = {kind.value for kind in CANONICAL_KINDS}

_DELTA_CLAMP = 20  # max absolute value for a single-turn delta

_MAX_ADJUSTMENTS = 4
_MAX_DESC_CHARS = 120
_MAX_CATEGORY_CHARS = 40
_MAX_LOCATION_CHARS = 80
_MAX_REASON_CHARS = 120
_ALLOWED_ADJUSTMENT_ACTIONS = {"add", "remove", "modify"}

_MAX_ARC_ADJUSTMENTS = 2
_MAX_ARC_TITLE_CHARS = 80
_MAX_ARC_SUMMARY_CHARS = 400
_ALLOWED_ARC_ACTIONS = {
    "advance_beat", "delay_beat", "modify_beat", "insert_beat",
    "mark_realized", "skip_beat",
}
_ALLOWED_ARC_TENSIONS = {"setup", "rising", "climax", "falling", "resolution"}
_MAX_ARC_SHIFT_DAYS = 14

_MAX_HISTORY_TURNS = 6
_MAX_HISTORY_CHARS_PER_TURN = 300


class LLMPostTurnProcessor(PostTurnProcessorPort):
    def __init__(
        self,
        *,
        provider: ActiveLLMProviderPort | None = None,
        model: ChatModelPort | None = None,
        feature_key: str | None = None,
        local_tz: tzinfo = timezone.utc,
    ) -> None:
        """Accept either a live ``provider`` (preferred — honours the
        operator's UI model pick per-call) or a fixed ``model`` (for
        tests that want a single deterministic backend). Exactly one
        must be supplied.

        ``feature_key`` enables per-feature routing (usually
        ``FEATURE_POST_TURN``). The container passes it when wiring
        the live provider; tests using a fixed model leave it ``None``.
        """
        self._resolver = ModelResolver(
            provider=provider, model=model, feature_key=feature_key,
        )
        self._local_tz = local_tz

    async def process(
        self,
        *,
        character: Character,
        conversation_id: str,
        user_message: str,
        assistant_message: str,
        recent_messages: list[Message] | None = None,
        active_schedule: DailySchedule | None = None,
        active_arc: StoryArc | None = None,
        operator: OperatorProfile | None = None,
        now: datetime | None = None,
        content_mode: str = "normal",
        peer_context_lines: list[str] | None = None,
        resolved_player_address: ResolvedAddress | None = None,
    ) -> PostTurnResult:
        # Short-circuit when the operator picked the fake provider —
        # fake emits deterministic text that won't parse as the JSON
        # memory schema and would pollute storage with nothing useful.
        if await self._resolver.is_fake(character=character):
            return PostTurnResult()
        prompt = _build_prompt(
            character=character,
            user_message=user_message,
            assistant_message=assistant_message,
            recent_messages=recent_messages or [],
            active_schedule=active_schedule,
            active_arc=active_arc,
            operator=operator,
            local_tz=_operator_timezone(operator, self._local_tz),
            now=now,
            content_mode=content_mode,
            peer_context_lines=peer_context_lines or [],
            resolved_player_address=resolved_player_address,
        )
        try:
            raw = await self._resolver.generate(prompt, character=character)
        except Exception:
            _LOGGER.exception("Post-turn LLM call failed")
            return PostTurnResult()

        known_ids = (
            {a.id for a in active_schedule.activities}
            if active_schedule is not None
            else set()
        )
        known_beat_ids = (
            {b.id for b in active_arc.beats if b.status == "pending"}
            if active_arc is not None
            else set()
        )
        return _parse_response(
            raw,
            character_id=character.id,
            conversation_id=conversation_id,
            known_activity_ids=known_ids,
            known_beat_ids=known_beat_ids,
            known_peer_lines=peer_context_lines or [],
        )

def _build_prompt(
    *,
    character: Character,
    user_message: str,
    assistant_message: str,
    recent_messages: list[Message],
    active_schedule: DailySchedule | None = None,
    active_arc: StoryArc | None = None,
    operator: OperatorProfile | None = None,
    local_tz: tzinfo = timezone.utc,
    now: datetime | None = None,
    content_mode: str = "normal",
    peer_context_lines: list[str] | None = None,
    resolved_player_address: ResolvedAddress | None = None,
) -> str:
    state = character.state
    ref_now = ensure_utc(now) if now is not None else datetime.now(timezone.utc)
    today = to_timezone(ref_now, local_tz).date()
    schedule_section = "\n".join(
        _render_schedule_context(active_schedule, local_tz=local_tz),
    )
    arc_section = "\n".join(
        _render_arc_context(
            active_arc,
            today=today,
        ),
    )
    history_section = "\n".join(
        _render_history(recent_messages, character_name=character.name),
    )
    operator_lines = [
        # Player-visible state fields (emotion word, current_intent) must
        # follow the operator's content language — same fact injected in
        # every other operator-visible LLM job (see operator_language.py).
        *render_operator_language_lines(
            operator.primary_language if operator is not None else None,
        ),
        *_render_operator_context(operator, resolved_player_address),
        *render_current_time_fact_lines(ref_now, local_tz),
        *_render_content_mode_context(content_mode),
        *(peer_context_lines or []),
    ]
    operator_section = "\n".join(operator_lines)
    prompt = get_default_loader().render(
        "post_turn/processor",
        character_name=character.name,
        character_summary=character.summary,
        character_personality=", ".join(character.personality) if character.personality else "（未設定）",
        character_boundaries=", ".join(character.boundaries) if character.boundaries else "（未設定）",
        state_emotion=state.emotion,
        state_affection=state.affection,
        state_fatigue=state.fatigue,
        state_trust=state.trust,
        state_energy=state.energy,
        operator_section=operator_section,
        history_section=history_section,
        schedule_section=schedule_section,
        arc_section=arc_section,
        user_message=user_message,
        assistant_message=assistant_message,
        kinds_hint=", ".join(sorted(_ALLOWED_KINDS)),
        max_adjustments=_MAX_ADJUSTMENTS,
        max_arc_shift_days=_MAX_ARC_SHIFT_DAYS,
        max_arc_adjustments=_MAX_ARC_ADJUSTMENTS,
    )
    if peer_context_lines:
        prompt += _peer_meet_intent_extension(character_name=character.name)
    return prompt


def _peer_meet_intent_extension(*, character_name: str) -> str:
    return (
        "\n\n額外輸出欄位：peer_meet_intents。\n"
        "- 即使上方 baseline 範例沒有列出，也請在 JSON 物件中加入 "
        '"peer_meet_intents" 欄位；沒有明確約定時給空陣列 []。\n'
        "- 只在使用者明確要求或確認，且角色也答應/接下，要讓"
        f"{character_name} 與上方已知角色名冊中的某一位碰面時產出。\n"
        "- 不要為模糊的「改天見」「有空找她」產出；必須有明確 peer，"
        "且能推得日期或最早時間。\n"
        "- 每筆欄位：peer_character_id, peer_name, desired_after_iso, topic, source_text。\n"
        "- peer_character_id 必須使用上方名冊的 id；若只知道名字，請先對到名冊中的 id。\n"
        "- desired_after_iso 使用 ISO-8601；若只說日期（例如明天），用該日 00:00。"
        "禁止輸出「明天」「下午」等相對詞。\n"
        "- topic 是要帶進碰面對話的自然語言話題或約定理由。\n"
        "- 同一輪最多 1 筆；寧可漏掉，不要誤判。\n"
    )




def _render_history(
    messages: list[Message], *, character_name: str,
) -> list[str]:
    if not messages:
        return ["", "近期對話脈絡：（無）"]
    tail = messages[-_MAX_HISTORY_TURNS:]
    lines = ["", "近期對話脈絡（較早 → 較新，不含本輪）："]
    for msg in tail:
        content = (msg.content or "").strip()
        if not content:
            continue
        if len(content) > _MAX_HISTORY_CHARS_PER_TURN:
            content = content[: _MAX_HISTORY_CHARS_PER_TURN] + "…"
        label = "使用者" if msg.role == MessageRole.USER else character_name
        lines.append(f"- {label}：{content}")
    if len(lines) == 2:
        lines.append("- （無有效內容）")
    return lines


def _render_operator_context(
    operator: OperatorProfile | None,
    resolved: "ResolvedAddress | None" = None,
) -> list[str]:
    """Render the operator-identity block injected near the top of the
    extractor prompt.

    Phase 1 of the world-system roadmap: when the operator has saved a
    real name, the extractor is told to write memory ``content`` using
    that name (e.g. "丹尼說他住在東京") instead of the generic
    "使用者". When no real name is set, returns an empty list so the
    legacy "使用者" wording in the example output still reads
    correctly. This is the minimal change that lets memories carry
    cross-character disambiguation without rewriting every example
    string in the prompt.

    When a ``resolved`` player address is supplied (the bidirectional
    address resolver, run by the caller with seed + persona + profile in
    hand), its primary term names the operator in memory content — so a
    per-character seed name outranks a raw platform/OAuth display name,
    and the lower-precedence names ride along as 別稱 so memories under an
    older name still resolve to the same person. Falls back to the legacy
    display-name rendering when no resolver result is passed (keeps
    non-chat callers untouched)."""
    if resolved is not None and not resolved.is_fallback:
        name = resolved.primary
        aliases: tuple[str, ...] = resolved.aliases
        pronouns = operator.pronouns if operator is not None else None
    elif operator is not None and operator.has_real_name():
        name = operator.display_name
        aliases = operator.aliases
        pronouns = operator.pronouns
    else:
        return []
    lines = [
        "",
        "對方身份（即下方對話中標記為「使用者」的那個人，請在 memory content 用這個稱呼）：",
        f"- 稱呼：{name}",
    ]
    if aliases:
        lines.append(f"- 別稱：{', '.join(aliases)}")
    if pronouns:
        lines.append(f"- 代名詞：{pronouns}")
    lines.append(
        f"在記憶 content 中請直接用「{name}」（或其別稱）"
        f"代替「使用者」一詞，例：「{name}說他住在東京」、"
        f"「{name}今天嘲笑我的興趣」。"
        "這樣未來其他角色檢索到這筆記憶時也能正確指認是誰。",
    )
    return lines


def _render_arc_context(arc: StoryArc | None, *, today: date) -> list[str]:
    if arc is None or not arc.beats:
        return ["", "劇情主軸：（無或尚未建立）"]
    lines = [
        "",
        f"劇情主軸「{arc.title}」：{arc.premise}",
        "尚未發生的 beat（供 arc_adjustments 引用的 id 清單；含推進事實）：",
    ]
    pending = [b for b in arc.beats if b.status == "pending"]
    if not pending:
        lines.append("- （所有 beat 都已實現或跳過）")
        return lines
    for beat in pending:
        overdue_days = max(0, (today - beat.scheduled_date).days)
        last_attempt = (
            beat.last_play_attempt_at.isoformat(timespec="minutes")
            if beat.last_play_attempt_at is not None else "無"
        )
        result = beat.last_play_attempt_result or "無"
        push = beat.last_play_push_intensity or "尚未嘗試"
        scene_type = f"｜場景類型={beat.scene_type}"
        required = f"｜必演={'是' if beat.required else '否'}"
        cast = (
            f"｜出場={ '、'.join(beat.scene_characters) }"
            if beat.scene_characters else ""
        )
        location = f"｜地點={beat.location}" if beat.location else ""
        question = (
            f"｜戲劇問題={beat.dramatic_question}"
            if beat.dramatic_question else ""
        )
        lines.append(
            f"- id={beat.id} {beat.scheduled_date.isoformat()} "
            f"[{beat.tension}] {beat.title}｜已延={overdue_days}天"
            f"｜嘗試={beat.play_attempt_count}次｜上次={last_attempt}"
            f"｜上次結果={result}｜上次力道={push}"
            f"{scene_type}{required}{cast}{location}{question}"
        )
    return lines


def _render_content_mode_context(content_mode: str) -> list[str]:
    if content_mode != CONTENT_MODE_NSFW:
        return []
    return [
        "",
        "內容流向模式：NSFW mode（使用者手動開啟的暫時模式）。",
        "- 記憶必須 born-safe：只保留情感、關係進展、界線、信任、里程碑與情緒餘韻。",
        "- 不要記錄露骨、煽情、可重建原場面的細節；不要引用露骨原文。",
        "- 若需要記住事件，用模糊且可放入一般模型 context 的寫法，例如「共度了一段親密時光」「關係更進一步」。",
    ]


def _render_schedule_context(
    schedule: DailySchedule | None,
    *,
    local_tz: tzinfo,
) -> list[str]:
    if schedule is None or not schedule.activities:
        return [
            "",
            "今日行程：（尚未建立）",
        ]
    lines = ["", "今日行程（供 schedule_adjustments 引用的 id 清單）："]
    for activity in schedule.activities:
        start = to_timezone(activity.start_at, local_tz).strftime("%H:%M")
        end = to_timezone(activity.end_at, local_tz).strftime("%H:%M")
        loc = f"｜{activity.location}" if activity.location else ""
        character_names = [
            ref.display_name
            for ref in activity.participant_refs
            if ref.actor_kind == "character" and ref.display_name
        ]
        companion_names = character_names or list(activity.companion_names)
        companions = (
            f"｜一起：{ '、'.join(companion_names) }"
            if companion_names else ""
        )
        lines.append(
            f"- id={activity.id} {start}-{end} {activity.description}"
            f"（{activity.category}{loc}{companions}）"
        )
    return lines


def _operator_timezone(
    operator: OperatorProfile | None,
    fallback: tzinfo,
) -> tzinfo:
    if operator is None:
        return fallback
    try:
        return timezone_for_id(getattr(operator, "timezone_id", None))
    except ValueError:
        return fallback


def _parse_response(
    raw: str,
    *,
    character_id: str,
    conversation_id: str,
    known_activity_ids: set[str] | None = None,
    known_beat_ids: set[str] | None = None,
    known_peer_lines: list[str] | None = None,
) -> PostTurnResult:
    obj = _extract_object(raw)
    if obj is None:
        return PostTurnResult()

    memories = _parse_memories(obj.get("memories"), character_id=character_id, conversation_id=conversation_id)
    state_suggestion = _parse_state(obj.get("state"))
    adjustments = _parse_adjustments(
        obj.get("schedule_adjustments"),
        known_activity_ids=known_activity_ids or set(),
    )
    arc_adjustments = _parse_arc_adjustments(
        obj.get("arc_adjustments"),
        known_beat_ids=known_beat_ids or set(),
    )
    message_promises = _parse_message_promises(obj.get("message_promises"))
    peer_ids, peer_names = _known_peers_from_lines(known_peer_lines or [])
    peer_meet_intents = _parse_peer_meet_intents(
        obj.get("peer_meet_intents"),
        known_peer_ids=peer_ids,
        known_peer_name_to_id=peer_names,
    )
    emotion_events = _parse_emotion_events(obj.get("emotion_events"))
    address_changes = _parse_address_changes(obj.get("address_changes"))
    return PostTurnResult(
        memories=memories,
        state_suggestion=state_suggestion,
        schedule_adjustments=adjustments,
        arc_adjustments=arc_adjustments,
        message_promises=message_promises,
        peer_meet_intents=peer_meet_intents,
        emotion_events=emotion_events,
        address_changes=address_changes,
    )


_MAX_MESSAGE_PROMISES = 2
"""Cap on message-promise extraction per turn. A single chat turn very
rarely produces more than one promise; >2 is almost always the model
over-emitting (e.g. converting every future-tense verb into a promise).
Beyond the cap we drop silently."""

_MAX_PEER_MEET_INTENTS = 1


def _known_peers_from_lines(lines: list[str]) -> tuple[set[str], dict[str, str]]:
    ids: set[str] = set()
    names: dict[str, str] = {}
    for line in lines:
        fragments = [part.strip(" -") for part in line.split("|")]
        peer_id = ""
        peer_name = ""
        for fragment in fragments:
            key, sep, value = fragment.partition("=")
            if not sep:
                continue
            key = key.strip()
            value = value.strip()
            if key == "id":
                peer_id = value
            elif key == "name":
                peer_name = value
        if peer_id:
            ids.add(peer_id)
            if peer_name:
                names[peer_name] = peer_id
    return ids, names


def _parse_peer_meet_intents(
    raw: Any,
    *,
    known_peer_ids: set[str],
    known_peer_name_to_id: dict[str, str],
) -> list[PeerMeetIntent]:
    if not isinstance(raw, list):
        return []
    out: list[PeerMeetIntent] = []
    for entry in raw[:_MAX_PEER_MEET_INTENTS]:
        if not isinstance(entry, dict):
            continue
        peer_id = _coerce_plain_str(
            entry.get("peer_character_id") or entry.get("peer_id"),
        )
        peer_name = (
            _coerce_adj_text(entry.get("peer_name") or entry.get("name"), limit=120)
            or ""
        )
        if not peer_id and peer_name:
            peer_id = known_peer_name_to_id.get(peer_name, "")
        if peer_id not in known_peer_ids:
            continue
        desired = _coerce_iso_date_or_datetime(entry.get("desired_after_iso"))
        topic = _coerce_adj_text(entry.get("topic") or entry.get("reason"), limit=300)
        if not desired or not topic:
            continue
        out.append(
            PeerMeetIntent(
                peer_character_id=peer_id,
                peer_name=peer_name,
                desired_after_iso=desired,
                topic=topic,
                source_text=_coerce_adj_text(entry.get("source_text"), limit=200) or "",
            ),
        )
    return out


_MAX_ADDRESS_CHANGES = 2
"""Cap on address-change signals per turn. A turn almost never changes
more than one direction of address; >2 is the model over-emitting."""

_VALID_ADDRESS_DIRECTIONS = frozenset({"player", "character"})


def _parse_address_changes(raw: Any) -> list:
    """Parse the LLM's ``address_changes`` array into the contract DTO.

    Each entry is a typed directional rename (「叫我森森」 → direction
    ``player``; 「我以後叫你小美」 → direction ``character``). Malformed
    entries drop silently, mirroring the other parsers. ``new_value`` is
    required and trimmed to the seed name length budget."""
    from kokoro_link.contracts.post_turn import AddressChangeSignal

    if not isinstance(raw, list):
        return []
    out: list = []
    for entry in raw[:_MAX_ADDRESS_CHANGES]:
        if not isinstance(entry, dict):
            continue
        direction = _coerce_plain_str(entry.get("direction")).strip().lower()
        if direction not in _VALID_ADDRESS_DIRECTIONS:
            continue
        new_value = _coerce_adj_text(
            entry.get("new_value") or entry.get("new"), limit=80,
        ) or ""
        if not new_value:
            continue
        subject = _coerce_plain_str(entry.get("subject")).strip().lower()
        # A ``player``-direction change becomes the operator's *own*
        # identity name (seed + persona name at high confidence), so it
        # must be the operator naming themselves — the same subject
        # discipline the persona extractor enforces. This drops a mis-read
        # like 「叫小美過來」 that names a peer. ``character``-direction names
        # the character (never the operator persona), so it isn't gated
        # here.
        if direction == "player" and subject != "operator_self":
            continue
        out.append(
            AddressChangeSignal(
                direction=direction,
                new_value=new_value,
                subject=subject,
                old_value=_coerce_adj_text(
                    entry.get("old_value") or entry.get("old"), limit=80,
                ) or "",
                source_text=_coerce_adj_text(
                    entry.get("source_text"), limit=200,
                ) or "",
            ),
        )
    return out


def _parse_message_promises(raw: Any) -> list:
    """Parse the LLM's ``message_promises`` array into the contract DTO.

    Tolerance policy mirrors the other parsers: drop malformed entries
    silently rather than raise. ISO-8601 validation happens at this
    layer so the service can trust ``scheduled_for_iso`` is parseable.
    """
    from kokoro_link.contracts.post_turn import MessagePromise

    if not isinstance(raw, list):
        return []
    out = []
    for entry in raw[:_MAX_MESSAGE_PROMISES]:
        if not isinstance(entry, dict):
            continue
        scheduled = _coerce_iso_datetime(entry.get("scheduled_for_iso"))
        intent = _coerce_adj_text(entry.get("intent"), limit=300)
        if not scheduled or not intent:
            continue
        source_text = _coerce_adj_text(entry.get("source_text"), limit=200)
        out.append(
            MessagePromise(
                scheduled_for_iso=scheduled,
                intent=intent,
                source_text=source_text,
            )
        )
    return out


def _coerce_iso_date_or_datetime(raw: Any) -> str | None:
    if raw is None or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if len(text) == 10:
        try:
            parsed_date = date.fromisoformat(text)
        except ValueError:
            return None
        return datetime.combine(parsed_date, datetime.min.time()).isoformat(
            timespec="minutes",
        )
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.isoformat(timespec="minutes")


def _coerce_iso_datetime(raw: Any) -> str | None:
    """Accept an ISO-8601 datetime and return canonical form.

    Returns ``None`` for missing / malformed / date-only input — a
    message promise needs a time-of-day. Naive datetimes are passed
    through verbatim (ChatService will apply the character's local
    timezone before persisting).
    """
    from datetime import datetime as _dt

    if raw is None or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        parsed = _dt.fromisoformat(text)
    except ValueError:
        return None
    # Reject date-only inputs (no time component). A wake-up at "明天"
    # without an hour is too vague to act on.
    if parsed.hour == 0 and parsed.minute == 0 and len(text) <= 10:
        return None
    return parsed.isoformat(timespec="minutes")


def _parse_arc_adjustments(
    raw: Any,
    *,
    known_beat_ids: set[str],
) -> list[ArcAdjustmentSignal]:
    if not isinstance(raw, list):
        return []
    out: list[ArcAdjustmentSignal] = []
    for entry in raw[: _MAX_ARC_ADJUSTMENTS * 2]:
        if not isinstance(entry, dict):
            continue
        action = _coerce_plain_str(entry.get("action")).lower()
        if action not in _ALLOWED_ARC_ACTIONS:
            continue

        if action in {"advance_beat", "delay_beat"}:
            beat_id = _coerce_plain_str(entry.get("beat_id"))
            if beat_id not in known_beat_ids:
                continue
            days = _coerce_int(entry.get("days"))
            if days is None or days == 0:
                continue
            magnitude = min(abs(days), _MAX_ARC_SHIFT_DAYS)
            signed = magnitude if action == "delay_beat" else -magnitude
            out.append(ArcAdjustmentSignal(
                action=action, beat_id=beat_id, days=signed,
                reason=_trim(entry.get("reason"), _MAX_REASON_CHARS),
            ))

        elif action == "modify_beat":
            beat_id = _coerce_plain_str(entry.get("beat_id"))
            if beat_id not in known_beat_ids:
                continue
            title = _trim(entry.get("title"), _MAX_ARC_TITLE_CHARS) or None
            summary = _trim(entry.get("summary"), _MAX_ARC_SUMMARY_CHARS) or None
            tension_raw = _coerce_plain_str(entry.get("tension")).lower()
            tension = tension_raw if tension_raw in _ALLOWED_ARC_TENSIONS else None
            if not any([title, summary, tension]):
                continue
            out.append(ArcAdjustmentSignal(
                action="modify_beat", beat_id=beat_id,
                title=title, summary=summary, tension=tension,
                reason=_trim(entry.get("reason"), _MAX_REASON_CHARS),
            ))

        elif action == "insert_beat":
            scheduled = _parse_iso_date(entry.get("scheduled_date"))
            title = _trim(entry.get("title"), _MAX_ARC_TITLE_CHARS)
            summary = _trim(entry.get("summary"), _MAX_ARC_SUMMARY_CHARS)
            tension_raw = _coerce_plain_str(entry.get("tension")).lower()
            tension = tension_raw if tension_raw in _ALLOWED_ARC_TENSIONS else "rising"
            if scheduled is None or not title or not summary:
                continue
            out.append(ArcAdjustmentSignal(
                action="insert_beat",
                scheduled_date=scheduled, title=title, summary=summary,
                tension=tension,
                reason=_trim(entry.get("reason"), _MAX_REASON_CHARS),
            ))

        elif action == "mark_realized":
            beat_id = _coerce_plain_str(entry.get("beat_id"))
            if beat_id not in known_beat_ids:
                continue
            narrative = (
                _trim(entry.get("narrative"), _MAX_ARC_SUMMARY_CHARS)
                or _trim(entry.get("summary"), _MAX_ARC_SUMMARY_CHARS)
            )
            out.append(ArcAdjustmentSignal(
                action="mark_realized", beat_id=beat_id,
                narrative=narrative or None,
                reason=_trim(entry.get("reason"), _MAX_REASON_CHARS),
            ))

        elif action == "skip_beat":
            beat_id = _coerce_plain_str(entry.get("beat_id"))
            if beat_id not in known_beat_ids:
                continue
            out.append(ArcAdjustmentSignal(
                action="skip_beat", beat_id=beat_id,
                reason=_trim(entry.get("reason"), _MAX_REASON_CHARS),
            ))

        if len(out) >= _MAX_ARC_ADJUSTMENTS:
            break
    return out


def _coerce_plain_str(raw: Any) -> str:
    if isinstance(raw, str):
        return raw.strip()
    return ""


def _coerce_int(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            return None
    return None


def _parse_iso_date(raw: Any) -> date | None:
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def _trim(raw: Any, limit: int) -> str:
    if isinstance(raw, str):
        return raw.strip()[:limit]
    return ""


def _extract_object(text: str) -> dict[str, Any] | None:
    """Extract the first top-level JSON object from possibly noisy text."""
    start = text.find("{")
    if start == -1:
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
                candidate = text[start : index + 1]
                try:
                    parsed = json.loads(candidate)
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


# ------------------------------------------------------------------
# Memory parsing (mirrors llm_extractor logic)
# ------------------------------------------------------------------

def _parse_memories(
    raw: Any,
    *,
    character_id: str,
    conversation_id: str,
) -> list[MemoryItem]:
    if not isinstance(raw, list):
        return []

    items: list[MemoryItem] = []
    for payload in raw[:_MAX_ITEMS]:
        if not isinstance(payload, dict):
            continue
        item = _payload_to_item(payload, character_id=character_id, conversation_id=conversation_id)
        if item is not None:
            items.append(item)
    return items


def _payload_to_item(
    payload: dict[str, Any],
    *,
    character_id: str,
    conversation_id: str,
) -> MemoryItem | None:
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    trimmed = content.strip()[:_MAX_CONTENT_CHARS]

    kind = _coerce_kind(payload.get("kind"))
    salience = _coerce_salience(payload.get("salience"))
    tags = _coerce_tags(payload.get("tags"))
    participants = _coerce_participants(payload.get("participants"))
    location = _coerce_location(payload.get("location"))
    audience = _coerce_plain_str(payload.get("audience"))
    try:
        return MemoryItem.create(
            character_id=character_id,
            conversation_id=conversation_id,
            kind=kind,
            content=trimmed,
            salience=salience,
            tags=tags,
            participants=participants,
            location=location,
            audience=audience,
        )
    except ValueError:
        return None


def _coerce_kind(raw: Any) -> MemoryKind:
    if isinstance(raw, str):
        candidate = raw.strip().lower()
        if candidate in _ALLOWED_KINDS:
            return MemoryKind.from_string(candidate)
    return MemoryKind.EPISODIC


def _coerce_salience(raw: Any) -> float:
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except ValueError:
            return 0.5
    return 0.5


def _coerce_tags(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    cleaned: list[str] = []
    for tag in raw:
        if not isinstance(tag, (str, int, float)):
            continue
        text = str(tag).strip().lower()[:_MAX_TAG_CHARS]
        if text:
            cleaned.append(text)
        if len(cleaned) >= _MAX_TAGS:
            break
    return tuple(cleaned)


def _coerce_participants(raw: Any) -> tuple[ParticipantRef, ...]:
    """Phase 2 of the world-system roadmap: parse the LLM's
    ``participants`` array into typed refs, dropping malformed entries
    rather than crashing the whole turn.

    Acceptable shapes per element:
    - ``{"actor_kind": "...", "actor_id": "...", "display_name": "...",
      "role": "..."}`` (preferred)
    - ``{"kind": ..., "id": ..., "name": ..., "role": ...}`` (alias —
      models occasionally drop the ``actor_`` prefix)
    - ``{"display_name": "..."}`` alone → defaults to npc with no id
    """
    if not isinstance(raw, list):
        return ()
    refs: list[ParticipantRef] = []
    for entry in raw[:_MAX_PARTICIPANTS]:
        if not isinstance(entry, dict):
            continue
        kind_raw = (
            entry.get("actor_kind") or entry.get("kind") or "npc"
        )
        kind = str(kind_raw).strip().lower()
        if kind not in _ALLOWED_PARTICIPANT_KINDS:
            kind = "npc"
        name_raw = (
            entry.get("display_name") or entry.get("name") or ""
        )
        name = str(name_raw).strip()[:_MAX_PARTICIPANT_NAME_CHARS]
        if not name:
            continue
        actor_id_raw = (
            entry.get("actor_id") if "actor_id" in entry else entry.get("id")
        )
        actor_id: str | None
        if actor_id_raw is None:
            actor_id = None
        else:
            actor_id = str(actor_id_raw).strip() or None
        role_raw = entry.get("role")
        role: str | None
        if role_raw is None:
            role = None
        else:
            role = str(role_raw).strip()[:_MAX_PARTICIPANT_ROLE_CHARS] or None
        try:
            refs.append(ParticipantRef(
                actor_kind=kind,  # type: ignore[arg-type]
                actor_id=actor_id,
                display_name=name,
                role=role,
            ))
        except ValueError:
            continue
    return tuple(refs)


def _coerce_location(raw: Any) -> str | None:
    """Best-effort string extractor for the optional ``location``
    field. Models sometimes echo ``"未知"`` / ``"unknown"`` / empty
    string when there's no setting; we treat all of those as
    ``None`` to keep the column tidy."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if text.lower() in {"unknown", "n/a", "none", "未知", "無"}:
        return None
    return text[:_MAX_LOCATION_CHARS_MEMORY]


# ------------------------------------------------------------------
# State parsing
# ------------------------------------------------------------------

def _parse_state(raw: Any) -> StateSuggestion | None:
    if not isinstance(raw, dict):
        return None

    emotion = raw.get("emotion")
    if not isinstance(emotion, str) or not emotion.strip():
        emotion = None
    else:
        emotion = emotion.strip()

    intent_raw = raw.get("current_intent")
    current_intent: str | None = None
    if isinstance(intent_raw, str):
        trimmed = intent_raw.strip()[:_MAX_INTENT_CHARS]
        if trimmed:
            current_intent = trimmed

    return StateSuggestion(
        emotion=emotion,
        affection_delta=_clamp_delta(raw.get("affection_delta")),
        fatigue_delta=_clamp_delta(raw.get("fatigue_delta")),
        trust_delta=_clamp_delta(raw.get("trust_delta")),
        energy_delta=_clamp_delta(raw.get("energy_delta")),
        current_intent=current_intent,
    )


def _clamp_delta(raw: Any) -> int:
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, (int, float)):
        val = int(raw)
        return max(-_DELTA_CLAMP, min(_DELTA_CLAMP, val))
    return 0


_MAX_EMOTION_EVENTS = 5
"""Cap per turn. Realistic conversations rarely produce more — beyond
this the model is usually splitting one feeling into duplicates."""

_MAX_EVIDENCE_CHARS = 200
_MIN_HALF_LIFE_MIN = 30
_MAX_HALF_LIFE_MIN = 60 * 24 * 14  # 14 days


def _clamp_float(raw: Any, *, lo: float, hi: float, default: float) -> float:
    if isinstance(raw, bool):
        return default
    if isinstance(raw, (int, float)):
        return max(lo, min(hi, float(raw)))
    return default


def _parse_emotion_events(raw: Any) -> list[EmotionEventCandidate]:
    if not isinstance(raw, list):
        return []
    out: list[EmotionEventCandidate] = []
    for entry in raw[:_MAX_EMOTION_EVENTS]:
        if not isinstance(entry, dict):
            continue
        label = entry.get("emotion_label")
        evidence = entry.get("evidence_quote")
        label_str = label.strip() if isinstance(label, str) else ""
        evidence_str = (
            evidence.strip()[:_MAX_EVIDENCE_CHARS]
            if isinstance(evidence, str) else ""
        )
        half_life_raw = entry.get("decay_half_life_minutes")
        if isinstance(half_life_raw, bool):
            half_life = 240
        elif isinstance(half_life_raw, (int, float)):
            half_life = max(
                _MIN_HALF_LIFE_MIN,
                min(_MAX_HALF_LIFE_MIN, int(half_life_raw)),
            )
        else:
            half_life = 240
        out.append(EmotionEventCandidate(
            emotion_label=label_str,
            evidence_quote=evidence_str,
            valence=_clamp_float(entry.get("valence"), lo=-1.0, hi=1.0, default=0.0),
            arousal=_clamp_float(entry.get("arousal"), lo=0.0, hi=1.0, default=0.0),
            intensity=_clamp_float(entry.get("intensity"), lo=0.0, hi=1.0, default=0.5),
            affection_delta=_clamp_delta(entry.get("affection_delta")),
            fatigue_delta=_clamp_delta(entry.get("fatigue_delta")),
            trust_delta=_clamp_delta(entry.get("trust_delta")),
            energy_delta=_clamp_delta(entry.get("energy_delta")),
            decay_half_life_minutes=half_life,
        ))
    return out


# ------------------------------------------------------------------
# Schedule adjustment parsing
# ------------------------------------------------------------------

def _parse_adjustments(
    raw: Any,
    *,
    known_activity_ids: set[str],
) -> list[ScheduleAdjustment]:
    if not isinstance(raw, list):
        return []
    out: list[ScheduleAdjustment] = []
    for entry in raw[:_MAX_ADJUSTMENTS]:
        if not isinstance(entry, dict):
            continue
        action_raw = entry.get("action")
        if not isinstance(action_raw, str):
            continue
        action = action_raw.strip().lower()
        if action not in _ALLOWED_ADJUSTMENT_ACTIONS:
            continue

        activity_id = entry.get("activity_id")
        if not isinstance(activity_id, str) or not activity_id.strip():
            activity_id = None
        else:
            activity_id = activity_id.strip()

        # remove / modify must target a known id — otherwise skip the
        # adjustment to avoid applying a guess.
        if action in ("remove", "modify"):
            if activity_id is None or activity_id not in known_activity_ids:
                continue

        start = _coerce_time_string(entry.get("start"))
        end = _coerce_time_string(entry.get("end"))
        description = _coerce_adj_text(entry.get("description"), limit=_MAX_DESC_CHARS)
        category = _coerce_adj_text(entry.get("category"), limit=_MAX_CATEGORY_CHARS)
        location = _coerce_adj_text(entry.get("location"), limit=_MAX_LOCATION_CHARS)
        busy_score = _coerce_adj_busy(entry.get("busy_score"))
        operator_involvement = _coerce_operator_involvement(
            entry.get("operator_involvement"),
        )
        operator_display_name = _coerce_adj_text(
            entry.get("operator_display_name"),
            limit=40,
        )
        reason = _coerce_adj_text(entry.get("reason"), limit=_MAX_REASON_CHARS)
        target_date_iso = _coerce_iso_date(entry.get("target_date_iso"))

        # ``add`` needs enough info to build a real activity — reject if
        # the core fields are missing.
        if action == "add":
            if start is None or end is None or not description or not category:
                continue
        else:
            # remove / modify never carry a future date — those operate
            # on known today-existing activities, identified by id.
            target_date_iso = None

        out.append(
            ScheduleAdjustment(
                action=action,
                activity_id=activity_id,
                start=start,
                end=end,
                description=description,
                category=category,
                location=location,
                busy_score=busy_score,
                operator_involvement=operator_involvement,
                operator_display_name=operator_display_name,
                reason=reason,
                target_date_iso=target_date_iso,
            )
        )
    return out


def _coerce_operator_involvement(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    aliases = {
        "confirmed_shared": OPERATOR_CONFIRMED_SHARED_ROLE,
        OPERATOR_CONFIRMED_SHARED_ROLE: OPERATOR_CONFIRMED_SHARED_ROLE,
        "invite_pending": OPERATOR_INVITE_PENDING_ROLE,
        OPERATOR_INVITE_PENDING_ROLE: OPERATOR_INVITE_PENDING_ROLE,
        "wish": OPERATOR_WISH_ROLE,
        OPERATOR_WISH_ROLE: OPERATOR_WISH_ROLE,
    }
    return aliases.get(value)


def _coerce_iso_date(raw: Any) -> str | None:
    """Accept an ISO-8601 date string and normalise it.

    Returns the canonical ``YYYY-MM-DD`` form on success, ``None``
    when the value is missing / malformed / out of range. ScheduleService
    runs its own parse on the way in, so this only guards against
    obviously broken LLM output (``"tomorrow"`` / ``"2026/05/19"``).
    """
    from datetime import date as _date_cls

    if raw is None or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        return _date_cls.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _coerce_time_string(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    # light validation — accept H:MM or HH:MM
    parts = text.split(":")
    if len(parts) < 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if hour == 24 and minute == 0:
        return "24:00"
    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def _coerce_adj_text(raw: Any, *, limit: int) -> str | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()[:limit]
    return text or None


def _coerce_adj_busy(raw: Any) -> float | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        return max(0.0, min(1.0, float(raw)))
    if isinstance(raw, str):
        try:
            return max(0.0, min(1.0, float(raw.strip())))
        except ValueError:
            return None
    return None
