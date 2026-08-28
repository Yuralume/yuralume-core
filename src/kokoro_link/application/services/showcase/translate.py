"""Translation for the showcase snapshot.

Two very different cost profiles share one implementation:

* **Posts** — translated once and cached by the control plane, keyed by
  the *final* text (so an owner-approved rewrite re-translates and an
  untouched post never does). Hundreds of tokens each, growing without
  bound as the account ages; caching is the whole ballgame, and it lives
  where the approvals live.
* **Card / now / schedule strip** — a handful of short strings that
  change every run by definition. Re-translated on each publish,
  deduplicated within the run so three "工作" blocks cost one call.

The persona is carried into the prompt deliberately: a first-person post
translated without voice reads like a press release, and the wall's claim
is that someone is living over there.

Failure is never papered over. A string that cannot be translated comes
back ``None``; the publisher drops the post rather than shipping a locale
where the text silently stayed Chinese.

Prompt lives here as a constant for the same reason as the reviewer's —
see :mod:`kokoro_link.application.services.showcase.review`.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.application.services.showcase.json_output import coerce_json_object
from kokoro_link.application.services.showcase.snapshot import SOURCE_LOCALE
from kokoro_link.domain.entities.character import Character

_LOGGER = logging.getLogger(__name__)

LOCALE_NAMES: Mapping[str, str] = {
    "zh": "繁體中文（台灣）",
    "en": "English",
    "ja": "日本語",
}

TRANSLATE_SYSTEM_PROMPT = """\
你是一個 AI 陪伴產品公開展示牆的翻譯者。

原文是一個 AI 角色用第一人稱寫的生活動態或行程短句，會被放在公開頁面
上。翻譯目標是讓外語讀者感受到「有一個人正在那邊生活」。

紀律：
- 保留角色的說話聲音、語氣、情緒強度與人稱視角，不要翻成新聞稿或
  說明文。
- 不要增譯、不要補充原文沒有的資訊、不要解釋文化背景。
- 不要改變長度層級（原文一句就翻一句，原文一段就翻一段）。
- 專有名詞若原文刻意模糊，翻譯也維持模糊。

輸出**只有一個 JSON 物件**，不要有其他文字：
{"translation": "翻譯後的全文"}
"""


class ShowcaseTranslator:
    """Translates one string into one locale per call.

    One call per (text, locale) rather than a batch: a batch that comes
    back the wrong length pairs a post with someone else's translation,
    and the volume here (a few dozen strings) does not justify carrying
    that failure mode.
    """

    def __init__(
        self,
        resolver: ModelResolver,
        *,
        character: Character | None = None,
        persona: str = "",
        character_name: str = "",
    ) -> None:
        self._resolver = resolver
        self._character = character
        self._persona = persona
        self._character_name = character_name
        self._memo: dict[tuple[str, str], str] = {}

    async def translate(self, text: str, locale: str) -> str | None:
        """The translation, or ``None`` when the call failed.

        The in-run memo means the same string is never paid for twice —
        the schedule strip repeats itself constantly.
        """
        source = (text or "").strip()
        if not source:
            return None
        if locale == SOURCE_LOCALE:
            return source
        key = (locale, source)
        cached = self._memo.get(key)
        if cached is not None:
            return cached
        operator_id = self._character.user_id if self._character is not None else None
        try:
            if await self._resolver.is_fake(
                character=self._character, operator_id=operator_id,
            ):
                return None
            raw = await self._resolver.generate(
                self._build_prompt(source, locale),
                character=self._character,
                operator_id=operator_id,
            )
        except Exception as exc:  # noqa: BLE001 — publisher decides what to skip
            _LOGGER.warning("showcase translation to %s failed: %s", locale, exc)
            return None
        payload = coerce_json_object(raw, site="showcase.translate")
        value = payload.get("translation") if payload is not None else None
        if not isinstance(value, str) or not value.strip():
            _LOGGER.warning(
                "showcase translation to %s returned no 'translation' field", locale,
            )
            return None
        result = value.strip()
        self._memo[key] = result
        return result

    def _build_prompt(self, text: str, locale: str) -> str:
        lines = [TRANSLATE_SYSTEM_PROMPT, ""]
        if self._character_name:
            lines.append(f"角色名稱：{self._character_name}")
        if self._persona:
            lines.append(f"角色設定（用於保留聲音，不要翻譯這段）：{self._persona}")
        lines.append(f"目標語言：{LOCALE_NAMES.get(locale, locale)}")
        lines.append("原文：")
        lines.append(text)
        lines.append("")
        lines.append("輸出 JSON：")
        return "\n".join(lines)


__all__ = [
    "LOCALE_NAMES",
    "TRANSLATE_SYSTEM_PROMPT",
    "ShowcaseTranslator",
]
