"""LLM-backed adapter for :class:`ActivityAftermathPort`.

Asks the model to read a completed activity in light of the character's
persona and produce a short emotional residue line that the memorialiser
folds into the episodic memory's content. The same activity affects
different personas differently because the LLM sees their persona axes
(個性 / 興趣 / 年齡 / 簡介 / 說話風格) and judges accordingly — per the
project's top directive, no enumerated "activity X → emotion Y" rules.

Output is plain text with labelled lines so parsing stays trivial
and tolerant to model jitter:

::

    情緒尾韻：被同事一直追問感情狀況，很煩
    情緒標籤：煩躁

Missing label → empty field. Empty result → "no notable residue", the
memorialiser falls back to the bare-activity memory.

CF4 adds a third, conditional line. When the activity carries an
operator commitment, the caller passes
:class:`SharedActivityEvidence` and the prompt gains a facts + rule
block plus a required ``實際情形`` line — the model's own account of
what actually happened under that evidence, which the memorialiser uses
as the memory body in place of the planner's (merely planned)
description. The rule is stated to the model; nothing on this side
inspects or rewrites the text it returns.
"""

from __future__ import annotations

import logging
import re

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.infrastructure.llm.cloud_refusal import (
    log_auxiliary_llm_failure,
)
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.activity_aftermath import (
    ActivityAftermath,
    ActivityAftermathPort,
)
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import ScheduleActivity
from kokoro_link.domain.services.shared_activity_evidence import (
    OperatorPresenceEvidence,
    SharedActivityEvidence,
    SharedClaimPolicy,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)

_MAX_RESIDUE_CHARS = 120
"""Cap on the residue summary. Sized for the widest shipped language, not
CJK: a Chinese residue is dense (~30-40 chars) but the same thought in
English ("still a bit drained after being grilled about my relationship
status the whole time") needs several times that. The former CJK-sized
60-char cap silently truncated natural-length non-CJK residues mid-word.
Models that overshoot get truncated rather than rejected (some signal
beats none)."""

_MAX_EMOTION_TAG_CHARS = 24
"""Mood tag should be a single short word ("煩躁" / "annoyed" /
"overwhelmed"). Widened from a CJK-sized 6 so a non-CJK tag
("passive-aggressive") isn't dropped for length. Longer than this is
almost certainly the model misreading the schema — a whole sentence."""

_MAX_FACTUAL_CHARS = 200
"""Cap on the CF4 factual line. Larger than the residue cap because this
line replaces the whole semantic body of the memory rather than being
appended to it — it has to carry what happened, not just how it felt."""


class LLMActivityAftermathJudge(ActivityAftermathPort):
    def __init__(
        self,
        model: ChatModelPort | None = None,
        *,
        provider: ActiveLLMProviderPort | None = None,
        feature_key: str | None = None,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider, model=model, feature_key=feature_key,
        )

    async def judge(
        self,
        *,
        character: Character,
        activity: ScheduleActivity,
        operator_primary_language: str = "zh-TW",
        evidence: SharedActivityEvidence | None = None,
    ) -> ActivityAftermath:
        if await self._resolver.is_fake(character=character):
            return ActivityAftermath()
        prompt = _build_prompt(
            character=character,
            activity=activity,
            operator_primary_language=operator_primary_language,
            evidence=evidence,
        )
        try:
            raw = await self._resolver.generate(prompt, character=character)
        except Exception:
            _LOGGER.exception(
                "aftermath LLM call failed character=%s activity=%s",
                character.id, activity.id,
            )
            return ActivityAftermath()
        return _parse(raw)


class NullActivityAftermathJudge(ActivityAftermathPort):
    """Always returns blank — used when the deployment is on the fake
    provider so the memorialiser degrades to bare-activity memories
    without any conditional in the container."""

    async def judge(
        self,
        *,
        character: Character,
        activity: ScheduleActivity,
        operator_primary_language: str = "zh-TW",
        evidence: SharedActivityEvidence | None = None,
    ) -> ActivityAftermath:
        _ = operator_primary_language, evidence
        return ActivityAftermath()


# ----------------------------------------------------------------------
# Prompt rendering
# ----------------------------------------------------------------------


