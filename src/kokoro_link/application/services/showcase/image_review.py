"""LLM image-consistency pre-review of showcase candidates.

The text reviewer (:mod:`.review`) reads what a post *says*; this one
looks at what its generated image *shows*. Image generation drifts: a
character defined with long golden hair occasionally comes back with a
short black bob, and on a public wall that reads as "who is this?".
The reviewer compares the post image against the character's own visual
record — portrait, appearance sheet, the images of neighbouring posts —
and reports when generation produced someone else.

Like the text reviewer it is advisory *in Core*: the verdict travels to
the Cloud control plane, and what a ``flag`` blocks is that side's
decision. Every failure path lands on ``needs_manual_review`` rather
than a clean bill of health, because "nothing looked at this image"
must never read as "the image is fine".

The prompt is a module constant for the same reason the text reviewer's
is: this is an ops feature that must stay runnable without waiting for
a prompt-pack release, and nothing here is meant to be tuned per
deployment.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.application.services.showcase.filters import ShowcaseCandidate
from kokoro_link.application.services.showcase.json_output import (
    as_reason_list,
    coerce_json_object,
)
from kokoro_link.application.services.showcase.review import (
    VERDICT_FLAG,
    VERDICT_NEEDS_MANUAL_REVIEW,
    VERDICT_PASS,
)
from kokoro_link.domain.entities.character import Character

_LOGGER = logging.getLogger(__name__)

MAX_REFERENCE_IMAGES = 3
"""Portrait plus a couple of neighbouring posts. More buys diminishing
signal at linear vision-token cost — this is an ops batch, not a lineup."""

IMAGE_REVIEW_SYSTEM_PROMPT = """\
你是一個 AI 陪伴產品的公開櫥窗「圖片一致性」審查員。

背景：一個 AI 角色的貼文即將放上完全公開的展示牆，每篇貼文附一張
生成圖片，圖片裡的人物應該就是這個角色本人。圖片生成偶爾會漂移：
髮色、髮型長度、瞳色、體型、物種或標誌性特徵跑掉，變成「另一個人」。
你的工作是判斷這張貼文圖片與角色的既有形象是否一致。

你會依序拿到：
- 第 1 張圖：本次要審的貼文圖片。
- 之後的圖（可能沒有）：參考圖——角色立繪與最近幾篇已收錄貼文的圖片。

判斷原則：
1. 只判斷「是不是同一個角色」：髮色、髮型長度、瞳色、體型/物種、
   標誌性特徵（獸耳、眼鏡、疤痕、飾品等設定明示的固定特徵）。
2. 服裝、場景、光線、構圖、畫風的差異**不算**偏差；表情、姿勢、
   年齡感的輕微變化也不算。
3. 沒有參考圖時，以文字外觀設定為準；設定與參考圖都沒提到的特徵，
   不要自行當成偏差。
4. 圖片裡沒有出現角色本人（風景、食物、寵物、手邊物品）→ 一致
   （"pass"）——貼文本來就可以拍別的東西。
5. 拿不準時傾向 "pass"：這一步只攔「明顯是別人」的大偏差，小差異
   由人來看。

輸出**只有一個 JSON 物件**，不要有其他文字：
{
  "verdict": "pass" 或 "flag",
  "reasons": ["具體指出哪個特徵偏差、與哪個依據不符", ...]
}

verdict 規則：
- 圖片與角色形象一致（或圖中沒有角色本人）→ "pass"，reasons 為空陣列。
- 明顯偏差（換了髮色/髮型/瞳色/物種等，看起來是另一個人）→ "flag"，
  reasons 逐條說明。
"""

MediaResolver = Callable[[str, bool], Awaitable[str | None]]
"""``(stored_url, prefer_public) -> llm-ingestible url or None``.

Injected so the reviewer stays unit-testable without object storage;
the service wires it to
:func:`kokoro_link.application.services.vision_media.to_vision_url_with_storage`.
"""


@dataclass(frozen=True, slots=True)
class ShowcaseImageReview:
    """One post image's advisory verdict."""

    post_id: str
    verdict: str = VERDICT_NEEDS_MANUAL_REVIEW
    reasons: tuple[str, ...] = ()


