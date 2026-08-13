"""BDD for the identity-drift 'reset memory/conversation' escape hatch.

The operator flips a character's personality halfway through a campaign
and wants the old memories & chat log gone so the new persona can't be
pulled back by stale content. The reset endpoint is the one-call way to
do that without deleting the character itself.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, func, insert, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

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
from kokoro_link.infrastructure.persistence.engine import build_session_factory
from kokoro_link.infrastructure.persistence.models import Base
from kokoro_link.infrastructure.persistence.sa_character_repository import (
    SACharacterRepository,
)
from kokoro_link.infrastructure.persistence.sa_character_reset_eraser import (
    SACharacterResetEraser,
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
async def test_reset_clears_conversations_only() -> None:
    service, memory_repo, conversation_repo, history_repo = _build_service()
    character_id = await _seed_data(service, memory_repo, conversation_repo, history_repo)

    result = await service.reset_character_data(
        character_id, conversations=True,
    )

    assert result == (0, 1, 0, 0)
    assert await conversation_repo.latest_for_character(
        character_id, source=None,
    ) is None
    # Memories + history untouched.
    assert await memory_repo.count_for_character(character_id) == 2
    assert await history_repo.query(character_id, limit=10)


@pytest.mark.asyncio
async def test_reset_clears_state_history_only() -> None:
    service, memory_repo, conversation_repo, history_repo = _build_service()
    character_id = await _seed_data(service, memory_repo, conversation_repo, history_repo)

    result = await service.reset_character_data(
        character_id, state_history=True,
    )

    assert result == (0, 0, 1, 0)
    assert await history_repo.query(character_id, limit=10) == []
    # Memories + conversation untouched.
    assert await memory_repo.count_for_character(character_id) == 2
    assert await conversation_repo.latest_for_character(
        character_id, source=None,
    ) is not None


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


# ======================================================================
# SA-backed (CD3): the registry-driven ``SACharacterResetEraser``, not
# the in-memory repository fallback the tests above exercise. The point
# of this section is the family CD0 documented but never ran against a
# real schema: ``conversations=True`` must take turn_journals /
# pending_follow_ups / story_scene_sessions with it and null out
# channel_bindings.conversation_id.
#
# **Every case runs twice, with and without ``PRAGMA foreign_keys``.**
# Production SQLite (self-host) never sets it — ``persistence/engine.py``
# has no such listener — so pinning these effects only under the pragma
# was pinning a cascade real deployments do not perform: a self-host
# ``reset(conversations=True)`` left every message, turn journal and
# unfulfilled promise behind, pointing at a conversation that no longer
# existed, while this file stayed green. The eraser must therefore state
# those effects itself, and both rigs must agree.
# ======================================================================

OWNER = "owner-1"
TARGET_ID = "char-target"
OTHER_ID = "char-other"


def _aware_now() -> datetime:
    return datetime.now(timezone.utc)


@pytest_asyncio.fixture(
    params=[False, True], ids=["no-fk-pragma", "fk-pragma"],
)
async def sa_world(request):  # noqa: ANN201
    fk_enforced = request.param
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    if fk_enforced:
        @event.listens_for(engine.sync_engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _record):  # noqa: ANN202
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        seeded = await _seed_sa_world(conn)
    session_factory = build_session_factory(engine)
    try:
        yield _SaWorld(session_factory, seeded, fk_enforced)
    finally:
        await engine.dispose()


class _SaWorld:
    def __init__(self, session_factory, seeded, fk_enforced) -> None:
        self.session_factory = session_factory
        self.seeded = seeded
        self.fk_enforced = fk_enforced

    def service(self, reset_eraser=None) -> CharacterService:
        return CharacterService(
            SACharacterRepository(self.session_factory),
            character_data_reset=reset_eraser or SACharacterResetEraser(
                self.session_factory,
            ),
        )

    async def count(self, table_name: str, character_id: str) -> int:
        table = Base.metadata.tables[table_name]
        if table_name == "characters":
            predicate = table.c.id == character_id
        elif table_name == "messages":
            # ``messages`` has no ``character_id`` column of its own —
            # reach the character through the conversation it belongs to,
            # exactly like ``character_predicate``'s ``parent_table`` path.
            conversations = Base.metadata.tables["conversations"]
            predicate = table.c.conversation_id.in_(
                select(conversations.c.id).where(
                    conversations.c.character_id == character_id,
                ),
            )
        else:
            predicate = table.c.character_id == character_id
        async with self.session_factory() as session:
            return int((await session.execute(
                select(func.count()).select_from(table).where(predicate),
            )).scalar_one() or 0)

    async def scalar(self, stmt):
        async with self.session_factory() as session:
            row = (await session.execute(stmt)).first()
            return row[0] if row is not None else None


async def _seed_character(conn, character_id: str) -> dict[str, str]:
    """One character's full ``CONVERSATIONS`` family plus a memory /
    state-history / operator-persona row.

    Seeded identically for the target and the bystander: the explicit
    statements the eraser now emits scope themselves with an ``IN
    (SELECT … FROM parent)`` subquery, and a subquery that forgot its
    character predicate would be invisible against an empty bystander.
    """
    now = _aware_now()
    t = Base.metadata.tables

    conversation_id = uuid.uuid4().hex
    await conn.execute(
        insert(t["conversations"]).values(
            id=conversation_id, character_id=character_id, source="web",
        ),
    )
    await conn.execute(
        insert(t["messages"]).values(
            conversation_id=conversation_id, position=0, role="user",
            content="hi",
        ),
    )
    await conn.execute(
        insert(t["turn_journals"]).values(
            id=uuid.uuid4().hex, conversation_id=conversation_id,
            character_id=character_id, turn_index=0, created_at=now,
            payload_json="{}",
        ),
    )
    await conn.execute(
        insert(t["pending_follow_ups"]).values(
            id=uuid.uuid4().hex, character_id=character_id,
            conversation_id=conversation_id, brief_reply="soon",
            messages_json="[]", scheduled_for=now, queued_at=now,
            updated_at=now,
        ),
    )
    await conn.execute(
        insert(t["story_scene_sessions"]).values(
            id=uuid.uuid4().hex, character_id=character_id,
            conversation_id=conversation_id, status="open",
            source_layer="arc", opened_at=now, last_activity_at=now,
        ),
    )
    account_id = uuid.uuid4().hex
    await conn.execute(
        insert(t["messaging_accounts"]).values(
            id=account_id, character_id=character_id, platform="line",
            webhook_slug=uuid.uuid4().hex, created_at=now, updated_at=now,
        ),
    )
    channel_binding_id = uuid.uuid4().hex
    await conn.execute(
        insert(t["channel_bindings"]).values(
            id=channel_binding_id, account_id=account_id,
            chat_ref="chat-1", conversation_id=conversation_id,
            created_at=now, updated_at=now,
        ),
    )
    await conn.execute(
        insert(t["memory_items"]).values(
            id=uuid.uuid4().hex, character_id=character_id, kind="semantic",
            content="舊設定的痕跡", created_at=now,
        ),
    )
    await conn.execute(
        insert(t["state_snapshots"]).values(
            id=uuid.uuid4().hex, character_id=character_id, source="manual",
            emotion="calm", affection=10, fatigue=0, trust=10, energy=100,
            created_at=now,
        ),
    )
    await conn.execute(
        insert(t["operator_profile_fields"]).values(
            id=uuid.uuid4().hex, character_id=character_id, operator_id=OWNER,
            layer=1, field_key="name", value="someone", confidence=0.9,
            created_at=now, updated_at=now,
        ),
    )
    return {
        "conversation_id": conversation_id,
        "account_id": account_id,
        "channel_binding_id": channel_binding_id,
    }


async def _seed_sa_world(conn) -> dict[str, str]:
    """Two equally-populated characters — the reset target and a
    bystander whose every row must survive every reset."""
    now = _aware_now()
    t = Base.metadata.tables

    await conn.execute(
        insert(t["operator_profiles"]).values(
            id=OWNER, display_name="Owner", created_at=now, updated_at=now,
        ),
    )
    for character_id in (TARGET_ID, OTHER_ID):
        await conn.execute(
            insert(t["characters"]).values(
                id=character_id, user_id=OWNER, name=character_id,
                image_urls="[]",
            ),
        )

    target = await _seed_character(conn, TARGET_ID)
    other = await _seed_character(conn, OTHER_ID)
    return {
        **target,
        "other_conversation_id": other["conversation_id"],
        "other_binding_id": other["channel_binding_id"],
    }


@pytest.mark.asyncio
async def test_sa_world_rigs_really_differ_on_fk_enforcement(
    sa_world,
) -> None:
    """Guard the parametrisation: the ``no-fk-pragma`` rig must genuinely
    have no cascade, or the pins below go back to testing SQLite."""
    async with sa_world.session_factory() as session:
        pragma = await session.execute(text("PRAGMA foreign_keys"))
        assert pragma.scalar_one() == (1 if sa_world.fk_enforced else 0)


@pytest.mark.asyncio
async def test_sa_reset_conversations_cascades_and_reports_conversations_only(
    sa_world,
) -> None:
    result = await sa_world.service().reset_character_data(
        TARGET_ID, conversations=True,
    )

    # One conversation row purged, reported — the messages/turn_journals/
    # pending_follow_ups/story_scene_sessions family is invisible in the
    # count on purpose (pre-CD3 semantics, unchanged).
    assert result == (0, 1, 0, 0)
    for table_name in (
        "conversations", "messages", "turn_journals",
        "pending_follow_ups", "story_scene_sessions",
    ):
        assert await sa_world.count(table_name, TARGET_ID) == 0, table_name


@pytest.mark.asyncio
async def test_sa_reset_conversations_clears_channel_binding_reference_only(
    sa_world,
) -> None:
    channel_bindings = Base.metadata.tables["channel_bindings"]
    await sa_world.service().reset_character_data(
        TARGET_ID, conversations=True,
    )

    binding_id = await sa_world.scalar(
        select(channel_bindings.c.id).where(
            channel_bindings.c.id == sa_world.seeded["channel_binding_id"],
        ),
    )
    conversation_pointer = await sa_world.scalar(
        select(channel_bindings.c.conversation_id).where(
            channel_bindings.c.id == sa_world.seeded["channel_binding_id"],
        ),
    )
    # The binding row survives; only its conversation pointer is nulled.
    # ``CLEAR_REFERENCE`` is the one policy where an over-eager fix would
    # be worse than the bug: emitting a DELETE here would take a live
    # channel binding out during a chat-log reset.
    assert binding_id == sa_world.seeded["channel_binding_id"]
    assert conversation_pointer is None
    # The sibling binding of another character keeps its own pointer.
    other_pointer = await sa_world.scalar(
        select(channel_bindings.c.conversation_id).where(
            channel_bindings.c.id == sa_world.seeded["other_binding_id"],
        ),
    )
    assert other_pointer == sa_world.seeded["other_conversation_id"]


@pytest.mark.asyncio
async def test_sa_reset_memories_state_history_operator_persona(
    sa_world,
) -> None:
    result = await sa_world.service().reset_character_data(
        TARGET_ID, memories=True, state_history=True, operator_persona=True,
    )

    assert result == (1, 0, 1, 1)
    assert await sa_world.count("memory_items", TARGET_ID) == 0
    assert await sa_world.count("state_snapshots", TARGET_ID) == 0
    assert await sa_world.count("operator_profile_fields", TARGET_ID) == 0
    # Conversation-family untouched by these three flags.
    assert await sa_world.count("conversations", TARGET_ID) == 1


@pytest.mark.asyncio
async def test_sa_reset_never_reaches_the_other_character(sa_world) -> None:
    """Over-deletion guard for the parent-scoped subqueries.

    The bystander is seeded exactly like the target, so a statement that
    dropped its character predicate — e.g. deleting every
    ``turn_journals`` row instead of only those hanging off *this*
    character's conversations — fails here rather than in production.
    """
    await sa_world.service().reset_character_data(
        TARGET_ID,
        memories=True, conversations=True, state_history=True,
        operator_persona=True,
    )

    for table_name in (
        "characters", "conversations", "messages", "turn_journals",
        "pending_follow_ups", "story_scene_sessions", "memory_items",
        "state_snapshots", "operator_profile_fields",
    ):
        assert await sa_world.count(table_name, OTHER_ID) == 1, table_name


@pytest.mark.asyncio
async def test_sa_reset_no_flags_touches_nothing(sa_world) -> None:
    result = await sa_world.service().reset_character_data(TARGET_ID)

    assert result == (0, 0, 0, 0)
    assert await sa_world.count("conversations", TARGET_ID) == 1
    assert await sa_world.count("memory_items", TARGET_ID) == 1


# ======================================================================
# A flag whose subsystem the deployment never built stays a no-op.
#
# Pre-CD3 the reset called one repository per flag, so
# ``KOKORO_PERSONA_ENABLED=false`` (persona_repository unbuilt) made
# ``operator_persona=True`` report zero and touch nothing. The SQL eraser
# has no such coupling and would hard-delete ``operator_profile_fields``
# on a deployment that does not otherwise use the persona feature — a
# change to reset's meaning CD3 was explicitly not allowed to make.
# ======================================================================


@pytest.mark.asyncio
async def test_persona_disabled_deployment_keeps_the_flag_a_noop(
    sa_world,
) -> None:
    from kokoro_link.bootstrap.container import _build_character_data_reset

    eraser = _build_character_data_reset(
        db_session_factory=sa_world.session_factory,
        memory_repository=_UnusedRepository(),
        conversation_repository=_UnusedRepository(),
        state_history_repository=_UnusedRepository(),
        # KOKORO_PERSONA_ENABLED=false → the container never builds it.
        operator_persona_repository=None,
    )

    result = await sa_world.service(reset_eraser=eraser).reset_character_data(
        TARGET_ID, memories=True, operator_persona=True,
    )

    # The wired flag still works; the unwired one reports zero (never
    # absent — pre-CD3 reported zero) and leaves its rows alone.
    assert result == (1, 0, 0, 0)
    assert await sa_world.count("memory_items", TARGET_ID) == 0
    assert await sa_world.count("operator_profile_fields", TARGET_ID) == 1


@pytest.mark.asyncio
async def test_persona_enabled_deployment_still_clears_the_flag(
    sa_world,
) -> None:
    """The gate must be availability, not a blanket disable."""
    from kokoro_link.bootstrap.container import _build_character_data_reset

    eraser = _build_character_data_reset(
        db_session_factory=sa_world.session_factory,
        memory_repository=_UnusedRepository(),
        conversation_repository=_UnusedRepository(),
        state_history_repository=_UnusedRepository(),
        operator_persona_repository=_UnusedRepository(),
    )

    result = await sa_world.service(reset_eraser=eraser).reset_character_data(
        TARGET_ID, operator_persona=True,
    )

    assert result == (0, 0, 0, 1)
    assert await sa_world.count("operator_profile_fields", TARGET_ID) == 0


class _UnusedRepository:
    """Stands in for a repository the container built.

    With a session factory present the SQL eraser does the work, so these
    are only ever read as presence/absence — calling one is a wiring bug.
    """

    async def delete_for_character(self, character_id: str) -> int:
        raise AssertionError(
            "the SQL reset eraser must not delegate to a repository",
        )
