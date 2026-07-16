from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kokoro_link.application.services.character_social_knowledge_service import (
    CharacterSocialKnowledgeService,
    PeerKnowledgeSeed,
)
from kokoro_link.application.services.character_relationship_service import (
    CharacterRelationshipService,
    CharacterRelationshipUpdate,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_peer_profile import CharacterPeerProfile
from kokoro_link.domain.entities.character_relationship import CharacterRelationship
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.actor import ParticipantRef
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.repositories.in_memory_character_peer_profiles import (
    InMemoryCharacterPeerProfileRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_character_relationships import (
    InMemoryCharacterRelationshipRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)


class _Embedder:
    @property
    def is_operational(self) -> bool:
        return True

    async def embed(self, text: str):  # pragma: no cover - helper compatibility
        return (float(len(text) or 1), 0.5, 0.25)

    async def embed_many(self, texts):
        return [(float(index + 1), 0.5, 0.25) for index, _ in enumerate(texts)]


class _Consolidator:
    async def consolidate(
        self,
        *,
        observer,
        peer,
        existing_profile,
        relationship,
        memories,
    ):
        base = existing_profile or CharacterPeerProfile.create(
            character_id=observer.id,
            peer_character_id=peer.id,
            peer_name=peer.name,
        )
        return base.with_updates(
            peer_name=peer.name,
            summary=f"{peer.name}常在神社附近被提起。",
            haunts=("神社",),
            confidence=0.8,
            last_consolidated_at=datetime(2026, 6, 18, tzinfo=timezone.utc),
            last_seen_at=memories[0].created_at,
            source_memory_ids=tuple(memory.id for memory in memories),
        )


def _character(name: str) -> Character:
    return Character.create(
        name=name,
        summary=f"{name} summary",
        personality=[],
        interests=[],
        speaking_style="natural",
        boundaries=[],
        state=CharacterState(
            emotion="neutral",
            affection=50,
            fatigue=0,
            trust=50,
            energy=100,
        ),
    )


def test_peer_profile_normalizes_flat_fields() -> None:
    profile = CharacterPeerProfile.create(
        character_id="char-a",
        peer_character_id="char-b",
        peer_name="  小英 ",
        summary="  神社打工的人 ",
        haunts=[" 神社 ", "", "神社", "商店街", "公園", "學校", "車站"],
        confidence=1.5,
        source_memory_ids=["m1", "m2", "m3", "m4", "m5", "m6"],
    )

    assert profile.peer_name == "小英"
    assert profile.summary == "神社打工的人"
    assert profile.haunts == ("神社", "商店街", "公園", "學校", "車站")
    assert profile.confidence == 1.0
    assert profile.source_memory_ids == ("m2", "m3", "m4", "m5", "m6")


@pytest.mark.asyncio
async def test_seed_profile_writes_embedded_relationship_memory() -> None:
    characters = InMemoryCharacterRepository()
    profiles = InMemoryCharacterPeerProfileRepository()
    relationships = InMemoryCharacterRelationshipRepository()
    memories = InMemoryMemoryRepository()
    a = _character("小蘭")
    b = _character("小英")
    await characters.save(a)
    await characters.save(b)
    service = CharacterSocialKnowledgeService(
        peer_profiles=profiles,
        relationships=relationships,
        characters=characters,
        memories=memories,
        embedder=_Embedder(),
    )

    profile = await service.seed_peer_profile(
        character_id=a.id,
        peer_character_id=b.id,
        seed=PeerKnowledgeSeed(
            summary="小英在神社打工，是小蘭常去找的人",
            occupation="神社巫女",
            haunts=("神社",),
            relationship_note="小蘭常去神社找她聊天",
        ),
    )

    assert profile is not None
    assert profile.summary == "小英在神社打工，是小蘭常去找的人"
    stored = await memories.list_all_for_character(a.id)
    assert len(stored) == 1
    assert stored[0].kind == MemoryKind.RELATIONSHIP
    assert stored[0].embedding is not None
    assert stored[0].tags_embedding is not None
    assert f"peer:{b.id}" in stored[0].tags
    assert stored[0].participants[0].actor_id == b.id


@pytest.mark.asyncio
async def test_render_encounter_context_includes_directional_profile_and_memory() -> None:
    characters = InMemoryCharacterRepository()
    profiles = InMemoryCharacterPeerProfileRepository()
    relationships = InMemoryCharacterRelationshipRepository()
    memories = InMemoryMemoryRepository()
    a = _character("小蘭")
    b = _character("小英")
    await characters.save(a)
    await characters.save(b)
    relationship_service = CharacterRelationshipService(
        repository=relationships,
        character_repository=characters,
    )
    relationship = await relationship_service.create_or_enable(
        character_id=a.id,
        target_character_id=b.id,
        relationship_label="朋友",
    )
    await relationship_service.update(
        relationship.id,
        CharacterRelationshipUpdate(
            how_a_sees_b="小蘭覺得小英很可靠",
            how_b_sees_a="小蘭覺得小英很可靠",
            affection_a_to_b=80,
            affection_b_to_a=80,
            trust_a_to_b=82,
            trust_b_to_a=82,
        ),
    )
    await profiles.save(CharacterPeerProfile.create(
        character_id=a.id,
        peer_character_id=b.id,
        peer_name=b.name,
        summary="小英常在神社幫忙",
        occupation="巫女",
        haunts=("神社",),
        habits=("下班後喝熱茶",),
        confidence=0.8,
    ))
    await memories.add_many([
        MemoryItem.create(
            character_id=a.id,
            kind=MemoryKind.RELATIONSHIP,
            content="小英說她最近常在神社整理繪馬。",
            tags=("peer_fact", f"peer:{b.id}"),
            created_at=datetime(2026, 6, 18, tzinfo=timezone.utc),
            participants=(
                ParticipantRef(
                    actor_kind="character",
                    actor_id=b.id,
                    display_name=b.name,
                    role="peer",
                ),
            ),
        ),
    ])
    service = CharacterSocialKnowledgeService(
        peer_profiles=profiles,
        relationships=relationships,
        characters=characters,
        memories=memories,
    )

    lines = await service.render_encounter_context(a.id, b.id)
    body = "\n".join(lines)

    assert "小蘭覺得小英很可靠" in body
    assert "小英常在神社幫忙" in body
    assert "下班後喝熱茶" in body
    assert "小英說她最近常在神社整理繪馬" in body


@pytest.mark.asyncio
async def test_roster_renders_profile_without_raw_scores() -> None:
    characters = InMemoryCharacterRepository()
    profiles = InMemoryCharacterPeerProfileRepository()
    relationships = InMemoryCharacterRelationshipRepository()
    memories = InMemoryMemoryRepository()
    a = _character("小蘭")
    b = _character("小英")
    await characters.save(a)
    await characters.save(b)
    relationship = CharacterRelationship.create(
        character_a_id=a.id,
        character_b_id=b.id,
        relationship_label="朋友",
        how_a_sees_b="小蘭覺得小英很可靠",
        affection_a_to_b=82,
        trust_a_to_b=76,
    )
    await relationships.save(relationship)
    await profiles.save(CharacterPeerProfile.create(
        character_id=a.id,
        peer_character_id=b.id,
        peer_name=b.name,
        summary="小英在神社打工",
        occupation="神社巫女",
        haunts=("神社",),
        confidence=0.8,
    ))
    service = CharacterSocialKnowledgeService(
        peer_profiles=profiles,
        relationships=relationships,
        characters=characters,
        memories=memories,
    )

    lines = await service.render_roster_for_prompt(a.id)
    rendered = "\n".join(lines)

    assert "你認識的人" in rendered
    assert "小英" in rendered
    assert "神社" in rendered
    assert "很親近" in rendered
    assert "82" not in rendered
    assert "76" not in rendered


@pytest.mark.asyncio
async def test_consolidate_due_only_processes_pairs_with_new_peer_memories() -> None:
    characters = InMemoryCharacterRepository()
    profiles = InMemoryCharacterPeerProfileRepository()
    relationships = InMemoryCharacterRelationshipRepository()
    memories = InMemoryMemoryRepository()
    a = _character("小蘭")
    b = _character("小英")
    await characters.save(a)
    await characters.save(b)
    await relationships.save(CharacterRelationship.create(
        character_a_id=a.id,
        character_b_id=b.id,
        relationship_label="朋友",
    ))
    memory = MemoryItem.create(
        character_id=a.id,
        kind=MemoryKind.RELATIONSHIP,
        content="小英在神社打工。",
        tags=("peer_fact", f"peer:{b.id}"),
        participants=(
            ParticipantRef(
                actor_kind="character",
                actor_id=b.id,
                display_name=b.name,
                role="peer",
            ),
        ),
    )
    await memories.add(memory)
    service = CharacterSocialKnowledgeService(
        peer_profiles=profiles,
        relationships=relationships,
        characters=characters,
        memories=memories,
        consolidator=_Consolidator(),
    )

    result = await service.consolidate_due(limit=4)
    profile = await profiles.get(a.id, b.id)

    assert result.consolidated == 1
    assert profile is not None
    assert profile.summary == "小英常在神社附近被提起。"
    assert profile.source_memory_ids == (memory.id,)

    second = await service.consolidate_due(limit=4)

    assert second.consolidated == 0
    assert second.skipped == 2


async def _bucketed_context_fixture(
    *,
    affection: int = 80,
    trust: int = 82,
    memory_items: list[MemoryItem] | None = None,
):
    characters = InMemoryCharacterRepository()
    profiles = InMemoryCharacterPeerProfileRepository()
    relationships = InMemoryCharacterRelationshipRepository()
    memories = InMemoryMemoryRepository()
    a = _character("小蘭")
    b = _character("小英")
    await characters.save(a)
    await characters.save(b)
    relationship_service = CharacterRelationshipService(
        repository=relationships,
        character_repository=characters,
    )
    relationship = await relationship_service.create_or_enable(
        character_id=a.id,
        target_character_id=b.id,
        relationship_label="朋友",
    )
    await relationship_service.update(
        relationship.id,
        CharacterRelationshipUpdate(
            affection_a_to_b=affection,
            affection_b_to_a=affection,
            trust_a_to_b=trust,
            trust_b_to_a=trust,
        ),
    )
    if memory_items:
        await memories.add_many(memory_items)
    service = CharacterSocialKnowledgeService(
        peer_profiles=profiles,
        relationships=relationships,
        characters=characters,
        memories=memories,
    )
    return service, a, b


def _peer_memory(
    owner_id: str,
    peer,
    content: str,
    *,
    kind: MemoryKind = MemoryKind.EPISODIC,
    created_at: datetime,
) -> MemoryItem:
    return MemoryItem.create(
        character_id=owner_id,
        kind=kind,
        content=content,
        tags=(f"peer:{peer.id}",),
        created_at=created_at,
        participants=(
            ParticipantRef(
                actor_kind="character",
                actor_id=peer.id,
                display_name=peer.name,
                role="peer",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_encounter_context_time_tags_and_hearsay_framing() -> None:
    now = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)
    service, a, b = await _bucketed_context_fixture()
    await service._memories.add_many([
        _peer_memory(
            a.id, b, "小英剛剛好像看到亮亮的東西",
            created_at=now.replace(day=6),
        ),
        _peer_memory(
            a.id, b, "聽小英說主人最近在趕專案",
            kind=MemoryKind.HEARSAY,
            created_at=now.replace(day=8),
        ),
    ])

    lines = await service.render_encounter_context(a.id, b.id, now=now)
    body = "\n".join(lines)

    # Relative-time anchor stops stale "剛剛" content passing as fresh.
    assert "約 3 天前" in body
    assert "發生時間已標註" in body
    # Hearsay carries an explicit second-hand marker (participant tag
    # from the shared chat renderer sits between marker and content).
    assert "（聽說、未經證實）[與 小英 一起] 聽小英說主人最近在趕專案" in body


@pytest.mark.asyncio
async def test_encounter_context_caps_peer_memories_at_two() -> None:
    now = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)
    service, a, b = await _bucketed_context_fixture()
    await service._memories.add_many([
        _peer_memory(a.id, b, f"舊事 {index}", created_at=now.replace(day=index + 1))
        for index in range(4)
    ])

    body = "\n".join(await service.render_encounter_context(a.id, b.id, now=now))

    assert "舊事 3" in body and "舊事 2" in body
    assert "舊事 0" not in body


@pytest.mark.asyncio
async def test_encounter_context_operator_summary_requires_medium_closeness() -> None:
    now = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)
    close_service, a1, b1 = await _bucketed_context_fixture(affection=70, trust=70)
    close_body = "\n".join(await close_service.render_encounter_context(
        a1.id, b1.id, now=now,
        operator_dialogue_summary="主人最近在準備搬家",
    ))
    assert "主人最近在準備搬家" in close_body
    assert "共同認識的人的近況是自然話題之一" in close_body

    distant_service, a2, b2 = await _bucketed_context_fixture(affection=40, trust=40)
    distant_body = "\n".join(await distant_service.render_encounter_context(
        a2.id, b2.id, now=now,
        operator_dialogue_summary="主人最近在準備搬家",
    ))
    assert "主人最近在準備搬家" not in distant_body
