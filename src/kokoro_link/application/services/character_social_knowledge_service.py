"""Character-to-character social knowledge orchestration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from kokoro_link.application.services.operator_persona_service import (
    OperatorPersonaService,
)
from kokoro_link.application.services.memory_embedding import attach_embeddings
from kokoro_link.contracts.character_peer_profile import (
    CharacterPeerProfileRepositoryPort,
)
from kokoro_link.contracts.character_relationship import (
    CharacterRelationshipRepositoryPort,
)
from kokoro_link.contracts.embedder import EmbedderPort
from kokoro_link.contracts.memory import MemoryRepositoryPort
from kokoro_link.contracts.peer_knowledge_consolidator import (
    PeerKnowledgeConsolidatorPort,
)
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_peer_profile import CharacterPeerProfile
from kokoro_link.domain.entities.character_relationship import CharacterRelationship
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.actor import ParticipantRef
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.prompt.memory_lines import format_memory_line

_LOGGER = logging.getLogger(__name__)

_ROSTER_TOP_N = 6
_ROSTER_MAX_CHARS = 1400
_CONSOLIDATION_MEMORY_LIMIT = 12
_CONSOLIDATION_PAIR_LIMIT = 4
_ENCOUNTER_CONTEXT_MAX_CHARS = 1800
_ENCOUNTER_PEER_MEMORY_LIMIT = 2
"""Peer-memory lines injected per encounter context.

