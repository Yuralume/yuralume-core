"""``ChatModelPort`` decorator that normalises reply script.

Sits at the outermost edge of the resolved adapter chain so every
non-streaming generation — chat tool cycles, LumeGram posts, proactive
messages, character drafts, story prose, schedule plans — passes through
one place, instead of each of the sixty-odd call sites growing its own
guard. Same role as
:mod:`kokoro_link.infrastructure.llm.think_tag_filter`: a deterministic
cleanup of a known model failure mode, applied uniformly rather than
per-case.

**Bound per resolve, not per process.** The language gate needs the
player's content language, which the adapter itself never sees. The
active-LLM provider resolves it alongside model routing and binds it
here, exactly as it binds reasoning and vision overrides.

**Non-streaming only, deliberately.** Simplified→Traditional is not a
per-character mapping: 发 → 發 / 髮 and 干 → 乾 / 幹 / 干 are decided
from surrounding words, so converting stream fragments as they arrive
would pick the wrong glyph whenever a chunk boundary splits a phrase.
:meth:`generate_stream` is therefore delegated untouched; giving the
streaming path its own sentence-boundary buffer (as ``think_tag_filter``
does for tags) is a separate piece of work.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.infrastructure.localization.chinese_script import (
    normalize_to_traditional_reporting,
    targets_traditional_chinese,
)

_LOGGER = logging.getLogger(__name__)


class ScriptNormalizingChatModel:
    """Wrap ``inner`` so non-streaming replies come back Traditional.

    Construct via :func:`bind_script_normalization`, which skips the
    wrapper entirely when the player's language does not want it — an
    unwrapped adapter keeps duck-typed capability probing (``getattr``
    for ``with_reasoning_overrides``, ``generate_stream_capturing``, …)
    on the shortest possible path.

    Structurally a ``ChatModelPort`` but deliberately **not** declared
    as a subclass: inheriting the Protocol would install its ellipsis
    method bodies on this class, so ``generate_stream`` / ``list_models``
    would resolve to those stubs (returning ``None``) instead of falling
    through :meth:`__getattr__` to the real adapter.
    """

    def __init__(self, inner: ChatModelPort) -> None:
        self._inner = inner

    # ---- ChatModelPort surface ---------------------------------------

    @property
    def provider_id(self) -> str:
        return self._inner.provider_id

    @property
    def supports_vision(self) -> bool:
        return self._inner.supports_vision

    @property
    def prefers_public_image_urls(self) -> bool:
        return self._inner.prefers_public_image_urls

    async def generate(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> str:
        raw = await self._inner.generate(
            prompt, image_urls=image_urls, model=model,
        )
        normalized, corrected = normalize_to_traditional_reporting(raw)
        if corrected:
            # The only place script drift is observable — the reply the
            # player sees is already fixed by the time anything else can
            # look at it. Logged so "which model wanders off-script, how
            # often" is answerable without adding a metric pipeline.
            _LOGGER.info(
                "script drift corrected: provider=%s model=%s chars=%d",
                getattr(self._inner, "provider_id", "?"), model or "-",
                len(raw),
            )
        return normalized

    # ---- everything else stays the inner adapter's ---------------------

    def __getattr__(self, name: str) -> Any:
        """Delegate unknown attributes to the wrapped adapter.

        Callers probe adapters for optional capabilities by attribute
        (``generate_stream``, ``generate_stream_capturing``,
        ``with_reasoning_overrides``, ``list_models``). Swallowing those
        would silently disable streaming metadata capture and routing
        overrides for every player this wrapper is bound for, so the
        decorator stays transparent for everything it does not itself
        implement.

        Only reached for attributes this class does not define, so
        ``generate`` above always wins.
        """
        if name == "_inner":
            # Reached only before ``__init__`` has run (unpickling, or a
            # subclass calling out early). Delegating would recurse
            # forever, so fail the lookup the way Python normally would.
            raise AttributeError(name)
        return getattr(self._inner, name)


def bind_script_normalization(
    model: ChatModelPort,
    *,
    language_tag: str | None,
) -> ChatModelPort:
    """Wrap ``model`` when ``language_tag`` wants Traditional Chinese.

    Returns ``model`` untouched otherwise — including for every
    non-Chinese language, where the conversion would actively corrupt
    output (Japanese shinjitai shares code points with Simplified
    forms). The gate lives in
    :func:`~kokoro_link.infrastructure.localization.chinese_script.targets_traditional_chinese`;
    see its docstring for which tags qualify.
    """
    if not targets_traditional_chinese(language_tag):
        return model
    return ScriptNormalizingChatModel(model)
