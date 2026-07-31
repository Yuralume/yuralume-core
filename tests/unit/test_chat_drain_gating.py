"""Chat turns under a draining replica (GD1-A).

``HOSTED_PROMPT_PACK_DISTRIBUTION_PLAN`` §7.1 lists what a mid-turn SIGKILL
costs: a 502 for the player, a conversation locked until the 180s lease TTL, and
credits reserved until the stale-reservation sweeper finds them. GD1 buys the
deploy a window in which none of that has to happen, and that window rests on
exactly two promises this file pins:

* a turn that *starts* on a draining replica costs nothing — refused before the
  charge, before the lease, before a single row is written. It is the
  belt-and-suspenders half (the router has already stopped routing here), so the
  bar is "leaves no trace", not "handled gracefully";
* a turn already *running* is untouched by the drain and is still counted while
  it runs. ``active_turns`` is what ``deploy.sh`` polls to zero before it
  recreates the container, so a count that under-reported would hand the deploy
  permission to kill live work — which is precisely the bug GD exists to fix.

The exception and abandoned-stream paths get their own tests because those are
the ones a ``finally`` is easy to get wrong on, and a leaked count does not fail
loudly: it silently spends the deploy's entire 180s budget.
"""

from __future__ import annotations

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
from kokoro_link.application.services.cloud_action_billing_service import (
    NullActionBillingService,
)
from kokoro_link.application.services.drain_state import (
    SERVER_DRAINING_CODE,
    DrainState,
    ServerDrainingError,
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

_LEASE_PREFIX = "chat:conversation:"


class _GatedChatModel(FakeChatModel):
    """Parks inside ``generate`` until released — the only way to hold a turn
    in flight while the test drains the replica underneath it."""

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
        yield await self.generate(prompt, **kwargs)


class _ExplodingChatModel(FakeChatModel):
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


class _CountingBilling(NullActionBillingService):
    """Charges nothing, but remembers whether anyone tried.

    A refused turn must not have opened a charge: under AP fixed pricing the
    reserve is immediate, so a charge opened here and abandoned by the SIGKILL
    is money parked on the player's wallet until a sweeper releases it.
    """

    def __init__(self) -> None:
        self.begins = 0
        self.actions = 0

    async def begin(self, action_key: str, **kwargs):  # noqa: ANN003
        self.begins += 1
        return await super().begin(action_key, **kwargs)

    def action(self, action_key: str, **kwargs):  # noqa: ANN003
        self.actions += 1
        return super().action(action_key, **kwargs)


class _Fixture:
    """One replica's worth of chat wiring over shared in-memory stores."""

    def __init__(self, *, model) -> None:  # noqa: ANN001
        self.characters = InMemoryCharacterRepository()
        self.conversations = InMemoryConversationRepository()
        self.model = model
        self.drain = DrainState()
        self.billing = _CountingBilling()
        self.lease_backend = InMemoryBackgroundCoordinatorLease()
        self._memories = InMemoryMemoryRepository()
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
            turn_lease=ChatTurnLease(self.lease_backend),
            drain_state=self.drain,
            action_billing=self.billing,
        )


def _build(*, model=None) -> _Fixture:  # noqa: ANN001
    return _Fixture(model=model or _GatedChatModel())


async def _seed(fixture: _Fixture, service: ChatService) -> tuple[str, str]:
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
        fixture.model.calls = 0
    fixture.billing.begins = 0
    fixture.billing.actions = 0
    return created.id, first.conversation_id


async def _drain_stream(token_stream) -> str:  # noqa: ANN001
    return "".join([chunk async for chunk in token_stream])


# --- refusing a new turn costs the player nothing ---------------------------


