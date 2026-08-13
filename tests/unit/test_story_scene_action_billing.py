"""SC3-C — the hosted one price behind 起幕.

STORY_SCENE_PLAN §3.4 red line 2 is the whole file: *the opening's fixed
price only exists if the opening did*. That splits into three claims, each
pinned below against the real ``CloudActionBillingService`` rather than a
mock of it, because what is being tested is the boundary between this
service's failure paths and the wallet's:

1. every refusal 起幕 can answer on its own happens **before** the charge,
   so the ledger never shows a spend-and-refund pair for a scene that
   never started (the demo gate, an already-running scene, the SC3-B daily
   ceiling, an empty waterfall, a busy thread);
2. an opening that fails or never reaches the thread is **released** —
   nothing was waived upstream, so the refund is whole;
3. a successful opening **settles**, once, at the price the player's own
   screen was quoting.

The wrap-up and the action chips deliberately raise nothing at all: both
are background feature keys whose cost is already inside this price
(§4.2 ②③), so an in-scene turn is billed as ordinary ``chat`` and a
scene that runs for twenty turns is not twenty scene charges.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.chat_turn_lease import (
    ChatTurnLease,
    ConversationBusyError,
    release_turn_lease,
)
from kokoro_link.application.services.cloud_action_billing_service import (
    CloudActionBillingService,
    NullActionBillingService,
)
from kokoro_link.application.services.story_arc_service import StoryArcService
from kokoro_link.application.services.story_scene_material import (
    PendingBeatSceneMaterialProvider,
)
from kokoro_link.application.services.story_scene_quota import (
    StorySceneDailyLimitReached,
)
from kokoro_link.application.services.story_scene_service import (
    SceneAlreadyOpen,
    SceneMaterialUnavailable,
    SceneOpenFailed,
    StorySceneService,
    StorySceneUnavailable,
)
from kokoro_link.contracts.cloud_action_billing import (
    ACTION_STORY_SCENE_OPEN,
    ActionCharge,
    ActionPriceChanged,
    client_quoted_price_scope,
)
from kokoro_link.contracts.interaction_context import (
    current_interaction,
    mark_interaction_call_served,
)
from kokoro_link.contracts.story_arc import StoryArcPlannerPort
from kokoro_link.contracts.story_scene import (
    StorySceneOpeningDraft,
    StorySceneOpenerPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import SOURCE_WEB, Conversation
from kokoro_link.domain.entities.story_arc import (
    SCENE_ENCOUNTER,
    StoryArc,
    StoryArcBeat,
    TENSION_SETUP,
)
from kokoro_link.domain.entities.story_scene_session import SCENE_CLOSE_MANUAL
from kokoro_link.domain.value_objects.account_runtime_profile import (
    BILLING_SHAPE_ACTION_FIXED,
    DEFAULT_ACCOUNT_RUNTIME_PROFILE,
    DEMO_ACCOUNT_RUNTIME_PROFILE,
    AccountRuntimeProfile,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryBackgroundCoordinatorLease,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_story_arcs import (
    InMemoryStoryArcRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_story_scene_sessions import (
    InMemoryStorySceneSessionRepository,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 6, 1)

_ACTION_TIER = AccountRuntimeProfile(
    name="standard", billing_shape=BILLING_SHAPE_ACTION_FIXED,
)

_DRAFT = StorySceneOpeningDraft(
    narration="頂樓的風把譜架吹得直響，她已經站在那裡很久了。",
    character_line="……你來了。我以為今天只有我一個人。",
    title="頂樓的獨奏",
    location="頂樓天台",
    mood="欲言又止",
)


# ── doubles ──────────────────────────────────────────────────────────


class _RecordingClient:
    """The User-service ledger, as this service's charge path reaches it."""

    def __init__(self, *, charge_error: Exception | None = None) -> None:
        self.charges: list[dict] = []
        self.settled: list[str] = []
        self.released: list[str] = []
        self._charge_error = charge_error

    async def charge(self, **kwargs) -> ActionCharge:  # noqa: ANN003
        self.charges.append(kwargs)
        if self._charge_error is not None:
            raise self._charge_error
        return ActionCharge(charge_id="chg-1", price_cr=6.0)

    async def settle(self, charge_id: str) -> None:
        self.settled.append(charge_id)

    async def release(
        self, charge_id: str, *, settle_if_probed: bool = False,
    ) -> None:
        self.released.append(charge_id)


