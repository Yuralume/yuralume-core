"""LLM-backed feed-post composer.

Renders a prompt that gives the model the character's persona, the
candidate's hint + supporting context snippets, and an instruction to
emit a JSON object with ``content_text`` and (when an image is wanted)
``image_prompt``. The automatic publishing boundary is fail-closed:
wrapper-less prose and invalid field types are rejected rather than
being mistaken for a post. Structurally broken JSON can still be
salvaged when a complete, quote-closed ``content_text`` field exists.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import replace

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.application.services.video_storyboard_shape import (
    DEFAULT_CLIP_SECONDS,
)
from kokoro_link.infrastructure.llm.cloud_refusal import (
    log_auxiliary_llm_failure,
)
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.feed import (
    FeedComposerInput,
    FeedComposerOutput,
    FeedComposerPort,
)
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.infrastructure.prompt.character_identity import (
    render_character_identity_lines,
)
from kokoro_link.infrastructure.prompt.visual_subject import (
    build_visual_subject_prompt,
    render_character_visual_subject_lines,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompt.role_boundary import (
    render_role_knowledge_boundary_lines,
)
from kokoro_link.infrastructure.prompt.timing_utils import (
    render_current_time_fact_lines,
)
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)

_MAX_BODY_CHARS = 280
"""Cap on the published post body — Twitter-ish; long posts feel
out of place on an IG-style feed wall and longer payloads burn more
ComfyUI context for the matching image prompt."""

_MAX_IMAGE_PROMPT_CHARS = 320
_MAX_VIDEO_PROMPT_CHARS = 600
"""Video prompts run longer than image prompts — Wan2.2 benefits from
2-3 short sentences with motion + camera direction, not just a tag
list. Cap is generous enough for that without inviting wall-of-text."""

_ALLOWED_MEDIA_KINDS = {"image", "video", "none"}
_MEDIA_KIND_LABELS = {
    "video": "影片",
    "image": "圖片",
    "none": "純文字",
}

_FIRST_FRAME_TEMPLATE_NAME = "feed/video_first_frame"


class _InvalidComposerOutput(ValueError):
    """Typed, secret-safe reason for rejecting one model response."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class LLMFeedComposer(FeedComposerPort):
    def __init__(
        self,
        model: ChatModelPort | None = None,
        *,
        provider: ActiveLLMProviderPort | None = None,
        feature_key: str | None = None,
        video_enabled: bool = False,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider, model=model, feature_key=feature_key,
        )
        # Composer-side flag mirrors the container-level "is a video
        # provider wired" check — when off, we don't even mention video
        # in the prompt so the LLM stays in the original 2-field
        # ``content_text + image_prompt`` shape. Avoids the model
        # picking ``media_kind=video`` for a deployment that can't
        # render it.
        self._video_enabled = video_enabled

    async def compose(
        self, payload: FeedComposerInput,
    ) -> FeedComposerOutput:
        if await self._resolver.is_fake(character=payload.character):
            return FeedComposerOutput(content_text="")
        prompt = _build_prompt(payload, video_enabled=self._video_enabled)
        try:
            raw = await self._resolver.generate(
                prompt, character=payload.character,
            )
        except Exception as exc:
            log_auxiliary_llm_failure(
                _LOGGER, exc,
                "feed composer LLM call failed character=%s",
                payload.character.id,
            )
            return FeedComposerOutput(content_text="")
        try:
            output = _parse_output(
                raw,
                image_required=payload.image_required,
                video_enabled=self._video_enabled,
            )
        except _InvalidComposerOutput as exc:
            _log_invalid_output(
                raw,
                payload=payload,
                reason=exc.reason,
            )
            return FeedComposerOutput(content_text="")
        if _needs_first_frame_prompt(output, image_required=payload.image_required):
            output = await self._add_first_frame_prompt(output, payload)
        return output

    async def _add_first_frame_prompt(
        self, output: FeedComposerOutput, payload: FeedComposerInput,
    ) -> FeedComposerOutput:
        """Second pass: write the still the clip is supposed to start on.

        A video post whose composer filled only ``video_prompt`` used to
        have that prompt handed straight to the *image* model as the
        clip's first frame. Its mandated shape is a three-beat
        ``A → then B → finally C`` action, and an image model asked to
        draw three beats at once answers with one picture holding three
        stacked panels — which the storyboard step then reads off the
        frame and pins as a composition anchor, so the finished clip is a
        three-tier contact sheet that never moves as a single scene.

        So the beats get collapsed back into one instant here, by the
        model that wrote them, rather than papered over downstream. This
        is advisory: a failure leaves ``image_prompt`` empty and the video
        branch declines to render a frame it knows will be wrong.
        """
        character = payload.character
        try:
            instruction = _build_first_frame_prompt(payload, output)
            raw = await self._resolver.generate(
                instruction, character=character,
            )
        except Exception as exc:
            log_auxiliary_llm_failure(
                _LOGGER, exc,
                "feed first-frame prompt call failed character=%s",
                character.id,
            )
            return output
        tags = _parse_first_frame_output(raw)
        if not tags:
            _LOGGER.warning(
                "feed first-frame prompt returned nothing usable "
                "character=%s — the video post will degrade to text",
                character.id,
            )
            return output
        return replace(output, image_prompt=tags)