def _build_prompt(
    *,
    character: Character,
    activity: ScheduleActivity,
    operator_primary_language: str = "zh-TW",
    evidence: SharedActivityEvidence | None = None,
) -> str:
    companions = (
        "、".join(activity.companion_names)
        if activity.companion_names else "（獨自進行）"
    )
    return get_default_loader().render(
        "schedule/aftermath",
        shared_activity_block=_render_shared_activity_block(evidence),
        # The 情緒尾韻 residue is folded verbatim into the episodic memory
        # content shown in MemoryBrowserPanel, so it must follow the
        # operator's content language (bug B2 class).
        language_hint=render_operator_language_hint(operator_primary_language),
        persona_block="\n".join(_persona_block(character)),
        activity_description=activity.description,
        activity_category=activity.category,
        activity_location=activity.location or "（未指定地點）",
        activity_companions=companions,
        busy_hint=_busy_label(activity.busy_score),
        activity_busy_score=f"{activity.busy_score:.2f}",
        max_residue_chars=_MAX_RESIDUE_CHARS,
        max_emotion_tag_chars=_MAX_EMOTION_TAG_CHARS,
    )


_ROLE_FACT_LABELS: dict[str, str] = {
    "operator_invite_pending": "角色單方面提出的邀請，使用者從頭到尾沒有答應",
    "operator_invite_expired": "邀請已經過期作廢，使用者從頭到尾沒有答應",
    "operator_wish": "只是角色心裡想要一起，從來沒有真的約成",
    "operator_confirmed_shared": "使用者當初確實答應過要一起",
    "operator_confirmed_lapsed": "使用者當初答應過，但這個約定已經過了時間，沒有留下一起完成的紀錄",
}
"""Role value → fact sentence. A projection of the structured participant
role, mirroring ``llm_planner._EXPIRED_ROLE_LABELS`` — never derived from
reading the activity's prose."""

_PRESENCE_FACT_LABELS: dict[OperatorPresenceEvidence, str] = {
    OperatorPresenceEvidence.UNKNOWN: (
        "查不到使用者當天的互動紀錄（是「查不到」，不是「他沒來」）"
    ),
    OperatorPresenceEvidence.NEVER: "使用者至今從來沒有跟角色說過話",
    OperatorPresenceEvidence.BEFORE_ACTIVITY: (
        "使用者最後一次說話是在這段活動開始之前，活動進行期間完全沒有訊息"
    ),
    OperatorPresenceEvidence.DURING_ACTIVITY: (
        "使用者在這段活動進行期間確實有跟角色互動"
    ),
    OperatorPresenceEvidence.ONLY_AFTER: (
        "只知道使用者在活動結束之後說過話；活動進行期間他在不在場，無從得知"
    ),
}

_CLAIM_RULES: dict[SharedClaimPolicy, str] = {
    SharedClaimPolicy.SOLO_ONLY: (
        "使用者從來沒有答應過，也沒有任何證據顯示他真的出現。"
        "這段時間只能寫成角色自己一個人的經歷——本來想約他一起，"
        "最後自己去了／臨時沒去成／改做了別的事。"
        "**絕對禁止**寫成「和他一起完成了」「終於一起吃到了」這類共同完成的敘述，"
        "也不准假設他默默來了、或幫他補一句沒說過的話。"
        "可以帶著「還是想跟他一起」「下次再約」的心情，但那是願望，不是已發生的事。"
    ),
    SharedClaimPolicy.UNVERIFIED: (
        "使用者當初有答應，但此刻沒有任何證據能證明他真的參與了。"
        "請寫成中性、不替結果下定論的敘述（例如「約好的那件事，那天實際上……」），"
        "既**不得**宣稱「我們一起完成了」，也**不得**反過來斷定他放了鴿子。"
        "只寫確定為真的部分，不確定的就讓它保持不確定。"
    ),
    SharedClaimPolicy.VERIFIED: (
        "使用者當初答應過，而且活動進行期間確實有互動紀錄。"
        "可以寫成兩人一起經歷的敘述。"
    ),
}


