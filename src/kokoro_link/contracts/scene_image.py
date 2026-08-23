"""Scene-illustration port — "draw this described moment".

Sibling of :class:`~kokoro_link.contracts.image_provider.ImageProviderPort`
but deliberately narrower. The image provider port is a *character*
renderer: every implementation composes the character's identity, runtime
emotion and reference art into its own prompt, and every caller has one
concrete character it wants a picture of. A branching-drama node is a
*scene* — a described moment several characters may walk through — so the
caller already owns the whole prompt (summary + appearances + visual
style) and wants nothing added to it that it did not write.

Why this exists at all: the branching-drama service used to hold a
concrete ``ComfySceneGenerator``. That made scene art a self-host-only
capability — a hosted deployment has no ComfyUI, so ``comfyui.enabled``
is false and every drama node silently landed without a picture. With a
port, the same service is fed either the local renderer or the hosted
Gateway one, and neither the service nor its fail-soft skip has to know
which.

``character`` is optional and is *not* the subject of the drawing: it is
the attribution/routing anchor a hosted adapter needs (whose tenant is
billed, which per-character image profile applies). Local renderers
ignore it entirely.

Failure semantics mirror the ComfyUI generator they replace, so a caller
catching :class:`SceneImageError` catches exactly what it caught before:
timeouts and empty results are subclasses, and adapters translate their
backend's own exception family into this one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from kokoro_link.domain.entities.character import Character


DEFAULT_SCENE_ASPECT = "landscape"


class SceneImageError(Exception):
    """Scene rendering failed. Base class — catch this to fail soft."""


class SceneImageTimeoutError(SceneImageError):
    """The backend took longer than its configured budget."""


class SceneImageNoOutputError(SceneImageError):
    """The backend reported success but produced no image."""


class SceneImagePort(Protocol):
    async def generate(
        self,
        *,
        positive: str,
        aspect: str = DEFAULT_SCENE_ASPECT,
        character: "Character | None" = None,
    ) -> bytes:
        """Render one scene image and return its raw bytes.

        ``positive`` is the complete prompt as the caller composed it —
        implementations must not rewrite it, only prepend whatever
        boilerplate their backend needs. ``aspect`` is one of
        ``"portrait" | "landscape" | "square"``; unknown values fall back
        to the backend's default.

        ``character`` is a routing/attribution hint, never a subject
        directive: hosted adapters use it to resolve the account the call
        is billed to and any per-character image-profile pin. Adapters
        that need it and did not get it raise :class:`SceneImageError`
        rather than guessing.

        Raises :class:`SceneImageError` (or a subclass) on failure —
        never a backend-specific exception.
        """
        ...
