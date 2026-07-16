"""LLM-backed safe-summary generator for restricted messages."""

from __future__ import annotations

import logging

from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.nsfw_safe_summary import NsfwSafeSummaryPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import (
    Message,
    MessageContentMode,
    MessageRole,
)

_LOGGER = logging.getLogger(__name__)

_MAX_INPUT_CHARS = 1600
_MAX_SUMMARY_CHARS = 300


class LLMNsfwSafeSummarizer(NsfwSafeSummaryPort):
    async def summarize(
        self,
        *,
        character: Character,
        message: Message,
        model: ChatModelPort | None = None,
        model_id: str | None = None,
    ) -> str:
        text = (message.content or "").strip()
        if message.content_mode is not MessageContentMode.NSFW or not text:
            return ""
        if model is None:
            return ""
        prompt = _build_prompt(character=character, message=message)
        try:
            raw = await model.generate(prompt, model=model_id)
        except Exception:
            _LOGGER.exception("NSFW safe-summary LLM call failed")
            return ""
        return _clean_summary(raw)


def _build_prompt(*, character: Character, message: Message) -> str:
    role_label = "使用者" if message.role is MessageRole.USER else character.name
    text = message.content.strip().replace("\r", " ").replace("\n", " ")
    if len(text) > _MAX_INPUT_CHARS:
        text = text[:_MAX_INPUT_CHARS]
    return "\n".join([
        "請將以下對話訊息改寫成 frontier 模型可安全接收的一句繁體中文摘要。",
        "目標：保留情緒、關係進展、承諾與開放話題；移除露骨或色情細節。",
        "限制：不要加入新事實；不要描述具體性行為、身體部位或挑逗細節。",
        "如果無法安全摘要，請只輸出空字串。",
        "",
        f"角色名稱：{character.name}",
        f"說話者：{role_label}",
        f"原文：{text}",
        "",
        "安全摘要：",
    ])


def _clean_summary(raw: str | None) -> str:
    summary = (raw or "").strip().strip('"').strip("'").strip()
    if not summary:
        return ""
    return " ".join(summary.split())[:_MAX_SUMMARY_CHARS]
