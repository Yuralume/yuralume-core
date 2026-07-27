"""Per-conversation turn mutex on the chat hot path.

Before this guard existed, two overlapping turns on one conversation each
assembled their prompt from a pre-turn snapshot, each ran the LLM, and each ran
the whole post-turn pipeline — the character answered the same moment twice and
the transcript could reflow out of order. The DB layer never lost data, so
nothing failed loudly; the damage was purely semantic.

These tests drive the failure shape directly: two ChatService instances sharing
one conversation repository and one lease backend, standing in for the hosted
2×api round-robin, plus the single-process case that the same mechanism fixes.
"""

import asyncio

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.dto.chat import SendChatMessageRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.application.services.chat_turn_lease import (
    ChatTurnLease,
    ConversationBusyError,
)
from kokoro_link.domain.entities.conversation import MessageRole
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.post_turn.null_processor import NullPostTurnProcessor
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryBackgroundCoordinatorLease,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.state.simple import SimpleStateEngine

pytestmark = pytest.mark.asyncio


class _GatedChatModel(FakeChatModel):
    """Chat model that parks inside ``generate`` until released.

    Lets a test hold turn A inside its LLM call while turn B arrives, which is
    the only window where the two turns actually overlap.
    """

    def __init__(self, provider_id: str = "fake") -> None:
        super().__init__(provider_id)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def generate(self, prompt: str, **kwargs) -> str:  # noqa: ANN003
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return await super().generate(prompt, **kwargs)

    async def generate_stream(self, prompt: str, **kwargs):  # noqa: ANN003
        text = await self.generate(prompt, **kwargs)
        yield text


class _ExplodingChatModel(FakeChatModel):
    """Succeeds for the seed turn, then fails every later call."""

    def __init__(self, provider_id: str = "fake", *, fail_after: int = 1) -> None:
        super().__init__(provider_id)
        self.calls = 0
        self._fail_after = fail_after

    async def generate(self, prompt: str, **kwargs) -> str:  # noqa: ANN003
        self.calls += 1
        if self.calls > self._fail_after:
            raise RuntimeError("upstream exploded")
        return await super().generate(prompt, **kwargs)

    async def generate_stream(self, prompt: str, **kwargs):  # noqa: ANN003
        yield await self.generate(prompt, **kwargs)


class _Fixture:
    """Two ChatService replicas over one shared conversation store."""

    def __init__(self, *, model, lease_backend, with_lease: bool) -> None:
        self.characters = InMemoryCharacterRepository()
        self.conversations = InMemoryConversationRepository()
        self.model = model
        self._memories = InMemoryMemoryRepository()
        self._lease_backend = lease_backend
        self._with_lease = with_lease
        self.character_service = CharacterService(
            self.characters,
            conversation_repository=self.conversations,
            memory_repository=self._memories,
        )

    def replica(self) -> ChatService:
        registry = InMemoryChatModelRegistry(default_provider_id="fake")
        registry.register(self.model)
        return ChatService(
            character_repository=self.characters,
            conversation_repository=self.conversations,
            memory_repository=self._memories,
            post_turn_processor=NullPostTurnProcessor(),
            prompt_context_builder=DefaultPromptContextBuilder(),
            model_registry=registry,
            state_engine=SimpleStateEngine(),
            turn_lease=(
                ChatTurnLease(self._lease_backend) if self._with_lease else None
            ),
        )


def _build(*, model=None, with_lease: bool = True) -> _Fixture:
    return _Fixture(
        model=model or _GatedChatModel(),
        # ONE backend shared by both replicas — the distributed lease table.
        lease_backend=InMemoryBackgroundCoordinatorLease(),
        with_lease=with_lease,
    )


async def _seed_conversation(fixture: _Fixture, service: ChatService) -> tuple[str, str]:
    """Create a character and open its conversation with one settled turn."""
    created = await fixture.character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )
    if isinstance(fixture.model, _GatedChatModel):
        fixture.model.release.set()
    first = await service.send_message(
        SendChatMessageRequest(character_id=created.id, message="第一句"),
    )
    if isinstance(fixture.model, _GatedChatModel):
        fixture.model.release.clear()
        fixture.model.entered.clear()
        # Ignore the seed turn: assertions below count only the raced turns.
        fixture.model.calls = 0
    return created.id, first.conversation_id