async def test_draining_replica_refuses_a_new_turn_with_zero_effects() -> None:
    fixture = _build()
    replica = fixture.replica()
    character_id, conversation_id = await _seed(fixture, replica)
    fixture.model.release.set()
    lease_name = f"{_LEASE_PREFIX}{conversation_id}"
    lease_before = await fixture.lease_backend.current(lease_name)

    fixture.drain.begin_drain()

    with pytest.raises(ServerDrainingError) as caught:
        await replica.send_message(
            SendChatMessageRequest(
                character_id=character_id,
                conversation_id=conversation_id,
                message="來不及了",
            ),
        )

    assert caught.value.code == SERVER_DRAINING_CODE
    # No model call, no charge, no lease, no persisted row: the refusal happens
    # ahead of every one of them, so a request that slipped past the router
    # leaves this replica exactly as it found it.
    assert fixture.model.calls == 0
    assert fixture.billing.begins == 0
    assert fixture.billing.actions == 0
    # Untouched, epoch included: no acquire was even attempted, so the
    # conversation is free for whichever replica the player lands on next.
    assert await fixture.lease_backend.current(lease_name) == lease_before
    conversation = await fixture.conversations.get(conversation_id)
    assert [
        m.content for m in conversation.messages if m.role == MessageRole.USER
    ] == ["第一句"]
    assert fixture.drain.active_turns == 0


async def test_draining_replica_refuses_a_new_streaming_turn_too() -> None:
    fixture = _build()
    replica = fixture.replica()
    character_id, conversation_id = await _seed(fixture, replica)
    fixture.model.release.set()

    fixture.drain.begin_drain()

    with pytest.raises(ServerDrainingError):
        await replica.send_message_stream(
            SendChatMessageRequest(
                character_id=character_id,
                conversation_id=conversation_id,
                message="串流也不行",
            ),
        )

    assert fixture.model.calls == 0
    assert fixture.billing.begins == 0
    assert fixture.drain.active_turns == 0


async def test_not_draining_is_the_historical_behaviour() -> None:
    """The self-host red line: a wired-but-idle drain state changes nothing."""
    fixture = _build()
    replica = fixture.replica()
    character_id, conversation_id = await _seed(fixture, replica)
    fixture.model.release.set()

    reply = await replica.send_message(
        SendChatMessageRequest(
            character_id=character_id,
            conversation_id=conversation_id,
            message="一切如常",
        ),
    )

    assert reply.assistant_message is not None
    assert fixture.drain.active_turns == 0
    assert fixture.drain.draining is False


async def test_a_service_without_drain_wiring_never_refuses() -> None:
    fixture = _build()
    unwired = ChatService(
        character_repository=fixture.characters,
        conversation_repository=fixture.conversations,
        memory_repository=fixture._memories,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=InMemoryChatModelRegistry(default_provider_id="fake"),
        state_engine=SimpleStateEngine(),
    )
    unwired._model_registry.register(fixture.model)
    character_id, conversation_id = await _seed(fixture, unwired)
    fixture.model.release.set()
    fixture.drain.begin_drain()

    reply = await unwired.send_message(
        SendChatMessageRequest(
            character_id=character_id,
            conversation_id=conversation_id,
            message="沒接 drain 就照舊",
        ),
    )

    assert reply.assistant_message is not None


# --- in-flight turns are counted, and untouched by the drain ----------------


async def test_in_flight_turn_is_counted_and_survives_the_drain() -> None:
    fixture = _build()
    replica = fixture.replica()
    character_id, conversation_id = await _seed(fixture, replica)

    task = asyncio.create_task(
        replica.send_message(
            SendChatMessageRequest(
                character_id=character_id,
                conversation_id=conversation_id,
                message="進行中",
            ),
        ),
    )
    await asyncio.wait_for(fixture.model.entered.wait(), timeout=5)
    assert fixture.drain.active_turns == 1

    # The deploy drains WHILE this turn is inside the LLM call. Draining must
    # neither abort it nor hide it — the whole point is that the deploy waits.
    fixture.drain.begin_drain()
    assert fixture.drain.active_turns == 1

    fixture.model.release.set()
    reply = await asyncio.wait_for(task, timeout=5)

    assert reply.assistant_message is not None
    assert fixture.drain.active_turns == 0