def _build_prompt(
    payload: FeedComposerInput, *, video_enabled: bool = False,
) -> str:
    character = payload.character
    persona_lines = _persona_block(character)
    subject_prompt = build_visual_subject_prompt(character)
    knowledge_boundary_lines = render_role_knowledge_boundary_lines()
    snippet_block = "\n".join(f"- {line}" for line in payload.context_snippets) \
        if payload.context_snippets else "（無）"
    if not payload.image_required:
        image_clause = "image_prompt：留空字串"
    elif subject_prompt.is_non_human_animal:
        image_clause = (
            "image_prompt：30-80 個英文 danbooru 風格 tag，描繪這篇貼文搭配的單張照片。"
            "聚焦在非人類動物角色本體、姿態 / 場景 / 光線 / 表情。"
            "必須使用 no humans、animal focus、物種與動物解剖 tag；"
            "禁止 1girl、1boy、person、human face、human body、cat ears on a human、"
            "furry humanoid，除非 Visual subject type 明確是 anthropomorphic。"
        )
    else:
        image_clause = (
            "image_prompt：30-80 個英文 danbooru 風格 tag，描繪這篇貼文搭配的單張照片。"
            "聚焦在角色當下的姿態 / 場景 / 光線 / 表情。"
            "只加入符合 Visual subject type 的基礎 tag；人類角色可加入 1girl, solo。"
        )

    if video_enabled and payload.image_required:
        schema_line = (
            '  {"content_text": "貼文本體", "media_kind": "image|video|none", '
            '"image_prompt": "英文 tag 串", "video_prompt": "英文自然語言 prompt"}'
        )
        media_lines = [
            "- media_kind：三選一，挑最能襯托這篇貼文的呈現方式：",
            "    * \"video\"：當貼文重點在『一個有動作 / 表情變化 / 鏡頭感的瞬間』",
            "      （例：翻書、玩手機翻來覆去、撥髮、低頭吃東西、轉身、嘟嘴後別過頭）",
            "      影片時間只有 5 秒，挑選一個能在 5 秒內走完的小動作。",
            "    * \"image\"：靜態氛圍 / 構圖大於動作（例：站在窗邊看夕陽、桌上擺好的甜點）",
            "    * \"none\"：純內心獨白 / 沒有具體場景可拍",
            "  先參考下方近期貼文媒體節奏；不要因成本直覺過度避開影片，",
            "  但也不要為了湊比例硬選 video。",
            f"- {image_clause}",
            "- image_prompt 是**必填**的，media_kind=\"video\" 時也一樣不能留空——"
            "影片是從這張圖動起來的，這張圖就是影片的第一幀。",
            "  media_kind=\"video\" 時，image_prompt 只畫 video_prompt 那三步動作裡"
            "**A 開始前的那一個靜止瞬間**，而且必須是一張完整的單一畫面："
            "不要把 A / B / C 三個時刻同時畫進同一張圖，"
            "不要用 comic、4koma、multiple views、split screen、sequence、"
            "panels、borders 這類會把畫面切成好幾格的 tag，也不要在 tag 串裡"
            "寫動作的先後順序（then、after、finally）。",
            "- video_prompt：當 media_kind=\"video\" 時必填，否則留空字串。",
            "  寫法是 30-150 字的英文自然語言（不是 tag），格式：",
            "    [Anime style, cinematic short clip.] + [外觀描述句] + [場景與動作 verbs，"
            "    A → then B → finally C 三步小動作] + [鏡頭：medium close-up / slow dolly / "
            "    handheld drift] + [光線、景深、24fps、5 seconds]。",
            "  動作要在 5 秒內能完成。識別角色靠『外觀描述句』，不要寫 tag。",
        ]
        prompts_clause = "\n".join(media_lines)
    else:
        schema_line = (
            '  {"content_text": "貼文本體（玩家可見自然語言）", "image_prompt": "英文 tag 串"}'
        )
        prompts_clause = f"- {image_clause}"

    # 「今日真實事實層」—— calendar + weather 兩條事實，貼文必須跟
    # chat / proactive 對齊（不能 chat 知道下雨，feed 還在貼晴朗午後）。
    # 兩條都是 LLM-first 純事實，不寫死「下雨就別貼戶外」這種行為條件。
    fact_block_lines: list[str] = []
    fact_block_lines.extend(
        render_current_time_fact_lines(payload.now, payload.local_tz),
    )
    if video_enabled and payload.image_required:
        fact_block_lines.extend(
            _render_recent_media_cadence(payload.recent_media_kinds),
        )
    cal = (payload.calendar_context or "").strip()
    if cal:
        fact_block_lines.append("今日真實世界行事曆：")
        fact_block_lines.append(cal)
    weather = (payload.weather_context or "").strip()
    if weather:
        fact_block_lines.append("此刻真實世界天氣：")
        fact_block_lines.append(weather)
        # Freshness authority (not a behavioural rule): the weather fact is
        # re-fetched per post, but the rainy context_snippets / memory /
        # earlier posts the model also reads can keep dragging the caption
        # AND the image_prompt back into the rain after the sky cleared.
        # Tell the model the current fact wins for both text and image; we
        # never say how the character should react to the weather.
        fact_block_lines.append(
            "（這是此刻真實天氣事實。若下方參考片段、近期記憶或先前貼文隱含的天氣"
            "與此刻不一致——例如先前在下雨、現在已轉晴——貼文內容與配圖一律以此刻"
            "天氣事實為準，不要延續已過時的天氣或雨天畫面。）"
        )
    location = (payload.operator_location_context or "").strip()
    if location:
        fact_block_lines.append(location)
    fact_block = ""
    if fact_block_lines:
        fact_block_lines.append(
            "（以上是事實層，請自行從中推導今天該寫怎樣的貼文；"
            "不要硬抄字面，也不要無視 — 例如下雨天就別寫「陽光燦爛」這種與事實衝突的內容。）"
        )
        fact_block = "\n".join(fact_block_lines) + "\n"
    body = get_default_loader().render(
        "feed/composer",
        schema_line=schema_line,
        max_body_chars=_MAX_BODY_CHARS,
        prompts_clause=prompts_clause,
        persona_block="\n".join(persona_lines),
        knowledge_boundary_block="\n".join(knowledge_boundary_lines),
        fact_block=fact_block,
        kind_value=payload.kind.value,
        source_kind=payload.source.kind,
        hint=payload.hint,
        snippet_block=snippet_block,
    )
    # FRONTEND_I18N_PLAN §使用者主要語言 — same fact line as chat /
    # proactive so feed posts can't drift into a different output
    # language. Prepended (not threaded into the template) to keep this
    # change self-contained — the template stays untouched.
    language_hint = render_operator_language_hint(
        payload.operator_primary_language,
    )
    if language_hint:
        body = f"{language_hint}\n\n{body}"
    return body