class ShowcaseImageReviewer:
    """Reviews one candidate's image per call.

    Fail-soft per post, like the text reviewer: a transport error, a
    non-vision route or an unresolvable image marks that post
    ``needs_manual_review`` and the batch carries on. Nothing here can
    produce an approval — blocking or releasing is the control plane's.
    """

    def __init__(self, resolver: ModelResolver, *, media: MediaResolver) -> None:
        self._resolver = resolver
        self._media = media

    async def review(
        self,
        candidate: ShowcaseCandidate,
        *,
        character: Character | None = None,
        reference_urls: Sequence[str] = (),
    ) -> ShowcaseImageReview:
        if not (candidate.image_url or "").strip():
            # Nothing rendered, nothing to drift. The mechanical filter
            # normally guarantees an image; this keeps the reviewer honest
            # if that rule ever loosens.
            return ShowcaseImageReview(post_id=candidate.id, verdict=VERDICT_PASS)
        operator_id = character.user_id if character is not None else None
        try:
            if await self._resolver.is_fake(
                character=character, operator_id=operator_id,
            ):
                return ShowcaseImageReview(
                    post_id=candidate.id,
                    reasons=("此部署未接上真實模型，圖片未經 LLM 預審",),
                )
            model, model_id = await self._resolver.resolve(
                character=character, operator_id=operator_id,
            )
        except Exception as exc:  # noqa: BLE001 — advisory step, never fatal
            _LOGGER.warning(
                "showcase image review resolution failed for post %s: %s",
                candidate.id, exc,
            )
            return ShowcaseImageReview(
                post_id=candidate.id,
                reasons=(f"圖片預審模型解析失敗：{exc}",),
            )
        if not bool(getattr(model, "supports_vision", False)):
            return ShowcaseImageReview(
                post_id=candidate.id,
                reasons=("showcase_image_review 路由到的模型不支援視覺，圖片未審",),
            )
        prefer_public = bool(getattr(model, "prefers_public_image_urls", False))
        post_image = await self._resolve_media(candidate.image_url, prefer_public)
        if post_image is None:
            return ShowcaseImageReview(
                post_id=candidate.id,
                reasons=("貼文圖片無法轉成模型可讀取的形式，圖片未審",),
            )
        references: list[str] = []
        for url in reference_urls[:MAX_REFERENCE_IMAGES]:
            converted = await self._resolve_media(url, prefer_public)
            if converted is not None:
                references.append(converted)
        prompt = _build_prompt(
            candidate,
            character=character,
            reference_count=len(references),
        )
        try:
            kwargs: dict[str, object] = {
                "image_urls": (post_image, *references),
            }
            if model_id is not None:
                kwargs["model"] = model_id
            raw = await model.generate(prompt, **kwargs)
        except Exception as exc:  # noqa: BLE001 — advisory step, never fatal
            _LOGGER.warning(
                "showcase image review failed for post %s: %s", candidate.id, exc,
            )
            return ShowcaseImageReview(
                post_id=candidate.id,
                reasons=(f"圖片 LLM 預審失敗：{exc}",),
            )
        payload = coerce_json_object(raw, site="showcase.image_review")
        if payload is None:
            return ShowcaseImageReview(
                post_id=candidate.id,
                reasons=("圖片 LLM 預審沒有回傳 JSON 物件",),
            )
        return _review_from_payload(candidate.id, payload)

    async def _resolve_media(self, url: str | None, prefer_public: bool) -> str | None:
        if not (url or "").strip():
            return None
        try:
            return await self._media(url, prefer_public)
        except Exception:  # noqa: BLE001 — one broken object must not kill the batch
            _LOGGER.exception("showcase image review media resolution failed for %s", url)
            return None


def _build_prompt(
    candidate: ShowcaseCandidate,
    *,
    character: Character | None,
    reference_count: int,
) -> str:
    lines = [IMAGE_REVIEW_SYSTEM_PROMPT, ""]
    if character is not None and character.name:
        lines.append(f"角色名稱：{character.name}")
    appearance = _appearance_of(character)
    if appearance:
        lines.append("外觀設定：")
        lines.append(appearance)
    if reference_count:
        lines.append(f"參考圖數量：{reference_count}（第 2 張起）")
    else:
        lines.append("參考圖數量：0（只依外觀設定文字判斷）")
    lines.append("")
    lines.append("輸出 JSON：")
    return "\n".join(lines)


def _appearance_of(character: Character | None) -> str:
    if character is None:
        return ""
    appearance = (character.appearance or "").strip()
    if appearance:
        return appearance
    # Older characters predate the structured appearance field; the
    # summary often carries the look in prose, and an empty sheet would
    # otherwise leave rule 3 with nothing to stand on.
    return (character.summary or "").strip()


def _review_from_payload(
    post_id: str, payload: Mapping[str, object],
) -> ShowcaseImageReview:
    raw_verdict = str(payload.get("verdict") or "").strip().lower()
    reasons = tuple(as_reason_list(payload.get("reasons")))
    if raw_verdict == VERDICT_PASS:
        return ShowcaseImageReview(post_id=post_id, verdict=VERDICT_PASS)
    if raw_verdict == VERDICT_FLAG:
        return ShowcaseImageReview(
            post_id=post_id, verdict=VERDICT_FLAG, reasons=reasons,
        )
    return ShowcaseImageReview(
        post_id=post_id,
        verdict=VERDICT_NEEDS_MANUAL_REVIEW,
        reasons=reasons or (f"圖片預審回傳未知 verdict：{raw_verdict!r}",),
    )


__all__ = [
    "IMAGE_REVIEW_SYSTEM_PROMPT",
    "MAX_REFERENCE_IMAGES",
    "MediaResolver",
    "ShowcaseImageReview",
    "ShowcaseImageReviewer",
]
