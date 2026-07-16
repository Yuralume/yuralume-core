"""Outline planner for the branching-drama tree.

Two operations:
- ``plan_root`` — generates the opening segment + drama title.
- ``plan_children`` — given a parent node's context, generates 3 tonal
  variants (dark / sunny / neutral) for the next segment.

Output is structured JSON validated into typed dicts. Bad shapes fall
back to synthetic templates so tree generation never stalls.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from kokoro_link.application.services.fusion_character_brief import (
    CharacterBrief,
)
from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.branching_drama import (
    TONE_DARK,
    TONE_NEUTRAL,
    TONE_SUNNY,
    VALID_TONES,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompts import get_default_loader


_LOGGER = logging.getLogger(__name__)
_FENCE_RE = re.compile(r"```(?:\w+)?\n?")


@dataclass(frozen=True, slots=True)
class NodeOutline:
    title: str
    summary: str
    appearing_character_ids: tuple[str, ...]


class BranchingDramaPlanner:
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

    async def plan_root(
        self,
        *,
        prompt: str,
        briefs: Sequence[CharacterBrief],
        total_segments: int,
        operator_primary_language: str = "zh-TW",
    ) -> tuple[str, NodeOutline]:
        """Returns (drama_title, root_outline)."""
        if await self._resolver.is_fake():
            return _synthetic_root(
                prompt, briefs,
                operator_primary_language=operator_primary_language,
            )

        system = _build_root_prompt(
            prompt=prompt,
            briefs=briefs,
            total_segments=total_segments,
            operator_primary_language=operator_primary_language,
        )
        try:
            raw = await self._resolver.generate(system)
        except Exception:
            _LOGGER.exception("branching drama: root plan LLM failed")
            return _synthetic_root(
                prompt, briefs,
                operator_primary_language=operator_primary_language,
            )

        parsed = _parse_root(
            raw, briefs, operator_primary_language=operator_primary_language,
        )
        if parsed is None:
            _LOGGER.warning("branching drama: unparseable root output")
            return _synthetic_root(
                prompt, briefs,
                operator_primary_language=operator_primary_language,
            )
        return parsed

    async def plan_children(
        self,
        *,
        prompt: str,
        briefs: Sequence[CharacterBrief],
        parent_summary: str,
        path_context: str,
        depth: int,
        total_segments: int,
        operator_primary_language: str = "zh-TW",
    ) -> dict[str, NodeOutline]:
        """Returns {tone: NodeOutline} for dark/sunny/neutral."""
        is_ending = depth == total_segments - 1
        if await self._resolver.is_fake():
            return _synthetic_children(
                briefs, is_ending,
                operator_primary_language=operator_primary_language,
            )

        system = _build_children_prompt(
            prompt=prompt,
            briefs=briefs,
            parent_summary=parent_summary,
            path_context=path_context,
            depth=depth,
            total_segments=total_segments,
            is_ending=is_ending,
            operator_primary_language=operator_primary_language,
        )
        try:
            raw = await self._resolver.generate(system)
        except Exception:
            _LOGGER.exception(
                "branching drama: children plan LLM failed depth=%s", depth,
            )
            return _synthetic_children(
                briefs, is_ending,
                operator_primary_language=operator_primary_language,
            )

        parsed = _parse_children(raw, briefs)
        if parsed is None:
            _LOGGER.warning(
                "branching drama: unparseable children output depth=%s",
                depth,
            )
            return _synthetic_children(
                briefs, is_ending,
                operator_primary_language=operator_primary_language,
            )
        return parsed


# ── prompt builders ───────────────────────────────────────────────────


def _build_root_prompt(
    *,
    prompt: str,
    briefs: Sequence[CharacterBrief],
    total_segments: int,
    operator_primary_language: str = "zh-TW",
) -> str:
    body = get_default_loader().render(
        "branching/planner_root",
        total_segments=total_segments,
        prompt_text=prompt.strip() or "（未指定，請依角色資料自行構思）",
        brief_block="\n\n".join(b.text for b in briefs),
        char_ids=", ".join(b.character_id for b in briefs),
    )
    language_hint = render_operator_language_hint(operator_primary_language)
    return f"{language_hint}\n\n{body}" if language_hint else body


def _build_children_prompt(
    *,
    prompt: str,
    briefs: Sequence[CharacterBrief],
    parent_summary: str,
    path_context: str,
    depth: int,
    total_segments: int,
    is_ending: bool,
    operator_primary_language: str = "zh-TW",
) -> str:
    ending_note = (
        "*** 這是最終段落，三種取向都必須推進到結局收束。 ***"
        if is_ending else ""
    )
    body = get_default_loader().render(
        "branching/planner_children",
        total_segments=total_segments,
        current_segment=depth + 1,
        ending_note=ending_note,
        prompt_text=prompt.strip(),
        brief_block="\n\n".join(b.text for b in briefs),
        parent_summary=parent_summary,
        path_context=path_context or "（這是從開場直接推進的第一次分歧）",
        char_ids=", ".join(b.character_id for b in briefs),
    )
    language_hint = render_operator_language_hint(operator_primary_language)
    return f"{language_hint}\n\n{body}" if language_hint else body


# ── parsers ───────────────────────────────────────────────────────────


def _strip_fences(raw: str) -> str:
    text = _FENCE_RE.sub("", raw or "").replace("```", "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return ""
    return text[start : end + 1]


def _parse_root(
    raw: str,
    briefs: Sequence[CharacterBrief],
    *,
    operator_primary_language: str = "zh-TW",
) -> tuple[str, NodeOutline] | None:
    blob = _strip_fences(raw)
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    pack = _synthetic_template_pack(operator_primary_language)
    drama_title = _str(data.get("drama_title")) or pack.untitled_drama
    root = data.get("root")
    if not isinstance(root, dict):
        return None
    outline = _parse_node_outline(root, briefs)
    if outline is None:
        return None
    return drama_title, outline


def _parse_children(
    raw: str, briefs: Sequence[CharacterBrief],
) -> dict[str, NodeOutline] | None:
    blob = _strip_fences(raw)
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    result: dict[str, NodeOutline] = {}
    for tone in (TONE_DARK, TONE_SUNNY, TONE_NEUTRAL):
        entry = data.get(tone)
        if not isinstance(entry, dict):
            return None
        outline = _parse_node_outline(entry, briefs)
        if outline is None:
            return None
        result[tone] = outline
    return result


def _parse_node_outline(
    entry: dict[str, Any], briefs: Sequence[CharacterBrief],
) -> NodeOutline | None:
    title = _str(entry.get("title"))
    summary = _str(entry.get("summary"))
    if not title or not summary:
        return None
    valid_ids = {b.character_id for b in briefs}
    appearing = _coerce_ids(
        entry.get("appearing_character_ids"), valid_ids,
    )
    return NodeOutline(
        title=title,
        summary=summary,
        appearing_character_ids=appearing,
    )


def _str(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def _coerce_ids(raw: Any, valid: set[str]) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[str] = []
    for entry in raw:
        if isinstance(entry, str):
            cleaned = entry.strip()
            if cleaned in valid and cleaned not in out:
                out.append(cleaned)
    return tuple(out)


# ── synthetic fallbacks ───────────────────────────────────────────────
#
# NOTE (LLM-first reviewers): the hardcoded per-locale strings below are
# intentional and scoped to the model-free fallback path only (fake
# provider, LLM error, unparseable output) — mirrors the exemption note
# in ``infrastructure/story/llm_arc_planner._synthetic_arc``. There is
# no model call in this path to carry the operator-language fact into,
# so the player-visible strings are static per shipped locale instead.
# This is not keyword-matching business logic; it is a static, editable
# placeholder immediately superseded by the next real LLM plan. New
# shipped languages get a new entry in ``_SYNTHETIC_DRAMA_TEMPLATES``.


@dataclass(frozen=True, slots=True)
class _SyntheticDramaTemplatePack:
    """Localized static strings for the LLM-free branching-drama fallback."""

    untitled_drama: str
    root_title: str
    root_summary_fmt: str
    dark_title: str
    dark_ending_title: str
    dark_summary: str
    sunny_title: str
    sunny_ending_title: str
    sunny_summary: str
    neutral_title: str
    neutral_ending_title: str
    neutral_summary: str
    ending_suffix: str
    plain_suffix: str
    name_join: str

    def root_summary(self, char_names: str) -> str:
        return self.root_summary_fmt.format(names=char_names)


_SYNTHETIC_DRAMA_TEMPLATES: dict[str, _SyntheticDramaTemplatePack] = {
    "zh-TW": _SyntheticDramaTemplatePack(
        untitled_drama="（未命名劇場）",
        root_title="序幕",
        root_summary_fmt=(
            "{names}在一個尋常的午後相遇，"
            "空氣中瀰漫著某種不安的預感。每個人都帶著自己的秘密。"
        ),
        dark_title="暗潮湧動",
        dark_ending_title="黑暗結局",
        dark_summary="氣氛急轉直下，角色之間的裂痕擴大",
        sunny_title="柳暗花明",
        sunny_ending_title="溫馨結局",
        sunny_summary="意外的善意化解了緊張，關係出現轉機",
        neutral_title="波瀾不驚",
        neutral_ending_title="平淡結局",
        neutral_summary="事態平穩推進，每個人各懷心思",
        ending_suffix="，故事迎來結局。",
        plain_suffix="。",
        name_join="、",
    ),
    "en-US": _SyntheticDramaTemplatePack(
        untitled_drama="(Untitled Drama)",
        root_title="Prologue",
        root_summary_fmt=(
            "{names} meet on an ordinary afternoon, an uneasy feeling "
            "hanging in the air. Everyone is carrying their own secret."
        ),
        dark_title="Undercurrents",
        dark_ending_title="A Dark Ending",
        dark_summary="The mood turns sharply — the cracks between them widen",
        sunny_title="A Break in the Clouds",
        sunny_ending_title="A Warm Ending",
        sunny_summary=(
            "An unexpected kindness eases the tension, and the "
            "relationship finds an opening"
        ),
        neutral_title="Steady Waters",
        neutral_ending_title="A Quiet Ending",
        neutral_summary=(
            "Things move forward evenly, each of them holding their own "
            "thoughts"
        ),
        ending_suffix=", and the story reaches its ending.",
        plain_suffix=".",
        name_join=", ",
    ),
    "ja-JP": _SyntheticDramaTemplatePack(
        untitled_drama="（無題の劇場）",
        root_title="序幕",
        root_summary_fmt=(
            "{names}はありふれた午後に出会う。空気には何か不穏な予感が"
            "漂っている。誰もが自分だけの秘密を抱えている。"
        ),
        dark_title="暗い潮流",
        dark_ending_title="暗い結末",
        dark_summary="空気が一変し、キャラクターたちの間の亀裂が広がる",
        sunny_title="差し込む光",
        sunny_ending_title="温かい結末",
        sunny_summary="思いがけない優しさが緊張をほぐし、関係に転機が訪れる",
        neutral_title="静かな水面",
        neutral_ending_title="穏やかな結末",
        neutral_summary="事態は落ち着いて進み、誰もがそれぞれの思いを抱えている",
        ending_suffix="、物語は結末を迎える。",
        plain_suffix="。",
        name_join="、",
    ),
}

_SYNTHETIC_DRAMA_FALLBACK_LANGUAGE = "zh-TW"


def _synthetic_template_pack(
    language_tag: str | None,
) -> _SyntheticDramaTemplatePack:
    """Pick the localized fallback template pack for a BCP-47 tag.

    Falls back to the documented ``zh-TW`` default for unknown /
    unsupported tags. Matches on the exact tag first, then the language
    subtag (mirrors ``llm_arc_planner._synthetic_template_pack``)."""
    tag = (language_tag or "").strip()
    if tag in _SYNTHETIC_DRAMA_TEMPLATES:
        return _SYNTHETIC_DRAMA_TEMPLATES[tag]
    subtag = tag.split("-", 1)[0].lower() if tag else ""
    for known_tag, pack in _SYNTHETIC_DRAMA_TEMPLATES.items():
        if known_tag.split("-", 1)[0].lower() == subtag and subtag:
            return pack
    return _SYNTHETIC_DRAMA_TEMPLATES[_SYNTHETIC_DRAMA_FALLBACK_LANGUAGE]


def _synthetic_root(
    prompt: str,
    briefs: Sequence[CharacterBrief],
    *,
    operator_primary_language: str = "zh-TW",
) -> tuple[str, NodeOutline]:
    pack = _synthetic_template_pack(operator_primary_language)
    char_names = pack.name_join.join(b.short_label() for b in briefs)
    title = (prompt.strip() or pack.untitled_drama)[:15]
    return title, NodeOutline(
        title=pack.root_title,
        summary=pack.root_summary(char_names),
        appearing_character_ids=tuple(b.character_id for b in briefs),
    )


def _synthetic_children(
    briefs: Sequence[CharacterBrief],
    is_ending: bool,
    *,
    operator_primary_language: str = "zh-TW",
) -> dict[str, NodeOutline]:
    pack = _synthetic_template_pack(operator_primary_language)
    all_ids = tuple(b.character_id for b in briefs)
    ending = pack.ending_suffix if is_ending else pack.plain_suffix
    return {
        TONE_DARK: NodeOutline(
            title=pack.dark_ending_title if is_ending else pack.dark_title,
            summary=f"{pack.dark_summary}{ending}",
            appearing_character_ids=all_ids,
        ),
        TONE_SUNNY: NodeOutline(
            title=pack.sunny_ending_title if is_ending else pack.sunny_title,
            summary=f"{pack.sunny_summary}{ending}",
            appearing_character_ids=all_ids,
        ),
        TONE_NEUTRAL: NodeOutline(
            title=pack.neutral_ending_title if is_ending else pack.neutral_title,
            summary=f"{pack.neutral_summary}{ending}",
            appearing_character_ids=all_ids,
        ),
    }
