"""LLM-backed memory extractor.

Prompts a chat model to read a completed user/assistant turn and emit
structured memory items as JSON. Extraction is best-effort: parsing
failures, empty output, or model errors all degrade to an empty list
so chat never breaks because of a flaky extractor.
"""

from __future__ import annotations

import logging
from typing import Any

from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.memory_extractor import MemoryExtractorPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Message, MessageRole
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.memory_kind import CANONICAL_KINDS, MemoryKind
from kokoro_link.infrastructure.memory.json_parser import parse_memory_payload
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)

_MAX_CONTENT_CHARS = 400
_MAX_TAGS = 5
_MAX_TAG_CHARS = 40
_MAX_ITEMS = 6
_MAX_HISTORY_TURNS = 6
_MAX_HISTORY_CHARS_PER_TURN = 300

_ALLOWED_KINDS = {kind.value for kind in CANONICAL_KINDS}


class LLMMemoryExtractor(MemoryExtractorPort):
    def __init__(self, model: ChatModelPort) -> None:
        self._model = model

    async def extract(
        self,
        *,
        character: Character,
        conversation_id: str,
        user_message: str,
        assistant_message: str,
        recent_messages: list[Message] | None = None,
    ) -> list[MemoryItem]:
        prompt = _build_prompt(
            character=character,
            user_message=user_message,
            assistant_message=assistant_message,
            recent_messages=recent_messages or [],
        )
        try:
            raw = await self._model.generate(prompt)
        except Exception:  # pragma: no cover - defensive, provider-specific
            _LOGGER.exception("Memory extractor LLM call failed")
            return []

        payloads = parse_memory_payload(raw)
        if not payloads:
            return []

        items: list[MemoryItem] = []
        for payload in payloads[:_MAX_ITEMS]:
            item = _payload_to_item(
                payload=payload,
                character_id=character.id,
                conversation_id=conversation_id,
            )
            if item is not None:
                items.append(item)
        return items


def _build_prompt(
    *,
    character: Character,
    user_message: str,
    assistant_message: str,
    recent_messages: list[Message],
) -> str:
    return get_default_loader().render(
        "memory/extractor",
        character_name=character.name,
        character_summary=character.summary,
        history_section="\n".join(_render_history(
            recent_messages, character_name=character.name,
        )),
        user_message=user_message,
        assistant_message=assistant_message,
        kinds_hint=", ".join(sorted(_ALLOWED_KINDS)),
    )




def _render_history(
    messages: list[Message], *, character_name: str,
) -> list[str]:
    if not messages:
        return ["", "近期對話脈絡：（無）"]
    tail = messages[-_MAX_HISTORY_TURNS:]
    lines = ["", "近期對話脈絡（較早 → 較新，不含本輪）："]
    for msg in tail:
        content = (msg.content or "").strip()
        if not content:
            continue
        if len(content) > _MAX_HISTORY_CHARS_PER_TURN:
            content = content[: _MAX_HISTORY_CHARS_PER_TURN] + "…"
        label = "使用者" if msg.role == MessageRole.USER else character_name
        lines.append(f"- {label}：{content}")
    if len(lines) == 2:
        lines.append("- （無有效內容）")
    return lines


def _payload_to_item(
    *,
    payload: dict[str, Any],
    character_id: str,
    conversation_id: str,
) -> MemoryItem | None:
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    trimmed = content.strip()[:_MAX_CONTENT_CHARS]

    kind_raw = payload.get("kind")
    kind = _coerce_kind(kind_raw)

    salience = _coerce_salience(payload.get("salience"))
    tags = _coerce_tags(payload.get("tags"))

    try:
        return MemoryItem.create(
            character_id=character_id,
            conversation_id=conversation_id,
            kind=kind,
            content=trimmed,
            salience=salience,
            tags=tags,
        )
    except ValueError:
        return None


def _coerce_kind(raw: Any) -> MemoryKind:
    if isinstance(raw, str):
        candidate = raw.strip().lower()
        if candidate in _ALLOWED_KINDS:
            return MemoryKind.from_string(candidate)
    return MemoryKind.EPISODIC


def _coerce_salience(raw: Any) -> float:
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except ValueError:
            return 0.5
    return 0.5


def _coerce_tags(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    cleaned: list[str] = []
    for tag in raw:
        if not isinstance(tag, (str, int, float)):
            continue
        text = str(tag).strip().lower()[:_MAX_TAG_CHARS]
        if text:
            cleaned.append(text)
        if len(cleaned) >= _MAX_TAGS:
            break
    return tuple(cleaned)
