"""LLM-backed character peer knowledge consolidator."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from kokoro_link.application.services.feature_keys import (
    FEATURE_PEER_KNOWLEDGE_CONSOLIDATE,
)
from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.peer_knowledge_consolidator import (
    PeerKnowledgeConsolidatorPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_peer_profile import CharacterPeerProfile
from kokoro_link.domain.entities.character_relationship import CharacterRelationship
from kokoro_link.domain.entities.memory_item import MemoryItem

_LOGGER = logging.getLogger(__name__)
_MAX_MEMORIES = 12


class LLMPeerKnowledgeConsolidator(PeerKnowledgeConsolidatorPort):
    def __init__(
        self,
        *,
        provider: ActiveLLMProviderPort | None = None,
        model: ChatModelPort | None = None,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider,
            model=model,
            feature_key=FEATURE_PEER_KNOWLEDGE_CONSOLIDATE,
        )

    async def consolidate(
        self,
        *,
        observer: Character,
        peer: Character,
        existing_profile: CharacterPeerProfile | None,
        relationship: CharacterRelationship,
        memories: list[MemoryItem],
    ) -> CharacterPeerProfile | None:
        if not memories:
            return None
        if await self._resolver.is_fake(character=observer):
            return None
        prompt = _build_prompt(
            observer=observer,
            peer=peer,
            existing_profile=existing_profile,
            relationship=relationship,
            memories=memories,
        )
        try:
            raw = await self._resolver.generate(prompt, character=observer)
        except Exception:
            _LOGGER.exception("Peer knowledge consolidation LLM call failed")
            return None
        payload = _json_object(raw)
        if not payload:
            return None
        return _profile_from_payload(
            payload,
            observer=observer,
            peer=peer,
            existing_profile=existing_profile,
            memories=memories,
        )


def _build_prompt(
    *,
    observer: Character,
    peer: Character,
    existing_profile: CharacterPeerProfile | None,
    relationship: CharacterRelationship,
    memories: list[MemoryItem],
) -> str:
    perspective = relationship.perspective_for(observer.id)
    profile_line = "（尚未建立）"
    if existing_profile is not None and existing_profile.has_prompt_material():
        profile_line = (
            f"summary={existing_profile.summary or '無'}; "
            f"occupation={existing_profile.occupation or '無'}; "
            f"haunts={', '.join(existing_profile.haunts) or '無'}; "
            f"habits={', '.join(existing_profile.habits) or '無'}; "
            f"relationship_note={existing_profile.relationship_note or '無'}; "
            f"confidence={existing_profile.confidence:.2f}"
        )
    memory_lines = []
    for memory in memories[:_MAX_MEMORIES]:
        tags = ", ".join(memory.tags) if memory.tags else "無"
        location = f" location={memory.location}" if memory.location else ""
        memory_lines.append(
            f"- id={memory.id} kind={memory.kind.value} tags={tags}{location}: "
            f"{memory.content}",
        )
    return "\n".join(
        [
            "你是角色社交知識整理器。請把多筆角色互動/關係記憶整理成穩定、保守的平結構資料。",
            "只保留 observer 親見、被對方直接說明，或既有關係設定支持的事；hearsay 可作線索但不可升級成確定事實。",
            "不要編造沒有 evidence 的職業、地點、習慣。沒有把握就留空或降低 confidence。",
            "輸出 JSON，欄位固定：",
            '{"summary": "", "occupation": "", "haunts": [], "habits": [], '
            '"relationship_note": "", "confidence": 0.0}',
            "",
            f"observer={observer.name} ({observer.id})",
            f"peer={peer.name} ({peer.id})",
            f"relationship_label={relationship.relationship_label or '未標註'}",
            f"observer 對 peer 的既有看法={perspective.how_self_sees_peer or '無'}",
            f"現有 profile={profile_line}",
            "",
            "可用記憶：",
            *memory_lines,
        ],
    )


def _profile_from_payload(
    payload: dict[str, Any],
    *,
    observer: Character,
    peer: Character,
    existing_profile: CharacterPeerProfile | None,
    memories: list[MemoryItem],
) -> CharacterPeerProfile:
    now = datetime.now(timezone.utc)
    source_ids = tuple(memory.id for memory in memories[:_MAX_MEMORIES])
    base = existing_profile or CharacterPeerProfile.create(
        character_id=observer.id,
        peer_character_id=peer.id,
        peer_name=peer.name,
    )
    return base.with_updates(
        peer_name=peer.name,
        summary=_str(payload.get("summary")),
        occupation=_str(payload.get("occupation")),
        haunts=_str_tuple(payload.get("haunts")),
        habits=_str_tuple(payload.get("habits")),
        relationship_note=_str(payload.get("relationship_note")),
        confidence=_float(payload.get("confidence")),
        last_consolidated_at=now,
        last_seen_at=_latest_memory_time(memories),
        source_memory_ids=source_ids,
    )


def _latest_memory_time(memories: list[MemoryItem]) -> datetime | None:
    if not memories:
        return None
    return max((memory.created_at for memory in memories), default=None)


def _json_object(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        parsed = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _str(raw: Any) -> str:
    return raw.strip() if isinstance(raw, str) else ""


def _str_tuple(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= 5:
            break
    return tuple(out)


def _float(raw: Any) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0
