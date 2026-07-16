"""LLM-backed relationship-coherence detector.

Runs from the dream-pass tail via :class:`RelationshipCoherenceService`.
Given authoritative address/identity facts (seed, rename-log, character
name, operator profile) plus a windowed raw transcript, and the suspect
derived stores (persona name/nickname, observed salutation, recent memory
participants), it asks a high-reasoning model to decide which derived
values are contaminated by a direction inversion and to emit a structured
repair plan.

LLM-first: no honorific denylists, no keyword matching. The model judges
against the authoritative sources and the raw transcript. Python side only
parses/bounds the JSON and requires each repair to cite the authoritative
source it contradicts. The service applies a second structural check
before mutating anything, so a hallucinated repair id or a value that does
not actually collide is dropped.
"""

from __future__ import annotations

import json
import logging
from typing import Final

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.relationship_coherence import (
    CoherenceFacts,
    CoherenceRepairPlan,
    CoherenceSuspects,
    MemoryRepair,
    PersonaFieldRepair,
    RelationshipCoherenceDetectorPort,
    SalutationRepair,
)

_LOGGER = logging.getLogger(__name__)

_VALID_CONTRADICTIONS: Final = frozenset(
    {
        "seed_user_address_name",
        "seed_character_address_name",
        "character_name",
        "operator_display_name",
        "operator_alias",
    },
)


class NullRelationshipCoherenceDetector(RelationshipCoherenceDetectorPort):
    """Pass-through detector for tests / fake provider — never repairs."""

    async def detect(self, *, facts, suspects):
        return CoherenceRepairPlan()


class LLMRelationshipCoherenceDetector(RelationshipCoherenceDetectorPort):
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

    async def detect(
        self,
        *,
        facts: CoherenceFacts,
        suspects: CoherenceSuspects,
    ) -> CoherenceRepairPlan:
        # Nothing suspect to examine → nothing to do.
        if (
            not suspects.persona_fields
            and not suspects.observed_salutation
            and not suspects.memories
        ):
            return CoherenceRepairPlan()
        try:
            if await self._resolver.is_fake():
                return CoherenceRepairPlan()
            prompt = _build_prompt(facts, suspects)
            raw = await self._resolver.generate(prompt)
        except Exception:
            _LOGGER.exception("coherence detector LLM call failed")
            return CoherenceRepairPlan()
        return _parse_plan(raw, suspects)


def _build_prompt(facts: CoherenceFacts, suspects: CoherenceSuspects) -> str:
    transcript = "\n".join(
        f"  [{t.role}] {t.content[:240]}" for t in facts.recent_transcript
    ) or "  （無近期對話）"
    persona_lines = "\n".join(
        f'  - field_id={f.field_id} key={f.field_key} value="{f.value}" '
        f"source={f.source} confidence={f.confidence:.2f}"
        for f in suspects.persona_fields
    ) or "  （無）"
    memory_lines = "\n".join(
        f'  - memory_id={m.memory_id} salience={m.salience:.2f} '
        f'operator_names={list(m.operator_participant_names)} '
        f'content="{m.content[:160]}"'
        for m in suspects.memories
    ) or "  （無）"
    aliases = "、".join(facts.operator_aliases) or "（無）"
    return (
        "你是一位人際稱呼一致性稽核員。系統有兩個稱呼方向：\n"
        "A) 角色該怎麼稱呼玩家（玩家的名字/暱稱）。\n"
        "B) 玩家怎麼稱呼角色（例如兄妹喊「哥哥」、情侶喊「老公」、"
        "或直接喊角色本名）。\n\n"
        "一種污染是：方向 B 的詞（玩家用來喊角色的稱呼）被錯誤寫進了"
        "方向 A 的欄位（本該記錄角色如何稱呼玩家）。你的工作是找出"
        "「衍生資料」中哪些值方向被搞反、或與已確認的權威事實矛盾，"
        "並只在高信心時提出修復。資料本來就一致時，回傳空修復。\n\n"
        "【權威事實（正解，優先於任何衍生資料）】\n"
        f"- 方向A｜角色稱呼玩家（seed.user_address_name）：{facts.seed_user_address_name or '（無）'}"
        f"（confirmed_by_user={facts.seed_confirmed_by_user}）\n"
        f"- 方向B｜玩家稱呼角色（seed.character_address_name）：{facts.seed_character_address_name or '（無）'}\n"
        f"- 角色本名：{facts.character_name or '（無）'}\n"
        f"- 玩家平台顯示名：{facts.operator_display_name or '（無）'}\n"
        f"- 玩家別名：{aliases}\n"
        f"- 最近一次改名（方向A）：{facts.latest_rename_player_direction or '（無）'}\n"
        f"- 最近一次改名（方向B）：{facts.latest_rename_character_direction or '（無）'}\n\n"
        "【近期原始對話（第一手佐證，用來裁決哪個衍生值是髒的，"
        "禁止拿它自由發明新名字）】\n"
        f"{transcript}\n\n"
        "【嫌疑衍生資料】\n"
        f"persona 身分欄位：\n{persona_lines}\n"
        f"觀察到的 salutation（玩家怎麼稱呼角色，方向B）："
        f"{suspects.observed_salutation or '（無）'}\n"
        f"近期記憶的玩家歸屬：\n{memory_lines}\n\n"
        "【裁決規則】\n"
        "1. persona 的 name/nickname 若其實是方向B的詞（等於"
        " seed.character_address_name 或角色本名），屬污染，列入 persona_field_repairs。\n"
        "2. salutation 若其實是方向A的詞（等於 seed.user_address_name 或玩家本名/別名），"
        "屬污染，設 salutation_repair。\n"
        "3. 記憶若把玩家用方向B的詞或角色名標記成 operator participant，"
        "屬污染：降低 salience 並把 participant 顯示名對齊到正解（方向A）。"
        "**絕不**改寫記憶內文、**絕不**刪記憶。\n"
        "4. 若近期對話顯示玩家「合法地」最近改了稱呼、且衍生資料只是反映該改動，"
        "那不是污染，別修。不確定就不要修（空修復是對的）。\n"
        "5. seed 是正解，永遠不要提議修改 seed。\n\n"
        "請只輸出如下 JSON（不確定的區塊給空陣列/ null）：\n"
        "{\n"
        '  "persona_field_repairs": [\n'
        '    {"field_id": "...", "contradicts": '
        '"seed_character_address_name|character_name", "reason": "..."}\n'
        "  ],\n"
        '  "salutation_repair": {"contradicts": '
        '"seed_user_address_name|operator_display_name|operator_alias", '
        '"reason": "..."} 或 null,\n'
        '  "memory_repairs": [\n'
        '    {"memory_id": "...", "lower_salience_to": 0.2, '
        '"reconcile_participant_to": "方向A的正解名字", "reason": "..."}\n'
        "  ]\n"
        "}"
    )