class _StubProfiles:
    def __init__(self, profile=_ACTION_TIER) -> None:  # noqa: ANN001
        self._profile = profile

    async def resolve_for_operator(self, operator_id: str):  # noqa: ANN201
        return self._profile


class _StubOperatorRepository:
    async def get(self, operator_id: str):  # noqa: ANN201
        class _Operator:
            cloud_tenant_id = "tenant-1"

        return _Operator()


class _UnusedPlanner(StoryArcPlannerPort):
    async def plan_arc(self, **kwargs):  # noqa: ANN003
        raise AssertionError("this ticket never plans an arc")


class _CoveredOpener(StorySceneOpenerPort):
    """The opening writer, waived by the Gateway under the enclosing charge.

    Marking the call served is what a real covered Gateway response does,
    and it is the fact ``release`` turns on — so a stub that skipped it
    would make every refund test pass for the wrong reason.
    """

    def __init__(
        self,
        draft: StorySceneOpeningDraft | None = _DRAFT,
        *,
        raises: Exception | None = None,
        covered: bool = True,
    ) -> None:
        self._draft = draft
        self._raises = raises
        self._covered = covered
        self.scopes: list = []

    async def write_opening(self, context):  # noqa: ANN001
        self.scopes.append(current_interaction())
        if self._raises is not None:
            raise self._raises
        if self._covered and self._draft is not None:
            mark_interaction_call_served()
        return self._draft


class _RefusingGuard:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def check(self, character, *, now=None):  # noqa: ANN001
        raise self._error


# ── fixture ──────────────────────────────────────────────────────────


def _character(
    user_id: str = "u1", *, origin_official_card_id: str | None = None,
) -> Character:
    return Character(
        id="c1",
        name="Mio",
        summary="a violinist",
        personality=(),
        interests=(),
        speaking_style="soft",
        boundaries=(),
        aspirations=(),
        appearance="",
        world_frame="modern",
        user_id=user_id,
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        origin_official_card_id=origin_official_card_id,
    )


def _arc() -> StoryArc:
    arc = StoryArc.create(
        character_id="c1",
        title="夏日的獨奏會",
        premise="她要準備一場獨奏會",
        theme="custom",
        start_date=TODAY,
        end_date=TODAY + timedelta(days=21),
    )
    return arc.with_beats([
        StoryArcBeat.create(
            arc_id=arc.id,
            sequence=0,
            scheduled_date=TODAY,
            title="第一顆",
            summary="頂樓的練習",
            tension=TENSION_SETUP,
            scene_type=SCENE_ENCOUNTER,
            location="頂樓",
            dramatic_question="她會說出口嗎？",
            scene_characters=["學姊"],
        ),
    ])


class _Fixture:
    def __init__(
        self,
        *,
        opener: StorySceneOpenerPort,
        arc: StoryArc | None = None,
        profile=DEFAULT_ACCOUNT_RUNTIME_PROFILE,  # noqa: ANN001
        billing_profile=_ACTION_TIER,  # noqa: ANN001
        quota_guard=None,  # noqa: ANN001
        turn_lease: ChatTurnLease | None = None,
        charge_error: Exception | None = None,
        billed: bool = True,
    ) -> None:
        self.arc = arc
        self.arcs_repo = InMemoryStoryArcRepository()
        self.conversations = InMemoryConversationRepository()
        self.sessions = InMemoryStorySceneSessionRepository()
        self.client = _RecordingClient(charge_error=charge_error)
        self.opener = opener
        billing = (
            CloudActionBillingService(
                client=self.client,  # type: ignore[arg-type]
                profile_resolver=_StubProfiles(billing_profile),
                operator_profiles=_StubOperatorRepository(),
            )
            if billed else NullActionBillingService()
        )
        self.service = StorySceneService(
            sessions=self.sessions,
            conversations=self.conversations,
            opener=opener,
            material_providers=(
                PendingBeatSceneMaterialProvider(
                    story_arc_service=StoryArcService(
                        repository=self.arcs_repo, planner=_UnusedPlanner(),
                    ),
                ),
            ),
            story_arc_service=StoryArcService(
                repository=self.arcs_repo, planner=_UnusedPlanner(),
            ),
            turn_lease=turn_lease,
            account_runtime_profile_resolver=_StubProfiles(profile),
            quota_guard=quota_guard,
            action_billing=billing,
        )

    async def setup(self) -> "_Fixture":
        if self.arc is not None:
            await self.arcs_repo.add(self.arc)
        return self


