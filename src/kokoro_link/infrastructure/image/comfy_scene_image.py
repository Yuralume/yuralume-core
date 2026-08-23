"""Local-ComfyUI adapter for :class:`SceneImagePort`.

Pure translation layer: the wrapped :class:`ComfySceneGenerator` already
does exactly what the port describes (positive prompt + aspect → bytes,
no character involved), so all this adds is the exception mapping that
lets callers catch one family regardless of which backend is wired.

Behaviour is byte-identical to calling the generator directly — the same
prompt reaches the same workflow, and the same three failure modes come
back, only renamed. A self-host deployment that upgrades into this
adapter cannot tell the difference.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kokoro_link.contracts.scene_image import (
    DEFAULT_SCENE_ASPECT,
    SceneImageError,
    SceneImageNoOutputError,
    SceneImageTimeoutError,
)
from kokoro_link.infrastructure.tools.comfyui.scene_generator import (
    ComfySceneGenerator,
    SceneGenerationError,
    SceneNoOutputError,
    SceneTimeoutError,
)

if TYPE_CHECKING:
    from kokoro_link.domain.entities.character import Character


class ComfySceneImageAdapter:
    """Adapt :class:`ComfySceneGenerator` to :class:`SceneImagePort`."""

    def __init__(self, generator: ComfySceneGenerator) -> None:
        self._generator = generator

    async def generate(
        self,
        *,
        positive: str,
        aspect: str = DEFAULT_SCENE_ASPECT,
        character: "Character | None" = None,
    ) -> bytes:
        # ComfyUI draws what the prompt says and nothing else; the
        # attribution anchor is meaningless to a local GPU.
        _ = character
        try:
            return await self._generator.generate(
                positive=positive, aspect=aspect,
            )
        except SceneTimeoutError as exc:
            raise SceneImageTimeoutError(str(exc)) from exc
        except SceneNoOutputError as exc:
            raise SceneImageNoOutputError(str(exc)) from exc
        except SceneGenerationError as exc:
            raise SceneImageError(str(exc)) from exc