def _render_recent_media_cadence(kinds: tuple[str, ...]) -> list[str]:
    normalised = tuple(
        kind for kind in kinds[:5] if kind in _MEDIA_KIND_LABELS
    )
    if not normalised:
        return ["近期貼文媒體節奏：尚無貼文紀錄"]

    lines = [
        "近期貼文媒體節奏（由新到舊）："
        + "、".join(_MEDIA_KIND_LABELS[kind] for kind in normalised)
    ]
    consecutive_non_video = 0
    for kind in normalised:
        if kind == "video":
            break
        consecutive_non_video += 1
    if consecutive_non_video:
        lines.append(f"最近連續 {consecutive_non_video} 篇沒有影片。")
    else:
        lines.append("最近一篇已經是影片。")
    return lines


def _needs_first_frame_prompt(
    output: FeedComposerOutput, *, image_required: bool,
) -> bool:
    """Whether the video pick is missing the still it has to start on.

    Only ``media_kind == "video"`` matters: an image post with no
    ``image_prompt`` is the composer saying "no picture", which is a
    legitimate text-only post, not a gap to fill."""
    return (
        image_required
        and output.media_kind == "video"
        and bool(output.video_prompt.strip())
        and not output.image_prompt.strip()
    )


def _build_first_frame_prompt(
    payload: FeedComposerInput, output: FeedComposerOutput,
) -> str:
    return get_default_loader().render(
        _FIRST_FRAME_TEMPLATE_NAME,
        clip_seconds=DEFAULT_CLIP_SECONDS,
        video_prompt=output.video_prompt.strip(),
        character_block="\n".join(
            _visual_subject_block(payload.character),
        ),
    )


