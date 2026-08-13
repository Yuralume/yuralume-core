"""AP2 — a chat turn is one action, one charge, one ledger row.

The turn is where the "聊一句扣三筆" problem was visible, so the assertions
here are about the *boundaries*: the charge is taken before the first LLM call,
the interaction scope is live while that call is made (which is what lets the
Gateway treat every hop inside the turn as already paid for), and the charge is
closed exactly once — settled when the assistant message lands, released on
every path where it does not.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.dto.chat import SendChatMessageRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.application.services.cloud_action_billing_service import (
    CloudActionBillingService,
)
from kokoro_link.contracts.cloud_action_billing import ActionCharge
from kokoro_link.contracts.interaction_context import (
    current_interaction,
    mark_interaction_call_served,
)
from kokoro_link.domain.value_objects.account_runtime_profile import (
    BILLING_SHAPE_ACTION_FIXED,
    DEFAULT_ACCOUNT_RUNTIME_PROFILE,
    AccountRuntimeProfile,
)
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.post_turn.null_processor import (
    NullPostTurnProcessor,
)
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.state.simple import SimpleStateEngine

pytestmark = pytest.mark.asyncio

_ACTION_TIER = AccountRuntimeProfile(
    name="standard", billing_shape=BILLING_SHAPE_ACTION_FIXED,
)


class _ScopeWatchingModel(FakeChatModel):
    """Records whether an interaction scope was live during generation."""

    def __init__(self, provider_id: str = "fake", *, explode: bool = False) -> None:
        super().__init__(provider_id)
        self.scopes: list[object] = []
        self._explode = explode

    async def generate(self, prompt: str, **kwargs) -> str:  # noqa: ANN003
        self.scopes.append(current_interaction())
        if self._explode:
            raise RuntimeError("upstream exploded")
        return await super().generate(prompt, **kwargs)

    async def generate_stream(self, prompt: str, **kwargs):  # noqa: ANN003
        yield await self.generate(prompt, **kwargs)


class _ServingModel(_ScopeWatchingModel):
    """Behaves like the real Cloud adapter: reports the call as served.

    ``CloudGatewayChatModel`` marks the interaction the moment the Gateway
    answers, because from then on the tokens are spent whatever the client
    does. A fake that never marks would let these tests "prove" a refund the
    production path must not give.
    """

    async def generate(self, prompt: str, **kwargs) -> str:  # noqa: ANN003
        text = await super().generate(prompt, **kwargs)
        mark_interaction_call_served()
        return text


class _RecordingClient:
    def __init__(self) -> None:
        self.charges: list[dict] = []
        self.settled: list[str] = []
        self.released: list[str] = []
        self.probed_releases: list[bool] = []
        """``settle_if_probed`` as sent per release — the flag that tells
        the ledger "Core cannot know what was served, you decide"."""

    async def charge(self, **kwargs) -> ActionCharge:
        self.charges.append(kwargs)
        return ActionCharge(charge_id="chg-1", price_cr=3.0)

    async def settle(self, charge_id: str) -> None:
        self.settled.append(charge_id)

    async def release(
        self, charge_id: str, *, settle_if_probed: bool = False,
    ) -> None:
        self.released.append(charge_id)
        self.probed_releases.append(settle_if_probed)


class _StubProfiles:
    def __init__(self, profile: AccountRuntimeProfile) -> None:
        self._profile = profile

    async def resolve_for_operator(self, operator_id: str):
        return self._profile


class _StubOperatorRepository:
    async def get(self, operator_id: str):
        class _Operator:
            cloud_tenant_id = "tenant-1"

        return _Operator()


class _Fixture:
    def __init__(
        self,
        *,
        model: _ScopeWatchingModel,
        profile: AccountRuntimeProfile = _ACTION_TIER,
    ) -> None:
        self.characters = InMemoryCharacterRepository()
        self.conversations = InMemoryConversationRepository()
        self.memories = InMemoryMemoryRepository()
        self.model = model
        self.client = _RecordingClient()
        self.billing = CloudActionBillingService(
            client=self.client,
            profile_resolver=_StubProfiles(profile),
            operator_profiles=_StubOperatorRepository(),
        )
        self.character_service = CharacterService(
            self.characters,
            conversation_repository=self.conversations,
            memory_repository=self.memories,
        )

    def chat(self) -> ChatService:
        registry = InMemoryChatModelRegistry(default_provider_id="fake")
        registry.register(self.model)
        return ChatService(
            character_repository=self.characters,
            conversation_repository=self.conversations,
            memory_repository=self.memories,
            post_turn_processor=NullPostTurnProcessor(),
            prompt_context_builder=DefaultPromptContextBuilder(),
            model_registry=registry,
            state_engine=SimpleStateEngine(),
            action_billing=self.billing,
        )

    async def character_id(self) -> str:
        created = await self.character_service.create_character(
            CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
        )
        return created.id

    async def managed_character_id(self) -> str:
        """EC7: a character installed from an official card (origin set)."""
        character_id = await self.character_id()
        character = await self.characters.get(character_id)
        assert character is not None
        managed = dataclasses.replace(
            character, origin_official_card_id="official-yumi",
        )
        await self.characters.save(managed)
        return character_id


async def test_one_turn_takes_one_charge_and_settles_it() -> None:
    fixture = _Fixture(model=_ServingModel())
    chat = fixture.chat()
    character_id = await fixture.character_id()

    await chat.send_message(
        SendChatMessageRequest(character_id=character_id, message="哈囉"),
    )

    assert len(fixture.client.charges) == 1
    assert fixture.client.charges[0]["action_key"] == "chat"
    assert fixture.client.charges[0]["character_origin"] is None
    assert fixture.client.settled == ["chg-1"]
    assert fixture.client.released == []


async def test_a_managed_characters_turn_charge_carries_its_origin() -> None:
    """EC7: the official card slug rides the chat action charge."""
    fixture = _Fixture(model=_ServingModel())
    chat = fixture.chat()
    character_id = await fixture.managed_character_id()

    await chat.send_message(
        SendChatMessageRequest(character_id=character_id, message="哈囉"),
    )

    assert fixture.client.charges[0]["character_origin"] == "official-yumi"


async def test_external_turn_reuses_its_stable_id_for_action_billing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every takeover of one durable LINE turn remains one billed action."""
    fixture = _Fixture(model=_ServingModel())
    chat = fixture.chat()
    character_id = await fixture.character_id()
    external_turn = SimpleNamespace(
        stable_turn_id=lambda: "external-turn-stable-1",
    )

    async def complete_without_external_writes(
        payload, prelude, *, external_turn=None,
    ):
        assert external_turn is not None
        mark_interaction_call_served()
        return object()

    monkeypatch.setattr(
        chat, "_send_message_turn", complete_without_external_writes,
    )

    await chat.send_message(
        SendChatMessageRequest(character_id=character_id, message="哈囉"),
        external_turn=external_turn,
    )

    assert fixture.client.charges[0]["interaction_id"] == (
        "external-turn-stable-1"
    )


