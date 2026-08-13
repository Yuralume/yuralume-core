"""Video-generation provider port.

Mirror of :class:`ImageProviderPort` for short-form video. Lives as a
separate port because the shape on either side diverges enough that a
unified interface would buy nothing:

  * **Return shape** — image generation returns ``list[bytes]`` (a
    batch of N candidate PNGs the caller picks from). Video generation
    returns a single bytes blob (mp4) — Wan2.2 produces one clip per
    run, batch is one in practice, and "pick a candidate" UX doesn't
    apply to multi-second clips.

  * **Latency budget** — video generation is 5–20× slower than image
    generation, so the surrounding code (timeouts, daily caps,
    progress UI) wants distinct knobs.

  * **Aspect / sizing semantics** — video adds a *length* (frames)
    dimension that image-side aspect doesn't. Smuggling it through the
    image port via an unused kwarg would be silently lossy.

Failure model mirrors the image side exactly:

  * Network / timeout              → :class:`VideoTimeoutError`
  * Non-2xx HTTP / malformed body  → :class:`VideoGenerationError`
  * Job completed but no output    → :class:`VideoNoOutputError`

Callers (currently only :class:`FeedComposerService`) pattern-match
these so the post degrades to image-only or text-only on any failure
instead of a 500ing the whole tick.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from kokoro_link.domain.entities.character import Character


class VideoGenerationError(Exception):
    """Provider-level failure — bad config, upstream error, bad input."""


class VideoTimeoutError(VideoGenerationError):
    """Upstream took longer than the configured budget."""


class VideoNoOutputError(VideoGenerationError):
    """Job completed but produced no usable file."""


class VideoProviderPort(Protocol):
    async def generate(
        self,
        *,
        character: "Character",
        positive: str,
        aspect: str = "portrait",
        length_frames: int = 81,
        recent_dialogue: str = "",
        use_runtime_state: bool = True,
        first_frame_url: str | None = None,
    ) -> bytes:
        """Render a single short clip and return its raw bytes (mp4).

        ``aspect`` is one of ``"portrait" | "landscape" | "square"``;
        unknown values fall back to ``"portrait"``. ``length_frames``
        is the latent frame count Wan2.2 produces (16 fps default →
        81 frames ≈ 5 seconds); concrete adapters clamp this to a
        reasonable range. Providers should combine ``positive`` with
        the character's appearance / visual gender presentation.
        ``recent_dialogue`` and ``use_runtime_state`` are hints
        adapters may ignore.

        ``first_frame_url`` is an image-to-video reference: a fetchable
        URL (``data:`` or absolute ``http(s)://``) for the frame the clip
        should start from, following the "pass a URL, not bytes" shape
        :attr:`ImageProviderPort.generate.user_attachment_urls` already
        established. Backends without an i2v path **ignore it** — none of
        the four adapters shipped today consume it, and adding it must
        not change a single byte of their behaviour.

        Note that the *hosted asynchronous* path does not travel through
        this parameter: it needs the frame as bytes so the broker can
        stage it for a worker that cannot reach Core's object store at
        all. See :mod:`kokoro_link.contracts.async_video_job` and
        the Media Jobs Service spec §2 for that decision.
        """
        ...