async def _fixture(**kwargs) -> _Fixture:  # noqa: ANN003
    kwargs.setdefault("arc", _arc())
    return await _Fixture(**kwargs).setup()


# ── the price is paid for an opening that happened ───────────────────


async def test_an_opening_is_one_charge_and_settles() -> None:
    fx = await _fixture(opener=_CoveredOpener())

    opening = await fx.service.open_scene(_character(), now=NOW)

    assert opening.session.title == "頂樓的獨奏"
    assert [c["action_key"] for c in fx.client.charges] == [
        ACTION_STORY_SCENE_OPEN,
    ]
    assert fx.client.settled == ["chg-1"]
    assert fx.client.released == []
    # ...and the writer ran inside the charge's scope, so the Gateway
    # waived it rather than billing the opening per call on top.
    assert fx.opener.scopes[0] is not None


async def test_opening_charge_carries_origin_for_a_managed_character() -> None:
    """EC7: the official card slug rides the ``story_scene_open`` charge."""
    fx = await _fixture(opener=_CoveredOpener())

    await fx.service.open_scene(
        _character(origin_official_card_id="official-yumi"), now=NOW,
    )

    assert fx.client.charges[0]["character_origin"] == "official-yumi"


async def test_opening_charge_omits_origin_for_an_ordinary_character() -> None:
    fx = await _fixture(opener=_CoveredOpener())

    await fx.service.open_scene(_character(), now=NOW)

    assert fx.client.charges[0]["character_origin"] is None


async def test_the_scene_is_charged_once_however_long_it_runs() -> None:
    """One press, one price. The turns inside are ordinary ``chat``.

    Closing the scene raises nothing either: the wrap-up is a background
    key whose cost is already inside this number (§4.2 ③), so a player who
    ends a scene is never billed for the ending.
    """
    fx = await _fixture(opener=_CoveredOpener())

    opening = await fx.service.open_scene(_character(), now=NOW)
    await fx.service.end_scene(
        _character(), session_id=opening.session.id, now=NOW,
    )

    assert len(fx.client.charges) == 1
    assert fx.client.settled == ["chg-1"]


async def test_closing_a_scene_never_reaches_the_wallet() -> None:
    fx = await _fixture(opener=_CoveredOpener())
    opening = await fx.service.open_scene(_character(), now=NOW)
    fx.client.charges.clear()
    fx.client.settled.clear()

    await fx.service.close_scene(
        _character(), session=opening.session, mode=SCENE_CLOSE_MANUAL,
        now=NOW,
    )

    assert fx.client.charges == []
    assert fx.client.settled == [] and fx.client.released == []


# ── red line 2: a failed opening is refunded ─────────────────────────


@pytest.mark.parametrize(
    "opener",
    [
        _CoveredOpener(raises=RuntimeError("upstream exploded")),
        _CoveredOpener(None),
    ],
    ids=["writer-crashed", "no-draft"],
)
async def test_a_failed_opening_releases_the_charge(opener) -> None:  # noqa: ANN001
    fx = await _fixture(opener=opener)

    with pytest.raises(SceneOpenFailed):
        await fx.service.open_scene(_character(), now=NOW)

    assert fx.client.settled == []
    assert fx.client.released == ["chg-1"]
    # and the failure left no trace to charge *for* next time
    assert await fx.sessions.get_open_for_character("c1") is None


async def test_an_opening_that_never_reaches_the_thread_is_refunded() -> None:
    """The deliverable is the scene in the player's thread, not the prose.

    The writer answered but the Gateway did not waive it (verifier outage,
    unknown charge id), so it billed that call itself — settling the fixed
    price on top would take the money twice for a scene the player cannot
    see anywhere.
    """
    fx = await _fixture(opener=_CoveredOpener(covered=False))

    async def _always_conflict(*args, **kwargs):  # noqa: ANN002, ANN003
        from kokoro_link.contracts.repositories import AppendResult
        return AppendResult(ok=False, conflict=True)

    fx.conversations.append_messages = _always_conflict  # type: ignore[assignment]

    with pytest.raises(SceneOpenFailed):
        await fx.service.open_scene(_character(), now=NOW)

    assert fx.client.settled == []
    assert fx.client.released == ["chg-1"]
    assert await fx.sessions.get_open_for_character("c1") is None