async def test_the_llm_call_runs_inside_the_covering_interaction() -> None:
    """This is what lets the Gateway recognise the hops as already paid for."""
    fixture = _Fixture(model=_ScopeWatchingModel())
    chat = fixture.chat()
    character_id = await fixture.character_id()

    await chat.send_message(
        SendChatMessageRequest(character_id=character_id, message="哈囉"),
    )

    assert fixture.model.scopes
    scope = fixture.model.scopes[0]
    assert scope is not None
    assert scope.charge_id == "chg-1"
    assert current_interaction() is None


async def test_a_failed_turn_releases_the_charge() -> None:
    fixture = _Fixture(model=_ScopeWatchingModel(explode=True))
    chat = fixture.chat()
    character_id = await fixture.character_id()

    with pytest.raises(Exception):
        await chat.send_message(
            SendChatMessageRequest(character_id=character_id, message="哈囉"),
        )

    assert fixture.client.settled == []
    assert fixture.client.released == ["chg-1"]


async def test_token_billed_tier_takes_no_charge_at_all() -> None:
    fixture = _Fixture(
        model=_ScopeWatchingModel(), profile=DEFAULT_ACCOUNT_RUNTIME_PROFILE,
    )
    chat = fixture.chat()
    character_id = await fixture.character_id()

    await chat.send_message(
        SendChatMessageRequest(character_id=character_id, message="哈囉"),
    )

    assert fixture.client.charges == []
    assert fixture.model.scopes == [None]


async def test_streaming_turn_settles_when_the_reply_lands() -> None:
    fixture = _Fixture(model=_ServingModel())
    chat = fixture.chat()
    character_id = await fixture.character_id()

    stream, finalizer = await chat.send_message_stream(
        SendChatMessageRequest(character_id=character_id, message="哈囉"),
    )
    text = "".join([token async for token in stream])
    # The charge stays open across the stream — the turn is not done until the
    # assistant message is persisted.
    assert fixture.client.settled == []
    await finalizer.finish(text)

    assert fixture.client.settled == ["chg-1"]
    assert fixture.client.released == []


