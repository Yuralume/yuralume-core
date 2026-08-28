"""Runtime director for branching-drama sessions.

Three operations:
- ``narrate`` — given a node outline + character briefs + previous turns,
  generates visual-novel-style scene narration.
- ``respond_in_scene`` — given player input within the current beat,
  generates an in-scene response and optionally suggests advancement.
- ``classify_tone`` — given accumulated exchanges + 3 child summaries,
  determines which tonal branch (dark / sunny / neutral) the
  interactions lean toward.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Any

from kokoro_link.application.services.fusion_character_brief import (
    CharacterBrief,
)
from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.branching_drama import (
    DEFAULT_OPERATOR_POSITION,
    TONE_DARK,
    TONE_NEUTRAL,
    TONE_SUNNY,
    DramaNode,
    DramaSessionTurn,
    Exchange,
    VALID_TONES,
)
from kokoro_link.infrastructure.prompt.drama_operator_position_lines import (
    STAGE_DIRECTOR,
    STAGE_DIRECTOR_INTERACT,
    render_drama_interact_framing,
    render_drama_operator_position_lines,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.llm_output import (
    OBJECT_REGION,
    ParseReason,
    extract_object_outcome,
    first_balanced_region,
    iter_embedded_json,
    log_parse_outcome,
)


_LOGGER = logging.getLogger(__name__)
_FENCE_RE = re.compile(r"```(?:\w+)?\n?")
_TURN_SUMMARY_BUDGET = 800
_PREVIOUS_TAIL_BUDGET = 280
"""Characters from the end of the previous turn surfaced verbatim so
the new narration can land an actual 承接 句 (a real opening that picks
up the closing beat) rather than just inferring from a summary."""


class BranchingDramaDirector:
    def __init__(
        self,
        *,
        provider: ActiveLLMProviderPort | None = None,
        model: ChatModelPort | None = None,
        feature_key: str | None = None,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider, model=model, feature_key=feature_key,
        )

    async def narrate(
        self,
        *,
        node: DramaNode,
        briefs: Sequence[CharacterBrief],
        previous_turns: Sequence[DramaSessionTurn],
        player_input: str = "",
        operator_primary_language: str = "zh-TW",
        operator_position: str = DEFAULT_OPERATOR_POSITION,
        operator_note: str | None = None,
    ) -> str:
        if await self._resolver.is_fake():
            return _synthetic_narration(node)

        prompt = _build_narrate_prompt(
            node=node,
            briefs=briefs,
            previous_turns=previous_turns,
            player_input=player_input,
            operator_primary_language=operator_primary_language,
            operator_position=operator_position,
            operator_note=operator_note,
        )
        try:
            raw = await self._resolver.generate(prompt)
        except Exception:
            _LOGGER.exception(
                "branching drama: narrate failed node=%s", node.id,
            )
            return _synthetic_narration(node)
        return raw.strip() if raw else _synthetic_narration(node)

    async def respond_in_scene(
        self,
        *,
        node: DramaNode,
        briefs: Sequence[CharacterBrief],
        previous_turns: Sequence[DramaSessionTurn],
        exchanges: Sequence[Exchange],
        player_input: str,
        operator_primary_language: str = "zh-TW",
        operator_position: str = DEFAULT_OPERATOR_POSITION,
        operator_note: str | None = None,
    ) -> tuple[str, str | None]:
        """Generate an in-scene response and optionally suggest advancement.

        Returns ``(response_text, advance_hint)``.  ``advance_hint`` is
        ``None`` when the beat still has room to explore, or a short
        phrase (for the advance button) when it feels complete.
        """
        if await self._resolver.is_fake():
            return f"（場景回應：{player_input}）", None

        prompt = _build_scene_response_prompt(
            node=node,
            briefs=briefs,
            previous_turns=previous_turns,
            exchanges=exchanges,
            player_input=player_input,
            operator_primary_language=operator_primary_language,
            operator_position=operator_position,
            operator_note=operator_note,
        )
        try:
            raw = await self._resolver.generate(prompt)
        except Exception:
            _LOGGER.exception(
                "branching drama: respond_in_scene failed node=%s", node.id,
            )
            return f"（場景回應生成失敗）", None
        return _parse_scene_response(raw)

    async def classify_tone(
        self,
        *,
        exchanges: Sequence[Exchange],
        children: dict[str, DramaNode],
    ) -> str:
        if await self._resolver.is_fake():
            return TONE_NEUTRAL

        prompt = _build_classify_prompt(
            exchanges=exchanges,
            children=children,
        )
        try:
            raw = await self._resolver.generate(prompt)
        except Exception:
            _LOGGER.exception("branching drama: classify failed")
            return TONE_NEUTRAL

        return _parse_tone(raw)


# ── prompt builders ───────────────────────────────────────────────────


def _build_narrate_prompt(
    *,
    node: DramaNode,
    briefs: Sequence[CharacterBrief],
    previous_turns: Sequence[DramaSessionTurn],
    player_input: str,
    operator_primary_language: str = "zh-TW",
    operator_position: str = DEFAULT_OPERATOR_POSITION,
    operator_note: str | None = None,
) -> str:
    appearing_ids = set(node.appearing_character_ids)
    relevant_briefs = [b for b in briefs if b.character_id in appearing_ids]
    if not relevant_briefs:
        relevant_briefs = list(briefs)

    brief_block = "\n\n".join(b.text for b in relevant_briefs)
    history_block = _summarise_turns(previous_turns)
    previous_tail = _extract_previous_tail(previous_turns)
    # Why we surface the tail verbatim *in addition to* the summary:
    # the summary tells the LLM what beats happened; the tail gives it
    # the actual closing prose to write a real opening against (so we
    # don't get "she went into the cafe" right after the previous
    # turn ended on "she pushed open the cafe door"). Same lever as
    # fusion's per-beat writer.
    tail_block = (
        f"\n上一幕的最後一段原文（**接續這裡寫，不要再重演**）：\n{previous_tail}\n"
        if previous_tail else ""
    )
    player_block = (
        f"\n玩家的行動/台詞：\n{player_input.strip()}\n"
        if player_input.strip() else ""
    )
    tone_note = (
        f"\n本段取向：{_tone_label(node.tone)}\n" if node.tone else ""
    )

    body = "\n".join([
        "你是「分歧劇場」的場景導演。",
        "請根據劇本綱要，以視覺小說 / 美少女遊戲的敘事風格演繹這個段落。",
        "",
        "出場角色資料：",
        brief_block,
        "",
        f"本段標題：{node.title}",
        f"劇本綱要：{node.summary}",
        tone_note,
        # BD2 — who the player is in this drama. Sits above 要求 so the
        # framing is context the requirements are written against, not one
        # more bullet competing with them.
        *render_drama_operator_position_lines(
            operator_position, operator_note, stage=STAGE_DIRECTOR,
        ),
        "",
        "之前發生的事：",
        history_block or "（這是第一幕，沒有前情）",
        tail_block,
        player_block,
        "",
        "要求：",
        "- 以小說敘事風格寫出這個段落",
        "- 讓每個出場角色都有台詞和動作，台詞要貼合角色的說話風格",
        "- 營造段落應有的氛圍",
        "- 篇幅 300~500 字",
        "- 段落文字是玩家可見自然語言，必須使用上方「玩家可見自然語言輸出語言（BCP 47 標籤）」指定的語言。",
        "- **不要重複「之前發生的事」的情緒進展、衝突點、標誌性措辭或停頓動作**",
        "- 直接輸出段落文字，不要標題、不要前言",
    ])
    language_hint = render_operator_language_hint(operator_primary_language)
    return f"{language_hint}\n\n{body}" if language_hint else body


def _build_scene_response_prompt(
    *,
    node: DramaNode,
    briefs: Sequence[CharacterBrief],
    previous_turns: Sequence[DramaSessionTurn],
    exchanges: Sequence[Exchange],
    player_input: str,
    operator_primary_language: str = "zh-TW",
    operator_position: str = DEFAULT_OPERATOR_POSITION,
    operator_note: str | None = None,
) -> str:
    appearing_ids = set(node.appearing_character_ids)
    relevant_briefs = [b for b in briefs if b.character_id in appearing_ids]
    if not relevant_briefs:
        relevant_briefs = list(briefs)

    brief_block = "\n\n".join(b.text for b in relevant_briefs)
    history_block = _summarise_turns(previous_turns)
    exchange_block = _summarise_exchanges(exchanges)
    tone_note = (
        f"\n本段取向：{_tone_label(node.tone)}\n" if node.tone else ""
    )
    # FX3 — the skeleton of this prompt (what the round is, what the
    # player's text is, what to produce from it) is itself framed by the
    # player's position. Written as protagonist-only literals it used to
    # contradict the spectator block further down, and unlike ``narrate``
    # this path has no critic pass to notice which framing won.
    framing = render_drama_interact_framing(operator_position)

    body = "\n".join([
        "你是「分歧劇場」的場景導演。",
        framing.brief,
        "",
        "出場角色資料：",
        brief_block,
        "",
        f"本段標題：{node.title}",
        f"劇本綱要：{node.summary}",
        tone_note,
        "",
        "之前發生的事：",
        history_block or "（這是第一幕，沒有前情）",
        "",
        "本段已有的互動：",
        exchange_block or "（尚無互動）",
        "",
        f"{framing.input_label}\n{player_input.strip()}",
        "",
        "要求：",
        framing.requirement,
        "- 推動場景氛圍，但不要跳出本段劇本綱要的範圍",
        "- 篇幅 150~300 字",
        "- response 與 advance_hint 都是玩家可見自然語言，必須使用上方「玩家可見自然語言輸出語言（BCP 47 標籤）」指定的語言。",
        # The position block closes the instructions rather than opening
        # them (as it does in ``narrate``): here it is the tie-breaker for
        # the fixed lines above, so it has to be read after them.
        *render_drama_operator_position_lines(
            operator_position, operator_note, stage=STAGE_DIRECTOR_INTERACT,
        ),
        "",
        "你的回覆必須是以下 JSON 格式（不加 markdown 標記）：",
        '{"response": "場景回應文字", "advance_hint": null}',
        "",
        "advance_hint 規則：",
        "- 如果這個場景還有值得探索的空間，設為 null",
        "- 如果互動已經充分演繹了本段綱要的內容、或場景來到一個自然的段落轉折點，",
        '  設為一個簡短的過渡提示（5~10字），例如 "離開酒館" "踏上旅程" "夜幕降臨"',
        "- 這個提示會顯示在按鈕上供玩家參考，不會出現在劇情文字中",
    ])
    language_hint = render_operator_language_hint(operator_primary_language)
    return f"{language_hint}\n\n{body}" if language_hint else body


def _build_classify_prompt(
    *,
    exchanges: Sequence[Exchange],
    children: dict[str, DramaNode],
) -> str:
    exchange_text = "\n".join(
        f"- 玩家：{ex.player_input}\n  回應：{ex.response}"
        for ex in exchanges
    ) if exchanges else "（無互動記錄，使用預設判斷）"

    lines = [
        "你是「分歧劇場」的導演。根據玩家在這個段落中的整體互動，判斷故事應該往哪個方向發展。",
        "",
        "玩家在本段的互動記錄：",
        exchange_text,
        "",
        "三個可能的方向：",
    ]
    for tone in (TONE_DARK, TONE_SUNNY, TONE_NEUTRAL):
        child = children.get(tone)
        if child:
            lines.append(
                f"- {tone}（{_tone_label(tone)}）：{child.summary}",
            )
    lines.extend([
        "",
        "綜合分析玩家在整段互動中的行為模式、語意和情緒傾向，判斷最契合哪個方向。",
        "只回覆一個詞：dark、sunny 或 neutral",
    ])
    return "\n".join(lines)


# ── helpers ───────────────────────────────────────────────────────────


def _tone_label(tone: str | None) -> str:
    labels = {
        TONE_DARK: "黑暗取向",
        TONE_SUNNY: "陽光取向",
        TONE_NEUTRAL: "中性取向",
    }
    return labels.get(tone or "", "")


def _summarise_exchanges(exchanges: Sequence[Exchange]) -> str:
    if not exchanges:
        return ""
    parts: list[str] = []
    for ex in exchanges[-6:]:
        parts.append(f"玩家：{ex.player_input.strip()}")
        parts.append(f"回應：{ex.response.strip()[:200]}")
    return "\n".join(parts)


def _carries_narration(value: object) -> bool:
    """Is this the scene envelope — i.e. does it carry a ``response``?

    A string ``response`` is the whole contract. Anything else (missing
    key, wrong type) means we are holding some other object.
    """
    return isinstance(value, dict) and isinstance(value.get("response"), str)


def _scene_object(raw: str, cleaned: str) -> dict[str, Any] | None:
    """The object this reply's narration lives in, or ``None`` for prose.

    Three answers, in order:

    1. the anchored extraction (first opener, truncation repair on) when
       it carries ``response`` — the overwhelmingly common case, and the
       one that recovers a reply chopped by ``max_tokens``;
    2. otherwise **any** later top-level region that carries it. L2-3: a
       reasoning-first model writes a small thought object before the
       envelope, and anchoring on the first opener then reads
       ``response`` out of the wrong object. That is worse than it
       sounds — a missing key comes back as ``""`` from ``.get``, which
       is a ``str``, so the JSON branch is taken and the player gets the
       "（回應生成失敗）" placeholder instead of the narration;
    3. otherwise the anchored object only if it *is* the entire reply.
       That is the pre-migration outcome for an envelope-shaped answer
       that simply forgot ``response`` (placeholder, not prose) and the
       differential pins it. An object with text around it is not that
       case, and falls through to the prose fallback the way it always
       did.
    """
    outcome = extract_object_outcome(raw)
    if outcome.reason is not ParseReason.NO_JSON:
        # L2-4: prose is a *designed-legal* reply on this path (see the
        # fallback below), so ``no_json`` is not a failure worth a
        # WARNING every turn. Same rule, same reason as
        # ``tool_call_parser``: log only when the model looks like it
        # tried to produce JSON and botched it.
        log_parse_outcome(
            _LOGGER, outcome, site="branching_drama.director.scene_response",
        )
    anchored = outcome.value
    if _carries_narration(anchored):
        return anchored
    for value in iter_embedded_json(raw):
        if _carries_narration(value):
            return value
    if isinstance(anchored, dict) and _is_the_whole_reply(cleaned):
        return anchored
    return None


def _is_the_whole_reply(cleaned: str) -> bool:
    """Does ``cleaned`` consist of exactly one object region, start to end?

    The structural spelling of the old code's "``json.loads`` on the
    whole fence-stripped reply succeeded". Deliberately balance-based
    rather than decode-based: a whole-reply object that fails to decode
    has nothing to hand back anyway, so widening here cannot change an
    outcome, while requiring a decode would reintroduce the guard that
    disappears the moment a model adds a trailing sentence.
    """
    region = first_balanced_region(cleaned)
    return (
        region is not None
        and region.kind == OBJECT_REGION
        and region.start == 0
        and region.end == len(cleaned) - 1
    )


def _parse_scene_response(raw: str) -> tuple[str, str | None]:
    if not raw:
        return "（回應生成失敗）", None
    cleaned = _strip_fences(raw).strip()
    # DH2-services: extraction goes through the shared scanner
    # (balanced-brace, truncation repair on — this prompt unconditionally
    # asks for the JSON envelope). The fallback quirk is preserved on
    # purpose: a reply that isn't a well-shaped ``{"response": str, ...}``
    # object — not JSON at all, or JSON with a non-string ``response`` —
    # is *not* a parse failure here. It falls back to treating the
    # fence-stripped raw text itself as the narration, because this path
    # has no other source of prose. Migrating to a strict ``None``-on-
    # failure extractor without keeping this branch would silently drop
    # replies that came back as plain prose instead of the JSON envelope.
    obj = _scene_object(raw, cleaned)
    if obj is not None:
        response_raw = obj.get("response", "")
        if isinstance(response_raw, str):
            response = response_raw.strip()
            hint = obj.get("advance_hint")
            hint = hint.strip() or None if isinstance(hint, str) else None
            return response or "（回應生成失敗）", hint
    return cleaned, None


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text).strip().rstrip("`")


def _extract_previous_tail(turns: Sequence[DramaSessionTurn]) -> str:
    """Take the last ~280 chars of the most recent turn's narration so
    the next narration can write against the actual closing prose.

    Picks the final paragraph (split on blank lines) first; falls back
    to the last ``_PREVIOUS_TAIL_BUDGET`` chars when the narration is
    one big block. Returns empty string when there's no prior turn.
    """
    if not turns:
        return ""
    last = turns[-1].narration.strip()
    if not last:
        return ""
    paragraphs = [p.strip() for p in last.split("\n\n") if p.strip()]
    if paragraphs:
        candidate = paragraphs[-1]
        if len(candidate) <= _PREVIOUS_TAIL_BUDGET:
            return candidate
    return last[-_PREVIOUS_TAIL_BUDGET:]


def _summarise_turns(turns: Sequence[DramaSessionTurn]) -> str:
    if not turns:
        return ""
    parts: list[str] = []
    budget = _TURN_SUMMARY_BUDGET
    for turn in reversed(turns):
        snippet = turn.narration.strip().replace("\n", " ")[:200]
        line = f"[{_tone_label(turn.chosen_tone) or '開場'}] {snippet}"
        if len(line) > budget and parts:
            break
        parts.append(line)
        budget -= len(line)
        if budget <= 0:
            break
    parts.reverse()
    return "\n".join(parts)


def _parse_tone(raw: str) -> str:
    if not raw:
        return TONE_NEUTRAL
    cleaned = raw.strip().lower().split()[0] if raw.strip() else ""
    for tone in VALID_TONES:
        if tone in cleaned:
            return tone
    return TONE_NEUTRAL


def _synthetic_narration(node: DramaNode) -> str:
    return (
        f"【{node.title}】\n\n"
        f"{node.summary}\n\n"
        "（場景敘事生成失敗，顯示綱要作為替代。）"
    )
