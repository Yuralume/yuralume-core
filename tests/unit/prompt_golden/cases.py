"""The golden-corpus case matrix.

Each :class:`GoldenCase` is one frozen call into
``DefaultPromptContextBuilder.build()``. The snapshot it produces is the
byte-identical oracle DH4b/DH5 refactors are measured against, so what
matters here is *branch coverage*, not realism: every mutually-exclusive
path inside ``build()`` must have a case standing on each side of it.

``branches`` is the machine-checkable part. :data:`BRANCH_PAIRS` lists
the either/or decisions ``build()`` makes; a structural test asserts both
sides of every pair are claimed by at least one case, so adding a new
exclusive branch to ``build()`` without covering both sides fails loudly
instead of silently leaving a refactor unguarded.

``markers`` / ``absent_markers`` are the human-readable part: they pin
*why* a case exists, so a snapshot that silently stops exercising its
branch (because a default changed upstream) turns red on the marker
assertion rather than quietly re-freezing as a weaker oracle.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from tests.unit.prompt_golden import factories as F


@dataclass(frozen=True, slots=True)
class GoldenCase:
    """One frozen ``build()`` invocation plus its coverage claims."""

    name: str
    """Snapshot file stem. Stable — renaming orphans the snapshot."""

    summary: str
    """One line on what this case is guarding."""

    branches: tuple[str, ...]
    """Branch ids from :data:`BRANCH_PAIRS` this case stands on."""

    build_kwargs: Callable[[], Mapping[str, object]]
    """Called fresh per render; never share mutable objects between cases."""

    markers: tuple[str, ...] = ()
    """Substrings that must appear in the rendered prompt."""

    absent_markers: tuple[str, ...] = ()
    """Substrings that must NOT appear — the suppressed side of a branch."""

    builder_kwargs: Mapping[str, object] = field(default_factory=dict)
    """Constructor kwargs for ``DefaultPromptContextBuilder``."""


# --------------------------------------------------------------------
# branch ledger
# --------------------------------------------------------------------

BRANCH_PAIRS: tuple[tuple[str, str], ...] = (
    ("story_scene.active", "story_scene.absent"),
    ("today_scene.rendered", "today_scene.suppressed_by_story_scene"),
    ("material_digest.hit", "material_digest.miss"),
    ("self_reflection.rendered", "self_reflection.suppressed"),
    ("body_state.rendered", "body_state.suppressed"),
    ("subjective_time.on", "subjective_time.off"),
    ("content_tolerance.frontier", "content_tolerance.community"),
    ("stage_nudge.on", "stage_nudge.off"),
    ("latest_user_message.rendered", "latest_user_message.omitted"),
    ("texting_style.on", "texting_style.off"),
    ("tools.present", "tools.absent"),
    ("tool_outcomes.present", "tool_outcomes.absent"),
    # HV2. Two pairs rather than one because the capability set is a
    # tri-state: undeclared is its own side, and the honesty section is
    # required to stay silent on it rather than guess an absence.
    ("tool_capabilities.declared", "tool_capabilities.undeclared"),
    ("browsing.available", "browsing.unavailable"),
    ("vision_markers.present", "vision_markers.absent"),
    ("image_recognition.present", "image_recognition.absent"),
    ("older_dialogue_summary.present", "older_dialogue_summary.absent"),
    ("retry_directive.present", "retry_directive.absent"),
    ("address_change.present", "address_change.absent"),
    ("operator_persona.present", "operator_persona.absent"),
    ("operator_identity.resolved", "operator_identity.absent"),
    ("history_gap.marker", "history_gap.none"),
    ("persona_curiosity.present", "persona_curiosity.absent"),
    ("schedule.present", "schedule.absent"),
)

MARKER_STORY_SCENE = "【劇情場景進行中】"
MARKER_TODAY_SCENE = "【今日場景指引"
MARKER_DIGEST = "近期素材事實摘要（已去除原文文體；只作事實參照）："
MARKER_EMOTION_EVENTS = "最近的情緒事件"
MARKER_SELF_REFLECTION = "內在敘事（你最近回頭整理自己生活時寫下的心情筆記"
MARKER_STORY_EVENTS = "今天你身上發生的小事"
MARKER_STORY_ARC = "你正在經歷的一段故事"
MARKER_RECENT_FEED = "你最近在動態牆上發過的貼文"
MARKER_LATEST_USER = "最新使用者訊息："
MARKER_OLDER_SUMMARY = "較早對話摘要（較舊輪次，系統壓縮）："
MARKER_RETRY = "上一輪嘗試的問題："
MARKER_ADDRESS_CHANGE = "稱呼變更（關係事件"
MARKER_OPERATOR_IDENTITY = "對方身份（即角色設定中所說的「使用者」）："
MARKER_VISION_LEGEND = "圖片標記：下方對話中共有"
MARKER_IMAGE_RECOGNITION = "[圖片識別摘要："
MARKER_SCHEDULE = "角色今日行程"
MARKER_TEXTING_STYLE = "手機即時通訊文體"
MARKER_PERSONA_CURIOSITY = "自然認識對方的提示："
MARKER_HISTORY_GAP = "——（中間隔了"
MARKER_HISTORY_GAP_TRAILING = "以下才是這次的新訊息）——"
MARKER_BODY_STATE = "身體訊號（事實層；自然體現於對話、不需直白報告）："
MARKER_HONESTY = "誠實界線（不管這一輪有沒有工具可用，這段都成立）："
MARKER_NO_BROWSING = "這個環境沒有給你上網的能力："
MARKER_SUBJECTIVE_TIME = (
    "主觀時間（話題層事實，與情緒層 idle drift 分離；話題選擇參考用，請勿照字面覆述）："
)

_LONG_IDLE_MINUTES = 4320.0
"""Three days — comfortably past the subjective-time catch-up threshold."""


# --------------------------------------------------------------------
# shared kwarg shapes
# --------------------------------------------------------------------


def _required(character, *, latest: str = "在嗎？") -> dict[str, object]:
    """The six parameters ``build()`` has no default for."""
    return {
        "character": character,
        "conversation": F.conversation(character),
        "recent_messages": [],
        "memories": [],
        "pending_state": character.state,
        "latest_user_message": latest,
    }


def _material_rich(**overrides: object) -> dict[str, object]:
    """A turn where nearly every optional block has something to render.

    Shared by the digest-miss and digest-hit cases so the only difference
    between those two snapshots is the digest itself — which is exactly
    what makes the five-block suppression readable in a diff."""
    character = F.rich_character()
    kwargs: dict[str, object] = {
        **_required(character, latest="今天排練還好嗎？"),
        "recent_messages": F.recent_messages(),
        "memories": F.memories(),
        "pending_state": F.pending_state(),
        "now": F.NOW,
        "today_local": F.TODAY,
        "idle_minutes": 9.0,
        "operator": F.operator(),
        "resolved_player_address": F.resolved_player_address(),
        "resolved_character_address": F.resolved_character_address(),
        "operator_persona_lines": F.operator_persona_lines(),
        "player_persona_note": "他說過自己很怕在人前唱歌。",
        "peer_roster_lines": F.peer_roster_lines(),
        "initial_relationship_lines": F.initial_relationship_lines(),
        "active_goals": F.active_goals(),
        "current_activity": F.current_activity(),
        "upcoming_activities": F.upcoming_activities(),
        "just_finished_activity": F.just_finished_activity(),
        "completed_today_activities": F.completed_today_activities(),
        "pending_invite_activities": F.pending_invite_activities(),
        "upcoming_day_schedules": F.upcoming_day_schedules(),
        "calendar_context": F.calendar_context(),
        "weather_context": F.weather_context(),
        "world_event_context": F.world_event_context(),
        "world_event_recall": F.world_event_recall(),
        "story_events": F.story_events(),
        "story_arc": F.story_arc_with_today_beat(),
        "upcoming_arc_beats": F.upcoming_arc_beats(),
        "emotion_events": F.emotion_events(),
        "self_reflections": F.self_reflections(),
        "recent_feed_posts": F.recent_feed_posts(),
        "recent_proactive_messages": F.recent_proactive_messages(),
        "persona_curiosity_plan": F.persona_curiosity_plan(),
        "turn_register_profile": F.turn_register_profile(),
        "reply_diversity_evidence": F.reply_diversity_evidence(),
        "self_repetition_hint": "最近很常以「其實我」開頭。",
        "phrase_habit_lines": F.phrase_habit_lines(),
    }
    kwargs.update(overrides)
    return kwargs


_MATERIAL_BLOCK_MARKERS = (
    MARKER_EMOTION_EVENTS,
    MARKER_SELF_REFLECTION,
    MARKER_STORY_EVENTS,
    MARKER_STORY_ARC,
    MARKER_RECENT_FEED,
)


# --------------------------------------------------------------------
# the matrix
# --------------------------------------------------------------------

GOLDEN_CASES: tuple[GoldenCase, ...] = (
    GoldenCase(
        name="minimal",
        summary="Only the six required parameters — every optional block silent.",
        branches=(
            "story_scene.absent",
            "material_digest.miss",
            "content_tolerance.frontier",
            "stage_nudge.off",
            "latest_user_message.rendered",
            "texting_style.off",
            "tools.absent",
            "tool_outcomes.absent",
            "vision_markers.absent",
            "image_recognition.absent",
            "older_dialogue_summary.absent",
            "retry_directive.absent",
            "address_change.absent",
            "operator_persona.absent",
            "operator_identity.absent",
            "history_gap.none",
            "persona_curiosity.absent",
            "schedule.absent",
            "tool_capabilities.undeclared",
        ),
        markers=("角色設定：", MARKER_LATEST_USER, "近期對話：", MARKER_HONESTY),
        absent_markers=(
            MARKER_STORY_SCENE,
            MARKER_TODAY_SCENE,
            MARKER_DIGEST,
            MARKER_SCHEDULE,
            MARKER_OPERATOR_IDENTITY,
            MARKER_VISION_LEGEND,
            MARKER_IMAGE_RECOGNITION,
            MARKER_TEXTING_STYLE,
            MARKER_OLDER_SUMMARY,
            MARKER_RETRY,
            MARKER_ADDRESS_CHANGE,
            MARKER_PERSONA_CURIOSITY,
        ),
        build_kwargs=lambda: _required(F.minimal_character(), latest="嗨"),
    ),
    GoldenCase(
        name="full_material",
        summary="Digest miss: all five material blocks render side by side.",
        branches=(
            "material_digest.miss",
            "self_reflection.rendered",
            "body_state.rendered",
            "today_scene.rendered",
            "story_scene.absent",
            "operator_identity.resolved",
            "operator_persona.present",
            "persona_curiosity.present",
            "schedule.present",
            "history_gap.none",
        ),
        markers=(*_MATERIAL_BLOCK_MARKERS, MARKER_TODAY_SCENE, MARKER_SCHEDULE,
                 MARKER_OPERATOR_IDENTITY, MARKER_PERSONA_CURIOSITY,
                 MARKER_BODY_STATE),
        absent_markers=(MARKER_DIGEST, MARKER_STORY_SCENE),
        build_kwargs=lambda: _material_rich(),
    ),
    GoldenCase(
        name="material_digest_hit",
        summary="Digest hit: the same turn with five material blocks cleared.",
        branches=("material_digest.hit", "self_reflection.suppressed"),
        markers=(MARKER_DIGEST, MARKER_TODAY_SCENE),
        absent_markers=_MATERIAL_BLOCK_MARKERS,
        build_kwargs=lambda: _material_rich(material_digest=F.material_digest()),
    ),
    GoldenCase(
        name="today_scene_active",
        summary="Today's beat directive with no live scene above it.",
        branches=("today_scene.rendered", "story_scene.absent"),
        markers=(MARKER_TODAY_SCENE, "練團室", "她要承認自己練得不夠嗎？"),
        absent_markers=(MARKER_STORY_SCENE,),
        build_kwargs=lambda: {
            **_required(F.rich_character(), latest="今天要去練團嗎？"),
            "now": F.NOW,
            "today_local": F.TODAY,
            "story_arc": F.story_arc_with_today_beat(),
        },
    ),
    GoldenCase(
        name="story_scene_active",
        summary="Live 起幕 scene replaces the beat directive (exactly-one rule).",
        branches=("story_scene.active", "today_scene.suppressed_by_story_scene"),
        markers=(MARKER_STORY_SCENE, "悶熱、器材嗡嗡作響"),
        absent_markers=(MARKER_TODAY_SCENE,),
        build_kwargs=lambda: {
            **_required(F.rich_character(), latest="我坐這裡等你。"),
            "now": F.NOW,
            "today_local": F.TODAY,
            "story_arc": F.story_arc_with_today_beat(),
            "story_scene": F.story_scene_session(),
        },
    ),
    GoldenCase(
        name="tools_and_outcomes",
        summary="Tool catalogue, a forced tool, and one ok / one failed outcome.",
        branches=(
            "tools.present",
            "tool_outcomes.present",
            "tool_capabilities.declared",
            "browsing.available",
        ),
        markers=("web_search", "send_photo", MARKER_HONESTY),
        absent_markers=(MARKER_NO_BROWSING,),
        build_kwargs=lambda: {
            **_required(F.rich_character(), latest="幫我查一下補助案。"),
            "now": F.NOW,
            "today_local": F.TODAY,
            "available_tools": F.available_tools(),
            "character_tool_names": ("web_search", "send_photo"),
            "tool_outcomes": F.tool_outcomes(),
            "forced_tool_name": "web_search",
        },
    ),
    GoldenCase(
        name="no_browsing_capability",
        summary=(
            "A declared capability set with no web tool: the honesty "
            "section says so out loud."
        ),
        branches=("tool_capabilities.declared", "browsing.unavailable"),
        markers=(MARKER_HONESTY, MARKER_NO_BROWSING),
        absent_markers=("web_search",),
        build_kwargs=lambda: {
            **_required(F.rich_character(), latest="這個連結你幫我看一下？"),
            "now": F.NOW,
            "today_local": F.TODAY,
            # The shape the line exists for: a character that can draw but
            # cannot look anything up, on the final hop where the tool rail
            # is hidden and ``available_tools`` is therefore empty.
            "character_tool_names": ("send_photo",),
        },
    ),
    GoldenCase(
        name="nsfw_frontier_sanitized",
        summary="Frontier tolerance drops the NSFW-marked turn from history.",
        branches=("content_tolerance.frontier",),
        markers=("今天過得還好嗎？", "那我們晚點再聊。"),
        absent_markers=("僅在 community 容忍度下才會進 prompt",),
        build_kwargs=lambda: {
            **_required(F.rich_character(), latest="還在嗎？"),
            "recent_messages": F.nsfw_mixed_messages(),
            "now": F.NOW,
            "today_local": F.TODAY,
            "content_tolerance": F.FRONTIER,
        },
    ),
    GoldenCase(
        name="nsfw_community_retained",
        summary="Community tolerance keeps the same turn verbatim.",
        branches=("content_tolerance.community",),
        markers=("僅在 community 容忍度下才會進 prompt",),
        build_kwargs=lambda: {
            **_required(F.rich_character(), latest="還在嗎？"),
            "recent_messages": F.nsfw_mixed_messages(),
            "now": F.NOW,
            "today_local": F.TODAY,
            "content_tolerance": F.COMMUNITY,
        },
    ),
    GoldenCase(
        name="experiment_overlay_off",
        summary="Overlay 'off' collapses self-reflection, body state, catch-up.",
        branches=(
            "self_reflection.suppressed",
            "body_state.suppressed",
            "subjective_time.off",
        ),
        absent_markers=(
            MARKER_SELF_REFLECTION, MARKER_BODY_STATE, MARKER_SUBJECTIVE_TIME,
        ),
        build_kwargs=lambda: {
            **_required(F.rich_character(), latest="好久不見。"),
            "recent_messages": F.recent_messages(),
            "now": F.NOW,
            "today_local": F.TODAY,
            "idle_minutes": _LONG_IDLE_MINUTES,
            "self_reflections": F.self_reflections(),
            "experiment_overlay": {
                "self_reflection": "off",
                "body_state": "off",
                "subjective_time": "off",
            },
        },
    ),
    GoldenCase(
        name="subjective_time_catchup",
        summary="Same long idle gap with no overlay — the catch-up hint renders.",
        branches=("subjective_time.on", "self_reflection.rendered",
                  "body_state.rendered"),
        markers=(MARKER_SUBJECTIVE_TIME, MARKER_BODY_STATE,
                 MARKER_SELF_REFLECTION),
        build_kwargs=lambda: {
            **_required(F.rich_character(), latest="好久不見。"),
            "recent_messages": F.recent_messages(),
            "now": F.NOW,
            "today_local": F.TODAY,
            "idle_minutes": _LONG_IDLE_MINUTES,
            "self_reflections": F.self_reflections(),
        },
    ),
    GoldenCase(
        name="stage_nudge_silent",
        summary="示意 with no player line — the 最新使用者訊息 slot is omitted.",
        branches=("stage_nudge.on", "latest_user_message.omitted"),
        absent_markers=(MARKER_LATEST_USER,),
        build_kwargs=lambda: {
            **_required(F.rich_character(), latest=""),
            "recent_messages": F.recent_messages(),
            "now": F.NOW,
            "today_local": F.TODAY,
            "stage_nudge": True,
        },
    ),
    GoldenCase(
        name="stage_nudge_messaging",
        summary="Same 示意 on a texting-style channel takes the neutral wording.",
        branches=("stage_nudge.on", "texting_style.on", "latest_user_message.omitted"),
        markers=(MARKER_TEXTING_STYLE,),
        build_kwargs=lambda: {
            **_required(F.rich_character(), latest=""),
            "recent_messages": F.recent_messages(),
            "now": F.NOW,
            "today_local": F.TODAY,
            "presence_frame": F.messaging_presence_frame(),
            "operator": F.operator(),
            "stage_nudge": True,
        },
    ),
    GoldenCase(
        name="retry_directive",
        summary="Retry feedback block sits between the player line and the footer.",
        branches=("retry_directive.present",),
        markers=(MARKER_RETRY,),
        build_kwargs=lambda: {
            **_required(F.rich_character(), latest="你剛剛那句是什麼意思？"),
            "recent_messages": F.recent_messages(),
            "now": F.NOW,
            "today_local": F.TODAY,
            "retry_directive": "上一輪只是把近況換句話重講，沒有帶出任何新的事。",
        },
    ),
    GoldenCase(
        name="vision_markers_and_recognition",
        summary="Cross-turn [圖 N] inventory plus the text-only recognition summary.",
        branches=("vision_markers.present", "image_recognition.present"),
        markers=(MARKER_VISION_LEGEND, MARKER_IMAGE_RECOGNITION,
                 "你自己稍早傳給對方的圖", "使用者這一輪剛傳來的圖"),
        build_kwargs=lambda: {
            **_required(F.rich_character(), latest="這張你看得懂嗎？"),
            "recent_messages": F.recent_messages(),
            "now": F.NOW,
            "today_local": F.TODAY,
            "vision_markers": F.vision_markers(),
            "image_recognition_context": F.image_recognition_context(),
        },
    ),
    GoldenCase(
        name="address_change_lines",
        summary="Latest rename per direction, right under the operator identity.",
        branches=("address_change.present", "operator_identity.resolved"),
        markers=(MARKER_ADDRESS_CHANGE, MARKER_OPERATOR_IDENTITY),
        build_kwargs=lambda: {
            **_required(F.rich_character(), latest="這樣叫你可以嗎？"),
            "recent_messages": F.recent_messages(),
            "now": F.NOW,
            "today_local": F.TODAY,
            "operator": F.operator(),
            "resolved_player_address": F.resolved_player_address(),
            "resolved_character_address": F.resolved_character_address(),
            "address_change_lines": F.address_change_lines(),
        },
    ),
    GoldenCase(
        name="operator_persona_five_layers",
        summary="All five persona layers plus the self-check block they gate.",
        branches=("operator_persona.present", "persona_curiosity.present",
                  "operator_identity.resolved"),
        markers=("- Layer 5 脆弱面：很怕在人前唱歌（請以保護姿態對待）。",
                 MARKER_PERSONA_CURIOSITY),
        build_kwargs=lambda: {
            **_required(F.rich_character(), latest="你今天怎麼樣？"),
            "recent_messages": F.recent_messages(),
            "now": F.NOW,
            "today_local": F.TODAY,
            "operator": F.operator(),
            "resolved_player_address": F.resolved_player_address(),
            "operator_persona_lines": F.operator_persona_lines(),
            "player_persona_note": "他說過自己很怕在人前唱歌。",
            "peer_roster_lines": F.peer_roster_lines(),
            "initial_relationship_lines": F.initial_relationship_lines(),
            "persona_curiosity_plan": F.persona_curiosity_plan(),
        },
    ),
    GoldenCase(
        name="older_dialogue_summary",
        summary="Compressed older-turn summary above the raw transcript.",
        branches=("older_dialogue_summary.present",),
        markers=(MARKER_OLDER_SUMMARY,),
        build_kwargs=lambda: {
            **_required(F.rich_character(), latest="所以你到底決定了沒？"),
            "recent_messages": F.recent_messages(),
            "now": F.NOW,
            "today_local": F.TODAY,
            "older_dialogue_summary": (
                "更早的幾輪裡，兩人談到唱片行可能搬家，"
                "以及她一直沒把副歌寫完這件事。"
            ),
        },
    ),
    GoldenCase(
        name="history_gap_markers",
        summary="Multi-sitting transcript: in-line separator plus trailing seam.",
        branches=("history_gap.marker",),
        markers=(MARKER_HISTORY_GAP, MARKER_HISTORY_GAP_TRAILING),
        build_kwargs=lambda: {
            **_required(F.rich_character(), latest="早安。"),
            "recent_messages": F.gapped_messages(),
            "now": F.NOW,
            "today_local": F.TODAY,
        },
    ),
    GoldenCase(
        name="schedule_and_world",
        summary="Schedule / calendar / weather / world-event rails without story.",
        branches=("schedule.present",),
        markers=(MARKER_SCHEDULE, "陣雨", "補助案"),
        absent_markers=(MARKER_STORY_ARC,),
        build_kwargs=lambda: {
            **_required(F.rich_character(), latest="你現在在幹嘛？"),
            "recent_messages": F.recent_messages(),
            "now": F.NOW,
            "today_local": F.TODAY,
            "operator": F.operator(),
            "current_activity": F.current_activity(),
            "upcoming_activities": F.upcoming_activities(),
            "just_finished_activity": F.just_finished_activity(),
            "completed_today_activities": F.completed_today_activities(),
            "pending_invite_activities": F.pending_invite_activities(),
            "upcoming_day_schedules": F.upcoming_day_schedules(),
            "calendar_context": F.calendar_context(),
            "weather_context": F.weather_context(),
            "world_event_context": F.world_event_context(),
            "world_event_recall": F.world_event_recall(),
        },
    ),
)

CASES_BY_NAME: dict[str, GoldenCase] = {case.name: case for case in GOLDEN_CASES}