async def test_abandoned_stream_releases_rather_than_stranding_credits() -> None:
    """The route's only hook for a client that disconnected mid-generation."""
    fixture = _Fixture(model=_ScopeWatchingModel())
    chat = fixture.chat()
    character_id = await fixture.character_id()

    _stream, finalizer = await chat.send_message_stream(
        SendChatMessageRequest(character_id=character_id, message="哈囉"),
    )
    await finalizer.release_turn_lease()

    assert fixture.client.released == ["chg-1"]
    assert fixture.client.settled == []


async def test_finishing_after_settle_does_not_double_close() -> None:
    fixture = _Fixture(model=_ServingModel())
    chat = fixture.chat()
    character_id = await fixture.character_id()

    stream, finalizer = await chat.send_message_stream(
        SendChatMessageRequest(character_id=character_id, message="哈囉"),
    )
    text = "".join([token async for token in stream])
    await finalizer.finish(text)
    await finalizer.release_turn_lease()

    assert fixture.client.settled == ["chg-1"]
    assert fixture.client.released == []


async def test_streaming_llm_call_runs_inside_the_interaction_scope() -> None:
    fixture = _Fixture(model=_ScopeWatchingModel())
    chat = fixture.chat()
    character_id = await fixture.character_id()

    stream, finalizer = await chat.send_message_stream(
        SendChatMessageRequest(character_id=character_id, message="哈囉"),
    )
    text = "".join([token async for token in stream])
    await finalizer.finish(text)

    assert fixture.model.scopes
    assert fixture.model.scopes[0] is not None


async def test_abandoning_a_served_stream_settles_instead_of_refunding() -> None:
    """Stop-after-the-first-token must not be an unlimited free-chat loop.

    The Gateway has already waived per-call billing for this turn's calls, so
    nobody else will ever charge for the tokens it burned. Refunding here would
    make "send, then hit stop" free generation on repeat.
    """
    fixture = _Fixture(model=_ServingModel())
    chat = fixture.chat()
    character_id = await fixture.character_id()

    stream, finalizer = await chat.send_message_stream(
        SendChatMessageRequest(character_id=character_id, message="哈囉"),
    )
    assert [token async for token in stream]
    await finalizer.release_turn_lease()

    assert fixture.client.settled == ["chg-1"]
    assert fixture.client.released == []


async def test_a_turn_the_gateway_never_covered_is_not_charged_twice() -> None:
    """C2': when the Gateway falls back to per-call billing it charges the same
    wallet for every hop of this turn. Settling the fixed charge on top would
    take the player's money twice for one message."""
    fixture = _Fixture(model=_ScopeWatchingModel())
    chat = fixture.chat()
    character_id = await fixture.character_id()

    await chat.send_message(
        SendChatMessageRequest(character_id=character_id, message="哈囉"),
    )

    assert fixture.client.released == ["chg-1"]
    assert fixture.client.settled == []
    assert fixture.billing.counters.released_uncovered == 1


async def test_the_turn_binds_the_price_the_player_had_on_screen() -> None:
    """R9: the quote comes from the client, not from this replica's cache."""
    fixture = _Fixture(model=_ServingModel())
    chat = fixture.chat()
    character_id = await fixture.character_id()

    await chat.send_message(
        SendChatMessageRequest(
            character_id=character_id, message="哈囉", quoted_price_cr=2.5,
        ),
    )

    assert fixture.client.charges[0]["expected_price_cr"] == 2.5


async def test_a_streaming_turn_binds_the_client_quote_too() -> None:
    fixture = _Fixture(model=_ServingModel())
    chat = fixture.chat()
    character_id = await fixture.character_id()

    stream, finalizer = await chat.send_message_stream(
        SendChatMessageRequest(
            character_id=character_id, message="哈囉", quoted_price_cr=2.5,
        ),
    )
    await finalizer.finish("".join([token async for token in stream]))

    assert fixture.client.charges[0]["expected_price_cr"] == 2.5


async def test_a_client_that_sends_no_quote_still_charges() -> None:
    """Older builds keep working — the charge falls back to the price cache."""
    fixture = _Fixture(model=_ServingModel())
    chat = fixture.chat()
    character_id = await fixture.character_id()

    await chat.send_message(
        SendChatMessageRequest(character_id=character_id, message="哈囉"),
    )

    assert fixture.client.charges[0]["expected_price_cr"] is None
    assert fixture.client.settled == ["chg-1"]


async def test_a_turn_that_fails_before_the_gateway_answers_is_refunded() -> None:
    """The refund test is "was anything served", not "did the turn finish"."""
    fixture = _Fixture(model=_ScopeWatchingModel(explode=True))
    chat = fixture.chat()
    character_id = await fixture.character_id()

    with pytest.raises(RuntimeError):
        await chat.send_message(
            SendChatMessageRequest(character_id=character_id, message="哈囉"),
        )

    assert fixture.client.released == ["chg-1"]
    assert fixture.client.settled == []
