"""LLM pre-review of showcase candidates.

This sits between the mechanical filter and the owner. The mechanical
filter knows structure; the owner knows judgement; this step is the one
that can *read* — it catches the city name in paragraph three of post 87
that no human is going to spot on the eighty-seventh read.

It has no authority. Its entire output is advice the owner acts on in the
admin console: a verdict, the reasons behind it, and — when the concern
is a removable identifying detail — a proposed rewrite.

The rewrite discipline is a red line and lives in the prompt:
**de-identify only**. The showcase thesis is "真實生活不是劇本"; a post
reshaped into something nicer is a lie about the product. If it cannot be
cleaned by swapping a place name or a form of address, the correct
outcome is a flag with no rewrite, and the owner rejects it.

The prompt is a module constant rather than a pack entry on purpose: the
prompt pack is versioned and distributed per deployment, so a new pack
file would make this ops feature un-runnable until the next pack release
reaches the host. Nothing here is meant to be tuned per deployment.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.application.services.showcase.filters import ShowcaseCandidate
from kokoro_link.application.services.showcase.json_output import (
    coerce_json_object,
    as_reason_list,
)
from kokoro_link.domain.entities.character import Character

_LOGGER = logging.getLogger(__name__)

VERDICT_PASS = "pass"
VERDICT_FLAG = "flag"
VERDICT_NEEDS_MANUAL_REVIEW = "needs_manual_review"
"""Recorded when the reviewer could not run at all (no route configured,
a transport failure, an unparseable answer). Deliberately distinct from
``flag``: nothing looked at this post, so "no reasons listed" must not
read as "nothing found"."""

REVIEW_SYSTEM_PROMPT = """\
你是一個 AI 陪伴產品的公開櫥窗內容審查員。

背景：這些貼文由一個 AI 角色在**私人使用**情境下自動產生，現在要被
放上完全公開、任何陌生人都看得到的展示牆。貼文原本不是在「會被公開」
的意識下寫的，所以你要逐篇檢查它是否適合公開。

檢查清單（逐項判斷）：
1. 城市與真實地點 — 出現可定位到現實世界的城市、街區、店名、學校、
   車站、地標。
2. 人名與稱呼 — 出現真實人名、暱稱、對飼主/使用者的專屬稱呼，或任何
   能指向特定真人的身分描述。
3. 使用者互動內容引用 — 直接引用或轉述使用者說過的話、抱怨使用者沒
   回訊息、描述兩人之間的私人往來。
4. 作息與規律 — 透露可推斷出真人生活作息的規律資訊（固定通勤時間、
   上班地點、家庭成員行程）。
5. 公開面適切性 — 露骨性內容、極端負面情緒、可能造成公關風險的言論。

輸出**只有一個 JSON 物件**，不要有其他文字：
{
  "verdict": "pass" 或 "flag",
  "reasons": ["具體指出哪一句、命中第幾項", ...],
  "rewrite": "去識別化改寫後的全文，或 null"
}

verdict 規則：
- 完全沒命中任何一項 → "pass"，reasons 為空陣列，rewrite 為 null。
- 命中任何一項 → "flag"，reasons 逐條說明。

rewrite 規則（**極重要**）：
- 只做「去識別化替換」：把具體地名換成模糊描述、把人名/稱呼換成中性
  說法、把可定位的細節抽象化。
- **禁止**重寫語氣、禁止改變事件、禁止美化、禁止補寫沒發生的內容、
  禁止改變長度風格。這面牆的價值在於「這是真實生活不是劇本」，重度
  改寫等於說謊。
- 如果問題無法只靠替換解決（例如整篇的主題就是使用者的私事），
  rewrite 直接給 null——由人決定要不要整篇不用。
- 語言與原文相同（原文是中文就回中文）。
"""


@dataclass(frozen=True, slots=True)
class ShowcaseReview:
    """One post's advisory verdict."""

    post_id: str
    verdict: str = VERDICT_NEEDS_MANUAL_REVIEW
    reasons: tuple[str, ...] = ()
    rewrite: str | None = None


@dataclass(slots=True)
class ReviewSummary:
    passed: int = 0
    flagged: int = 0
    failed: int = 0
    reviews: list[ShowcaseReview] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.passed + self.flagged + self.failed