async def _concurrent_turns(
    fixture: _Fixture,
    replica_a: ChatService,
    replica_b: ChatService,
    character_id: str,
    conversation_id: str,
) -> list:
    """Fire turn A, wait until it is inside the LLM, then fire turn B."""
    task_a = asyncio.create_task(
        replica_a.send_message(
            SendChatMessageRequest(
                character_id=character_id,
                conversation_id=conversation_id,
                message="A 的訊息",
            ),
        ),
    )
    await asyncio.wait_for(fixture.model.entered.wait(), timeout=5)
    task_b = asyncio.create_task(
        replica_b.send_message(
            SendChatMessageRequest(
                character_id=character_id,
                conversation_id=conversation_id,
                message="B 的訊息",
            ),
        ),
    )
    # Let B reach (and fail) the lease before A is allowed to finish, so the
    # test proves exclusion rather than accidental serialization.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    fixture.model.release.set()
    return await asyncio.gather(task_a, task_b, return_exceptions=True)


# --- cross-replica exclusion -------------------------------------------------


async def test_concurrent_turns_across_replicas_reject_the_second() -> None:
    fixture = _build()
    replica_a = fixture.replica()
    replica_b = fixture.replica()
    character_id, conversation_id = await _seed_conversation(fixture, replica_a)

    results = await _concurrent_turns(
        fixture, replica_a, replica_b, character_id, conversation_id,
    )

    winners = [r for r in results if not isinstance(r, BaseException)]
    busy = [r for r in results if isinstance(r, ConversationBusyError)]
    assert len(winners) == 1, results
    assert len(busy) == 1, results
    assert busy[0].conversation_id == conversation_id
    assert busy[0].code == "conversation_busy"
    # The rejected turn never reached the model and never wrote a user row.
    assert fixture.model.calls == 1
    conversation = await fixture.conversations.get(conversation_id)
    assert conversation is not None
    user_texts = [
        m.content for m in conversation.messages if m.role == MessageRole.USER
    ]
    assert user_texts == ["第一句", "A 的訊息"]


async def test_same_replica_concurrency_is_also_rejected() -> None:
    """Self-host is a single process — the in-process lease must still exclude."""
    fixture = _build()
    replica = fixture.replica()
    character_id, conversation_id = await _seed_conversation(fixture, replica)

    results = await _concurrent_turns(
        fixture, replica, replica, character_id, conversation_id,
    )

    assert len([r for r in results if isinstance(r, ConversationBusyError)]) == 1
    assert fixture.model.calls == 1


async def test_without_a_lease_both_turns_run_as_before() -> None:
    """Regression fence: ``turn_lease=None`` keeps the historical behaviour."""
    fixture = _build(with_lease=False)
    replica_a = fixture.replica()
    replica_b = fixture.replica()
    character_id, conversation_id = await _seed_conversation(fixture, replica_a)

    results = await _concurrent_turns(
        fixture, replica_a, replica_b, character_id, conversation_id,
    )

    assert all(not isinstance(r, BaseException) for r in results), results
    assert fixture.model.calls == 2


async def test_lease_is_released_so_the_next_turn_succeeds() -> None:
    fixture = _build()
    replica_a = fixture.replica()
    replica_b = fixture.replica()
    character_id, conversation_id = await _seed_conversation(fixture, replica_a)
    fixture.model.release.set()

    await replica_a.send_message(
        SendChatMessageRequest(
            character_id=character_id,
            conversation_id=conversation_id,
            message="第一輪",
        ),
    )
    # A different replica must be able to claim the same conversation next.
    reply = await replica_b.send_message(
        SendChatMessageRequest(
            character_id=character_id,
            conversation_id=conversation_id,
            message="第二輪",
        ),
    )
    assert reply.assistant_message is not None


async def test_lease_is_released_when_the_turn_raises() -> None:
    fixture = _build(model=_ExplodingChatModel(provider_id="fake"))
    replica_a = fixture.replica()
    replica_b = fixture.replica()
    character_id, conversation_id = await _seed_conversation(fixture, replica_a)

    with pytest.raises(RuntimeError, match="upstream exploded"):
        await replica_a.send_message(
            SendChatMessageRequest(
                character_id=character_id,
                conversation_id=conversation_id,
                message="會爆炸",
            ),
        )
    # A failed turn must not strand the conversation until the TTL lapses.
    with pytest.raises(RuntimeError, match="upstream exploded"):
        await replica_b.send_message(
            SendChatMessageRequest(
                character_id=character_id,
                conversation_id=conversation_id,
                message="再試一次",
            ),
        )


# --- streaming: the lease spans start → finalizer ----------------------------


