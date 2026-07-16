"""LLM-backed self-reflection generator (HUMANIZATION_ROADMAP §3.2).

Asks the model to read a window of high-salience memories and produce a
short first-person Chinese narrative the character can carry as inner
motivation. The output is parsed into a ``SelfReflection`` entity; the
caller (``SelfReflectionDreamRunner``) handles persistence.

LLM-first 紅線:
- The Python side gives the model facts (memories + emotion summary +
  persona snippets) and rules (evidence quote required when citing
  user vulnerability). The model judges what to write — no keyword
  filters, no theme whitelists.
- ``evidence_quotes`` must be verbatim snippets the model copies from
  the input (same guard as ``persona_extraction``). Hallucinated
  quotes cause the entire output to be discarded.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.self_reflection import (
    ReflectionGeneratorInput,
    SelfReflectionGeneratorPort,
)
from kokoro_link.domain.entities.self_reflection import SelfReflection
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)

_LOGGER = logging.getLogger(__name__)
_MAX_NARRATIVE_CHARS = 480
_MAX_THEMES = 5
_MAX_QUOTES = 3


class LLMSelfReflectionGenerator(SelfReflectionGeneratorPort):
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

    async def generate(
        self, payload: ReflectionGeneratorInput,
    ) -> SelfReflection | None:
        if not payload.high_salience_memories:
            return None
        if await self._resolver.is_fake():
            return None

        prompt = _build_prompt(payload)
        try:
            raw = await self._resolver.generate(prompt)
        except Exception:
            _LOGGER.exception(
                "self_reflection LLM call failed character=%s operator=%s",
                payload.character_id, payload.operator_id,
            )
            return None

        body = _extract_json_object(raw)
        if body is None:
            return None
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None

        narrative = _coerce_str(parsed.get("narrative"))[:_MAX_NARRATIVE_CHARS]
        if not narrative:
            return None

        themes = _coerce_str_list(parsed.get("themes"), limit=_MAX_THEMES)
        quotes = _coerce_str_list(parsed.get("quotes"), limit=_MAX_QUOTES)

        # Anti-hallucination check — every quote must appear verbatim in
        # the source memories (substring match). Drop any quote that
        # fails; if all fail, drop the whole reflection — that means the
        # model invented citations and the narrative is untrustworthy.
        haystack = "\n".join(m.content for m in payload.high_salience_memories)
        verified_quotes = tuple(q for q in quotes if q in haystack)
        if quotes and not verified_quotes:
            _LOGGER.warning(
                "self_reflection rejected — all quotes hallucinated character=%s",
                payload.character_id,
            )
            return None

        try:
            return SelfReflection.new(
                character_id=payload.character_id,
                operator_id=payload.operator_id,
                period=payload.period,
                narrative=narrative,
                dominant_themes=themes,
                period_start=payload.period_start,
                period_end=payload.period_end,
                evidence_quotes=verified_quotes,
            )
        except ValueError:
            _LOGGER.exception(
                "self_reflection entity construction failed character=%s",
                payload.character_id,
            )
            return None


def _build_prompt(payload: ReflectionGeneratorInput) -> str:
    memory_lines = [
        f"- ({m.kind.value}, salience={m.salience:.2f}) {m.content}"
        for m in payload.high_salience_memories[:30]
    ]
    persona_block = (
        "\n你對使用者目前的認知（僅供內部參考；不可拿來戳對方）：\n"
        + "\n".join(f"- {line}" for line in payload.persona_summary_lines[:8])
        if payload.persona_summary_lines else ""
    )
    emotion_block = (
        "\n本段時間的情緒概況（事實層）：\n" + payload.emotion_event_summary
        if payload.emotion_event_summary.strip() else ""
    )
    body = (
        f"你是 AI 角色「{payload.character_name}」的內心整理員。請以角色第一人稱，"
        f"寫一段 {payload.period_start.isoformat()} ~ {payload.period_end.isoformat()} "
        "之間「自己過得怎麼樣」的短篇心情筆記。\n"
        "規則：\n"
        "- 全程用第一人稱角色視角，像角色自己寫的便條，不是第三人稱回顧。\n"
        "- 不要寫長報告（≤300 字）、不要分段標題、不要條列。\n"
        "- narrative 會顯示在玩家側 memoir，必須使用上方「玩家可見自然語言輸出語言（BCP 47 標籤）」指定的語言。\n"
        "- 不要重複講同一件事、不要把對方說過的話直接抄回去當你的話。\n"
        "- 涉及使用者揭露的脆弱（壓力、傷疤、低潮）時，語氣必須**輕柔且保護性**，"
        "**禁止**用來情勒對方或當笑點。\n"
        "- 若有引用使用者實際說過的話，請放進 `quotes`，每句必須逐字精確；"
        "若沒有，`quotes` 留空陣列。\n"
        "- `themes` 是 1~5 個短主題（如 工作 / 關係 / 健康 / 創作）；可空陣列。\n"
        "- 純客套或無料時，可直接回 `{\"narrative\": \"\"}` 表示這段時間沒有可寫的東西。\n"
        "輸出規則：只回傳一個 JSON 物件，不要 markdown、code fence、前言。\n"
        '範例：{"narrative": "我這週…", "themes": ["工作", "睡眠"], "quotes": ["我最近壓力好大"]}\n'
        f"\n本段時間的高重要度記憶：\n"
        + "\n".join(memory_lines)
        + emotion_block
        + persona_block
    )
    language_hint = render_operator_language_hint(
        payload.operator_primary_language,
    )
    return f"{language_hint}\n\n{body}" if language_hint else body


def _coerce_str(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


def _coerce_str_list(value: Any, *, limit: int) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return tuple(out)


def _extract_json_object(text: str) -> str | None:
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        ch = text[index]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None