def _visual_subject_block(character: Character) -> list[str]:
    """Only what decides how the subject is drawn — the caption is
    already written by the time this runs, so persona, mood and speaking
    style would just be tokens the image tags must not absorb."""
    lines = [f"- 名稱：{character.name}"]
    lines.extend(
        f"- {line}" for line in render_character_visual_subject_lines(character)
    )
    if character.appearance:
        lines.append(f"- 外觀：{character.appearance[:300]}")
    return lines


def _parse_first_frame_output(raw: str) -> str:
    """Tag string out of the second pass, however it came wrapped.

    The template asks for a bare comma-separated tag string, but the same
    model that wraps composer output in a fence or a JSON object will do
    it here too — and a leaked ``{"image_prompt": ...}`` envelope would be
    rendered as literal text into the frame."""
    text = (raw or "").strip()
    if not text:
        return ""
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"```$", "", text).strip()
    if text.startswith("{"):
        salvaged = _salvage_string_field(text, _IMAGE_PROMPT_FIELD_RE)
        return (salvaged or "")[:_MAX_IMAGE_PROMPT_CHARS]
    if _looks_like_schema_leak(text):
        return ""
    return text[:_MAX_IMAGE_PROMPT_CHARS]


def _persona_block(character: Character) -> list[str]:
    lines = [f"- 名稱：{character.name}"]
    lines.extend(render_character_identity_lines(character))
    lines.extend(f"- {line}" for line in render_character_visual_subject_lines(character))
    if character.summary:
        lines.append(f"- 簡介：{character.summary[:200]}")
    if character.personality:
        lines.append("- 性格：" + "、".join(character.personality[:6]))
    if character.speaking_style:
        lines.append(f"- 說話風格：{character.speaking_style[:120]}")
    if character.boundaries:
        lines.append("- 底線：" + "、".join(character.boundaries[:4]))
    state = character.state
    lines.append(
        "- 當前狀態：情緒 "
        f"{state.emotion}/好感 {state.affection}/疲勞 {state.fatigue}/"
        f"信任 {state.trust}/能量 {state.energy}",
    )
    return lines


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)

# Field-level rescue for a structurally-broken composer object. When the
# model emits invalid JSON — a stray un-keyed element, or a response cut
# off mid-object by a ``max_tokens`` ceiling — neither ``json.loads`` nor
# the ``{...}`` block regex above can recover it (the latter needs a
# closing brace the truncated tail never reached). But the leading string
# fields are usually intact and already quote-closed, so we can pull their
# values out directly. The capture honours JSON backslash escapes so an
# escaped quote inside the value doesn't end the match early.
_CONTENT_TEXT_FIELD_RE = re.compile(
    r'"content_text"\s*:\s*"((?:\\.|[^"\\])*)"', re.DOTALL,
)
_IMAGE_PROMPT_FIELD_RE = re.compile(
    r'"image_prompt"\s*:\s*"((?:\\.|[^"\\])*)"', re.DOTALL,
)

_SCHEMA_LEAK_MARKERS = (
    '"content_text"',
    '"image_prompt"',
    '"video_prompt"',
    '"media_kind"',
)
"""If the fallback body still carries one of these keys it's a serialized
composer object that failed to parse — never publish that envelope to the
player-facing feed."""


