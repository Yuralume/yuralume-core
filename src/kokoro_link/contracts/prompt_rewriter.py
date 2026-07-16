"""Prompt rewriter port.

Converts a free-form description (any language, any style) into a
ComfyUI-friendly positive prompt — typically danbooru-style English
tags for Illustrious / SDXL-family models. Inserted between the
operator's typed input and ``ComfyPortraitGenerator`` so a Chinese
phrase like ``咖啡店裡看書``（which SDXL tokenisers don't understand）
becomes something like ``cafe, indoors, reading book, soft light``.

Implementations should focus on *scene / pose / mood* — identity
(hair, outfit, etc.) is separately handled by the generator from
``character.appearance``, so the rewriter shouldn't duplicate it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from kokoro_link.domain.entities.character import Character


class PromptRewriteError(Exception):
    """Raised when the rewriter can't produce a usable result.

    Callers typically catch this and fall back to the raw input so
    generation still proceeds rather than failing the whole turn."""


class PromptRewriterPort(Protocol):
    async def rewrite(
        self,
        text: str,
        *,
        character: "Character | None" = None,
        image_urls: Sequence[str] = (),
    ) -> str:
        """Return a rewritten positive-prompt string.

        ``character`` (optional) lets the LLM-backed implementation
        honour the per-character LLM override chain so the rewriter
        follows whatever model the operator pinned for that character.
        Implementations without an LLM step (no-op, fixed table) can
        ignore it.

        ``image_urls`` (optional) carries reference images the user
        attached this turn — already resolved to LLM-fetchable form
        (data: or absolute http(s)://). Vision-capable rewriters
        forward them to the underlying model so outfit / scene cues
        from the picture land in the rewritten tags. Non-vision
        implementations may ignore them.

        Must not raise on empty input — return the empty string back.
        Network / LLM errors should surface as ``PromptRewriteError``
        so the caller can decide whether to fall back.
        """
