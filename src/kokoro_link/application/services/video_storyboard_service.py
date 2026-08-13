"""``video_storyboard`` — first frame + post draft → neutral storyboard.

Where this sits: the feed composer already
writes a short ``video_prompt`` blind, from text alone, before any pixel
exists. Once the image step has rendered the clip's **first frame**, that
blind prompt is the weakest input in the chain — it describes a scene the
renderer has since made concrete, and an I2V model that is handed a frame
plus a prompt written without seeing it will happily drift away from the
frame it was told to start on.

This service closes that gap. It shows the rendered first frame to a
vision-capable model together with the post draft and the character's
appearance context, and asks for a storyboard of the next few seconds:
what is seen, how the camera moves, what must not drift from the frame,
what is **heard**, and whether anything is scored. The frame is presented
as *the first frame*, never as a style reference — anchoring is the
entire point.

Structured, and deliberately **engine-neutral** (owner ruling D12,
2026-08-07). V0 asked for free English prose and never mentioned audio at
all, which threw away half of a model that generates sound alongside
picture. The obvious fix — teach this prompt the hosted engine's own
mandatory prompt format — was rejected: format knowledge belongs to the
pipeline adapter, so that swapping the engine costs one adapter instead
of a prompt-pack release. What leaves here is the neutral shape defined
in :mod:`.video_storyboard_shape`, serialised into the existing
``prompt: string`` wire field so no contract had to change.

Fail-open by construction. Every failure mode — no vision route
configured, the routed model is text-only, the frame cannot be turned
into something the model can ingest, the call raises, the answer will not
parse as a storyboard — degrades to the composer's own ``video_prompt``
and logs a warning. A storyboard is an upgrade to a post that was going
to be made anyway; it must never be the reason a post is not made.

Nothing in the synchronous self-host path reaches this module: the
composer only builds a storyboard service where the deferred pipeline is
wired, and an unwired one is itself a fallback to the blind prompt.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.application.services.video_storyboard_shape import (
    DEFAULT_CLIP_SECONDS,
    LEGACY_PROSE_FIELD,
    MAX_STORYBOARD_CHARS,
    NeutralStoryboard,
    decode_json_object,
    looks_like_schema_leak,
    parse_storyboard,
    strip_code_fence,
)
from kokoro_link.application.services.vision_media import (
    to_vision_url_with_storage,
)
from kokoro_link.contracts.object_storage import ObjectStoragePort
from kokoro_link.domain.entities.character import Character
from kokoro_link.infrastructure.prompt.character_identity import (
    render_character_identity_lines,
)
from kokoro_link.infrastructure.prompt.visual_subject import (
    render_character_visual_subject_lines,
)
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)

PROMPT_TEMPLATE_NAME = "feed/video_storyboard"

_MAX_POST_TEXT_CHARS = 400
_MAX_SNIPPETS = 6
_MAX_SNIPPET_CHARS = 160

SOURCE_LLM = "llm"
"""The storyboard model saw the frame and wrote the prompt."""

SOURCE_FALLBACK = "fallback"
"""The storyboard step could not run; the composer's blind
``video_prompt`` is being used instead. ``reason`` says which gate."""

REASON_NO_ROUTE = "no_route"
REASON_NO_VISION = "no_vision_model"
REASON_NO_FRAME = "unusable_first_frame"
REASON_PROMPT_FAILED = "prompt_render_failed"
REASON_CALL_FAILED = "call_failed"
REASON_UNPARSEABLE = "unparseable_output"


@dataclass(frozen=True, slots=True)
class VideoStoryboardRequest:
    """Everything the storyboard call needs, pre-rendered by the caller."""

    character: Character
    first_frame_url: str
    """Storage / public URL of the already-rendered first frame. This is
    the frame the clip must start on, so it is sent as an image, not
    described in words."""
    post_text: str = ""
    """The post body the clip will accompany — read for intent and mood,
    never transcribed into the English prompt."""
    base_video_prompt: str = ""
    """The composer's blind ``video_prompt``. Doubles as directional
    input for the model *and* as the fallback when the call cannot run."""
    context_snippets: tuple[str, ...] = ()
    """Optional supporting bullets (activity, beat, mood) the caller has
    already trimmed."""
    clip_seconds: int = DEFAULT_CLIP_SECONDS


@dataclass(frozen=True, slots=True)
class VideoStoryboardResult:
    prompt: str
    """What to hand the I2V pipeline, always as a string because that is
    what the job contract's ``prompt`` field is.

    Normally a **serialised neutral storyboard** (a JSON object with a
    top-level ``shots`` array) — the pipeline adapter detects that shape
    and renders it into its engine's own format. Otherwise free prose,
    which every adapter still accepts as the body of a single shot. May be
    empty only when the call failed *and* the composer produced no
    ``video_prompt`` either — the caller decides whether that is still
    worth submitting."""
    source: str
    reason: str = ""
    """Populated on fallback with one of the ``REASON_*`` constants."""
    storyboard: NeutralStoryboard | None = None
    """The parsed structure behind ``prompt``, when there is one. Kept for
    diagnostics (the CV6 trace, tests); the wire never sees it."""

    @property
    def used_fallback(self) -> bool:
        return self.source != SOURCE_LLM

    @property
    def is_structured(self) -> bool:
        return self.storyboard is not None


class VideoStoryboardService:
    """Generates one storyboard prompt per call. Never raises."""

    def __init__(
        self,
        resolver: ModelResolver | None = None,
        *,
        object_storage: ObjectStoragePort | None = None,
        public_base_url: str = "",
        uploads_dir: Path | None = None,
    ) -> None:
        self._resolver = resolver
        self._object_storage = object_storage
        self._public_base_url = public_base_url
        self._uploads_dir = uploads_dir

    async def generate(
        self, request: VideoStoryboardRequest,
    ) -> VideoStoryboardResult:
        if self._resolver is None:
            return self._fallback(request, REASON_NO_ROUTE)

        character = request.character
        operator_id = getattr(character, "user_id", None)
        try:
            if await self._resolver.is_fake(
                character=character, operator_id=operator_id,
            ):
                return self._fallback(request, REASON_NO_ROUTE)
            model, model_id = await self._resolver.resolve(
                character=character, operator_id=operator_id,
            )
        except Exception as exc:  # noqa: BLE001 — advisory step, never fatal
            _LOGGER.warning(
                "video storyboard route resolution failed character=%s: %s",
                character.id, exc,
            )
            return self._fallback(request, REASON_NO_ROUTE)

        # Vision is not optional here: a storyboard written without seeing
        # the frame is exactly the blind prompt we already have.
        if not bool(getattr(model, "supports_vision", False)):
            _LOGGER.warning(
                "video storyboard resolved to a non-vision model "
                "character=%s; using the composer video_prompt",
                character.id,
            )
            return self._fallback(request, REASON_NO_VISION)

        frame_url = await self._resolve_frame_url(request, model)
        if not frame_url:
            return self._fallback(request, REASON_NO_FRAME)

        try:
            instruction = build_storyboard_prompt(request)
        except Exception as exc:  # noqa: BLE001 — advisory step, never fatal
            # Practically only reachable through a stale external prompt
            # pack asking for a variable this release no longer supplies.
            _LOGGER.warning(
                "video storyboard instruction could not be rendered "
                "character=%s: %s",
                character.id, exc,
            )
            return self._fallback(request, REASON_PROMPT_FAILED)

        try:
            raw = await model.generate(
                instruction, image_urls=(frame_url,), model=model_id,
            )
        except Exception as exc:  # noqa: BLE001 — advisory step, never fatal
            _LOGGER.warning(
                "video storyboard generation failed character=%s: %s",
                character.id, exc,
            )
            return self._fallback(request, REASON_CALL_FAILED)

        output, storyboard = resolve_storyboard_output(
            raw, clip_seconds=request.clip_seconds or DEFAULT_CLIP_SECONDS,
        )
        if not output:
            _LOGGER.warning(
                "video storyboard returned no usable prompt character=%s",
                character.id,
            )
            return self._fallback(request, REASON_UNPARSEABLE)
        if storyboard is None:
            # Prose is still a usable I2V prompt — every adapter accepts it
            # as the body of a single shot — but it means the soundscape
            # and music the engine can render were never asked for.
            _LOGGER.info(
                "video storyboard fell back to prose character=%s — the "
                "model answered without a usable shots array",
                character.id,
            )
        return VideoStoryboardResult(
            prompt=output, source=SOURCE_LLM, storyboard=storyboard,
        )

    async def _resolve_frame_url(
        self, request: VideoStoryboardRequest, model: object,
    ) -> str | None:
        url = (request.first_frame_url or "").strip()
        if not url:
            return None
        try:
            return await to_vision_url_with_storage(
                url,
                uploads_dir=self._uploads_dir,
                public_base_url=self._public_base_url,
                object_storage=self._object_storage,
                prefer_public_image_urls=bool(
                    getattr(model, "prefers_public_image_urls", False),
                ),
            )
        except Exception as exc:  # noqa: BLE001 — advisory step, never fatal
            _LOGGER.warning(
                "video storyboard could not prepare the first frame "
                "character=%s: %s",
                request.character.id, exc,
            )
            return None

    @staticmethod
    def _fallback(
        request: VideoStoryboardRequest, reason: str,
    ) -> VideoStoryboardResult:
        return VideoStoryboardResult(
            prompt=(request.base_video_prompt or "").strip(),
            source=SOURCE_FALLBACK,
            reason=reason,
        )


def build_storyboard_prompt(request: VideoStoryboardRequest) -> str:
    """Render the storyboard instruction. Exposed for the CV6 trace,
    which records the exact text the model was shown."""

    clip_seconds = request.clip_seconds or DEFAULT_CLIP_SECONDS

    base = (request.base_video_prompt or "").strip()
    base_prompt_block = ""
    if base:
        base_prompt_block = (
            "初版方向（撰文階段在還沒看到畫面時寫的 video prompt，"
            "可以沿用它的動作構想，但畫面細節一律以第一幀為準；"
            "與第一幀衝突的部分直接捨棄）：\n"
            f"{base}\n\n"
        )

    snippets = [
        snippet.strip()[:_MAX_SNIPPET_CHARS]
        for snippet in request.context_snippets[:_MAX_SNIPPETS]
        if snippet and snippet.strip()
    ]
    context_block = ""
    if snippets:
        joined = "\n".join(f"- {snippet}" for snippet in snippets)
        context_block = f"補充脈絡：\n{joined}\n\n"

    return get_default_loader().render(
        PROMPT_TEMPLATE_NAME,
        clip_seconds=clip_seconds,
        max_prompt_chars=MAX_STORYBOARD_CHARS,
        # V0's single-field schema line. The shipped template no longer
        # references it, but ``Template.substitute`` ignores extra keys and
        # an external prompt pack mounted before this release still asks
        # for it — dropping the variable would turn a stale overlay from
        # "degrades to prose" into "raises on every post".
        schema_line='  {"storyboard_prompt": "英文分鏡 prompt 全文"}',
        character_block="\n".join(_character_block(request.character)),
        post_text=(request.post_text or "").strip()[:_MAX_POST_TEXT_CHARS]
        or "（無貼文草稿，依畫面本身決定情緒）",
        base_prompt_block=base_prompt_block,
        context_block=context_block,
    )


def _character_block(character: Character) -> list[str]:
    lines = [f"- 名稱：{character.name}"]
    lines.extend(render_character_identity_lines(character))
    lines.extend(
        f"- {line}" for line in render_character_visual_subject_lines(character)
    )
    if character.appearance:
        lines.append(f"- 外觀：{character.appearance[:300]}")
    if character.summary:
        lines.append(f"- 簡介：{character.summary[:200]}")
    if character.personality:
        lines.append("- 性格：" + "、".join(character.personality[:6]))
    return lines


_LEGACY_FIELD_RE = re.compile(
    rf'"{LEGACY_PROSE_FIELD}"\s*:\s*"((?:\\.|[^"\\])*)"', re.DOTALL,
)


def resolve_storyboard_output(
    raw: str, *, clip_seconds: int = DEFAULT_CLIP_SECONDS,
) -> tuple[str, NeutralStoryboard | None]:
    """Model answer → ``(prompt string, structure or None)``.

    The ladder, in the order deviations actually happen:

    1. a neutral storyboard — fenced, bare, wrapped in chatter, or cut off
       by a token ceiling with some complete shots still intact. Serialised
       back out under :data:`MAX_STORYBOARD_CHARS`;
    2. V0's ``{"storyboard_prompt": "..."}``, which an external prompt pack
       mounted before this release still produces. Prose, not structure;
    3. plain prose that dropped the wrapper entirely.

    The one thing never returned is a half-serialised envelope: forwarding
    ``{"shots": [{"visual": "...`` to the video pipeline would render JSON
    keys into the clip, so it degrades to the blind prompt instead.
    """
    text = strip_code_fence(raw)
    if not text:
        return "", None

    storyboard = parse_storyboard(text, clip_seconds=clip_seconds)
    if storyboard is not None:
        return storyboard.to_json(max_chars=MAX_STORYBOARD_CHARS), storyboard

    decoded = decode_json_object(text)
    if decoded is not None:
        # A well-formed object that yielded no shots. Its prose field is the
        # only thing worth forwarding; anything else in there is a schema we
        # do not recognise, and shipping it verbatim would put JSON in the
        # clip.
        legacy = str(decoded.get(LEGACY_PROSE_FIELD, "") or "").strip()
        return legacy[:MAX_STORYBOARD_CHARS], None

    salvaged = _salvage_legacy_prose(text)
    if salvaged:
        return salvaged[:MAX_STORYBOARD_CHARS], None
    if looks_like_schema_leak(text):
        return "", None
    return text[:MAX_STORYBOARD_CHARS], None


def parse_storyboard_output(
    raw: str, *, clip_seconds: int = DEFAULT_CLIP_SECONDS,
) -> str:
    """The string half of :func:`resolve_storyboard_output`."""
    return resolve_storyboard_output(raw, clip_seconds=clip_seconds)[0]


def _salvage_legacy_prose(text: str) -> str:
    """V0's single prose field pulled out of a truncated object."""
    match = _LEGACY_FIELD_RE.search(text)
    if match is None:
        return ""
    try:
        value = json.loads(f'"{match.group(1)}"')
    except json.JSONDecodeError:
        return ""
    return str(value).strip()


__all__ = [
    "DEFAULT_CLIP_SECONDS",
    "MAX_STORYBOARD_CHARS",
    "PROMPT_TEMPLATE_NAME",
    "REASON_CALL_FAILED",
    "REASON_NO_FRAME",
    "REASON_NO_ROUTE",
    "REASON_NO_VISION",
    "REASON_PROMPT_FAILED",
    "REASON_UNPARSEABLE",
    "SOURCE_FALLBACK",
    "SOURCE_LLM",
    "NeutralStoryboard",
    "VideoStoryboardRequest",
    "VideoStoryboardResult",
    "VideoStoryboardService",
    "build_storyboard_prompt",
    "parse_storyboard_output",
    "resolve_storyboard_output",
]