async def _drain(token_stream) -> str:  # noqa: ANN001
    return "".join([chunk async for chunk in token_stream])


async def test_streaming_turn_blocks_a_concurrent_turn_until_finish() -> None:
    fixture = _build()
    replica_a = fixture.replica()
    replica_b = fixture.replica()
    character_id, conversation_id = await _seed_conversation(fixture, replica_a)
    fixture.model.release.set()

    token_stream, finalizer = await replica_a.send_message_stream(
        SendChatMessageRequest(
            character_id=character_id,
            conversation_id=conversation_id,
            message="串流中",
        ),
    )
    # Stream started, assistant reply not yet persisted → still busy.
    with pytest.raises(ConversationBusyError):
        await replica_b.send_message(
            SendChatMessageRequest(
                character_id=character_id,
                conversation_id=conversation_id,
                message="插隊",
            ),
        )

    text = await _drain(token_stream)
    await finalizer.finish(text)

    # Finalizer released → the next turn is accepted.
    reply = await replica_b.send_message(
        SendChatMessageRequest(
            character_id=character_id,
            conversation_id=conversation_id,
            message="輪到我",
        ),
    )
    assert reply.assistant_message is not None


async def test_streaming_lease_released_on_abandoned_stream() -> None:
    """A stream that never reaches ``finish`` must still free the lease."""
    fixture = _build()
    replica_a = fixture.replica()
    replica_b = fixture.replica()
    character_id, conversation_id = await _seed_conversation(fixture, replica_a)
    fixture.model.release.set()

    token_stream, finalizer = await replica_a.send_message_stream(
        SendChatMessageRequest(
            character_id=character_id,
            conversation_id=conversation_id,
            message="會中斷",
        ),
    )
    await token_stream.aclose()
    await finalizer.release_turn_lease()

    reply = await replica_b.send_message(
        SendChatMessageRequest(
            character_id=character_id,
            conversation_id=conversation_id,
            message="接手",
        ),
    )
    assert reply.assistant_message is not None


async def test_streaming_lease_released_when_start_raises() -> None:
    fixture = _build(model=_ExplodingChatModel(provider_id="fake"))
    replica_a = fixture.replica()
    replica_b = fixture.replica()
    character_id, conversation_id = await _seed_conversation(fixture, replica_a)

    with pytest.raises(RuntimeError, match="upstream exploded"):
        await replica_a.send_message_stream(
            SendChatMessageRequest(
                character_id=character_id,
                conversation_id=conversation_id,
                message="開場就炸",
            ),
        )
    with pytest.raises(RuntimeError, match="upstream exploded"):
        await replica_b.send_message_stream(
            SendChatMessageRequest(
                character_id=character_id,
                conversation_id=conversation_id,
                message="再來一次",
            ),
        )


async def test_heartbeat_stops_at_max_lifetime_so_a_dropped_turn_decays() -> None:
    """A turn whose owner is dropped must not renew its lease forever."""
    backend = InMemoryBackgroundCoordinatorLease()
    lease = ChatTurnLease(
        backend,
        ttl_seconds=1,
        heartbeat_interval_seconds=0.01,
        max_lifetime_seconds=0.03,
    )
    session = await lease.acquire("conv-dropped")

    await asyncio.sleep(0.15)

    assert session.lost is False  # still the owner, just no longer renewing
    owner, _epoch, lease_until = await backend.current(
        "chat:conversation:conv-dropped",
    )
    assert owner  # untouched by anyone else
    # Renewals stopped: the recorded expiry is no longer being pushed out.
    await asyncio.sleep(0.05)
    _owner2, _epoch2, lease_until_later = await backend.current(
        "chat:conversation:conv-dropped",
    )
    assert lease_until_later == lease_until
    await session.__aexit__(None, None, None)


async def test_release_turn_lease_is_idempotent() -> None:
    fixture = _build()
    replica = fixture.replica()
    character_id, conversation_id = await _seed_conversation(fixture, replica)
    fixture.model.release.set()

    token_stream, finalizer = await replica.send_message_stream(
        SendChatMessageRequest(
            character_id=character_id,
            conversation_id=conversation_id,
            message="重複釋放",
        ),
    )
    text = await _drain(token_stream)
    await finalizer.finish(text)
    await finalizer.release_turn_lease()
    await finalizer.release_turn_lease()

    reply = await replica.send_message(
        SendChatMessageRequest(
            character_id=character_id,
            conversation_id=conversation_id,
            message="仍然可用",
        ),
    )
    assert reply.assistant_message is not None