# ── refusals this service already knows never move money ─────────────


async def test_a_demo_account_is_refused_without_a_charge() -> None:
    fx = await _fixture(
        opener=_CoveredOpener(), profile=DEMO_ACCOUNT_RUNTIME_PROFILE,
    )

    with pytest.raises(StorySceneUnavailable):
        await fx.service.open_scene(_character(), now=NOW)

    assert fx.client.charges == []


async def test_a_running_scene_is_refused_without_a_second_charge() -> None:
    fx = await _fixture(opener=_CoveredOpener())
    await fx.service.open_scene(_character(), now=NOW)

    with pytest.raises(SceneAlreadyOpen):
        await fx.service.open_scene(_character(), now=NOW)

    assert len(fx.client.charges) == 1


async def test_the_daily_ceiling_refuses_without_a_charge() -> None:
    """SC3-B's knob: "達上限的拒絕不扣點" is a wallet fact, not a UI one."""
    fx = await _fixture(
        opener=_CoveredOpener(),
        quota_guard=_RefusingGuard(StorySceneDailyLimitReached(limit=2, used=2)),
    )

    with pytest.raises(StorySceneDailyLimitReached):
        await fx.service.open_scene(_character(), now=NOW)

    assert fx.client.charges == []


async def test_an_empty_waterfall_is_refused_without_a_charge() -> None:
    fx = await _fixture(opener=_CoveredOpener(), arc=None)

    with pytest.raises(SceneMaterialUnavailable):
        await fx.service.open_scene(_character(), now=NOW)

    assert fx.client.charges == []


async def test_a_busy_thread_is_refused_without_a_charge() -> None:
    lease = ChatTurnLease(InMemoryBackgroundCoordinatorLease(), ttl_seconds=60)
    fx = await _fixture(opener=_CoveredOpener(), turn_lease=lease)
    conversation = Conversation.start(character_id="c1", source=SOURCE_WEB)
    await fx.conversations.save(conversation)
    held = await lease.acquire(conversation.id)
    try:
        with pytest.raises(ConversationBusyError):
            await fx.service.open_scene(_character(), now=NOW)
    finally:
        await release_turn_lease(held)

    assert fx.client.charges == []


# ── R-9: the charge binds to the number on the player's screen ───────


async def test_the_charge_binds_the_price_the_player_was_quoted() -> None:
    fx = await _fixture(opener=_CoveredOpener())

    with client_quoted_price_scope({ACTION_STORY_SCENE_OPEN: 9.5}):
        await fx.service.open_scene(_character(), now=NOW)

    assert fx.client.charges[0]["expected_price_cr"] == 9.5


async def test_without_a_client_quote_the_charge_is_unbound() -> None:
    # An older SPA sends no body at all; the scene must still open.
    fx = await _fixture(opener=_CoveredOpener())

    await fx.service.open_scene(_character(), now=NOW)

    assert fx.client.charges[0]["expected_price_cr"] is None


async def test_a_moved_price_refuses_before_the_writer_runs() -> None:
    """409, and nothing reserved — so there is nothing to release either."""
    fx = await _fixture(
        opener=_CoveredOpener(),
        charge_error=ActionPriceChanged(
            action_key=ACTION_STORY_SCENE_OPEN,
            expected_price_cr=6.0,
            current_price_cr=8.0,
        ),
    )

    with pytest.raises(ActionPriceChanged):
        with client_quoted_price_scope({ACTION_STORY_SCENE_OPEN: 6.0}):
            await fx.service.open_scene(_character(), now=NOW)

    assert fx.opener.scopes == []
    assert fx.client.settled == [] and fx.client.released == []
    assert await fx.sessions.get_open_for_character("c1") is None


# ── inside the scene: ordinary chat, and only ordinary chat ──────────