class ShowcaseReviewer:
    """Reviews one candidate per call.

    Fail-soft per post: a transport error marks that post
    ``needs_manual_review`` and the batch carries on, because losing 60
    good reviews to one 502 is worse. Nothing here can ever produce
    "approved" — approval is the owner's, in the console.
    """

    def __init__(self, resolver: ModelResolver) -> None:
        self._resolver = resolver

    async def review(
        self,
        candidate: ShowcaseCandidate,
        *,
        character: Character | None = None,
    ) -> ShowcaseReview:
        prompt = _build_prompt(candidate, character_name=_name_of(character))
        try:
            if await self._resolver.is_fake(
                character=character, operator_id=_operator_of(character),
            ):
                return ShowcaseReview(
                    post_id=candidate.id,
                    reasons=("此部署未接上真實模型，未經 LLM 預審",),
                )
            raw = await self._resolver.generate(
                prompt,
                character=character,
                operator_id=_operator_of(character),
            )
        except Exception as exc:  # noqa: BLE001 — advisory step, never fatal
            _LOGGER.warning("showcase review failed for post %s: %s", candidate.id, exc)
            return ShowcaseReview(
                post_id=candidate.id,
                reasons=(f"LLM 預審失敗：{exc}",),
            )
        payload = coerce_json_object(raw, site="showcase.review")
        if payload is None:
            return ShowcaseReview(
                post_id=candidate.id,
                reasons=("LLM 預審沒有回傳 JSON 物件",),
            )
        return _review_from_payload(candidate.id, payload)


async def review_candidates(
    candidates: Sequence[ShowcaseCandidate],
    *,
    reviewer: ShowcaseReviewer,
    character: Character | None = None,
) -> ReviewSummary:
    """Review each candidate in turn, tallying the outcome.

    Sequential rather than gathered: this is an ops batch nobody is
    waiting on, and a burst of parallel calls against one operator's
    routed endpoint buys nothing but rate limits.
    """
    summary = ReviewSummary()
    for candidate in candidates:
        review = await reviewer.review(candidate, character=character)
        summary.reviews.append(review)
        if review.verdict == VERDICT_PASS:
            summary.passed += 1
        elif review.verdict == VERDICT_FLAG:
            summary.flagged += 1
        else:
            summary.failed += 1
    return summary


def _build_prompt(candidate: ShowcaseCandidate, *, character_name: str) -> str:
    lines = [REVIEW_SYSTEM_PROMPT, ""]
    if character_name:
        lines.append(f"角色名稱：{character_name}")
    lines.append(f"貼文類型：{candidate.kind}")
    lines.append(f"發文時間：{candidate.created_at.isoformat()}")
    lines.append("貼文全文：")
    lines.append(candidate.text)
    lines.append("")
    lines.append("輸出 JSON：")
    return "\n".join(lines)


def _review_from_payload(
    post_id: str, payload: Mapping[str, object],
) -> ShowcaseReview:
    raw_verdict = str(payload.get("verdict") or "").strip().lower()
    reasons = tuple(as_reason_list(payload.get("reasons")))
    rewrite_raw = payload.get("rewrite")
    rewrite = (
        rewrite_raw.strip()
        if isinstance(rewrite_raw, str) and rewrite_raw.strip()
        else None
    )
    if raw_verdict == VERDICT_PASS:
        # A "pass" that ships a rewrite is contradictory; keep the verdict
        # and drop the rewrite rather than letting the owner approve an
        # edit nobody said was needed.
        return ShowcaseReview(post_id=post_id, verdict=VERDICT_PASS, reasons=reasons)
    if raw_verdict == VERDICT_FLAG:
        return ShowcaseReview(
            post_id=post_id,
            verdict=VERDICT_FLAG,
            reasons=reasons,
            rewrite=rewrite,
        )
    # Unknown verdict — the model answered something we did not ask for.
    # That is exactly the case a human must look at.
    return ShowcaseReview(
        post_id=post_id,
        verdict=VERDICT_NEEDS_MANUAL_REVIEW,
        reasons=reasons or (f"LLM 回傳未知 verdict：{raw_verdict!r}",),
        rewrite=rewrite,
    )


def _name_of(character: Character | None) -> str:
    return character.name if character is not None else ""


def _operator_of(character: Character | None) -> str | None:
    return character.user_id if character is not None else None


__all__ = [
    "REVIEW_SYSTEM_PROMPT",
    "VERDICT_FLAG",
    "VERDICT_NEEDS_MANUAL_REVIEW",
    "VERDICT_PASS",
    "ReviewSummary",
    "ShowcaseReview",
    "ShowcaseReviewer",
    "review_candidates",
]