Deliberately small (was 4): after the first encounter the most recent
peer-tagged memories are dominated by the previous encounter's own
summary/hearsay output, so a large window turns the encounter dialogue
into an echo chamber — the pair keeps re-discussing whatever they
discussed last time (ENCOUNTER_CHAT_PARITY_PLAN §1). Fresh material now
comes from CharacterLifeContextBuilder instead; recent encounter
summaries are injected separately as "already discussed" negative
examples by the encounter service."""


@dataclass(frozen=True, slots=True)
class PeerKnowledgeSeed:
    summary: str = ""
    occupation: str = ""
    haunts: tuple[str, ...] = ()
    habits: tuple[str, ...] = ()
    relationship_note: str = ""
    shared_activities: tuple[str, ...] = ()

    def has_material(self) -> bool:
        return bool(
            self.summary
            or self.occupation
            or self.haunts
            or self.habits
            or self.relationship_note
            or self.shared_activities
        )


@dataclass(frozen=True, slots=True)
class PeerKnowledgeTickResult:
    consolidated: int = 0
    skipped: int = 0
    failed: int = 0
    consolidated_pairs: tuple[tuple[str, str], ...] = field(default_factory=tuple)


class CharacterSocialKnowledgeService:
    def __init__(
        self,
        *,
        peer_profiles: CharacterPeerProfileRepositoryPort,
        relationships: CharacterRelationshipRepositoryPort,
        characters: CharacterRepositoryPort,
        memories: MemoryRepositoryPort,
        consolidator: PeerKnowledgeConsolidatorPort | None = None,
        embedder: EmbedderPort | None = None,
        operator_persona_service: OperatorPersonaService | None = None,
    ) -> None:
        self._peer_profiles = peer_profiles
        self._relationships = relationships
        self._characters = characters
        self._memories = memories
        self._consolidator = consolidator
        self._embedder = embedder
        self._operator_persona_service = operator_persona_service

    async def render_roster_for_prompt(self, character_id: str) -> list[str]:
        rows = await self._eligible_roster_rows(character_id)
        if not rows:
            return []
        lines = [
            "你認識的人（角色社交名冊，僅供內部理解；不要逐字背誦，也不要暴露數字）：",
        ]
        for relationship, peer, profile in rows[:_ROSTER_TOP_N]:
            perspective = relationship.perspective_for(character_id)
            parts = [f"{peer.name}"]
            label = relationship.relationship_label.strip()
            if label:
                parts.append(label)
            familiarity = _familiarity_label(
                perspective.affection_self_to_peer,
                perspective.trust_self_to_peer,
            )
            parts.append(familiarity)
            details: list[str] = []
            if perspective.how_self_sees_peer:
                details.append(perspective.how_self_sees_peer)
            if profile.summary:
                details.append(profile.summary)
            if profile.occupation:
                details.append(f"職業/身分：{profile.occupation}")
            if profile.haunts:
                details.append(f"常出沒：{'、'.join(profile.haunts)}")
            if profile.habits:
                details.append(f"習慣：{'、'.join(profile.habits)}")
            if profile.relationship_note:
                details.append(profile.relationship_note)
            line = f"- {'｜'.join(parts)}"
            if details:
                line += f"：{'；'.join(details)}"
            lines.append(line)
        return _cap_lines(lines, _ROSTER_MAX_CHARS)

    async def render_known_peers_for_extraction(self, character_id: str) -> list[str]:
        relationships = await self._relationships.list_for_character(character_id)
        lines = [
            "已知角色名冊（供記憶抽取 participants 使用；提到這些角色或其關聯地點時，可寫 relationship memory）：",
        ]
        for relationship in relationships:
            if not relationship.enabled:
                continue
            try:
                perspective = relationship.perspective_for(character_id)
            except ValueError:
                continue
            peer = await self._characters.get(perspective.peer_character_id)
            if peer is None:
                continue
            profile = await self._peer_profiles.get(character_id, peer.id)
            fragments = [f"id={peer.id}", f"name={peer.name}"]
            if relationship.relationship_label:
                fragments.append(f"label={relationship.relationship_label}")
            if perspective.how_self_sees_peer:
                fragments.append(f"view={perspective.how_self_sees_peer}")
            if profile is not None and profile.haunts:
                fragments.append(f"haunts={', '.join(profile.haunts)}")
            if profile is not None and profile.summary:
                fragments.append(f"summary={profile.summary}")
            lines.append("- " + " | ".join(fragments))
        return lines if len(lines) > 1 else []

    async def render_encounter_context(
        self,
        observer_id: str,
        peer_id: str,
        *,
        now: datetime | None = None,
        operator_dialogue_summary: str = "",
    ) -> list[str]:
        """Bucketed per-speaker context for an encounter prompt.

        ``now`` anchors relative-time tags on injected memories so a
        "剛剛看到…" written three days ago cannot masquerade as fresh.
        ``operator_dialogue_summary`` (the speaker's own recent chat
        digest with their operator) is only surfaced when the speaker
        trusts the peer enough — same closeness gate as persona gossip.
        """
        relationship = await self._relationships.get_pair(observer_id, peer_id)
        if relationship is None or not relationship.enabled:
            return []
        observer = await self._characters.get(observer_id)
        peer = await self._characters.get(peer_id)
        if observer is None or peer is None:
            return []
        perspective = relationship.perspective_for(observer_id)
        profile = await self._peer_profiles.get(observer_id, peer_id)
        memories = await self._recent_peer_memories(
            character_id=observer_id,
            peer_character_id=peer_id,
        )
        lines = [
            f"- 對方：{peer.name}",
            f"- 關係標籤：{relationship.relationship_label or '未標註'}",
            "- 自己視角："
            f"{perspective.how_self_sees_peer or '尚未整理'}；"
            f"熟識度={_familiarity_label(perspective.affection_self_to_peer, perspective.trust_self_to_peer)}",
        ]
        if profile is not None and profile.has_prompt_material():
            profile_parts: list[str] = []
            if profile.summary:
                profile_parts.append(profile.summary)
            if profile.occupation:
                profile_parts.append(f"身分/職業：{profile.occupation}")
            if profile.haunts:
                profile_parts.append(f"常出沒：{'、'.join(profile.haunts)}")
            if profile.habits:
                profile_parts.append(f"習慣：{'、'.join(profile.habits)}")
            if profile.relationship_note:
                profile_parts.append(profile.relationship_note)
            if profile_parts:
                lines.append("- 已知對方側寫：" + "；".join(profile_parts))
        rendered_memories = [
            _render_peer_memory_line(memory, now=now)
            for memory in memories[:_ENCOUNTER_PEER_MEMORY_LIMIT]
            if memory.content.strip()
        ]
        if rendered_memories:
            lines.append(
                "- 近期相關記憶（發生時間已標註，別把舊事講成剛發生）：",
            )
            lines.extend(rendered_memories)
        tier = _closeness_tier(
            perspective.affection_self_to_peer,
            perspective.trust_self_to_peer,
        )
        gossip = await self._operator_gossip_lines(observer=observer, tier=tier)
        operator_summary = (
            operator_dialogue_summary.strip()
            if tier in ("medium", "high")
            else ""
        )
        if gossip or operator_summary:
            lines.append(
                "- 主人相關（共同認識的人的近況是自然話題之一；"
                "分享深度依你對對方的信任，超出下列範圍的私密不可外洩）：",
            )
            lines.extend(f"  {line}" for line in gossip[:8])
            if operator_summary:
                lines.append(f"  - 自己最近和主人相處的近況：{operator_summary}")
        return _cap_lines(lines, _ENCOUNTER_CONTEXT_MAX_CHARS)

    async def _operator_gossip_lines(
        self,
        *,
        observer: Character,
        tier: str,
    ) -> list[str]:
        if self._operator_persona_service is None:
            return []
        try:
            persona = await self._operator_persona_service.get_current(
                observer.id,
                observer.user_id,
            )
        except Exception:
            _LOGGER.exception(
                "operator persona gossip lookup failed character=%s",
                observer.id,
            )
            return []
        return self._operator_persona_service.render_for_peer_gossip(
            persona,
            closeness_tier=tier,
        )

    async def seed_peer_profile(
        self,
        *,
        character_id: str,
        peer_character_id: str,
        seed: PeerKnowledgeSeed,
        peer_name: str | None = None,
    ) -> CharacterPeerProfile | None:
        if not seed.has_material():
            return None
        peer = await self._characters.get(peer_character_id)
        resolved_peer_name = peer_name or (peer.name if peer is not None else "")
        existing = await self._peer_profiles.get(character_id, peer_character_id)
        now = datetime.now(timezone.utc)
        profile = existing or CharacterPeerProfile.create(
            character_id=character_id,
            peer_character_id=peer_character_id,
            peer_name=resolved_peer_name,
        )
        profile = profile.with_updates(
            peer_name=resolved_peer_name,
            summary=seed.summary or profile.summary,
            occupation=seed.occupation or profile.occupation,
            haunts=seed.haunts or profile.haunts,
            habits=seed.habits or profile.habits,
            relationship_note=seed.relationship_note or profile.relationship_note,
            confidence=max(profile.confidence, 0.72),
            last_consolidated_at=profile.last_consolidated_at or now,
            last_seen_at=profile.last_seen_at,
        )
        memory = _seed_memory(
            character_id=character_id,
            peer_character_id=peer_character_id,
            peer_name=resolved_peer_name,
            seed=seed,
            now=now,
        )
        embedded = await attach_embeddings([memory], self._embedder)
        stored = await self._memories.add_many(embedded)
        if stored:
            profile = profile.with_updates(
                source_memory_ids=tuple(profile.source_memory_ids + (stored[0].id,)),
            )
        await self._peer_profiles.save(profile)
        return profile

    async def consolidate(
        self,
        character_id: str,
        peer_character_id: str,
        relationship: CharacterRelationship | None = None,
    ) -> CharacterPeerProfile | None:
        if self._consolidator is None:
            return None
        observer = await self._characters.get(character_id)
        peer = await self._characters.get(peer_character_id)
        if observer is None or peer is None:
            return None
        # A frozen character halts all background consolidation
        # (CHARACTER_FREEZE_PLAN) — skip the peer-knowledge LLM call when
        # either side of the pair is frozen.
        if observer.frozen or peer.frozen:
            return None
        relationship = relationship or await self._relationships.get_pair(
            character_id,
            peer_character_id,
        )
        if relationship is None or not relationship.enabled:
            return None
        existing = await self._peer_profiles.get(character_id, peer_character_id)
        memories = await self._recent_peer_memories(
            character_id=character_id,
            peer_character_id=peer_character_id,
        )
        if not memories:
            return None
        if existing is not None:
            known = set(existing.source_memory_ids)
            if all(memory.id in known for memory in memories):
                return None
        updated = await self._consolidator.consolidate(
            observer=observer,
            peer=peer,
            existing_profile=existing,
            relationship=relationship,
            memories=memories,
        )
        if updated is None:
            return None
        await self._peer_profiles.save(updated)
        return updated

    async def consolidate_due(
        self,
        *,
        limit: int = _CONSOLIDATION_PAIR_LIMIT,
    ) -> PeerKnowledgeTickResult:
        if self._consolidator is None:
            return PeerKnowledgeTickResult()
        consolidated: list[tuple[str, str]] = []
        skipped = 0
        failed = 0
        relationships = await self._relationships.list_enabled()
        for relationship in relationships:
            for character_id in (
                relationship.character_a_id,
                relationship.character_b_id,
            ):
                if len(consolidated) >= limit:
                    return PeerKnowledgeTickResult(
                        consolidated=len(consolidated),
                        skipped=skipped,
                        failed=failed,
                        consolidated_pairs=tuple(consolidated),
                    )
                try:
                    perspective = relationship.perspective_for(character_id)
                    profile = await self.consolidate(
                        character_id=character_id,
                        peer_character_id=perspective.peer_character_id,
                        relationship=relationship,
                    )
                except Exception:
                    failed += 1
                    _LOGGER.exception(
                        "peer knowledge consolidation failed character=%s",
                        character_id,
                    )
                    continue
                if profile is None:
                    skipped += 1
                else:
                    consolidated.append((character_id, perspective.peer_character_id))
        return PeerKnowledgeTickResult(
            consolidated=len(consolidated),
            skipped=skipped,
            failed=failed,
            consolidated_pairs=tuple(consolidated),
        )

    async def _eligible_roster_rows(
        self,
        character_id: str,
    ) -> list[tuple[CharacterRelationship, Character, CharacterPeerProfile]]:
        relationships = await self._relationships.list_for_character(character_id)
        rows: list[tuple[CharacterRelationship, Character, CharacterPeerProfile]] = []
        for relationship in relationships:
            if not relationship.enabled:
                continue
            try:
                perspective = relationship.perspective_for(character_id)
            except ValueError:
                continue
            profile = await self._peer_profiles.get(character_id, perspective.peer_character_id)
            if profile is None or not profile.has_prompt_material():
                continue
            peer = await self._characters.get(perspective.peer_character_id)
            if peer is None:
                continue
            rows.append((relationship, peer, profile))
        rows.sort(key=_roster_sort_key, reverse=True)
        return rows

    async def _recent_peer_memories(
        self,
        *,
        character_id: str,
        peer_character_id: str,
    ) -> list[MemoryItem]:
        rows = await self._memories.list_all_for_character(
            character_id,
            kinds=(MemoryKind.RELATIONSHIP, MemoryKind.EPISODIC, MemoryKind.HEARSAY),
            world_scope=None,
        )
        filtered = [
            memory for memory in rows
            if _memory_mentions_peer(memory, peer_character_id)
        ]
        filtered.sort(key=lambda memory: memory.created_at, reverse=True)
        return filtered[:_CONSOLIDATION_MEMORY_LIMIT]


def _render_peer_memory_line(
    memory: MemoryItem, *, now: datetime | None,
) -> str:
    """One peer-memory line with time anchor + hearsay framing.

    Reuses the chat renderer (participant tag + relative-time suffix) so
    encounter and chat quote memories identically; hearsay additionally
    gets an explicit second-hand marker because a bare "聽說資訊" section
    header does not exist in this bucketed context."""
    rendered = format_memory_line(memory, now=now)
    body = rendered[2:] if rendered.startswith("- ") else rendered
    if memory.kind is MemoryKind.HEARSAY:
        return f"  - （聽說、未經證實）{body}"
    return f"  - {body}"


def _memory_mentions_peer(memory: MemoryItem, peer_character_id: str) -> bool:
    if f"peer:{peer_character_id}" in memory.tags:
        return True
    return any(
        participant.actor_kind == "character"
        and participant.actor_id == peer_character_id
        for participant in memory.participants
    )


def _seed_memory(
    *,
    character_id: str,
    peer_character_id: str,
    peer_name: str,
    seed: PeerKnowledgeSeed,
    now: datetime,
) -> MemoryItem:
    parts: list[str] = []
    if seed.summary:
        parts.append(seed.summary)
    if seed.occupation:
        parts.append(f"{peer_name}的身分/工作：{seed.occupation}")
    if seed.haunts:
        parts.append(f"{peer_name}常出沒：{'、'.join(seed.haunts)}")
    if seed.habits:
        parts.append(f"{peer_name}的習慣：{'、'.join(seed.habits)}")
    if seed.shared_activities:
        parts.append(f"常一起做的事：{'、'.join(seed.shared_activities)}")
    if seed.relationship_note:
        parts.append(seed.relationship_note)
    content = "；".join(parts)
    return MemoryItem.create(
        character_id=character_id,
        kind=MemoryKind.RELATIONSHIP,
        content=content,
        salience=0.64,
        tags=("relationship_seed", "peer_fact", f"peer:{peer_character_id}"),
        created_at=now,
        participants=(
            ParticipantRef(
                actor_kind="character",
                actor_id=peer_character_id,
                display_name=peer_name,
                role="peer",
            ),
        ),
    )


def _familiarity_label(affection: int, trust: int) -> str:
    score = (affection + trust) / 2
    if score >= 75:
        return "很親近且信任"
    if score >= 65:
        return "熟悉、互動自然"
    if score >= 45:
        return "普通熟悉"
    return "仍有距離或保留"


def _closeness_tier(affection: int, trust: int) -> str:
    score = (affection + trust) / 2
    if score >= 75:
        return "high"
    if score >= 55:
        return "medium"
    return "low"


def _roster_sort_key(
    row: tuple[CharacterRelationship, Character, CharacterPeerProfile],
) -> tuple[datetime, float]:
    relationship, _, profile = row
    last = (
        profile.last_seen_at
        or relationship.last_interaction_at
        or profile.updated_at
        or relationship.updated_at
        or datetime.min.replace(tzinfo=timezone.utc)
    )
    perspective = relationship.perspective_for(profile.character_id)
    score = (perspective.affection_self_to_peer + perspective.trust_self_to_peer) / 2
    return last, score


def _cap_lines(lines: list[str], max_chars: int) -> list[str]:
    out: list[str] = []
    total = 0
    for line in lines:
        next_total = total + len(line)
        if out and next_total > max_chars:
            break
        out.append(line)
        total = next_total
    return out