async def test_active_turns_returns_to_zero_when_a_turn_raises() -> None:
    fixture = _build(model=_ExplodingChatModel(provider_id="fake"))
    replica = fixture.replica()
    character_id, conversation_id = await _seed(fixture, replica)

    with pytest.raises(RuntimeError, match="upstream exploded"):
        await replica.send_message(
            SendChatMessageRequest(
                character_id=character_id,
                conversation_id=conversation_id,
                message="會爆炸",
            ),
        )

    # A leaked count would make the deploy wait out its full 180s cap and then
    # kill the replica anyway — the failure mode is silent, hence the test.
    assert fixture.drain.active_turns == 0


async def test_a_turn_rejected_as_busy_gives_its_count_back() -> None:
    """Busy turns are counted while they run, and only while they run.

    Admission now happens before the lease is even attempted (it has to — the
    lease acquire is an await, and anything before the count is invisible to
    the deploy), so a turn that is about to be rejected as ``conversation_busy``
    is briefly counted. That is honest: it is in-flight work on this replica.
    What must not happen is a leak, which would leave the gauge permanently
    above zero and burn the deploy's whole 180s budget.
    """
    fixture = _build()
    replica_a = fixture.replica()
    replica_b = fixture.replica()
    character_id, conversation_id = await _seed(fixture, replica_a)

    task_a = asyncio.create_task(
        replica_a.send_message(
            SendChatMessageRequest(
                character_id=character_id,
                conversation_id=conversation_id,
                message="A",
            ),
        ),
    )
    await asyncio.wait_for(fixture.model.entered.wait(), timeout=5)
    with pytest.raises(ConversationBusyError):
        await replica_b.send_message(
            SendChatMessageRequest(
                character_id=character_id,
                conversation_id=conversation_id,
                message="B",
            ),
        )

    # Back to just the turn that actually holds the conversation.
    assert fixture.drain.active_turns == 1
    fixture.model.release.set()
    await asyncio.wait_for(task_a, timeout=5)
    assert fixture.drain.active_turns == 0


# --- the prelude window (C2): admitted and counted, or refused --------------


async def test_the_prelude_is_counted_from_the_admission_decision() -> None:
    """A turn is visible to the gauge before it has cost anything.

    The window this pins used to be wide open: the drain check sat on the first
    line of the prelude but the count was taken several awaits later, after the
    character load, the action charge and the lease acquire. A deploy polling
    ``active_turns`` during that stretch read ``0``, recreated the container,
    and SIGKILLed a turn that was about to reserve credits and claim a
    conversation — the exact loss GD exists to prevent, caused by the
    machinery meant to prevent it.
    """
    fixture = _build()
    replica = fixture.replica()
    character_id, conversation_id = await _seed(fixture, replica)
    fixture.model.release.set()
    observed: list[int] = []
    original = replica._load_character_with_recovery

    async def _spy(cid: str):  # noqa: ANN202
        # The deploy's poll lands here: one await into the prelude, before the
        # charge and before the lease.
        observed.append(fixture.drain.active_turns)
        fixture.drain.begin_drain()
        return await original(cid)

    replica._load_character_with_recovery = _spy

    reply = await replica.send_message(
        SendChatMessageRequest(
            character_id=character_id,
            conversation_id=conversation_id,
            message="剛好卡在前置",
        ),
    )

    assert observed == [1]
    # And draining mid-prelude does not retroactively refuse an admitted turn:
    # drain stops new turns, it never aborts one that is already in.
    assert reply.assistant_message is not None
    assert fixture.drain.active_turns == 0


