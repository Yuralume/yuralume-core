"""LLM-backed translator + Null fallback for the TTS pre-step.

Uses :class:`ModelResolver` so per-feature LLM routing applies — the
operator can pin translation to a small/fast model (e.g. an
LM Studio 7B) while keeping the main chat path on a bigger model.
"""

from __future__ import annotations

import logging
import re

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.tts_translator import TTSTranslatorPort
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)

_LANG_NAMES = {
    "zh": "繁體中文",
    "ja": "日文",
    "en": "英文",
    "ko": "韓文",
}


class LLMTTSTranslator(TTSTranslatorPort):
    def __init__(
        self,
        model: ChatModelPort | None = None,
        *,
        provider: ActiveLLMProviderPort | None = None,
        feature_key: str | None = None,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider, model=model, feature_key=feature_key,
        )

    async def translate(
        self, *, text: str, source_lang: str, target_lang: str,
    ) -> str:
        text = (text or "").strip()
        if not text or source_lang == target_lang:
            return text
        if await self._resolver.is_fake():
            return ""
        prompt = _build_prompt(text, source_lang, target_lang)
        try:
            raw = await self._resolver.generate(prompt)
        except Exception:
            _LOGGER.exception(
                "tts translator: LLM call failed src=%s dst=%s",
                source_lang, target_lang,
            )
            return ""
        return _clean_output(raw)


class NullTTSTranslator(TTSTranslatorPort):
    """No-op translator. Used when the deployment runs on the fake
    provider — TTSService treats the empty return as "skip translation,
    synth source text as-is" so tests don't need an LLM."""

    async def translate(
        self, *, text: str, source_lang: str, target_lang: str,
    ) -> str:
        return ""


# ----------------------------------------------------------------------
# Prompt + parsing
# ----------------------------------------------------------------------


def _build_prompt(text: str, source_lang: str, target_lang: str) -> str:
    src_name = _LANG_NAMES.get(source_lang, source_lang)
    dst_name = _LANG_NAMES.get(target_lang, target_lang)
    return get_default_loader().render(
        "tts/translator",
        src_name=src_name,
        dst_name=dst_name,
        text=text,
    )


_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_LEADING_LABEL_RE = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9 _·\-]{0,16}[:：]\s*")


def _clean_output(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    text = _FENCE_RE.sub("", text).strip()
    # Some models add a label like "翻譯：" or "Output:" before the
    # actual content even when told not to. Strip the first occurrence.
    text = _LEADING_LABEL_RE.sub("", text)
    # Single line only — fold internal newlines so a model-emitted
    # paragraph doesn't bleed weird pauses into the synth.
    text = " ".join(text.split())
    # Strip surrounding quotes if any.
    for pair in (("「", "」"), ("『", "』"), ('"', '"'), ("'", "'")):
        if text.startswith(pair[0]) and text.endswith(pair[1]):
            text = text[1:-1].strip()
            break
    return text