def _render_shared_activity_block(
    evidence: SharedActivityEvidence | None,
) -> str:
    """Render the CF4 facts + claim rule + extra output line.

    Empty string for an activity with no operator commitment, which keeps
    the prompt byte-identical to the pre-CF4 shape for the overwhelming
    majority of activities (and keeps the two-line output contract).

    The block only ever states facts the structured data supports and the
    rule that follows from them. What the memory ends up *saying* is the
    model's call — this is the "give the LLM correct context + an explicit
    rule" path, not a text filter.
    """
    if evidence is None or not evidence.involves_operator:
        return ""
    who = evidence.operator_display_name or "使用者"
    role_fact = _ROLE_FACT_LABELS.get(
        evidence.operator_role or "", "與使用者有關，但約定狀態不明",
    )
    presence_fact = _PRESENCE_FACT_LABELS[evidence.presence]
    claim_rule = _CLAIM_RULES[evidence.policy]
    return "\n".join((
        "",
        "**這段活動牽涉到使用者（操作者），先判斷「到底是不是真的一起完成」**：",
        f"- 使用者稱呼：{who}",
        f"- 約定狀態：{role_fact}",
        f"- 活動當下的互動跡象：{presence_fact}",
        f"- 敘述規則：{claim_rule}",
        "- 「剛完成的活動－內容」那一行是**當初排進行程的計畫**，不是已發生事實的"
        "紀錄；它寫「一起」不代表真的一起了。判斷實際情形時以上面的事實為準。",
        "",
        "因此請**多輸出一行**（同樣是玩家可見自然語言，使用上方指定的輸出語言；"
        "欄位標籤保留原樣）：",
        f"實際情形：<不超過 {_MAX_FACTUAL_CHARS} 字，以角色第一人稱寫這段時間實際發生了什麼>",
        "這一行會直接成為角色的長期記憶內容，日後角色會照著它回想、也可能拿來跟使用者聊，"
        "所以它必須完全符合上面的敘述規則；不得留空。",
        "",
    ))


def _persona_block(character: Character) -> list[str]:
    lines = [f"- 名稱：{character.name}"]
    if character.summary:
        lines.append(f"- 簡介：{character.summary[:160]}")
    if character.personality:
        lines.append("- 性格：" + "、".join(character.personality[:6]))
    if character.interests:
        lines.append("- 興趣：" + "、".join(character.interests[:6]))
    if character.speaking_style:
        lines.append(f"- 說話風格：{character.speaking_style[:120]}")
    state = character.state
    lines.append(
        f"- 當前情緒：{state.emotion}（好感 {state.affection}/疲勞 "
        f"{state.fatigue}/精力 {state.energy}）",
    )
    return lines


def _busy_label(score: float) -> str:
    if score >= 0.8:
        return "高度忙碌（強度大）"
    if score >= 0.5:
        return "中等忙碌"
    if score >= 0.2:
        return "輕度忙碌"
    return "幾乎沒在動"


# ----------------------------------------------------------------------
# Output parsing
# ----------------------------------------------------------------------


_RESIDUE_RE = re.compile(r"情緒尾韻\s*[:：]\s*(.*)")
_EMOTION_RE = re.compile(r"情緒標籤\s*[:：]\s*(.*)")
_FACTUAL_RE = re.compile(r"實際情形\s*[:：]\s*(.*)")
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def _parse(raw: str) -> ActivityAftermath:
    text = (raw or "").strip()
    if not text:
        return ActivityAftermath()
    text = _FENCE_RE.sub("", text).strip()
    residue = ""
    emotion = ""
    factual = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = _RESIDUE_RE.match(stripped)
        if m:
            residue = m.group(1).strip()
            continue
        m = _EMOTION_RE.match(stripped)
        if m:
            emotion = m.group(1).strip()
            continue
        m = _FACTUAL_RE.match(stripped)
        if m:
            factual = m.group(1).strip()
    residue = _clean_residue(residue)
    emotion = _clean_emotion(emotion)
    factual = _clean_factual(factual)
    return ActivityAftermath(
        residue_summary=residue,
        emotion_tag=emotion,
        factual_summary=factual,
    )


def _clean_residue(text: str) -> str:
    cleaned = text.strip().strip('「」"\'')
    if not cleaned:
        return ""
    if len(cleaned) > _MAX_RESIDUE_CHARS:
        cleaned = cleaned[:_MAX_RESIDUE_CHARS].rstrip() + "…"
    return cleaned


def _clean_factual(text: str) -> str:
    """Trim the CF4 factual line the same way the residue is trimmed.

    Truncation (rather than rejection) on overshoot: a clipped honest
    account still beats falling back to the planner's "we did it
    together" description, which is what a dropped line would cost us."""
    cleaned = text.strip().strip('「」"\'')
    if not cleaned:
        return ""
    if len(cleaned) > _MAX_FACTUAL_CHARS:
        cleaned = cleaned[:_MAX_FACTUAL_CHARS].rstrip() + "…"
    return cleaned


def _clean_emotion(text: str) -> str:
    cleaned = text.strip().strip('「」"\'')
    if not cleaned:
        return ""
    if len(cleaned) > _MAX_EMOTION_TAG_CHARS:
        # Model overshot — likely a sentence instead of a label. Drop
        # rather than truncate; an emotion tag of "被同事煩到頭痛" would
        # poison the memory tags downstream.
        return ""
    return cleaned