async def test_a_turn_inside_a_scene_is_billed_as_plain_chat() -> None:
    """§3.3: no new billing surface inside the scene, and no double charge.

    Runs the real ``ChatService`` against the same wallet the opening used,
    because the failure this guards against is structural: an in-scene turn
    that also raised ``story_scene_open`` (or a scene service that charged
    on the verdict) would bill the player twice for one message, and no
    amount of reading either service in isolation would show it.
    """
    from kokoro_link.application.dto.character import CreateCharacterRequest
    from kokoro_link.application.dto.chat import SendChatMessageRequest
    from kokoro_link.application.services.character_service import (
        CharacterService,
    )
    from kokoro_link.application.services.chat_service import ChatService
    from kokoro_link.infrastructure.llm.fake import FakeChatModel
    from kokoro_link.infrastructure.llm.registry import (
        InMemoryChatModelRegistry,
    )
    from kokoro_link.infrastructure.memory.in_memory import (
        InMemoryMemoryRepository,
    )
    from kokoro_link.infrastructure.post_turn.null_processor import (
        NullPostTurnProcessor,
    )
    from kokoro_link.infrastructure.prompt.default import (
        DefaultPromptContextBuilder,
    )
    from kokoro_link.infrastructure.repositories.in_memory_characters import (
        InMemoryCharacterRepository,
    )
    from kokoro_link.infrastructure.state.simple import SimpleStateEngine

    fx = await _fixture(opener=_CoveredOpener())
    characters = InMemoryCharacterRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel("fake"))
    billing = fx.service._action_billing  # noqa: SLF001 - one wallet, two services
    chat = ChatService(
        character_repository=characters,
        conversation_repository=fx.conversations,
        memory_repository=InMemoryMemoryRepository(),
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        story_scene_sessions=fx.sessions,
        story_scene_service=fx.service,
        action_billing=billing,
    )
    created = await CharacterService(characters).create_character(
        CreateCharacterRequest(name="Mio", personality=[], interests=[]),
    )
    # The scene the arc fixture describes belongs to "c1"; give the chat
    # character its own arc-free session so the turn plays inside a scene.
    from kokoro_link.domain.entities.story_scene_session import (
        SCENE_LAYER_SIDE_STORY,
        StorySceneSession,
    )
    conversation = Conversation.start(
        character_id=created.id, source=SOURCE_WEB,
    )
    await fx.conversations.save(conversation)
    await fx.sessions.add(StorySceneSession.open_scene(
        character_id=created.id,
        conversation_id=conversation.id,
        source_layer=SCENE_LAYER_SIDE_STORY,
        title="把話說完",
        dramatic_question="她要承認自己練得不夠嗎？",
        opened_at=NOW,
    ))

    assert await fx.sessions.get_open_for_character(created.id) is not None

    await chat.send_message(
        SendChatMessageRequest(character_id=created.id, message="我說完了。"),
    )

    # The premise: the turn really was played inside a live scene …
    assert await fx.sessions.get_open_for_character(created.id) is not None
    # … and it cost exactly one ordinary chat turn. (How that charge is
    # *closed* is the chat path's own business — the fake model never
    # produces a covered Gateway call, so it is refunded here.)
    assert [c["action_key"] for c in fx.client.charges] == ["chat"]


# ── self-host: the whole feature exists with no wallet at all ────────


async def test_self_host_opens_a_scene_with_no_billing_wired() -> None:
    """No ``action_billing`` argument at all — SC1-A's constructor.

    Byte-compatible on purpose: 起幕 is an open-source feature that is
    complete without hosting, and the null object is what keeps the
    charge-free path from being a second implementation.
    """
    fx = await _fixture(opener=_CoveredOpener(), billed=False)

    opening = await fx.service.open_scene(_character(), now=NOW)

    assert opening.session.title == "頂樓的獨奏"
    assert fx.client.charges == []
    # nothing stamped the upstream call, so the Gateway (if there were
    # one) would bill it exactly as it did before this ticket
    assert fx.opener.scopes == [None]


async def test_a_token_billed_tier_is_not_action_charged() -> None:
    """Hosted, but still metering per call — no fixed charge is raised."""
    fx = await _fixture(
        opener=_CoveredOpener(),
        billing_profile=AccountRuntimeProfile(name="legacy"),
    )

    await fx.service.open_scene(_character(), now=NOW)

    assert fx.client.charges == []
    assert fx.opener.scopes == [None]
