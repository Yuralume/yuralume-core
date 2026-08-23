"""Hosted adapter for :class:`SceneImagePort`, over the active image provider.

This is how a deployment with no ComfyUI draws scenes: it borrows the
same :class:`ActiveImageProviderPort` every other image surface already
resolves through (chat tool, portrait, feed), so scene art inherits that
port's routing — hosted Gateway preset in cloud mode, per-character
profile pins in self-host — without the drama service learning any of it.

Two deliberate choices about what is *not* forwarded:

* ``use_runtime_state=False``. A drama node is fiction the player walks
  through, not a snapshot of the character's live mood; letting today's
  emotion bleed into a scene the tree scripted weeks of story ago would
  contradict the narration next to it. The local ComfyUI path never had
  runtime state either, so this keeps both backends drawing the same
  thing.
* No reference-art attachments. The caller's prompt already carries the
  appearance lines for every character appearing in the node, and a
  scene frames several of them at once — pinning likeness to one of them
  is the wrong anchor, and BD-series scope stops short of picking a
  policy for multi-subject reference art.

``character`` is required here even though the port makes it optional:
the hosted provider resolves the billed account and character-scoped
routing from it, and a scene drawn against the wrong account is worse
than a scene not drawn at all — so a missing anchor fails soft (the
service skips the image) rather than defaulting to somebody.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kokoro_link.contracts.active_image import ActiveImageProviderPort
from kokoro_link.contracts.image_provider import (
    ImageGenerationError,
    ImageNoOutputError,
    ImageTimeoutError,
)
from kokoro_link.contracts.scene_image import (
    DEFAULT_SCENE_ASPECT,
    SceneImageError,
    SceneImageNoOutputError,
    SceneImageTimeoutError,
)

if TYPE_CHECKING:
    from kokoro_link.domain.entities.character import Character

_LOGGER = logging.getLogger(__name__)


class ActiveProviderSceneImageAdapter:
    """Adapt :class:`ActiveImageProviderPort` to :class:`SceneImagePort`."""

    def __init__(
        self,
        *,
        image_provider: ActiveImageProviderPort,
        feature_key: str,
    ) -> None:
        # ``feature_key`` is injected rather than imported so this
        # infrastructure adapter stays free of application-layer feature
        # tables — the same shape ``CloudGatewayImageProvider`` uses.
        self._image_provider = image_provider
        self._feature_key = feature_key

    async def generate(
        self,
        *,
        positive: str,
        aspect: str = DEFAULT_SCENE_ASPECT,
        character: "Character | None" = None,
    ) -> bytes:
        if character is None:
            raise SceneImageError(
                "scene image: no character anchor supplied — the hosted "
                "image provider needs one to resolve billing and routing",
            )
        provider = await self._image_provider.resolve(
            self._feature_key, character=character,
        )
        if provider is None:
            raise SceneImageError(
                "scene image: no image provider resolved for feature "
                f"{self._feature_key!r}",
            )
        try:
            images = await provider.generate(
                character=character,
                positive=positive,
                aspect=aspect,
                batch=1,
                use_runtime_state=False,
            )
        except ImageTimeoutError as exc:
            raise SceneImageTimeoutError(str(exc)) from exc
        except ImageNoOutputError as exc:
            raise SceneImageNoOutputError(str(exc)) from exc
        except ImageGenerationError as exc:
            raise SceneImageError(str(exc)) from exc
        first = next((data for data in images if data), None)
        if first is None:
            raise SceneImageNoOutputError(
                "scene image: provider returned no image bytes",
            )
        if len(images) > 1:
            _LOGGER.debug(
                "scene image: provider returned %d images, keeping the first",
                len(images),
            )
        return first
