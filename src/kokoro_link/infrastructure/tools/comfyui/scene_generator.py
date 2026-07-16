"""Character-free ComfyUI generator for scenes / events.

Sibling of :class:`ComfyPortraitGenerator`. The portrait generator is
specialised for "render a character at this scene": it threads the
character's appearance + LoRAs + runtime emotion into the prompt and
uses a portrait aspect by default. Scenes / events have neither a
character nor LoRAs — just a description ("morning light through the
classroom window, empty desks, silence") + an aspect. Forcing the
portrait generator to do scenes meant building a fake :class:`Character`
just to satisfy its signature, which obscures intent.

This generator stays narrow:

- Takes a positive prompt + aspect, period.
- No prompt rewriting (callers compose tag-style prompts directly).
- Same client + workflow + checkpoint plumbing so a single ComfyUI
  deployment serves both portraits and scenes.

Failure semantics mirror the portrait generator: subclasses of
``SceneGenerationError`` so callers can surface "ComfyUI down" without
re-parsing exceptions.
"""

from __future__ import annotations

import logging

from kokoro_link.infrastructure.tools.comfyui.client import (
    AsyncComfyUiClient,
    ComfyUiError,
    ComfyUiTimeout,
)
from kokoro_link.infrastructure.tools.comfyui.workflow import (
    DEFAULT_NEGATIVE_PROMPT,
    PromptSpec,
    WorkflowBuilder,
)

_LOGGER = logging.getLogger(__name__)

# Scenes default to landscape — most place / event illustrations want a
# wide horizontal frame ("a busy market", "the classroom at dusk").
# Callers can override per call.
ASPECT_TO_WH: dict[str, tuple[int, int]] = {
    "portrait": (832, 1216),
    "landscape": (1216, 832),
    "square": (1024, 1024),
}
DEFAULT_ASPECT = "landscape"

QUALITY_BOILERPLATE = (
    "masterpiece, best quality, amazing quality, very aesthetic, absurdres"
)


class SceneGenerationError(Exception):
    """Base class — pattern-match to map to HTTP / ToolResult."""


class SceneTimeoutError(SceneGenerationError):
    pass


class SceneNoOutputError(SceneGenerationError):
    pass


class ComfySceneGenerator:
    def __init__(
        self,
        *,
        client: AsyncComfyUiClient,
        workflow_builder: WorkflowBuilder,
        checkpoint: str | None = None,
    ) -> None:
        self._client = client
        self._workflow_builder = workflow_builder
        self._checkpoint = checkpoint

    async def generate(
        self,
        *,
        positive: str,
        aspect: str = DEFAULT_ASPECT,
    ) -> bytes:
        """Render a single scene image.

        Returns the raw bytes of the first produced image. The caller
        decides where it lives on disk — service layer writes to the
        right uploads subdir and persists the relative URL.
        """
        positive_clean = positive.strip()
        if not positive_clean:
            raise SceneGenerationError("缺少 positive prompt")

        width, height = ASPECT_TO_WH.get(
            aspect.lower(), ASPECT_TO_WH[DEFAULT_ASPECT],
        )
        full_positive = f"{QUALITY_BOILERPLATE}, {positive_clean}"
        spec = PromptSpec(
            positive=full_positive,
            negative=DEFAULT_NEGATIVE_PROMPT,
            width=width,
            height=height,
            batch_count=1,
            checkpoint=self._checkpoint
            or PromptSpec.__dataclass_fields__["checkpoint"].default,
            loras=(),
        )
        prompt = self._workflow_builder.build(spec)

        try:
            prompt_id = await self._client.queue_prompt(prompt)
            history = await self._client.wait_for_completion(prompt_id)
        except ComfyUiTimeout as exc:
            raise SceneTimeoutError(f"ComfyUI 逾時：{exc}") from exc
        except ComfyUiError as exc:
            raise SceneGenerationError(f"ComfyUI 錯誤：{exc}") from exc

        for node_output in history.get("outputs", {}).values():
            for image_meta in node_output.get("images", []) or []:
                try:
                    return await self._client.download_image(
                        filename=image_meta["filename"],
                        subfolder=image_meta.get("subfolder", ""),
                        folder_type=image_meta.get("type", "output"),
                    )
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.exception("ComfyUI scene download failed")
                    raise SceneGenerationError(
                        f"下載圖片失敗：{exc}",
                    ) from exc
        raise SceneNoOutputError("ComfyUI 沒有回傳任何圖片")
