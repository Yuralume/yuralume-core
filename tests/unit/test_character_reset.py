"""BDD for the identity-drift 'reset memory/conversation' escape hatch.

The operator flips a character's personality halfway through a campaign
and wants the old memories & chat log gone so the new persona can't be
pulled back by stale content. The reset endpoint is the one-call way to
do that without deleting the character itself.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.routes.characters import router as character_router
from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.state_tracker import StateChangeTracker
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.state_snapshot import SOURCE_HEURISTIC
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_state_history import (
    InMemoryStateHistoryRepository,
)
from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageRole,
)


def _build_service() -> tuple[
    CharacterService,
    InMemoryMemoryRepository,
    InMemoryConversationRepository,
    InMemoryStateHistoryRepository,
]:
    character_repository = InMemoryCharacterRepository()
    memory_repository = InMemoryMemoryRepository()
    conversation_repository = InMemoryConversationRepository()
    state_history_repository = InMemoryStateHistoryRepository()
    service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        state_history_repository=state_history_repository,
        state_tracker=StateChangeTracker(state_history_repository),
    )
    return service, memory_repository, conversation_repository, state_history_repository


class _StubPersonaRepository:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete_for_character(self, character_id: str) -> int:
        self.deleted.append(character_id)
        return 3


async def _seed_data(
    service: CharacterService,
    memory_repo: InMemoryMemoryRepository,
    conversation_repo: InMemoryConversationRepository,
    history_repo: InMemoryStateHistoryRepository,
) -> str:
    created = await service.create_character(CreateCharacterRequest(name="Yuki"))
    await memory_repo.add(
        MemoryItem.create(
            character_id=created.id,
            kind=MemoryKind.SEMANTIC,
            content="舊設定的痕跡",
            salience=0.6,
        ),
    )
    await memory_repo.add(
        MemoryItem.create(
            character_id=created.id,
            kind=MemoryKind.REFLECTION,
            content="我剛剛用溫柔語氣回答",
            salience=0.4,
        ),
    )
    conversation = Conversation.start(character_id=created.id).append(
        Message(role=MessageRole.USER, content="hi"),
    )
    await conversation_repo.save(conversation)
    before = CharacterState(emotion="calm", affection=10, fatigue=0, trust=10, energy=100)
    after = CharacterState(emotion="happy", affection=20, fatigue=0, trust=10, energy=100)
    await StateChangeTracker(history_repo).record(
        character_id=created.id,
        source=SOURCE_HEURISTIC,
        before=before,
        after=after,
    )
    return created.id


@pytest.mark.asyncio
async def test_reset_clears_memories_only() -> None:
    service, memory_repo, conversation_repo, history_repo = _build_service()
    character_id = await _seed_data(service, memory_repo, conversation_repo, history_repo)

    result = await service.reset_character_data(
        character_id, memories=True,
    )

    assert result == (2, 0, 0, 0)
    assert await memory_repo.count_for_character(character_id) == 0
    # Conversation + history untouched.
    assert await conversation_repo.latest_for_character(
        character_id, source=None,
    ) is not None
    assert await history_repo.query(character_id, limit=10)


@pytest.mark.asyncio
async def test_reset_clears_everything_when_all_flags_true() -> None:
    service, memory_repo, conversation_repo, history_repo = _build_service()
    character_id = await _seed_data(service, memory_repo, conversation_repo, history_repo)

    result = await service.reset_character_data(
        character_id,
        memories=True,
        conversations=True,
        state_history=True,
    )

    assert result[0] == 2
    assert result[1] >= 1
    assert result[2] >= 1
    assert await memory_repo.count_for_character(character_id) == 0
    assert await conversation_repo.latest_for_character(
        character_id, source=None,
    ) is None
    assert await history_repo.query(character_id, limit=10) == []
    # Character entity must survive the wipe.
    assert await service.get_character(character_id) is not None


@pytest.mark.asyncio
async def test_reset_returns_none_for_unknown_character() -> None:
    service, *_ = _build_service()
    result = await service.reset_character_data("ghost", memories=True)
    assert result is None


@pytest.mark.asyncio
async def test_reset_no_flags_is_noop_and_reports_zero() -> None:
    service, memory_repo, conversation_repo, history_repo = _build_service()
    character_id = await _seed_data(service, memory_repo, conversation_repo, history_repo)

    result = await service.reset_character_data(character_id)

    assert result == (0, 0, 0, 0)
    assert await memory_repo.count_for_character(character_id) == 2


def _client(service: CharacterService) -> TestClient:
    class _Container:
        pass

    container = _Container()
    container.character_service = service

    app = FastAPI()
    app.state.container = container
    app.include_router(character_router, prefix="/api/v1")
    return TestClient(app)


@pytest.mark.asyncio
async def test_reset_route_returns_counts() -> None:
    service, memory_repo, conversation_repo, history_repo = _build_service()
    character_id = await _seed_data(service, memory_repo, conversation_repo, history_repo)

    client = _client(service)
    response = client.post(
        f"/api/v1/characters/{character_id}/reset",
        json={"memories": True, "conversations": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["character_id"] == character_id
    assert body["memories_deleted"] == 2
    assert body["conversations_deleted"] >= 1
    assert body["state_history_deleted"] == 0
    assert body["operator_persona_deleted"] == 0


@pytest.mark.asyncio
async def test_reset_can_clear_operator_persona() -> None:
    character_repository = InMemoryCharacterRepository()
    persona_repo = _StubPersonaRepository()
    service = CharacterService(
        character_repository,
        operator_persona_repository=persona_repo,  # type: ignore[arg-type]
    )
    created = await service.create_character(CreateCharacterRequest(name="Yuki"))

    result = await service.reset_character_data(
        created.id, operator_persona=True,
    )

    assert result == (0, 0, 0, 3)
    assert persona_repo.deleted == [created.id]


@pytest.mark.asyncio
async def test_reset_route_404_for_unknown_character() -> None:
    service, *_ = _build_service()
    client = _client(service)
    response = client.post(
        "/api/v1/characters/ghost/reset",
        json={"memories": True},
    )
    assert response.status_code == 404