async def test_the_streaming_prelude_is_counted_too() -> None:
    fixture = _build()
    replica = fixture.replica()
    character_id, conversation_id = await _seed(fixture, replica)
    fixture.model.release.set()
    observed: list[int] = []
    original = replica._load_character_with_recovery

    async def _spy(cid: str):  # noqa: ANN202
        observed.append(fixture.drain.active_turns)
        fixture.drain.begin_drain()
        return await original(cid)

    replica._load_character_with_recovery = _spy

    token_stream, finalizer = await replica.send_message_stream(
        SendChatMessageRequest(
            character_id=character_id,
            conversation_id=conversation_id,
            message="串流也卡在前置",
        ),
    )

    assert observed == [1]
    assert fixture.drain.active_turns == 1
    await finalizer.finish(await _drain_stream(token_stream))
    assert fixture.drain.active_turns == 0


async def test_a_prelude_that_fails_gives_the_count_back() -> None:
    """Every exit from the prelude releases — including the ones before a lease.

    ``character not found`` is the cheapest of them and the easiest to leak:
    it raises three lines into the prelude, long before any of the try/finally
    scaffolding that guards the turn body.
    """
    fixture = _build()
    replica = fixture.replica()
    await _seed(fixture, replica)

    with pytest.raises(ValueError, match="Character not found"):
        await replica.send_message(
            SendChatMessageRequest(character_id="nope", message="誰?"),
        )

    assert fixture.drain.active_turns == 0

    with pytest.raises(ValueError, match="Character not found"):
        await replica.send_message_stream(
            SendChatMessageRequest(character_id="nope", message="誰?"),
        )

    assert fixture.drain.active_turns == 0


# --- streaming: the count spans start → finalizer ---------------------------


async def test_streaming_turn_stays_counted_until_the_finalizer_lands() -> None:
    fixture = _build()
    replica = fixture.replica()
    character_id, conversation_id = await _seed(fixture, replica)
    fixture.model.release.set()

    token_stream, finalizer = await replica.send_message_stream(
        SendChatMessageRequest(
            character_id=character_id,
            conversation_id=conversation_id,
            message="串流中",
        ),
    )
    # The assistant message has not landed yet: killing the replica here is
    # exactly the loss GD prevents, so the gauge must still say 1.
    assert fixture.drain.active_turns == 1

    text = await _drain_stream(token_stream)
    await finalizer.finish(text)

    assert fixture.drain.active_turns == 0


async def test_abandoned_stream_releases_the_count() -> None:
    fixture = _build()
    replica = fixture.replica()
    character_id, conversation_id = await _seed(fixture, replica)
    fixture.model.release.set()

    token_stream, finalizer = await replica.send_message_stream(
        SendChatMessageRequest(
            character_id=character_id,
            conversation_id=conversation_id,
            message="會中斷",
        ),
    )
    await token_stream.aclose()
    await finalizer.release_turn_lease()

    assert fixture.drain.active_turns == 0
    # Idempotent, because the route releases in a ``finally`` on top of the
    # finalizer's own release. A double decrement would under-report.
    await finalizer.release_turn_lease()
    assert fixture.drain.active_turns == 0


async def test_streaming_start_failure_releases_the_count() -> None:
    fixture = _build(model=_ExplodingChatModel(provider_id="fake"))
    replica = fixture.replica()
    character_id, conversation_id = await _seed(fixture, replica)

    with pytest.raises(RuntimeError, match="upstream exploded"):
        await replica.send_message_stream(
            SendChatMessageRequest(
                character_id=character_id,
                conversation_id=conversation_id,
                message="開場就炸",
            ),
        )

    assert fixture.drain.active_turns == 0


async def test_finish_releases_the_count_even_when_it_raises() -> None:
    fixture = _build()
    replica = fixture.replica()
    character_id, conversation_id = await _seed(fixture, replica)
    fixture.model.release.set()

    token_stream, finalizer = await replica.send_message_stream(
        SendChatMessageRequest(
            character_id=character_id,
            conversation_id=conversation_id,
            message="收尾會炸",
        ),
    )
    await _drain_stream(token_stream)

    async def _boom(_text: str):
        raise RuntimeError("finalize exploded")

    finalizer._finish_turn = _boom

    with pytest.raises(RuntimeError, match="finalize exploded"):
        await finalizer.finish("whatever")

    assert fixture.drain.active_turns == 0
