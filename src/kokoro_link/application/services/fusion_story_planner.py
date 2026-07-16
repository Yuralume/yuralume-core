"""Outline planner stage of the fusion-story pipeline.

Takes the operator prompt + per-character briefs and asks the LLM for a
4-act 起承轉合 outline. Output is structured JSON validated into a
``FusionOutline`` value object — bad shapes fall through to a synthetic
template so the orchestrator always has something to write from.

Why a dedicated stage (not inlined into the orchestrator):

- Outline-only regenerate is a first-class operation: the operator may
  want a different premise without throwing away the briefs / character
  selection.
- Pure function over (prompt, briefs) — testable without the writer or
  polisher stages.
"""

from __future__ import annotations

import json
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
from kokoro_link.domain.value_objects.fusion_outline import (
    ACT_OPENING,
    ACT_RESOLUTION,
    ACT_RISING,
    ACT_TURN,
    CANONICAL_ACTS,
    FusionBeatPlan,
    FusionOutline,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompts import get_default_loader


_LOGGER = logging.getLogger(__name__)
_FENCE_RE = re.compile(r"```(?:\w+)?\n?")

# Beat-count contract. 4 was the original 起承轉合 fixed shape and felt
# rushed — every act got ~600 字 which is too tight for actual scene work.
# Expanding to 6–10 beats (default 8) lets the rising / turn arcs breathe.
# Acts remain ordered opening → rising → turn → resolution but multiple
# beats may share the same act label.
_MIN_BEATS = 6
_MAX_BEATS = 10
_DEFAULT_BEATS = 8
_DEFAULT_TARGET_TOTAL = 6400
"""Total prose target. 6400 ÷ 8 ≈ 800 字/幕 — enough for a real scene
rather than a synopsis. Acceptable range is roughly 5000–8000."""

_PER_BEAT_FLOOR = 500
"""Each beat must clear this so a slacker LLM doesn't emit a 200-char
"幕" that's really just a summary line."""


# Fallback template — 8 beats, distribution 起1 / 承4 / 轉2 / 合1.
# Used whenever the planner LLM call fails / returns garbage; lets the
# orchestrator still produce a writable outline.
#
# NOTE (LLM-first reviewers): the hardcoded per-locale strings below are
# intentional and scoped to the model-free fallback path only (fake
# provider, LLM error, unparseable output) — mirrors the exemption note
# in ``infrastructure/story/llm_arc_planner._synthetic_arc``. There is
# no model call in this path to carry the operator-language fact into,
# so the player-visible strings are static per shipped locale instead.
# This is not keyword-matching business logic; it is a static, editable
# placeholder immediately superseded by the next real LLM plan. New
# shipped languages get a new entry in ``_FALLBACK_BEATS_BY_LANGUAGE``.
_FALLBACK_BEATS_BY_LANGUAGE: dict[
    str, tuple[tuple[str, str, str, str, str, str, str], ...],
] = {
    "zh-TW": (
        (
            ACT_OPENING,
            "起：日常裡的訊號",
            "幾位主角在各自的日常中觸及同一個訊號，預示故事即將開展。",
            "他們是否願意承認這個訊號真的指向自己？",
            "平日午後，各自所在的日常場景，多視角輪轉",
            "傍晚前，彼此的位置開始靠近",
            "開場",
        ),
        (
            ACT_RISING,
            "承一：第一次匯合",
            "兩條原本平行的軌跡因為一個外部事件被迫交錯。",
            "他們會選擇正視這次相遇，還是各自繞開？",
            "傍晚，第一個讓他們匯合的場景",
            "入夜，初步試探彼此的距離",
            "短跳躍（數小時）+ 場景切換到匯合點",
        ),
        (
            ACT_RISING,
            "承二：誤會醞釀",
            "因為各自的立場和過去，誤會慢慢成形，但表面上仍維持禮貌。",
            "誰會先掀開那層客氣？",
            "夜裡，相對私密的場景，POV 在較被動的一方",
            "深夜，誤會堆到某個臨界",
            "直接承接 + POV 轉換到被動方",
        ),
        (
            ACT_RISING,
            "承三：第三方擾動",
            "另一個角色或事件打進場，把原本兩人之間的張力擴大。",
            "他們有辦法把第三方納入，還是只能擇一？",
            "深夜，新角色或事件介入的場景",
            "凌晨，局面變得更難收",
            "場景切換 + 新角色加入",
        ),
        (
            ACT_RISING,
            "承四：累積到爆點前",
            "矛盾累到無法閃避，每個人的真實需求第一次明確擺上檯面。",
            "他們會繼續壓抑還是要求一個答案？",
            "凌晨，被迫面對彼此的封閉空間",
            "天將亮，全部攤牌的前一刻",
            "直接承接（情緒壓到頂）",
        ),
        (
            ACT_TURN,
            "轉一：核心抉擇",
            "一個關鍵時刻迫使每個人面對自己最害怕的東西，故事走向轉折。",
            "他們會選擇守住自己，還是為對方退一步？",
            "天亮前，私密的轉折場景，POV 集中在抉擇者",
            "破曉，抉擇已下但代價未明",
            "POV 收束到抉擇者",
        ),
        (
            ACT_TURN,
            "轉二：代價浮現",
            "抉擇的後果立刻反彈到所有人身上，原本的關係被迫重組。",
            "他們承擔得起這個代價嗎？",
            "清晨，抉擇後的第一個共同場景",
            "上午，新的關係樣態浮出輪廓",
            "短跳躍（數小時）+ 場景延續",
        ),
        (
            ACT_RESOLUTION,
            "合：餘溫的尾聲",
            "塵埃落定後，每個角色帶著新的理解走回自己的軌道，世界因此微微改變。",
            "新的日常裡他們會記得彼此什麼？",
            "隔日，回到接近開場的日常場景但氣味已變",
            "黃昏，各自的軌道但彼此知道對方還在",
            "時間跳躍（隔日）+ 視角回到多角",
        ),
    ),
    "en-US": (
        (
            ACT_OPENING,
            "Opening: A Signal in the Ordinary",
            "The leads each brush against the same signal in their own "
            "daily lives, hinting that the story is about to begin.",
            "Are they willing to admit this signal is really pointing at "
            "them?",
            "An ordinary afternoon, each in their own daily setting, "
            "multiple POVs rotating",
            "Toward evening, their positions begin drifting closer",
            "Opening",
        ),
        (
            ACT_RISING,
            "Rising I: The First Crossing",
            "Two once-parallel paths are forced to cross by an outside "
            "event.",
            "Will they choose to face this meeting head-on, or each "
            "steer away?",
            "Evening, the scene that first brings them together",
            "Nightfall, a first tentative gauge of the distance between "
            "them",
            "Short jump (a few hours) + scene cut to the meeting point",
        ),
        (
            ACT_RISING,
            "Rising II: A Misunderstanding Brews",
            "Shaped by their separate positions and pasts, a "
            "misunderstanding quietly forms while politeness holds on "
            "the surface.",
            "Who will be the first to drop that polite front?",
            "Night, a more private scene, POV on the more passive side",
            "Late night, the misunderstanding builds toward a breaking "
            "point",
            "Direct continuation + POV shifts to the passive side",
        ),
        (
            ACT_RISING,
            "Rising III: A Third Party Disrupts",
            "Another character or event pushes in, widening the tension "
            "already between the two of them.",
            "Can they fold the third party in, or must they choose one "
            "side?",
            "Late night, the scene where the new character or event "
            "steps in",
            "Small hours, the situation grows harder to contain",
            "Scene cut + new character joins",
        ),
        (
            ACT_RISING,
            "Rising IV: Building to the Brink",
            "The conflict piles up until it can no longer be dodged; "
            "everyone's real needs are laid bare for the first time.",
            "Will they keep holding it in, or demand an answer?",
            "Small hours, forced together in an enclosed space",
            "Just before dawn, the moment right before everything comes "
            "out",
            "Direct continuation (emotion pushed to its peak)",
        ),
        (
            ACT_TURN,
            "Turn I: The Core Choice",
            "A pivotal moment forces everyone to face the thing they "
            "fear most, and the story bends toward its turn.",
            "Will they choose to hold their ground, or give an inch for "
            "the other?",
            "Just before dawn, a private turning-point scene, POV "
            "centered on the one making the choice",
            "Daybreak, the choice is made but its cost is not yet clear",
            "POV narrows onto the one making the choice",
        ),
        (
            ACT_TURN,
            "Turn II: The Cost Surfaces",
            "The consequences of the choice bounce back onto everyone at "
            "once, forcing the relationship to reorganize.",
            "Can they actually bear this cost?",
            "Early morning, the first scene they share after the choice",
            "Morning, the outline of a new kind of relationship starts "
            "to show",
            "Short jump (a few hours) + scene continues",
        ),
        (
            ACT_RESOLUTION,
            "Resolution: A Lingering Warmth",
            "Once the dust settles, each character carries a new "
            "understanding back onto their own path, and the world has "
            "shifted, just slightly.",
            "What will they remember of each other in this new "
            "ordinary?",
            "The next day, back in a setting close to the opening, but "
            "the air has changed",
            "Dusk, each on their own path, but each aware the other is "
            "still there",
            "Time jump (next day) + perspective returns to the full cast",
        ),
    ),
    "ja-JP": (
        (
            ACT_OPENING,
            "起：日常の中の予兆",
            "主人公たちはそれぞれの日常の中で同じ予兆に触れ、物語の始まりを"
            "予感させる。",
            "彼らはこの予兆が本当に自分に向けられていると認める気があるの"
            "だろうか？",
            "平日の午後、それぞれの日常の場面、複数視点が入れ替わる",
            "夕方前、互いの位置が近づき始める",
            "開幕",
        ),
        (
            ACT_RISING,
            "承一：最初の交わり",
            "本来は平行していた二つの軌跡が、外部の出来事によって交差を"
            "強いられる。",
            "彼らはこの出会いに正面から向き合うのか、それぞれ避けて通る"
            "のか？",
            "夕方、彼らを初めて引き合わせる場面",
            "夜になり、互いの距離を探り始める",
            "短い時間跳躍（数時間）+ 合流地点へ場面転換",
        ),
        (
            ACT_RISING,
            "承二：誤解の醸成",
            "それぞれの立場と過去のせいで誤解が静かに形作られていくが、"
            "表面上はまだ礼儀を保っている。",
            "誰が先にその礼儀の仮面を脱ぐのだろう？",
            "夜、比較的私的な場面、POVは受け身な側に",
            "深夜、誤解がある臨界点まで積み上がる",
            "直接続き + POVを受け身側に転換",
        ),
        (
            ACT_RISING,
            "承三：第三者の介入",
            "別のキャラクターや出来事が割り込み、二人の間の緊張をさらに"
            "広げる。",
            "彼らは第三者を受け入れられるのか、それとも一方を選ぶしか"
            "ないのか？",
            "深夜、新しいキャラクターや出来事が介入する場面",
            "未明、状況はさらに収拾がつかなくなる",
            "場面転換 + 新キャラクター参加",
        ),
        (
            ACT_RISING,
            "承四：限界寸前までの積み重ね",
            "矛盾が避けられないところまで積み上がり、誰もが自分の本当の"
            "望みを初めてはっきりと突きつけられる。",
            "彼らはこのまま抑え続けるのか、それとも答えを求めるのか？",
            "未明、互いに逃げ場のない空間で向き合わされる",
            "夜明け前、すべてが明るみに出る直前の瞬間",
            "直接続き（感情が頂点まで押し上げられる）",
        ),
        (
            ACT_TURN,
            "転一：核心の選択",
            "決定的な瞬間が、誰もが最も恐れているものと向き合うことを"
            "強い、物語は転機を迎える。",
            "彼らは自分を守り抜くのか、それとも相手のために一歩譲るの"
            "か？",
            "夜明け前、私的な転機の場面、POVは選択する者に集中する",
            "夜明け、選択はすでに下されたが、その代償はまだ見えない",
            "POVを選択する者に収束",
        ),
        (
            ACT_TURN,
            "転二：代償の浮上",
            "選択の結果はすぐさま全員に跳ね返り、これまでの関係の"
            "組み直しを迫る。",
            "彼らはこの代償を背負いきれるのだろうか？",
            "早朝、選択のあとに初めて共有する場面",
            "午前、新しい関係のかたちが輪郭を現し始める",
            "短い時間跳躍（数時間）+ 場面継続",
        ),
        (
            ACT_RESOLUTION,
            "合：余韻の終章",
            "すべてが落ち着いたあと、それぞれのキャラクターは新しい理解を"
            "抱えて自分の軌道に戻り、世界はわずかに変わっている。",
            "新しい日常の中で、彼らは互いの何を覚えているのだろう？",
            "翌日、開幕に近い日常の場面に戻るが、空気はすでに変わって"
            "いる",
            "黄昏、それぞれの軌道にいながらも、互いにまだ相手がそこに"
            "いると知っている",
            "時間跳躍（翌日）+ 視点は再び群像へ",
        ),
    ),
}

_FALLBACK_BEATS_FALLBACK_LANGUAGE = "zh-TW"


def _resolve_fallback_beats(
    language_tag: str | None,
) -> tuple[tuple[str, str, str, str, str, str, str], ...]:
    """Pick the localized fallback beat set for a BCP-47 tag.

    Falls back to the documented ``zh-TW`` default for unknown /
    unsupported tags. Matches on the exact tag first, then the language
    subtag (mirrors ``llm_arc_planner._synthetic_template_pack``)."""
    tag = (language_tag or "").strip()
    if tag in _FALLBACK_BEATS_BY_LANGUAGE:
        return _FALLBACK_BEATS_BY_LANGUAGE[tag]
    subtag = tag.split("-", 1)[0].lower() if tag else ""
    for known_tag, beats in _FALLBACK_BEATS_BY_LANGUAGE.items():
        if known_tag.split("-", 1)[0].lower() == subtag and subtag:
            return beats
    return _FALLBACK_BEATS_BY_LANGUAGE[_FALLBACK_BEATS_FALLBACK_LANGUAGE]


_FALLBACK_TITLE_BY_LANGUAGE: dict[str, str] = {
    "zh-TW": "未命名的相遇",
    "en-US": "An Untitled Encounter",
    "ja-JP": "無題の出会い",
}
_FALLBACK_PREMISE_FMT_BY_LANGUAGE: dict[str, str] = {
    "zh-TW": "關於 {names} 的一段短篇相遇。系統先擺好骨架，可再叫 LLM 重寫。",
    "en-US": (
        "A short encounter involving {names}. The system lays out the "
        "skeleton first; the LLM can rewrite it from here."
    ),
    "ja-JP": (
        "{names}をめぐる短い出会いの物語。まずシステムが骨組みを用意し、"
        "続きはLLMに書き直させることができる。"
    ),
}


class FusionStoryPlanner:
    """LLM-backed planner. Falls back to a synthetic outline on failure.

    Wraps a ``ModelResolver`` so per-feature / per-character routing
    works the same as the rest of the auxiliary LLM pipeline.
    """

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

    async def plan(
        self,
        *,
        prompt: str,
        briefs: Sequence[CharacterBrief],
        previous_outline: FusionOutline | None = None,
        operator_primary_language: str = "zh-TW",
    ) -> FusionOutline:
        if not briefs:
            raise ValueError("plan() requires at least one character brief")
        if await self._resolver.is_fake():
            return _synthetic_outline(
                prompt=prompt, briefs=briefs,
                operator_primary_language=operator_primary_language,
            )

        full_prompt = _build_prompt(
            prompt=prompt,
            briefs=briefs,
            previous_outline=previous_outline,
            operator_primary_language=operator_primary_language,
        )
        try:
            raw = await self._resolver.generate(full_prompt)
        except Exception:
            _LOGGER.exception("fusion planner LLM call failed")
            return _synthetic_outline(
                prompt=prompt, briefs=briefs,
                operator_primary_language=operator_primary_language,
            )

        parsed = _parse_outline(raw, briefs=briefs)
        if parsed is None:
            _LOGGER.warning("fusion planner: unparseable LLM output")
            return _synthetic_outline(
                prompt=prompt, briefs=briefs,
                operator_primary_language=operator_primary_language,
            )
        return parsed


# --- prompt + parsing ------------------------------------------------


def _build_prompt(
    *,
    prompt: str,
    briefs: Sequence[CharacterBrief],
    previous_outline: FusionOutline | None,
    operator_primary_language: str = "zh-TW",
) -> str:
    brief_block = "\n\n".join(b.text for b in briefs)
    char_ids = ", ".join(b.character_id for b in briefs)
    char_names = "、".join(b.short_label() for b in briefs)
    previous_block = ""
    if previous_outline is not None:
        previous_block = (
            "上一版大綱（操作者要求重新規劃，請避免直接重複；可保留主題但調整切入點）：\n"
            + json.dumps(
                {
                    "title": previous_outline.title,
                    "premise": previous_outline.premise,
                    "beats": [
                        {
                            "act": b.act,
                            "title": b.title,
                            "hook": b.hook,
                        }
                        for b in previous_outline.beats
                    ],
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    body = get_default_loader().render(
        "fusion/planner",
        char_names=char_names,
        char_ids=char_ids,
        default_beats=_DEFAULT_BEATS,
        min_beats=_MIN_BEATS,
        max_beats=_MAX_BEATS,
        target_min=_DEFAULT_TARGET_TOTAL - 1400,
        target_max=_DEFAULT_TARGET_TOTAL + 1600,
        per_beat_floor=_PER_BEAT_FLOOR,
        prompt_text=prompt.strip() or "（未指定，請依角色資料自行構思）",
        brief_block=brief_block,
        previous_block=previous_block.rstrip(),
    )
    language_hint = render_operator_language_hint(operator_primary_language)
    return f"{language_hint}\n\n{body}" if language_hint else body




def _parse_outline(
    raw: str, *, briefs: Sequence[CharacterBrief],
) -> FusionOutline | None:
    text = _FENCE_RE.sub("", raw or "").replace("```", "").strip()
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
    title = _coerce_str(data.get("title")) or "（未命名）"
    premise = _coerce_str(data.get("premise")) or "（無前提）"
    theme = _coerce_str(data.get("theme")) or "custom"
    raw_beats = data.get("beats")
    if not isinstance(raw_beats, list) or not raw_beats:
        return None

    valid_ids = {b.character_id for b in briefs}
    default_per_beat = max(
        _PER_BEAT_FLOOR, _DEFAULT_TARGET_TOTAL // _DEFAULT_BEATS,
    )
    ordered: list[FusionBeatPlan] = []
    for idx, entry in enumerate(raw_beats[:_MAX_BEATS]):
        if not isinstance(entry, dict):
            continue
        act = _coerce_act_loose(entry.get("act"))
        title_v = _coerce_str(entry.get("title")) or f"第 {idx + 1} 幕"
        hook = _coerce_str(entry.get("hook")) or title_v
        question = _coerce_str(entry.get("dramatic_question")) or ""
        target_chars = _coerce_int(
            entry.get("target_chars"), default=default_per_beat,
        )
        focus = _coerce_focus(entry.get("focus_character_ids"), valid_ids)
        entry_state = _coerce_str(entry.get("entry_state"))
        exit_state = _coerce_str(entry.get("exit_state"))
        transition = _coerce_str(entry.get("transition_from_previous"))
        try:
            ordered.append(
                FusionBeatPlan.create(
                    sequence=idx,
                    act=act,
                    title=title_v,
                    hook=hook,
                    dramatic_question=question,
                    target_chars=max(target_chars, _PER_BEAT_FLOOR),
                    focus_character_ids=focus,
                    entry_state=entry_state,
                    exit_state=exit_state,
                    transition_from_previous=transition,
                ),
            )
        except ValueError:
            _LOGGER.warning(
                "fusion planner: dropping invalid beat sequence=%s entry=%r",
                idx, entry,
            )
            continue
    if len(ordered) < _MIN_BEATS:
        return None
    if not _acts_monotone(ordered):
        # LLM emitted beats that go backwards in canonical order (e.g.
        # rising then opening). Reject rather than try to re-sort —
        # re-sorting would scramble the planner's narrative pacing.
        _LOGGER.warning(
            "fusion planner: beats are not act-monotone; falling back",
        )
        return None
    try:
        return FusionOutline.create(
            title=title, premise=premise, theme=theme, beats=ordered,
        )
    except ValueError:
        return None


def _coerce_str(raw: Any) -> str:
    if isinstance(raw, str):
        return raw.strip()
    return ""


def _coerce_int(raw: Any, *, default: int) -> int:
    if isinstance(raw, bool):
        return default
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            return default
    return default


def _coerce_act_loose(raw: Any) -> str:
    """Normalize the act label. Falls back to ``opening`` when the LLM
    returns junk — the monotone check downstream will reject the whole
    outline if too many beats land on the wrong act."""
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in CANONICAL_ACTS:
            return lowered
    return ACT_OPENING


def _acts_monotone(beats: Sequence[FusionBeatPlan]) -> bool:
    """Return ``True`` when the beats' acts only move forward in
    canonical order (opening → rising → turn → resolution). Same-act
    repetition is allowed (that's the whole point of expanding from 4
    fixed acts to N beats). Backwards moves indicate the LLM scrambled
    the outline and we should reject rather than render garbage."""
    rank = {a: i for i, a in enumerate(CANONICAL_ACTS)}
    last = -1
    for beat in beats:
        cur = rank.get(beat.act, -1)
        if cur < last:
            return False
        last = cur
    return True


def _coerce_focus(raw: Any, valid_ids: set[str]) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        cleaned = entry.strip()
        if cleaned in valid_ids and cleaned not in out:
            out.append(cleaned)
    return tuple(out)


_FALLBACK_NAME_JOIN_BY_LANGUAGE: dict[str, str] = {
    "zh-TW": "、",
    "en-US": ", ",
    "ja-JP": "、",
}
_FALLBACK_UNSPECIFIED_NAME_BY_LANGUAGE: dict[str, str] = {
    "zh-TW": "（未指定）",
    "en-US": "(unspecified)",
    "ja-JP": "（未指定）",
}


def _resolve_fallback_language(language_tag: str | None) -> str:
    """Resolve a BCP-47 tag to one of the shipped fallback template keys.

    Exact tag -> language-subtag family -> ``zh-TW``, mirroring
    ``llm_arc_planner._synthetic_template_pack``."""
    tag = (language_tag or "").strip()
    if tag in _FALLBACK_BEATS_BY_LANGUAGE:
        return tag
    subtag = tag.split("-", 1)[0].lower() if tag else ""
    for known_tag in _FALLBACK_BEATS_BY_LANGUAGE:
        if known_tag.split("-", 1)[0].lower() == subtag and subtag:
            return known_tag
    return _FALLBACK_BEATS_FALLBACK_LANGUAGE


def _synthetic_outline(
    *,
    prompt: str,
    briefs: Sequence[CharacterBrief],
    operator_primary_language: str = "zh-TW",
) -> FusionOutline:
    """Template fallback so the orchestrator always has something to write.

    Uses every selected character as focus across all four acts and
    splits the 2500-char target evenly. Operators see this when the
    LLM call failed or the provider was the fake backend.

    LLM-FIRST EXEMPTION: this is the deliberately model-free path (fake
    provider, LLM error, unparseable output) — see the exemption note on
    ``_FALLBACK_BEATS_BY_LANGUAGE``. Player-visible strings are picked
    from a static per-locale template pack keyed off
    ``operator_primary_language`` instead of a model call.
    """
    language = _resolve_fallback_language(operator_primary_language)
    fallback_beats = _resolve_fallback_beats(operator_primary_language)
    fallback_title = _FALLBACK_TITLE_BY_LANGUAGE[language]
    premise_fmt = _FALLBACK_PREMISE_FMT_BY_LANGUAGE[language]
    name_join = _FALLBACK_NAME_JOIN_BY_LANGUAGE[language]
    unspecified = _FALLBACK_UNSPECIFIED_NAME_BY_LANGUAGE[language]

    title = (prompt.strip() or fallback_title)[:60]
    char_names = name_join.join(b.short_label() for b in briefs) or unspecified
    premise = premise_fmt.format(names=char_names)
    theme = "custom"
    target_each = max(
        _PER_BEAT_FLOOR, _DEFAULT_TARGET_TOTAL // len(fallback_beats),
    )
    focus_all = tuple(b.character_id for b in briefs)
    beats = [
        FusionBeatPlan.create(
            sequence=i,
            act=act,
            title=t,
            hook=h,
            dramatic_question=q,
            target_chars=target_each,
            focus_character_ids=focus_all,
            entry_state=entry,
            exit_state=exit_,
            transition_from_previous=trans,
        )
        for i, (act, t, h, q, entry, exit_, trans) in enumerate(fallback_beats)
    ]
    return FusionOutline.create(
        title=title, premise=premise, theme=theme, beats=beats,
    )
