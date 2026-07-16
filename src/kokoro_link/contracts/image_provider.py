"""Image-generation provider port.

Turns ``(character, scene description)`` into one-or-more rendered
image bytes. Adapters wrap whichever backend the deployment chose —
local ComfyUI for tag-driven SDXL workflows, OpenAI GPT Image
for hosted natural-language generation, etc.

Why a port at all: ``CharacterImageService.generate_portrait``,
``ComfyImageTool.invoke`` and ``FeedComposerService._materialise``
all want the same answer ("here's a character + scene, give me PNG
bytes") and shouldn't care which renderer is wired. The port lets the
container pick a provider per deployment without those call sites
knowing about ComfyUI / OpenAI / future backends.

Implementations must:
  * Raise :class:`ImageGenerationError` (or a subclass) on failure.
    Callers pattern-match these to render an apology in chat or
    return an HTTP error from REST handlers.
  * Return at least one byte string on success (``batch=1`` minimum).
  * Apply any backend-specific prompt rewriting *internally* — the
    caller hands in ``positive`` as the operator typed it, plus
    structured runtime hints and character identity facts (including
    visual gender presentation), and the provider decides whether it
    needs danbooru tags, natural-language prose, etc.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from kokoro_link.domain.entities.character import Character


class ImageGenerationError(Exception):
    """Provider-level failure — bad config, upstream error, bad input."""


class ImageTimeoutError(ImageGenerationError):
    """Upstream took longer than the configured budget."""


class ImageNoOutputError(ImageGenerationError):
    """Upstream returned success but produced zero images."""


def _int_value(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True, slots=True)
class ImageTokenUsage:
    """Provider-reported image token usage exposed as a side channel."""

    input_tokens: int = 0
    input_text_tokens: int = 0
    input_image_tokens: int = 0
    output_tokens: int = 0
    output_image_tokens: int = 0
    output_text_tokens: int = 0
    total_tokens: int = 0
    estimated: bool = False

    @classmethod
    def from_mapping(cls, raw: Any) -> "ImageTokenUsage | None":
        if not isinstance(raw, dict):
            return None
        unit = str(raw.get("unit", "") or "").strip().lower()
        has_token_shape = any(
            key in raw for key in ("input_tokens", "output_tokens", "total_tokens")
        )
        if unit and unit != "token" and not has_token_shape:
            return None
        if not has_token_shape:
            return None

        input_details = raw.get("input_tokens_details")
        if not isinstance(input_details, dict):
            input_details = {}
        output_details = raw.get("output_tokens_details")
        if not isinstance(output_details, dict):
            output_details = {}

        input_tokens = _int_value(raw.get("input_tokens", raw.get("input")))
        output_tokens = _int_value(raw.get("output_tokens", raw.get("output")))
        input_text_tokens = _int_value(
            raw.get("input_text_tokens", input_details.get("text_tokens")),
        )
        input_image_tokens = _int_value(
            raw.get("input_image_tokens", input_details.get("image_tokens")),
        )
        output_text_tokens = _int_value(
            raw.get("output_text_tokens", output_details.get("text_tokens")),
        )
        output_image_tokens = _int_value(
            raw.get("output_image_tokens", output_details.get("image_tokens")),
        )
        if output_image_tokens == 0 and output_text_tokens == 0:
            output_image_tokens = output_tokens
        total_tokens = _int_value(raw.get("total_tokens", raw.get("total")))
        if total_tokens == 0:
            total_tokens = input_tokens + output_tokens
        if total_tokens == 0:
            return None

        return cls(
            input_tokens=input_tokens,
            input_text_tokens=input_text_tokens,
            input_image_tokens=input_image_tokens,
            output_tokens=output_tokens,
            output_image_tokens=output_image_tokens,
            output_text_tokens=output_text_tokens,
            total_tokens=total_tokens,
            estimated=bool(raw.get("estimated", False)),
        )

    def to_metadata(self) -> dict[str, int]:
        return {
            "input_text_tokens": self.input_text_tokens,
            "input_image_tokens": self.input_image_tokens,
            "output_image_tokens": self.output_image_tokens,
            "output_text_tokens": self.output_text_tokens,
        }


class ImageProviderPort(Protocol):
    async def generate(
        self,
        *,
        character: "Character",
        positive: str,
        aspect: str = "portrait",
        batch: int = 1,
        recent_dialogue: str = "",
        use_runtime_state: bool = True,
        user_attachment_urls: Sequence[str] = (),
    ) -> list[bytes]:
        """Render ``batch`` images and return their raw bytes.

        ``aspect`` is one of ``"portrait" | "landscape" | "square"``;
        unknown values fall back to ``"portrait"``. ``recent_dialogue``
        and ``use_runtime_state`` are hints the provider may use to
        resolve pronouns / inject mood — providers that don't need
        them simply ignore them.

        ``user_attachment_urls`` are LLM-fetchable URLs (data: or
        absolute http(s)://) the user attached to *this* turn. A
        vision-capable provider / prompt rewriter looks at them to
        extract outfit / scene cues for requests like "幫我換上這件
        衣服". Providers without a vision step ignore them.
        """
        ...
