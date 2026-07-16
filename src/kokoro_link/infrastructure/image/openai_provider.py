"""OpenAI GPT Image 2 :class:`ImageProviderPort` adapter.

Hosted alternative to the local ComfyUI workflow. Picks up
``OpenAIImageSettings`` from the container and turns
``(character, scene)`` into one or more PNGs via the public
``/v1/images/generations`` REST endpoint.

Compared with the ComfyUI path this adapter is much shorter because:

  * GPT Image reads natural language directly, so we skip the
    danbooru rewriter and just concatenate the structured fields the
    operator already wrote (``appearance`` + runtime mood + scene).
  * Aspect → ``size`` is a fixed three-way mapping; there's no LoRA /
    workflow / checkpoint plumbing to thread.
  * The API returns base64 PNGs inline, so there's no follow-up
    download step like ComfyUI's history walk.

Failure model maps onto the port:

  * Network / timeout              → :class:`ImageTimeoutError`
  * Non-2xx HTTP / malformed body  → :class:`ImageGenerationError`
  * 2xx with empty ``data``        → :class:`ImageNoOutputError`

We never fall back silently — callers (``CharacterImageService`` /
``ComfyImageTool`` / ``FeedComposerService``) already decide whether
to apologise in chat, surface an HTTP error, or text-only the feed
post.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

import httpx

from kokoro_link.contracts.image_provider import (
    ImageGenerationError,
    ImageNoOutputError,
    ImageProviderPort,
    ImageTokenUsage,
    ImageTimeoutError,
)
from kokoro_link.infrastructure.http_error_logging import log_http_error_response
from kokoro_link.infrastructure.prompt.character_identity import (
    render_character_visual_identity_lines,
)
from kokoro_link.infrastructure.prompt.visual_subject import (
    render_character_visual_subject_lines,
)

if TYPE_CHECKING:
    from kokoro_link.domain.entities.character import Character

_LOGGER = logging.getLogger(__name__)

# Allowed GPT Image sizes (per OpenAI docs). Anything else gets
# rejected upstream with a 400, so we clamp here to a known-good set.
ASPECT_TO_SIZE: dict[str, str] = {
    "portrait": "1024x1536",
    "landscape": "1536x1024",
    "square": "1024x1024",
}
_DEFAULT_ASPECT = "portrait"
_ALLOWED_QUALITIES = {"low", "medium", "high", "auto"}
_MAX_BATCH = 4
"""Cap matches ``CharacterImageService.MAX_CANDIDATES_PER_BATCH`` —
keeps cost predictable when the gacha route hands us a big ``batch``
and matches the de-facto upper bound the rest of the system enforces."""


class OpenAIImageProvider(ImageProviderPort):
    provider_id = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-image-2",
        quality: str = "medium",
        timeout_seconds: float = 180.0,
        base_url: str = "https://api.openai.com/v1",
    ) -> None:
        if not api_key:
            raise ValueError("OpenAIImageProvider requires a non-empty api_key")
        self._api_key = api_key
        self._model = model or "gpt-image-2"
        self._quality = quality if quality in _ALLOWED_QUALITIES else "medium"
        self._timeout = float(timeout_seconds)
        self._endpoint = f"{base_url.rstrip('/')}/images/generations"
        self.last_model_id = self._model
        self.last_usage: ImageTokenUsage | None = None

    async def generate(
        self,
        *,
        character: "Character",
        positive: str,
        aspect: str = _DEFAULT_ASPECT,
        batch: int = 1,
        recent_dialogue: str = "",
        use_runtime_state: bool = True,
        user_attachment_urls: Sequence[str] = (),
    ) -> list[bytes]:
        self.last_model_id = self._model
        self.last_usage = None
        # ``user_attachment_urls`` accepted for ``ImageProviderPort``
        # parity. This text-only endpoint doesn't ingest
        # reference images here; if support is added later, encode them
        # into the request and the call sites need no change.
        del user_attachment_urls
        positive_clean = positive.strip()
        if not positive_clean:
            raise ImageGenerationError("缺少 positive prompt")

        size = ASPECT_TO_SIZE.get(aspect.lower(), ASPECT_TO_SIZE[_DEFAULT_ASPECT])
        n = max(1, min(int(batch or 1), _MAX_BATCH))
        prompt_text = self._compose_prompt(
            character=character,
            scene=positive_clean,
            recent_dialogue=recent_dialogue,
            use_runtime_state=use_runtime_state,
        )

        payload = {
            "model": self._model,
            "prompt": prompt_text,
            "size": size,
            "quality": self._quality,
            "n": n,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    self._endpoint, headers=headers, json=payload,
                )
        except httpx.TimeoutException as exc:
            raise ImageTimeoutError(
                f"OpenAI image API 逾時：{exc}",
            ) from exc
        except httpx.HTTPError as exc:
            raise ImageGenerationError(
                f"OpenAI image API 連線錯誤：{exc}",
            ) from exc

        if resp.status_code != 200:
            # Surface the upstream message verbatim — operator-facing
            # routes already log + render this, so a clean string beats
            # losing the upstream error code.
            log_http_error_response(_LOGGER, resp, operation="OpenAI image API")
            detail = self._safe_error_detail(resp)
            raise ImageGenerationError(
                f"OpenAI image API HTTP {resp.status_code}: {detail}",
            )

        try:
            body = resp.json()
        except ValueError as exc:
            raise ImageGenerationError(
                f"OpenAI image API 回傳格式錯誤：{exc}",
            ) from exc

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or not data:
            raise ImageNoOutputError("OpenAI image API 沒有回傳任何圖片")
        self.last_usage = ImageTokenUsage.from_mapping(body.get("usage"))

        images: list[bytes] = []
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                continue
            b64 = item.get("b64_json")
            if not isinstance(b64, str) or not b64:
                _LOGGER.warning(
                    "OpenAI image item %s missing b64_json (keys=%s)",
                    i, list(item.keys()) if isinstance(item, dict) else None,
                )
                continue
            try:
                images.append(base64.b64decode(b64))
            except (ValueError, base64.binascii.Error) as exc:
                _LOGGER.warning(
                    "OpenAI image item %s base64 decode failed: %s", i, exc,
                )
        if not images:
            raise ImageNoOutputError(
                "OpenAI image API 回傳資料中沒有可用的 b64_json",
            )
        return images

    @staticmethod
    def _compose_prompt(
        *,
        character: "Character",
        scene: str,
        recent_dialogue: str,
        use_runtime_state: bool,
    ) -> str:
        """Build a natural-language prompt for GPT Image.

        GPT Image reads prose, not tags. Layer identity → wardrobe →
        runtime mood → scene as labelled lines so the model can resolve
        conflicts (sleeping + holding wand → drop the wand) the same way
        a human reader would. ``recent_dialogue`` is offered as
        pronoun-resolution context only — explicitly framed so the model
        doesn't invent new visual elements from chat history.
        """
        parts: list[str] = []
        parts.append(f"Character: {character.name}")
        appearance = (character.appearance or "").strip()
        if appearance:
            parts.append(f"Character appearance: {appearance}")
        parts.extend(render_character_visual_identity_lines(character))
        parts.extend(render_character_visual_subject_lines(character))
        if use_runtime_state:
            emotion = (character.state.emotion or "").strip()
            if emotion:
                parts.append(f"Current mood: {emotion}")
            intent = (character.state.current_intent or "").strip()
            if intent:
                parts.append(f"Current focus: {intent}")
        parts.append(f"Scene: {scene}")
        dialogue = (recent_dialogue or "").strip()
        if dialogue:
            parts.append(
                "Recent chat dialogue (use ONLY to resolve pronouns / "
                "implicit references in Scene; do not invent new visual "
                "elements from it):\n" + dialogue,
            )
        parts.append(
            "Render the character in the scene as a single coherent "
            "illustration. Follow Visual subject type/body-plan rules "
            "before applying ordinary portrait conventions. Resolve "
            "conflicts in favour of the scene "
            "(e.g. drop held items if the activity makes no sense for "
            "them; close eyes for sleeping; adjust outfit if the scene "
            "demands it).",
        )
        return "\n\n".join(parts)

    @staticmethod
    def _safe_error_detail(resp: httpx.Response) -> str:
        try:
            payload = resp.json()
        except ValueError:
            return resp.text[:500]
        if isinstance(payload, dict):
            err = payload.get("error")
            if isinstance(err, dict):
                msg = err.get("message")
                if isinstance(msg, str) and msg:
                    return msg
        return resp.text[:500]
