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
from kokoro_link.llm_output import (
    extract_object_outcome,
    first_region_is_array,
    log_parse_outcome,
)

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
            "",
            "【整份改寫，不是逐次追加】",
            "1. 你的輸出會**整份取代**現有 profile，不是接在後面。每個欄位都要當成從頭重寫一次：",
            "   先把「現有 profile」與「可用記憶」放在一起看，合併去重之後再寫出最終版本。",
            "2. 同一件事只講一次：現有內容與新記憶如果在說同一件事（同一個習慣、同一個地點、",
            "   同一種相處方式、同一類行為），必須**合併成一句**完整敘述，不得在 summary 裡重述第二次，",
            "   也不得在 haunts / habits 裡列成兩條意思相同、只是措辭不同的項目。",
            "3. 近重複也要合併：措辭不同但講的是同一件事，一律視為重複；只有真正多出來的新細節才值得獨立成句。",
            "4. 合併時不得丟資訊：新記憶帶來的新細節（頻率、對象、場合、變化）要併進那一句裡，",
            "   而不是為了避免重複就整段刪掉。舊事實若被新事實推翻，寫新的並捨棄舊的。",
            "5. summary 是一段收斂過的敘述，不是事件流水帳；每個欄位維持精簡，寧可短也不要重複。",
            "",
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


def _crude_object_span_decodes(text: str) -> bool:
    """Old behaviour, preserved exactly — see the identical helper's
    docstring in ``fusion_story_critic``."""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return False
    try:
        json.loads(text[start: end + 1])
    except (json.JSONDecodeError, RecursionError):
        return False
    return True


def _json_object(raw: str) -> dict[str, Any] | None:
    """DH2-services: object extraction via the shared scanner.

    Near-duplicate of ``character_encounter_service._json_object``
    (same signature, near-identical body) — migrated as one unit.
    Branch selection still mirrors the old crude find/rfind slice
    exactly (see the helper above) so a genuinely array-shaped reply is
    not misread as its first nested object.

    **Truncation repair is off**, unlike its encounter-service twin, and
    the difference is what the two do with the result. The encounter
    service reads a payload; this one *replaces* a record. The prompt
    above says so in as many words — 「整份改寫，不是逐次追加」,
    「你的輸出會**整份取代**現有 profile」 — so every field here is
    written over whatever the observer knew about this peer before.
    Repair would close a reply cut mid-``summary`` into an ordinary
    ``str``, and that half-sentence becomes the profile: the previous,
    complete summary is gone and nothing downstream can tell.

    Failing closed skips one consolidation pass. The memories that
    triggered it are still there, the existing profile is still there,
    and the next pass rewrites it properly.

    FX1/DH-2: this is the site the array confusion was proved on, and
    the array guard is structural because of it. The consolidator asks
    for one peer's profile; a model that answers with a list of peers
    and then adds a sentence used to slip past the old whole-string
    guard, and the object extractor would reach into the list and write
    **peer A's** profile onto whichever peer this call was about.
    """
    text = (raw or "").strip()
    if not _crude_object_span_decodes(text) and first_region_is_array(text):
        return None
    outcome = extract_object_outcome(raw, repair_truncated=False)
    log_parse_outcome(_LOGGER, outcome, site="peer_knowledge_consolidator")
    return outcome.value


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
