"""LLM-backed disposition-drift judge (HUMANIZATION_ROADMAP §3.1).

Run during the dream pass once per cooldown window. The judge reads:

- the character's current ``CharacterDisposition`` qualitative bands
- a 30-day emotion-event summary
- high-salience memories from the same window
- thresholded persona snippets for relationship context

and decides whether one dimension should nudge. The ``DispositionDriftService``
applies the proposal — this adapter only judges. LLM-first 紅線: the
output names a dimension + direction + evidence quote; the service maps
direction → ±1 band shift. We never write keyword-trigger rules here.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.disposition_drift import (
    DispositionDriftInput,
    DispositionDriftJudgePort,
    DispositionDriftProposal,
)
from kokoro_link.contracts.llm import ChatModelPort

_LOGGER = logging.getLogger(__name__)
_MAX_REASON_CHARS = 240
_MAX_QUOTE_CHARS = 200
_VALID_DIMENSIONS = (
    "self_centeredness",
    "candor",
    "sharing_drive",
    "associativeness",
)
_VALID_DIRECTIONS = ("up", "down", "none")


class LLMDispositionDriftJudge(DispositionDriftJudgePort):
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

    async def judge(
        self, payload: DispositionDriftInput,
    ) -> DispositionDriftProposal | None:
        if not payload.high_salience_memories:
            return None
        if await self._resolver.is_fake():
            return None

        prompt = _build_prompt(payload)
        try:
            raw = await self._resolver.generate(prompt)
        except Exception:
            _LOGGER.exception(
                "disposition_drift LLM call failed character=%s",
                payload.character_id,
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

        dimension = _coerce_str(parsed.get("dimension"))
        direction = _coerce_str(parsed.get("direction")).lower()
        reason = _coerce_str(parsed.get("reason"))[:_MAX_REASON_CHARS]
        evidence = _coerce_str(parsed.get("evidence_quote"))[:_MAX_QUOTE_CHARS]

        if dimension not in _VALID_DIMENSIONS:
            return None
        if direction not in _VALID_DIRECTIONS:
            return None
        if direction == "none":
            return None
        if not reason:
            return None

        # Anti-hallucination guard — when the judge wrote an evidence
        # quote, it must appear verbatim somewhere in the input window.
        if evidence:
            haystack = "\n".join(
                m.content for m in payload.high_salience_memories
            ) + "\n" + payload.emotion_event_summary
            if evidence not in haystack:
                _LOGGER.info(
                    "disposition_drift rejected (hallucinated quote) character=%s",
                    payload.character_id,
                )
                return None

        return DispositionDriftProposal(
            dimension=dimension,
            direction=direction,
            reason=reason,
            evidence_quote=evidence,
        )


def _build_prompt(payload: DispositionDriftInput) -> str:
    disposition = payload.disposition
    memory_lines = [
        f"- ({m.kind.value}, salience={m.salience:.2f}) {m.content}"
        for m in payload.high_salience_memories[:25]
    ]
    persona_block = (
        "\n你對使用者目前的認識（事實層）：\n"
        + "\n".join(f"- {line}" for line in payload.persona_summary_lines[:8])
        if payload.persona_summary_lines else ""
    )
    emotion_block = (
        f"\n本段時間的情緒概況：\n{payload.emotion_event_summary}"
        if payload.emotion_event_summary.strip() else ""
    )
    return (
        f"你是角色「{payload.character_name}」的內在傾向審核員。"
        f"請判讀過去 {payload.window_days} 天的高重要度記憶 + 情緒事件，"
        "決定是否要把角色的四維內在動機傾向（每維 low/medium/high）的**某一維**輕微 nudge。\n"
        "規則：\n"
        f"- 一次最多動一個維度、一格（low↔medium↔high）。沒明顯依據就回 direction=none。\n"
        "- 訊號不夠強或證據不足，請大方回 direction=none；寧可保守。\n"
        "- 若決定 nudge，必須引一句使用者或角色實際說過的話（verbatim）填到 evidence_quote。\n"
        "- 禁止為了「讓 nudge 看起來合理」而拼湊不存在的引言。\n"
        "目前的內在傾向：\n"
        f"- self_centeredness: {disposition.self_centeredness}\n"
        f"- candor: {disposition.candor}\n"
        f"- sharing_drive: {disposition.sharing_drive}\n"
        f"- associativeness: {disposition.associativeness}\n"
        + emotion_block
        + persona_block
        + "\n本段時間的高重要度記憶：\n"
        + "\n".join(memory_lines)
        + "\n\n輸出規則：只回傳一個 JSON 物件，不要 markdown / code fence。"
        '\n範例：{"dimension": "candor", "direction": "up", '
        '"reason": "對方多次主動分享脆弱，角色開始更願意直說想法", '
        '"evidence_quote": "..."}'
        '\n沒判斷依據：{"dimension": "candor", "direction": "none", '
        '"reason": "訊號不足", "evidence_quote": ""}'
    )


def _coerce_str(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


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