def _parse_plan(raw: str, suspects: CoherenceSuspects) -> CoherenceRepairPlan:
    if not raw:
        return CoherenceRepairPlan()
    body = raw.strip()
    if body.startswith("```"):
        body = body.strip("`")
        if body.lower().startswith("json"):
            body = body[4:]
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        _LOGGER.warning("coherence detector returned unparseable JSON: %s", raw[:200])
        return CoherenceRepairPlan()
    if not isinstance(data, dict):
        return CoherenceRepairPlan()

    known_field_ids = {f.field_id for f in suspects.persona_fields}
    known_memory_ids = {m.memory_id for m in suspects.memories}

    persona_repairs = _parse_persona_repairs(
        data.get("persona_field_repairs"), known_field_ids,
    )
    salutation_repair = _parse_salutation_repair(data.get("salutation_repair"))
    memory_repairs = _parse_memory_repairs(
        data.get("memory_repairs"), known_memory_ids,
    )
    return CoherenceRepairPlan(
        persona_field_repairs=persona_repairs,
        salutation_repair=salutation_repair,
        memory_repairs=memory_repairs,
    )


def _parse_persona_repairs(
    raw, known_ids: set[str],
) -> tuple[PersonaFieldRepair, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[PersonaFieldRepair] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        field_id = str(entry.get("field_id") or "").strip()
        contradicts = str(entry.get("contradicts") or "").strip()
        if field_id not in known_ids or contradicts not in _VALID_CONTRADICTIONS:
            continue
        out.append(
            PersonaFieldRepair(
                field_id=field_id,
                contradicts=contradicts,  # type: ignore[arg-type]
                reason=str(entry.get("reason") or "")[:240],
            ),
        )
    return tuple(out)


def _parse_salutation_repair(raw) -> SalutationRepair | None:
    if not isinstance(raw, dict):
        return None
    contradicts = str(raw.get("contradicts") or "").strip()
    if contradicts not in _VALID_CONTRADICTIONS:
        return None
    return SalutationRepair(
        contradicts=contradicts,  # type: ignore[arg-type]
        reason=str(raw.get("reason") or "")[:240],
    )


def _parse_memory_repairs(
    raw, known_ids: set[str],
) -> tuple[MemoryRepair, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[MemoryRepair] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        memory_id = str(entry.get("memory_id") or "").strip()
        if memory_id not in known_ids:
            continue
        try:
            lower_to = float(entry.get("lower_salience_to"))
        except (TypeError, ValueError):
            lower_to = 0.2
        out.append(
            MemoryRepair(
                memory_id=memory_id,
                lower_salience_to=max(0.0, min(1.0, lower_to)),
                reconcile_participant_to=str(
                    entry.get("reconcile_participant_to") or "",
                ).strip()[:64],
                reason=str(entry.get("reason") or "")[:240],
            ),
        )
    return tuple(out)
