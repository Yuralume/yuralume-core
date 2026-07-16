"""LLM-backed prompt rewriters for world illustrations.

Place illustrations and Play turn/event illustrations need different
contracts. A place cover should avoid characters so it remains reusable;
a turn image must preserve visible actors and the player's latest action.
"""

from __future__ import annotations

import logging
import re

from collections.abc import Sequence

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.prompt_rewriter import (
    PromptRewriteError,
    PromptRewriterPort,
)

_LOGGER = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You rewrite world/location context into ONE line of English tags for "
    "anime-style SDXL (Illustrious family).\n"
    "This is a PLACE illustration, not a character portrait.\n\n"
    "Rules:\n"
    "- Output only comma-separated tags. No prose, no explanations, no JSON.\n"
    "- Environment only: architecture, objects, layout, lighting, weather, time.\n"
    "- No people tags. Never output 1girl/1boy/person/man/woman/crowd.\n"
    "- Prefer concrete visual tags over abstract emotions.\n"
    "- Keep under ~28 tags.\n"
    "- Do not include quality boosters (masterpiece, best quality, etc.).\n"
    "- Do not include negative tags.\n"
)

_WORLD_VISUAL_SYSTEM_PROMPT = (
    "You rewrite World Play scene context into ONE line of English tags "
    "for anime-style SDXL (Illustrious family).\n"
    "This is a TURN/EVENT illustration, not reusable location cover art.\n\n"
    "Rules:\n"
    "- Output only comma-separated tags. No prose, no explanations, no JSON.\n"
    "- Preserve visible non-viewer characters and the latest Recent happenings.\n"
    "- If a character is present, include concrete person tags and keep the "
    "character visible in frame.\n"
    "- Translate actions/states into concrete visual tags: pose, expression, "
    "props, camera relation, room objects, lighting, weather, time.\n"
    "- Prefer concrete visual tags over abstract emotions.\n"
    "- Keep under ~36 tags.\n"
    "- Do not include quality boosters (masterpiece, best quality, etc.).\n"
    "- Do not include negative tags.\n"
)

_USER_TEMPLATE = "{text}\n\nScene tags:"

_MIN_OUTPUT_CHARS = 3
_MAX_OUTPUT_CHARS = 800

_FENCE_RE = re.compile(r"```(?:\w+)?\n?")
_LABEL_RE = re.compile(
    r"^(?:positive|prompt|tags|scene\s*tags?)\s*[:：]\s*",
    re.IGNORECASE,
)
_HUMAN_TAG_FRAGMENTS = {
    "1girl", "1boy", "girl", "boy", "man", "woman", "people", "person",
    "crowd", "human", "solo",
}


class LLMScenePromptRewriter(PromptRewriterPort):
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

    async def rewrite(
        self,
        text: str,
        *,
        character=None,  # noqa: ANN001
        image_urls: Sequence[str] = (),
    ) -> str:
        # ``image_urls`` accepted for ``PromptRewriterPort`` parity but
        # deliberately ignored — place covers are character-free; the
        # rewriter must not pull person/outfit cues from a user photo.
        del image_urls
        source = text.strip()
        if not source:
            return ""
        if await self._resolver.is_fake():
            return _ensure_no_humans(source[:_MAX_OUTPUT_CHARS])
        prompt = f"{_SYSTEM_PROMPT}\n\n{_USER_TEMPLATE.format(text=source)}"
        try:
            raw = await self._resolver.generate(prompt)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("scene prompt rewriter LLM call failed")
            raise PromptRewriteError(f"LLM call failed: {exc}") from exc

        cleaned = _clean(raw)
        if len(cleaned) < _MIN_OUTPUT_CHARS:
            raise PromptRewriteError(
                f"rewritten prompt too short: {cleaned!r}",
            )
        if len(cleaned) > _MAX_OUTPUT_CHARS:
            cleaned = cleaned[:_MAX_OUTPUT_CHARS].rstrip().rstrip(",")
        return _ensure_no_humans(cleaned)


class LLMWorldVisualPromptRewriter(PromptRewriterPort):
    """Rewriter for Play turn/event images where characters are allowed.

    This deliberately does not reuse :class:`LLMScenePromptRewriter`
    because that class removes people tags by contract.
    """

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

    async def rewrite(
        self,
        text: str,
        *,
        character=None,  # noqa: ANN001
        image_urls: Sequence[str] = (),
    ) -> str:
        # ``image_urls`` accepted for ``PromptRewriterPort`` parity. The
        # World Play turn-image path doesn't currently surface user
        # attachments, so we ignore them — but the signature must
        # tolerate them so chat-path callers can hand the rewriter the
        # same kwargs uniformly.
        del image_urls
        source = text.strip()
        if not source:
            return ""
        if await self._resolver.is_fake():
            return source[:_MAX_OUTPUT_CHARS]
        prompt = (
            f"{_WORLD_VISUAL_SYSTEM_PROMPT}\n\n"
            f"{_USER_TEMPLATE.format(text=source)}"
        )
        try:
            raw = await self._resolver.generate(prompt)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("world visual prompt rewriter LLM call failed")
            raise PromptRewriteError(f"LLM call failed: {exc}") from exc

        cleaned = _clean(raw)
        if len(cleaned) < _MIN_OUTPUT_CHARS:
            raise PromptRewriteError(
                f"rewritten prompt too short: {cleaned!r}",
            )
        if len(cleaned) > _MAX_OUTPUT_CHARS:
            cleaned = cleaned[:_MAX_OUTPUT_CHARS].rstrip().rstrip(",")
        return cleaned


def _clean(raw: str) -> str:
    text = raw.strip()
    text = _FENCE_RE.sub("", text)
    text = text.replace("```", "")
    for line in text.splitlines():
        candidate = line.strip()
        if candidate:
            text = candidate
            break
    else:
        text = ""
    text = _LABEL_RE.sub("", text).strip()
    if len(text) >= 2 and text[0] in "\"'“" and text[-1] in "\"'”":
        text = text[1:-1].strip()
    text = re.sub(r"\s*,\s*", ", ", text).strip(", ").strip()
    return text


def _ensure_no_humans(text: str) -> str:
    kept: list[str] = []
    for raw_tag in text.split(","):
        tag = raw_tag.strip()
        if not tag:
            continue
        lowered = tag.lower()
        if any(fragment in lowered for fragment in _HUMAN_TAG_FRAGMENTS):
            continue
        kept.append(tag)
    if not any(tag.lower() == "no humans" for tag in kept):
        kept.insert(0, "no humans")
    return ", ".join(kept)
