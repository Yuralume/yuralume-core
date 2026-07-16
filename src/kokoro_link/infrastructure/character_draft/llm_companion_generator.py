"""OpenAI-compatible LLM-backed companion generator.

Sister-port of :class:`LLMCharacterDraftGenerator`: takes a flattened
character description (name + summary + personality + interests) and
asks the model to produce a small list of "private NPC companions"
that fit the character's life. Used by
``POST /characters/{id}/companions/generate`` so the operator can ask
for more NPCs after the character already exists.

The output JSON shape matches ``CompanionDraft`` fields so the API can
hand the result straight to the operator's UI for accept / edit /
discard before persisting.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.character_draft import (
    CompanionDraft,
    CompanionDraftGeneratorPort,
    CompanionGenerationContext,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)

_LOGGER = logging.getLogger(__name__)

_MAX_NAME_CHARS = 40
_MAX_ROLE_CHARS = 40
_MAX_PROFILE_CHARS = 240
_MAX_REL_CHARS = 160
_MAX_LIST_ITEMS = 6
_MAX_LIST_ITEM_CHARS = 30
_HARD_CAP = 6


class LLMCompanionDraftGenerator(CompanionDraftGeneratorPort):
    def __init__(
        self,
        *,
        provider: ActiveLLMProviderPort | None = None,
        model_port: ChatModelPort | None = None,
        base_url: str = "",
        api_key: str | None = None,
        model: str = "",
        feature_key: str | None = None,
        timeout_seconds: float = 45.0,
    ) -> None:
        self._resolver: ModelResolver | None = None
        if provider is not None or model_port is not None:
            self._resolver = ModelResolver(
                provider=provider, model=model_port, feature_key=feature_key,
            )
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds

    async def generate(
        self, *, context: CompanionGenerationContext,
    ) -> list[CompanionDraft]:
        if self._resolver is not None and await self._resolver.is_fake():
            return []
        # Clamp + sanity-bound count so a runaway frontend can't ask for
        # thirty NPCs in one shot.
        wanted = max(1, min(_HARD_CAP, int(context.count or 3)))
        instruction = _build_instruction(context, wanted=wanted)
        try:
            raw = await self._call(instruction)
        except Exception:
            _LOGGER.exception("Companion generator LLM call failed")
            return []
        return _parse(raw, max_items=wanted)

    async def _call(self, instruction: str) -> str:
        if self._resolver is not None:
            return await self._resolver.generate(instruction)
        if not self._base_url or not self._model:
            raise RuntimeError("LLMCompanionDraftGenerator is not configured")
        messages = [
            {
                "role": "system",
                "content": "You are a companion / supporting-character designer.",
            },
            {"role": "user", "content": instruction},
        ]
        payload: dict[str, Any] = {"model": self._model, "messages": messages}
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
        return data["choices"][0]["message"]["content"]


def _build_instruction(
    context: CompanionGenerationContext, *, wanted: int,
) -> str:
    name = (context.character_name or "").strip() or "（未命名角色）"
    summary = (context.character_summary or "").strip() or "（未提供）"
    personality = (context.character_personality or "").strip() or "（未提供）"
    interests = (context.character_interests or "").strip() or "（未提供）"
    existing = (context.existing_companions_summary or "").strip()
    hint = (context.hint or "").strip()
    language_hint = render_operator_language_hint(
        context.operator_primary_language,
    )
    lines = [
        *([language_hint, ""] if language_hint else []),
        f"請為角色「{name}」設計 {wanted} 位「私人 NPC 同伴」。",
        "這些 NPC 不會自己出來講話，是讓主角的日常更生動的配角：",
        "同事、室友、家人、好友、青梅竹馬、合作對象之類。",
        "",
        "主角資料：",
        f"- 名稱：{name}",
        f"- 簡介：{summary}",
        f"- 性格：{personality}",
        f"- 興趣：{interests}",
    ]
    if existing:
        lines.extend([
            "",
            "主角現有的同伴（請避免重複生成、避免名字或角色定位太相近）：",
            existing,
        ])
    if hint:
        lines.extend([
            "",
            f"使用者額外的指示：{hint}",
        ])
    lines.extend([
        "",
        "設計原則：",
        "- NPC 必須跟主角的人設、年齡、生活方式自然吻合 —— 御宅族不該有「常一起去夜店的朋友」、退休奶奶不該有「同寢的學妹」之類的不協調。",
        "- 每個 NPC 都該有點記憶點：別都是「個性溫柔的好朋友」。",
        "- 關係不一定都是甜蜜順遂，可以是「最近在冷戰的同事」、「老愛唸我的姊姊」這類有戲的關係。",
        "",
        "輸出規則：",
        "- 只輸出一個 JSON 陣列，不要任何前言、不要 code fence。",
        "- 每個元素包含五個欄位：",
        "  · name (str)：對方的稱呼。",
        "  · role (str)：與主角的關係（例：室友、同事、表姐）。",
        "  · brief_profile (str)：一句話速寫，30 字內。",
        "  · personality_sketch (list[str])：1~3 個短詞個性形容。",
        "  · relationship_snippet (str)：一句話描述目前關係狀態，30 字內。",
        "- 玩家會看到 JSON 中的文字欄位；每個 NPC 的 name、role、brief_profile、personality_sketch、relationship_snippet 都必須使用上方「玩家可見自然語言輸出語言（BCP 47 標籤）」指定的語言。",
        "- 禁止輸出色情、暴力或未成年相關內容。",
        "",
        "範例：",
        '[{"name": "葉澄", "role": "室友", "brief_profile": "都市設計系學生，總把模型材料堆滿餐桌",'
        ' "personality_sketch": ["細心", "毒舌"], "relationship_snippet": "兩年室友，熟到會互相吐槽生活習慣"}]',
    ])
    return "\n".join(lines)


def _parse(raw: str, *, max_items: int) -> list[CompanionDraft]:
    array = _extract_array(raw)
    if array is None:
        return []
    out: list[CompanionDraft] = []
    for entry in array:
        if not isinstance(entry, dict):
            continue
        name = _coerce_str(entry.get("name"), _MAX_NAME_CHARS)
        if not name:
            continue
        out.append(
            CompanionDraft(
                name=name,
                role=_coerce_str(entry.get("role"), _MAX_ROLE_CHARS),
                brief_profile=_coerce_str(
                    entry.get("brief_profile"), _MAX_PROFILE_CHARS,
                ),
                personality_sketch=_coerce_str_list(
                    entry.get("personality_sketch"),
                ),
                relationship_snippet=_coerce_str(
                    entry.get("relationship_snippet"), _MAX_REL_CHARS,
                ),
            )
        )
        if len(out) >= max_items:
            break
    return out


def _extract_array(text: str) -> list[Any] | None:
    start = text.find("[")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                candidate = text[start : index + 1]
                try:
                    parsed = json.loads(candidate)
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, list) else None
    return None


def _coerce_str(value: Any, max_chars: int) -> str:
    if isinstance(value, str):
        return value.strip()[:max_chars]
    return ""


def _coerce_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, (str, int, float)):
            continue
        text = str(item).strip()[:_MAX_LIST_ITEM_CHARS]
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= _MAX_LIST_ITEMS:
            break
    return out
