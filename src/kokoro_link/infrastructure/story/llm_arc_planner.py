"""LLM-backed ``StoryArcPlannerPort`` implementation.

Produces a multi-week arc in a single LLM call: title + premise +
theme + a sequence of beats with scheduled_date offsets and tension
markers. Same JSON-parsing tolerance as the memory / schedule
planners — code fences and preamble are stripped before JSON decode.

On failure (LLM error / bad JSON / empty beats) we return a sparse
synthetic arc built from a generic template so the service layer
always has *something* to persist and the UI never shows "planning
failed" as a dead end — the operator can still edit it.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta
from typing import Any

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.story_arc import StoryArcPlannerPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_arc import (
    SCENE_CONFLICT,
    SCENE_ENCOUNTER,
    SCENE_INTERLUDE,
    SCENE_RESOLUTION,
    SCENE_REVELATION,
    StoryArc,
    StoryArcBeat,
    TENSION_CLIMAX,
    TENSION_FALLING,
    TENSION_RESOLUTION,
    TENSION_RISING,
    TENSION_SETUP,
    normalise_operator_position,
)
from kokoro_link.domain.entities.story_seed import StorySeed
from kokoro_link.infrastructure.prompt.character_identity import (
    render_character_identity_lines,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompts import get_default_loader
from kokoro_link.infrastructure.story.date_context import (
    format_absolute_day,
    render_story_date_context_block,
)

_LOGGER = logging.getLogger(__name__)

_CANONICAL_TENSIONS = (
    TENSION_SETUP, TENSION_RISING, TENSION_CLIMAX,
    TENSION_FALLING, TENSION_RESOLUTION,
)
_CANONICAL_SCENE_TYPES = (
    SCENE_ENCOUNTER, SCENE_REVELATION, SCENE_CONFLICT,
    SCENE_RESOLUTION, SCENE_INTERLUDE,
)
_FENCE_RE = re.compile(r"```(?:\w+)?\n?")
_MAX_BEATS = 7
_MIN_BEATS = 3
# Cap scene_characters per beat — keeps prompts predictable; if a
# planner returns 20 names something is wrong with its output.
_MAX_SCENE_CHARACTERS = 6
# Seed candidate text is one line by construction; the clamp only
# protects the prompt from a pathological hand-edited seed.
_MAX_SEED_TEXT_CHARS = 160
# Same bound ``StoryArc`` normalisation applies — reading more than this
# out of a model response would only be discarded downstream.
_MAX_SEED_IDS_USED = 8
# Arc-history digests arrive pre-formatted from the service; the clamp
# is a prompt-size guard, not a content rule.
_MAX_ARC_HISTORY_LINE_CHARS = 200
# ``operator_note`` is one sentence about the player's dramatic place in
# a scene; same budget as ``dramatic_question``, which it sits beside.
_MAX_OPERATOR_NOTE_CHARS = 120
# Relationship facts arrive pre-rendered from the service (one label per
# line). The clamp only guards the prompt against a pathological seed.
_MAX_OPERATOR_RELATIONSHIP_LINE_CHARS = 200


class NullStoryArcPlanner(StoryArcPlannerPort):
    """Template planner used when no real LLM is wired.

    Generates an arc from a built-in template so unit tests and the
    fake-provider dev flow exercise the same service paths as the
    real planner.
    """

    async def plan_arc(
        self,
        *,
        character: Character,
        start_date: date,
        duration_days: int = 21,
        beat_count_hint: int = 5,
        hint: str | None = None,
        recent_dialogue_summary: str = "",
        operator_primary_language: str = "zh-TW",
        today: date | None = None,
        seed_candidates: tuple[StorySeed, ...] = (),
        arc_history: tuple[str, ...] = (),
        operator_relationship_lines: tuple[str, ...] = (),
    ) -> StoryArc:
        # ``today`` only shapes the LLM prompt; the template fallback has
        # no dates in its prose so it is deliberately ignored here. Seed
        # candidates, arc history and the relationship material are
        # prompt-only inputs for the same reason: this path never calls a
        # model, so there is nothing to weave them into, no provenance to
        # record, and no judgement to make about where the player stands
        # (the template's beats stay ``None`` = unjudged).
        del today, seed_candidates, arc_history, operator_relationship_lines
        return _synthetic_arc(
            character=character,
            start_date=start_date,
            duration_days=duration_days,
            beat_count=max(_MIN_BEATS, min(beat_count_hint, _MAX_BEATS)),
            hint=hint,
            operator_primary_language=operator_primary_language,
        )


class LLMStoryArcPlanner(StoryArcPlannerPort):
    def __init__(
        self,
        *,
        model: ChatModelPort | None = None,
        provider: ActiveLLMProviderPort | None = None,
        feature_key: str | None = None,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider, model=model, feature_key=feature_key,
        )

    async def plan_arc(
        self,
        *,
        character: Character,
        start_date: date,
        duration_days: int = 21,
        beat_count_hint: int = 5,
        hint: str | None = None,
        recent_dialogue_summary: str = "",
        operator_primary_language: str = "zh-TW",
        today: date | None = None,
        seed_candidates: tuple[StorySeed, ...] = (),
        arc_history: tuple[str, ...] = (),
        operator_relationship_lines: tuple[str, ...] = (),
    ) -> StoryArc:
        if await self._resolver.is_fake(character=character):
            return _synthetic_arc(
                character=character,
                start_date=start_date,
                duration_days=duration_days,
                beat_count=max(_MIN_BEATS, min(beat_count_hint, _MAX_BEATS)),
                hint=hint,
                operator_primary_language=operator_primary_language,
            )
        prompt = _build_prompt(
            character=character,
            start_date=start_date,
            duration_days=duration_days,
            beat_count_hint=max(_MIN_BEATS, min(beat_count_hint, _MAX_BEATS)),
            hint=hint,
            recent_dialogue_summary=recent_dialogue_summary,
            operator_primary_language=operator_primary_language,
            today=today,
            seed_candidates=seed_candidates,
            arc_history=arc_history,
            operator_relationship_lines=operator_relationship_lines,
        )
        try:
            raw = await self._resolver.generate(prompt, character=character)
        except Exception:
            _LOGGER.exception("story arc planner LLM call failed")
            return _synthetic_arc(
                character=character,
                start_date=start_date,
                duration_days=duration_days,
                beat_count=max(_MIN_BEATS, min(beat_count_hint, _MAX_BEATS)),
                hint=hint,
                operator_primary_language=operator_primary_language,
            )

        parsed = _parse_plan(raw)
        if parsed is None:
            _LOGGER.warning("story arc planner: unparseable LLM output")
            return _synthetic_arc(
                character=character,
                start_date=start_date,
                duration_days=duration_days,
                beat_count=max(_MIN_BEATS, min(beat_count_hint, _MAX_BEATS)),
                hint=hint,
                operator_primary_language=operator_primary_language,
            )

        title, premise, theme, beats_raw, claimed_seed_ids = parsed
        beats = _build_beats(beats_raw, start_date=start_date, duration_days=duration_days)
        if not beats:
            return _synthetic_arc(
                character=character,
                start_date=start_date,
                duration_days=duration_days,
                beat_count=max(_MIN_BEATS, min(beat_count_hint, _MAX_BEATS)),
                hint=hint,
                operator_primary_language=operator_primary_language,
            )
        arc_id_placeholder = ""  # populated by StoryArc.create
        end_date = max(b["scheduled_date"] for b in beats)
        # end_date defaults to last beat day; extend at least to requested duration
        desired_end = start_date + timedelta(days=duration_days)
        end_date = max(end_date, desired_end)

        # Provenance is recorded only for seeds we actually offered. A
        # model that invents ids (or echoes ids from an older prompt)
        # must not be able to write them into the arc row — the
        # intersection is the whole trust boundary here.
        offered_ids = {seed.id for seed in seed_candidates}
        used_seed_ids = tuple(
            sid for sid in claimed_seed_ids if sid in offered_ids
        )
        arc = StoryArc.create(
            character_id=character.id,
            title=title or f"{character.name}的故事",
            premise=premise or "一段新的生活篇章。",
            theme=theme or "custom",
            start_date=start_date,
            end_date=end_date,
            source_seed_ids=used_seed_ids,
        )
        # Re-create beats with arc_id populated now we have the id.
        beat_entities = [
            StoryArcBeat.create(
                arc_id=arc.id,
                sequence=i,
                scheduled_date=b["scheduled_date"],
                title=b["title"],
                summary=b["summary"],
                tension=b["tension"],
                scene_characters=b["scene_characters"],
                location=b["location"],
                dramatic_question=b["dramatic_question"],
                scene_type=b["scene_type"],
                required=b["required"],
                operator_position=b["operator_position"],
                operator_note=b["operator_note"],
            )
            for i, b in enumerate(beats)
        ]
        return arc.with_beats(beat_entities)


# --- helpers ---------------------------------------------------------


def _build_prompt(
    *,
    character: Character,
    start_date: date,
    duration_days: int,
    beat_count_hint: int,
    hint: str | None,
    recent_dialogue_summary: str = "",
    operator_primary_language: str = "zh-TW",
    today: date | None = None,
    seed_candidates: tuple[StorySeed, ...] = (),
    arc_history: tuple[str, ...] = (),
    operator_relationship_lines: tuple[str, ...] = (),
) -> str:
    personality = "、".join(character.personality) or "（未設定）"
    interests = "、".join(character.interests) or "（未設定）"
    aspirations = "、".join(character.aspirations) if character.aspirations else "（未設定）"
    hint_line = (
        _optional_block([f"使用者給的方向：{hint.strip()}"])
        if hint and hint.strip()
        else ""
    )
    dialogue_line = _render_dialogue_block(recent_dialogue_summary)
    body = get_default_loader().render(
        "story/arc_planner",
        date_context_block=_render_date_context(
            start_date=start_date,
            duration_days=duration_days,
            today=today,
            language_tag=operator_primary_language,
        ),
        duration_days=duration_days,
        beat_count_hint=beat_count_hint,
        character_name=character.name,
        character_summary=character.summary or "（未設定）",
        identity_block="\n".join(render_character_identity_lines(character)),
        personality=personality,
        interests=interests,
        aspirations=aspirations,
        world_frame=character.world_frame or "modern",
        operator_block=_render_operator_relationship_block(
            operator_relationship_lines,
        ),
        dialogue_block=dialogue_line,
        seed_block=_render_seed_candidates_block(seed_candidates),
        history_block=_render_arc_history_block(arc_history),
        hint_block=hint_line,
        min_beats=_MIN_BEATS,
        max_beats=_MAX_BEATS,
    )
    language_hint = render_operator_language_hint(operator_primary_language)
    return f"{language_hint}\n\n{body}" if language_hint else body


def _optional_block(lines: list[str]) -> str:
    """Join one optional prompt section, terminated by a blank line.

    Every optional section of the arc-planner prompt (dialogue context,
    seed candidates, arc history, operator hint) uses this shape, and the
    template concatenates them with no separator of its own
    (``${dialogue_block}${seed_block}…``). Consequence: whichever
    sections are present are separated by exactly one blank line, and the
    absent ones contribute *nothing* — no orphan heading, no accumulating
    blank lines. Adding a new optional section means adding another
    adjacent placeholder, never another template line.
    """
    return "\n".join(lines) + "\n\n"


def _render_operator_relationship_block(lines: tuple[str, ...]) -> str:
    """OP1-A — who the player is to this character, in the planner's words.

    Before this block the planner's only knowledge of the player was the
    continuity constraint "promises already made must still hold": it
    could not name them, did not know how close they stand, and was
    therefore in no position to decide whether a beat is *about* them.
    Facts arrive pre-rendered from the service (same contract as
    ``arc_history``); the instruction on what to do with them lives here,
    beside the ``operator_position`` schema it exists to serve.

    Empty tuple renders as the empty string — no heading, no placeholder.
    A section titled "the player's relationship" with nothing under it is
    worse than silence: the model treats the hole as something to fill
    and invents a relationship nobody agreed to.
    """
    entries = []
    for raw in lines:
        cleaned = " ".join(str(raw).split())
        if not cleaned:
            continue
        if len(cleaned) > _MAX_OPERATOR_RELATIONSHIP_LINE_CHARS:
            cleaned = cleaned[:_MAX_OPERATOR_RELATIONSHIP_LINE_CHARS] + "…"
        entries.append(cleaned)
    if not entries:
        return ""
    return _optional_block([
        "玩家（使用者本人）與這個角色的關係（使用者創角時確認的設定）：",
        *entries,
        "用途：決定每顆 beat 的 operator_position 與 operator_note 時，你需要知道玩家在這個"
        "角色的生活裡是什麼位置、被怎麼稱呼、能靠多近。beat.summary 與 operator_note 提到玩家時，"
        "請用上面這個稱呼，不要另外發明一個。",
        "邊界：這裡沒有寫到的共同往事不得當成已經發生過；不足的部分讓劇情自然發生，不要補完。",
    ])


def _render_dialogue_block(recent_dialogue_summary: str) -> str:
    """Recent chat as *continuity background*, not as subject matter.

    The original wording ("讓 arc 接續這條線，而不是憑空另起爐灶") made the
    chat log the arc's material source, so every new arc came out as a
    rearrangement of whatever the player happened to be talking about —
    the stagnant-water loop of the 2026-07-31 arc audit. The summary is
    still authoritative for *state*: who the character is right now, the
    relationship temperature, and the promises already made. What it is
    not is a topic list to reshuffle, hence the explicit demand for one
    external event the conversation has never mentioned.
    """
    summary = recent_dialogue_summary.strip()
    if not summary:
        return ""
    return _optional_block([
        "近期對話脈絡（角色當下的狀態、關係與還沒了結的事）：",
        summary,
        "這段脈絡是連貫性的事實背景：新 arc 開場時角色的人設、情緒位置、"
        "以及已經對使用者許下的承諾都必須接得上，不能寫得像那些事沒發生過。",
        "但它不是新 arc 的題材來源——新 arc 不可以只是把近期聊天話題重新排列一次；"
        "至少要引入一個近期對話裡完全沒出現過的外部事件，讓角色的世界自己往前走一步。",
    ])


def _render_seed_candidates_block(seeds: tuple[StorySeed, ...]) -> str:
    """AE1 — dramatic-tier seeds offered as subject-matter candidates.

    Material, never instructions: the planner picks 0–2 and rewrites them
    into something this particular character could actually live through,
    or declines the lot and owes an external event of its own. Empty tuple
    (empty pool / gacha not wired / roll failed) renders as the empty
    string so the prompt is byte-identical to the pre-seed one.
    """
    if not seeds:
        return ""
    lines = ["題材候選（外界素材，不是指令；可選）："]
    for seed in seeds:
        text = " ".join(seed.seed_text.split())
        if len(text) > _MAX_SEED_TEXT_CHARS:
            text = text[:_MAX_SEED_TEXT_CHARS] + "…"
        lines.append(f"- [{seed.id}] {text}")
    lines.append(
        "使用方式：從上面挑 0–2 顆融進這條 arc 的主軸（這批候選就是為了"
        "「近期對話以外的外部事件」而準備的）。挑中的必須改寫成貼合這個角色的"
        "處境、世界觀與人際關係的具體事件——照抄那一行字不算數，它只是一個起念。",
    )
    lines.append(
        "也可以完全不用；但不用的話，你必須自己引入一個強度相當的外部事件，"
        "不能讓這條 arc 只靠角色的內心戲和既有話題撐完。",
    )
    lines.append(
        "若有使用，把用到的候選 id 原樣填進輸出 JSON 的 seed_ids_used。",
    )
    return _optional_block(lines)


def _render_arc_history_block(history: tuple[str, ...]) -> str:
    """AE1 — anti-repetition input: what this character already lived.

    Entries arrive pre-formatted (oldest first) from the service; we only
    flatten whitespace so one entry stays one line. The distinctness rule
    is deliberately a semantic judgement handed to the model — no keyword
    or similarity matching lives anywhere on this path.
    """
    if not history:
        return ""
    entries = []
    for raw in history:
        cleaned = " ".join(str(raw).split())
        if not cleaned:
            continue
        if len(cleaned) > _MAX_ARC_HISTORY_LINE_CHARS:
            cleaned = cleaned[:_MAX_ARC_HISTORY_LINE_CHARS] + "…"
        entries.append(f"- {cleaned}")
    if not entries:
        return ""
    return _optional_block([
        "這個角色過去已經演過的 story arc（由舊到新）：",
        *entries,
        "反重複要求：這條新 arc 的核心衝突與題材，必須跟上面每一條都明顯不同。"
        "同一個主題換個場地、換個配角、換個說法的變奏也算重複——"
        "請自己語意判斷，寧可換一個方向，也不要交出讀起來像已經演過的那一條。",
    ])


def _render_date_context(
    *,
    start_date: date,
    duration_days: int,
    today: date | None,
    language_tag: str | None,
) -> str:
    """Absolute-date coordinates the planner needs (CF1b).

    Beat prose outlives the day it was written by weeks, so the planner
    is told today's civil date, the arc's own window, and the
    relative-word → absolute-date table. ``today`` falls back to
    ``start_date`` because callers that never resolved a civil day
    (direct API use, older planner wiring) still start the arc *now* in
    every path except a mid-arc replan — an anchor one arc-length off is
    strictly better than no anchor at all.
    """
    anchor_day = today or start_date
    end_date = start_date + timedelta(days=duration_days)
    return render_story_date_context_block(
        anchor_day,
        language_tag=language_tag,
        extra_lines=(
            f"- 這條 arc 的起始日：{format_absolute_day(start_date, language_tag)}",
            f"- 這條 arc 的預計結束日：{format_absolute_day(end_date, language_tag)}",
            "- 每個 beat 的實際日期＝起始日 + 該 beat 的 day_offset 天"
            f"（day_offset 0 ＝ {start_date.isoformat()}）。",
        ),
    )


def _parse_plan(
    raw: str,
) -> tuple[str, str, str, list[dict[str, Any]], tuple[str, ...]] | None:
    """Decode the planner response. ``None`` == unusable, fall back.

    The trailing tuple element is the optional ``seed_ids_used``
    provenance; it is the *only* field whose absence or malformation is
    not allowed to matter — see ``_coerce_seed_ids``.
    """
    text = _FENCE_RE.sub("", raw or "").replace("```", "").strip()
    # Find the outermost JSON object.
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    blob = text[start : end + 1]
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    beats = data.get("beats")
    if not isinstance(beats, list) or not beats:
        return None
    title = _coerce_str(data.get("title"))
    premise = _coerce_str(data.get("premise"))
    theme = _coerce_str(data.get("theme")) or "custom"
    return title, premise, theme, beats, _coerce_seed_ids(data.get("seed_ids_used"))


def _coerce_seed_ids(raw: Any) -> tuple[str, ...]:
    """Optional seed provenance — never a reason to reject a plan.

    ``seed_ids_used`` is bookkeeping: a model that omits the key, hands
    back a bare string, a dict, or a list of numbers costs us the
    provenance for one arc and nothing else, so every unusable shape
    degrades silently to ``()``. The caller still intersects whatever
    survives with the ids actually offered, which is where invented ids
    are stopped.
    """
    if not isinstance(raw, list):
        return ()
    ids: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        cleaned = entry.strip()
        if cleaned and cleaned not in ids:
            ids.append(cleaned)
        if len(ids) >= _MAX_SEED_IDS_USED:
            break
    return tuple(ids)


def _build_beats(
    beats_raw: list[dict[str, Any]],
    *,
    start_date: date,
    duration_days: int,
) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for entry in beats_raw[: _MAX_BEATS * 2]:
        if not isinstance(entry, dict):
            continue
        offset = _coerce_int(entry.get("day_offset"))
        if offset is None or offset < 0:
            continue
        offset = min(offset, duration_days)
        title = _coerce_str(entry.get("title"))
        summary = _coerce_str(entry.get("summary"))
        if not title or not summary:
            continue
        tension = _coerce_tension(entry.get("tension"))
        cleaned.append({
            "scheduled_date": start_date + timedelta(days=offset),
            "title": title[:80],
            "summary": summary[:400],
            "tension": tension,
            "scene_type": _coerce_scene_type(entry.get("scene_type")),
            "location": _coerce_optional_str(entry.get("location"), max_len=80),
            "scene_characters": _coerce_scene_characters(
                entry.get("scene_characters"),
            ),
            "dramatic_question": _coerce_optional_str(
                entry.get("dramatic_question"), max_len=120,
            ),
            "required": _coerce_required(entry.get("required")),
            "operator_position": _coerce_operator_position(
                entry.get("operator_position"),
            ),
            "operator_note": _coerce_optional_str(
                entry.get("operator_note"),
                max_len=_MAX_OPERATOR_NOTE_CHARS,
            ),
        })
    cleaned.sort(key=lambda b: b["scheduled_date"])
    return cleaned[:_MAX_BEATS]


def _coerce_str(raw: Any) -> str:
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


def _coerce_tension(raw: Any) -> str:
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in _CANONICAL_TENSIONS:
            return lowered
    return TENSION_RISING


def _coerce_scene_type(raw: Any) -> str:
    """Permissive scene_type extraction.

    Returns the canonical value when the planner stays on-list;
    otherwise drops back to ``encounter`` so a new shade of label
    doesn't break the whole arc. We only canonicalise; the domain
    layer accepts unknown values too (the prompt builder degrades
    them to encounter semantics).
    """
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in _CANONICAL_SCENE_TYPES:
            return lowered
    return SCENE_ENCOUNTER


def _coerce_operator_position(raw: Any) -> str | None:
    """Ingest side of the closed player-position vocabulary (OP1-A).

    ``normalise_operator_position`` is strict by design — the domain
    boundary must not let a fourth position exist — but a planner
    response is untrusted input: an older model that never saw the field,
    one that answers ``"spectator"``, and one that answers ``42`` must
    all still produce a usable arc. So every rejection lands on ``None``
    (*unjudged*), which is exactly what a beat planned before this field
    existed reads back as, and every consumer already derives a framing
    for it semantically.

    Note what is deliberately absent: no synonym table, no "watching ⇒
    present" mapping. The vocabulary is taught in the prompt; a model
    that answers outside it has not judged, and pretending otherwise
    would be this path inventing a position the model never chose.
    """
    try:
        return normalise_operator_position(raw)
    except ValueError:
        return None


def _coerce_optional_str(raw: Any, *, max_len: int) -> str | None:
    if isinstance(raw, str):
        cleaned = raw.strip()
        if cleaned:
            return cleaned[:max_len]
    return None


def _coerce_scene_characters(raw: Any) -> tuple[str, ...]:
    """Tolerate both list-of-string and a single comma-separated string.

    Some smaller / older LLMs return ``"A, B"`` instead of ``["A", "B"]``;
    we accept either shape so a single planner output style doesn't
    silently lose all NPC labels.
    """
    if raw is None:
        return ()
    if isinstance(raw, str):
        parts = [chunk.strip() for chunk in raw.split(",")]
        names = tuple(p for p in parts if p)[:_MAX_SCENE_CHARACTERS]
        return names
    if isinstance(raw, list):
        names: list[str] = []
        for entry in raw:
            if not isinstance(entry, str):
                continue
            cleaned = entry.strip()
            if cleaned and cleaned not in names:
                names.append(cleaned)
            if len(names) >= _MAX_SCENE_CHARACTERS:
                break
        return tuple(names)
    return ()


def _coerce_required(raw: Any) -> bool:
    """Default to ``True`` — main-line semantics is the safer fallback.

    Pre-Phase-1 planner outputs (no ``required`` key) end up here too;
    treating them as required matches the prior behaviour where every
    beat was effectively required.
    """
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() not in {"false", "0", "no", "n", ""}
    if isinstance(raw, (int, float)):
        return bool(raw)
    return True


# A single beat template: (title, summary, tension, scene_type,
# location, dramatic_question). ``location`` is always None here — the
# synthetic fallback is deliberately location-agnostic.
_SyntheticBeat = tuple[str, str, str, str, "str | None", "str | None"]


class _SyntheticArcTemplatePack:
    """Localized static strings for the LLM-free fallback arc.

    Grouped per BCP-47 language so the fallback arc reads in the
    operator's language instead of always being zh-TW. Beats carry the
    same scene-structure shape as a real LLM output so callers reading a
    synthetic arc get the same downstream prompt behaviour (today's beat
    block, expander hints, etc.).
    """

    __slots__ = ("_title_fmt", "premise", "beats")

    def __init__(
        self,
        *,
        title_fmt: str,
        premise: str,
        beats: list[_SyntheticBeat],
    ) -> None:
        self._title_fmt = title_fmt
        self.premise = premise
        self.beats = beats

    def title(self, character_name: str) -> str:
        return self._title_fmt.format(name=character_name)


# NOTE (LLM-first reviewers): the hardcoded translations below are
# intentional and scoped to the model-free fallback path only — see the
# exemption note in ``_synthetic_arc``. Do not "fix" this into an LLM
# call; the whole point of this path is that no model is available.
_SYNTHETIC_ARC_TEMPLATES: dict[str, _SyntheticArcTemplatePack] = {
    "zh-TW": _SyntheticArcTemplatePack(
        title_fmt="{name}的下一段故事",
        premise=(
            "角色生活的下一段篇章 —— 由系統先起個頭，"
            "後續可由操作者或對話一起把它填滿。"
        ),
        beats=[
            ("新的起點", "一段新的篇章開始了。今天有什麼和平常不太一樣的小信號。",
             TENSION_SETUP, SCENE_ENCOUNTER, None, "今天的小信號意味著什麼？"),
            ("開始動起來", "事情開始有了進展，但也出現了第一個需要克服的困難。",
             TENSION_RISING, SCENE_CONFLICT, None, "她要怎麼面對這個困難？"),
            ("中段的拉扯", "進展卡住了。開始懷疑自己，也開始思考真正想要的是什麼。",
             TENSION_RISING, SCENE_REVELATION, None, "她真正想要的到底是什麼？"),
            ("關鍵一天", "今天是決定性的一天。結果會把整條路帶往不同方向。",
             TENSION_CLIMAX, SCENE_CONFLICT, None, "她能在關鍵時刻守住自己嗎？"),
            ("餘波", "結果之後的第一個清晨，世界還在，但已經不太一樣了。",
             TENSION_FALLING, SCENE_INTERLUDE, None, "她要怎麼消化昨天的結果？"),
            ("新的平衡", "新的節奏落定。這不是結局，而是下一段生活的起點。",
             TENSION_RESOLUTION, SCENE_RESOLUTION, None, "新的日常會長什麼樣子？"),
            ("再次出發", "帶著這段日子的痕跡，走向下一個階段。",
             TENSION_RESOLUTION, SCENE_INTERLUDE, None, "下一段路會帶她去哪裡？"),
        ],
    ),
    "en-US": _SyntheticArcTemplatePack(
        title_fmt="{name}'s Next Chapter",
        premise=(
            "The next chapter of the character's life — the system sketches "
            "an opening, and the operator or the ongoing conversation fills "
            "it in from here."
        ),
        beats=[
            ("A New Start",
             "A new chapter begins. Today carries a small signal that things "
             "are not quite the same as usual.",
             TENSION_SETUP, SCENE_ENCOUNTER, None,
             "What does today's small signal mean?"),
            ("Getting Moving",
             "Things start to move forward, but the first obstacle worth "
             "overcoming appears.",
             TENSION_RISING, SCENE_CONFLICT, None,
             "How will she face this obstacle?"),
            ("The Middle Pull",
             "Progress stalls. Doubt creeps in, and she starts to ask what "
             "she truly wants.",
             TENSION_RISING, SCENE_REVELATION, None,
             "What is it she really wants?"),
            ("The Decisive Day",
             "Today is the day that decides things. The outcome will send the "
             "whole path in a different direction.",
             TENSION_CLIMAX, SCENE_CONFLICT, None,
             "Can she hold on to herself at the crucial moment?"),
            ("The Aftermath",
             "The first morning after the outcome. The world is still here, "
             "but it is not quite the same.",
             TENSION_FALLING, SCENE_INTERLUDE, None,
             "How will she make sense of yesterday?"),
            ("A New Balance",
             "A new rhythm settles in. This is not an ending, but the start "
             "of the next stretch of life.",
             TENSION_RESOLUTION, SCENE_RESOLUTION, None,
             "What will the new everyday look like?"),
            ("Setting Out Again",
             "Carrying the marks of these days, she moves toward the next "
             "stage.",
             TENSION_RESOLUTION, SCENE_INTERLUDE, None,
             "Where will the next road take her?"),
        ],
    ),
    "ja-JP": _SyntheticArcTemplatePack(
        title_fmt="{name}の次の物語",
        premise=(
            "キャラクターの人生の次の章 —— システムがまず口火を切り、"
            "その先は操作者や会話が一緒に埋めていく。"
        ),
        beats=[
            ("新しい始まり",
             "新しい章が始まった。今日はいつもと少し違う小さな兆しがある。",
             TENSION_SETUP, SCENE_ENCOUNTER, None,
             "今日の小さな兆しは何を意味するのだろう？"),
            ("動き出す",
             "物事が前に進み始めるが、最初に乗り越えるべき壁が現れる。",
             TENSION_RISING, SCENE_CONFLICT, None,
             "彼女はこの壁にどう向き合うのだろう？"),
            ("中盤の揺れ",
             "前進が止まる。自分を疑い始め、本当に望むものは何かを考え始める。",
             TENSION_RISING, SCENE_REVELATION, None,
             "彼女が本当に望むものは何なのか？"),
            ("決定的な一日",
             "今日はすべてを決める日。その結果が道全体を別の方向へ導く。",
             TENSION_CLIMAX, SCENE_CONFLICT, None,
             "彼女は肝心なときに自分を守れるだろうか？"),
            ("余波",
             "結果のあとの最初の朝。世界はまだあるが、もう少し違っている。",
             TENSION_FALLING, SCENE_INTERLUDE, None,
             "彼女は昨日をどう受け止めるのだろう？"),
            ("新しい均衡",
             "新しいリズムが定まる。これは終わりではなく、次の生活の始まりだ。",
             TENSION_RESOLUTION, SCENE_RESOLUTION, None,
             "新しい日常はどんな姿になるのだろう？"),
            ("再び歩き出す",
             "この日々の痕跡を抱えて、次の段階へと歩き出す。",
             TENSION_RESOLUTION, SCENE_INTERLUDE, None,
             "次の道は彼女をどこへ連れて行くのだろう？"),
        ],
    ),
}

_SYNTHETIC_ARC_FALLBACK_LANGUAGE = "zh-TW"


def _synthetic_template_pack(language_tag: str | None) -> _SyntheticArcTemplatePack:
    """Pick the localized fallback template pack for a BCP-47 tag.

    Falls back to the documented ``zh-TW`` default for unknown /
    unsupported tags. Matches on the exact tag first, then the language
    subtag (so ``en-GB`` still resolves to the ``en`` family via
    ``en-US``)."""
    tag = (language_tag or "").strip()
    if tag in _SYNTHETIC_ARC_TEMPLATES:
        return _SYNTHETIC_ARC_TEMPLATES[tag]
    subtag = tag.split("-", 1)[0].lower() if tag else ""
    for known_tag, pack in _SYNTHETIC_ARC_TEMPLATES.items():
        if known_tag.split("-", 1)[0].lower() == subtag and subtag:
            return pack
    return _SYNTHETIC_ARC_TEMPLATES[_SYNTHETIC_ARC_FALLBACK_LANGUAGE]


def _synthetic_arc(
    *,
    character: Character,
    start_date: date,
    duration_days: int,
    beat_count: int,
    hint: str | None,
    operator_primary_language: str = "zh-TW",
) -> StoryArc:
    """Template fallback — produces a plausible, editable arc.

    LLM-FIRST EXEMPTION: this is the deliberately model-free path (fake
    provider, LLM error, unparseable output). There is no model call to
    carry the operator-language fact into, so — and only here — the
    player-visible strings are hardcoded per shipped locale. This is not
    keyword-matching business logic; it is a static, editable placeholder
    the operator or the next real LLM plan immediately supersedes. New
    shipped languages get a new entry in ``_SYNTHETIC_ARC_TEMPLATES``.
    """
    pack = _synthetic_template_pack(operator_primary_language)
    title = (hint or "").strip() or pack.title(character.name)
    premise = pack.premise
    theme = "custom"
    arc = StoryArc.create(
        character_id=character.id,
        title=title,
        premise=premise,
        theme=theme,
        start_date=start_date,
        end_date=start_date + timedelta(days=duration_days),
    )
    span = max(1, duration_days // max(1, beat_count - 1))
    count = max(_MIN_BEATS, min(beat_count, _MAX_BEATS))
    picked = pack.beats[:count]
    beats: list[StoryArcBeat] = []
    for i, (btitle, bsum, tension, scene_type, location, question) in enumerate(picked):
        offset = min(duration_days, i * span)
        beats.append(
            StoryArcBeat.create(
                arc_id=arc.id,
                sequence=i,
                scheduled_date=start_date + timedelta(days=offset),
                title=btitle,
                summary=bsum,
                tension=tension,
                scene_type=scene_type,
                location=location,
                dramatic_question=question,
                required=True,
            )
        )
    return arc.with_beats(beats)