def _salvage_string_field(
    candidate: str, pattern: re.Pattern[str],
) -> str | None:
    """Pull one JSON string field out of broken/truncated composer output.

    Returns the JSON-decoded, stripped value, or ``None`` when the field
    is absent (genuine prose that dropped the wrapper) or its escapes
    can't be decoded. Only matches a fully quote-closed value, so a field
    truncated mid-string yields ``None`` and degrades cleanly."""
    match = pattern.search(candidate)
    if match is None:
        return None
    try:
        value = json.loads(f'"{match.group(1)}"')
    except json.JSONDecodeError:
        return None
    value = value.strip()
    return value or None


def _looks_like_schema_leak(candidate: str) -> bool:
    """Whether ``candidate`` is a serialized composer object rather than
    the plain-prose fallback we're happy to publish verbatim."""
    stripped = candidate.lstrip()
    return stripped.startswith("{") and any(
        marker in stripped for marker in _SCHEMA_LEAK_MARKERS
    )


def _log_invalid_output(
    raw: str | None,
    *,
    payload: FeedComposerInput,
    reason: str,
) -> None:
    """Log correlation evidence without persisting model-authored text."""
    text = raw or ""
    fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    _LOGGER.error(
        "feed composer rejected invalid LLM output character=%s source=%s "
        "reason=%s response_chars=%d response_sha256=%s",
        payload.character.id,
        payload.source.kind,
        reason,
        len(text),
        fingerprint,
    )


def _parse_output(
    raw: str, *, image_required: bool, video_enabled: bool = False,
) -> FeedComposerOutput:
    text = (raw or "").strip()
    if not text:
        raise _InvalidComposerOutput("empty_response")
    candidate = text
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
        candidate = re.sub(r"```$", "", candidate)
        candidate = candidate.strip()
    parsed: dict | None = None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        match = _JSON_BLOCK_RE.search(candidate)
        if match is not None:
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                parsed = None
    if not isinstance(parsed, dict):
        # Structurally broken JSON — a stray un-keyed element, or a
        # response truncated mid-object by a max_tokens ceiling — lands
        # here alongside wrapper-less prose and scalar sentinels. Rescue
        # only a complete, quote-closed schema field. Everything else
        # fails closed; automatic posts must cross the structured-output
        # boundary before they can reach persistence.
        salvaged = _salvage_string_field(candidate, _CONTENT_TEXT_FIELD_RE)
        if salvaged is not None:
            image_prompt = ""
            if image_required:
                recovered = _salvage_string_field(
                    candidate, _IMAGE_PROMPT_FIELD_RE,
                )
                if recovered:
                    image_prompt = recovered[:_MAX_IMAGE_PROMPT_CHARS]
            return FeedComposerOutput(
                content_text=salvaged[:_MAX_BODY_CHARS],
                image_prompt=image_prompt,
            )
        reason = (
            "unrecoverable_schema_payload"
            if _looks_like_schema_leak(candidate)
            else "response_not_json_object"
        )
        raise _InvalidComposerOutput(reason)

    raw_body = parsed.get("content_text")
    if not isinstance(raw_body, str):
        raise _InvalidComposerOutput("content_text_not_string")
    body = raw_body.strip()[:_MAX_BODY_CHARS]
    if not body:
        raise _InvalidComposerOutput("content_text_empty")
    image_prompt = (
        str(parsed.get("image_prompt", "") or "").strip()[:_MAX_IMAGE_PROMPT_CHARS]
        if image_required else ""
    )

    # media_kind + video_prompt are only honoured when the container
    # actually has a video provider wired. Falls back to "image" so a
    # composer trained on the old schema (or a model that ignored the
    # new field) keeps producing image posts.
    media_kind = "image"
    video_prompt = ""
    if video_enabled:
        raw_kind = str(parsed.get("media_kind", "") or "").strip().lower()
        if raw_kind in _ALLOWED_MEDIA_KINDS:
            media_kind = raw_kind
        if media_kind == "video":
            video_prompt = str(
                parsed.get("video_prompt", "") or "",
            ).strip()[:_MAX_VIDEO_PROMPT_CHARS]
            # If the model picked video but emitted no prompt, demote
            # back to image so the service has something to render
            # instead of skipping the visual entirely.
            if not video_prompt:
                media_kind = "image"
        if media_kind == "none":
            image_prompt = ""
            video_prompt = ""

    return FeedComposerOutput(
        content_text=body,
        image_prompt=image_prompt,
        video_prompt=video_prompt,
        media_kind=media_kind,
    )
