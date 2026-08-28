"""QG7 — 起幕's two player-visible writes go through the quality band.

Before this ticket the opening and the wrap-up were the only messages in
the product that reached a player's thread without any review at all: a
leaked schema fragment, a reply in the wrong language or a sentence that
stopped mid-word landed exactly as the model produced it, and the wrap-up
did so on the one path (the idle sweep) nobody is watching.

Three claims are pinned here, and the third is the one worth the file:

1. **the opening reviews, regenerates once, and refuses to raise the
   curtain on a hard defect** — 起幕 sits on the background half of the
   D1 disposal table, so a hard failure that survives its regeneration
   fails the action rather than showing the player the defect;
2. **the wrap-up does the same, and degrades exactly the way an absent
   wrap-up already did** — no new fail-soft path, so the timeout sweep
   cannot walk one nobody tested;
3. **a withheld opening is refunded in full.** The plan's §3.4 red line 2
   says a player is never charged for an opening that never happened, and
   a gate skip is the first refusal that arrives *after* the writer has
   already burned a covered upstream call. That is asserted against the
   real ``CloudActionBillingService``, because the whole risk lives in the
   boundary between this service's failure path and the wallet's.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.cloud_action_billing_service import (
    CloudActionBillingService,
)
from kokoro_link.application.services.output_quality import (
    OUTCOME_HARD_RECOVERED,
    OUTCOME_HARD_SKIPPED,
    OUTCOME_PASS,
    OUTCOME_SOFT_RECOVERED,
    OutputQualityOrchestrator,
)
from kokoro_link.application.services.story_arc_service import StoryArcService
from kokoro_link.application.services.story_event_service import (
    StoryEventService,
)
from kokoro_link.application.services.story_gacha import StoryGachaService
from kokoro_link.application.services.story_scene_closing import (
    SCENE_CLOSING_QUALITY_SURFACE,
    SCENE_OPENING_QUALITY_SURFACE,
)
from kokoro_link.application.services.story_scene_material import (
    PendingBeatSceneMaterialProvider,
)
from kokoro_link.application.services.story_scene_service import (
    SceneOpenFailed,
    StorySceneService,
)
from kokoro_link.contracts.cloud_action_billing import ActionCharge
from kokoro_link.contracts.interaction_context import (
    mark_interaction_call_served,
)
from kokoro_link.contracts.novelty_gate import (
    NoveltyGateContext,
    NoveltyVerdict,
)
from kokoro_link.contracts.story import StoryEventExpanderPort
from kokoro_link.contracts.story_arc import StoryArcPlannerPort
from kokoro_link.contracts.story_scene import (
    StorySceneClosingDraft,
    StorySceneCloserPort,
    StorySceneOpeningDraft,
    StorySceneOpenerPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import (
    SOURCE_WEB,
    Conversation,
    Message,
    MessageKind,
    MessageRole,
)
from kokoro_link.domain.entities.story_arc import (
    SCENE_ENCOUNTER,
    StoryArc,
    StoryArcBeat,
    TENSION_SETUP,
)
from kokoro_link.domain.entities.story_scene_session import (
    SCENE_CLOSE_MANUAL,
    SCENE_CLOSED,
    SCENE_LAYER_SIDE_STORY,
    StorySceneSession,
)
from kokoro_link.domain.value_objects.account_runtime_profile import (
    BILLING_SHAPE_ACTION_FIXED,
    AccountRuntimeProfile,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_stories import (
    InMemoryStoryEventRepository,
    InMemoryStorySeedRepository,
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

OPENING_NARRATION = "頂樓的風把譜架吹得直響，她已經站在那裡很久了。"
CLOSING_NARRATION = "你離開之後，她把譜架收起來，一個人又站了很久。"
CANON_SUMMARY = "他走了以後，我自己把那段又拉了一遍。"
DEGRADED_TAG = "story_scene_unnarrated"


def _draft(narration: str = OPENING_NARRATION) -> StorySceneOpeningDraft:
    return StorySceneOpeningDraft(
        narration=narration,
        character_line="……你來了。我以為今天只有我一個人。",
        title="頂樓的獨奏",
        location="頂樓天台",
        mood="欲言又止",
    )


def _passes() -> NoveltyVerdict:
    return NoveltyVerdict(passes=True)


def _hard() -> NoveltyVerdict:
    """A hard axis — the kind that is worth withholding a whole write for."""
    return NoveltyVerdict(
        passes=False,
        structural_leak=True,
        feedback="正文混進了 JSON 欄位名，重寫成純粹的敘事。",
    )


def _soft() -> NoveltyVerdict:
    return NoveltyVerdict(
        passes=False,
        formulaic=True,
        feedback="開場像套版，補一個此刻看得見的具體細節。",
    )


# ── doubles ──────────────────────────────────────────────────────────


class _ScriptedGate:
    """A judge whose verdicts are written in advance, in order."""

    def __init__(self, *verdicts: NoveltyVerdict) -> None:
        self._verdicts = list(verdicts)
        self.contexts: list[NoveltyGateContext] = []

    async def evaluate(self, context, *, character=None):  # noqa: ANN001
        self.contexts.append(context)
        if not self._verdicts:
            return _passes()
        return self._verdicts.pop(0)


class _SequenceOpener(StorySceneOpenerPort):
    """Answers with each draft in turn; the last one repeats.

    ``covered`` reproduces what a real waived Gateway response does to the
    enclosing action charge. A stub that skipped it would make every refund
    assertion in this file pass for the wrong reason.
    """

    def __init__(
        self,
        *drafts: StorySceneOpeningDraft | None,
        covered: bool = False,
    ) -> None:
        self._drafts = list(drafts) or [_draft()]
        self._covered = covered
        self.calls = 0

    async def write_opening(self, context):  # noqa: ANN001
        self.calls += 1
        draft = self._drafts[min(self.calls - 1, len(self._drafts) - 1)]
        if self._covered and draft is not None:
            mark_interaction_call_served()
        return draft


class _SequenceCloser(StorySceneCloserPort):
    def __init__(self, *drafts: StorySceneClosingDraft | None) -> None:
        self._drafts = list(drafts)
        self.calls = 0

    async def write_closing(self, context):  # noqa: ANN001
        self.calls += 1
        return self._drafts[min(self.calls - 1, len(self._drafts) - 1)]


class _UnusedOpener(StorySceneOpenerPort):
    async def write_opening(self, context):  # noqa: ANN001
        raise AssertionError("the wrap-up never opens a scene")


class _UnusedPlanner(StoryArcPlannerPort):
    async def plan_arc(self, **kwargs):  # noqa: ANN003
        raise AssertionError("this ticket never plans an arc")


class _UnusedExpander(StoryEventExpanderPort):
    async def expand(self, **kwargs):  # noqa: ANN003
        raise AssertionError("realization narratives come from the wrap-up")


class _RecordingClient:
    """The User-service ledger, as the charge path reaches it."""

    def __init__(self) -> None:
        self.charges: list[dict] = []
        self.settled: list[str] = []
        self.released: list[str] = []

    async def charge(self, **kwargs) -> ActionCharge:  # noqa: ANN003
        self.charges.append(kwargs)
        return ActionCharge(charge_id="chg-1", price_cr=6.0)

    async def settle(self, charge_id: str) -> None:
        self.settled.append(charge_id)

    async def release(
        self, charge_id: str, *, settle_if_probed: bool = False,
    ) -> None:
        self.released.append(charge_id)


class _StubProfiles:
    async def resolve_for_operator(self, operator_id: str):  # noqa: ANN201
        return AccountRuntimeProfile(
            name="standard", billing_shape=BILLING_SHAPE_ACTION_FIXED,
        )


class _StubOperatorRepository:
    async def get(self, operator_id: str):  # noqa: ANN201
        class _Operator:
            cloud_tenant_id = "tenant-1"

        return _Operator()


# ── fixtures ─────────────────────────────────────────────────────────


def _character() -> Character:
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
        user_id="u1",
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
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


class _OpeningFixture:
    def __init__(
        self,
        *,
        opener: StorySceneOpenerPort,
        gate: _ScriptedGate | None,
        billed: bool = False,
        reply_quality_gate_enabled: bool = True,
        reply_quality_gate_max_retries: int = 1,
    ) -> None:
        self.arcs_repo = InMemoryStoryArcRepository()
        self.conversations = InMemoryConversationRepository()
        self.sessions = InMemoryStorySceneSessionRepository()
        self.client = _RecordingClient()
        self.opener = opener
        self.gate = gate
        self.orchestrator = (
            OutputQualityOrchestrator(gate=gate) if gate is not None else None
        )
        arc_service = StoryArcService(
            repository=self.arcs_repo, planner=_UnusedPlanner(),
        )
        billing = (
            CloudActionBillingService(
                client=self.client,  # type: ignore[arg-type]
                profile_resolver=_StubProfiles(),
                operator_profiles=_StubOperatorRepository(),
            )
            if billed else None
        )
        self.service = StorySceneService(
            sessions=self.sessions,
            conversations=self.conversations,
            opener=opener,
            material_providers=(
                PendingBeatSceneMaterialProvider(
                    story_arc_service=arc_service,
                ),
            ),
            story_arc_service=arc_service,
            action_billing=billing,
            output_quality_orchestrator=self.orchestrator,
            reply_quality_gate_enabled=reply_quality_gate_enabled,
            reply_quality_gate_max_retries=reply_quality_gate_max_retries,
        )

    async def setup(self) -> "_OpeningFixture":
        await self.arcs_repo.add(_arc())
        return self

    async def thread_messages(self) -> list[Message]:
        conversation = await self.conversations.latest_for_character(
            "c1", source=SOURCE_WEB,
        )
        return list(conversation.messages) if conversation else []


async def _opening_fixture(**kwargs) -> _OpeningFixture:  # noqa: ANN003
    return await _OpeningFixture(**kwargs).setup()


class _ClosingFixture:
    def __init__(
        self,
        *,
        closer: StorySceneCloserPort,
        gate: _ScriptedGate | None,
        reply_quality_gate_enabled: bool = True,
        reply_quality_gate_max_retries: int = 1,
    ) -> None:
        self.sessions = InMemoryStorySceneSessionRepository()
        self.conversations = InMemoryConversationRepository()
        self.memories = InMemoryMemoryRepository()
        self.events = InMemoryStoryEventRepository()
        self.closer = closer
        self.gate = gate
        self.orchestrator = (
            OutputQualityOrchestrator(gate=gate) if gate is not None else None
        )
        arc_service = StoryArcService(
            repository=InMemoryStoryArcRepository(), planner=_UnusedPlanner(),
        )
        self.service = StorySceneService(
            sessions=self.sessions,
            conversations=self.conversations,
            opener=_UnusedOpener(),
            material_providers=(),
            closer=closer,
            story_arc_service=arc_service,
            story_event_service=StoryEventService(
                gacha=StoryGachaService(
                    seed_repository=InMemoryStorySeedRepository(),
                    event_repository=self.events,
                ),
                expander=_UnusedExpander(),
                event_repository=self.events,
                memory_repository=self.memories,
                local_tz=timezone.utc,
                arc_service=arc_service,
            ),
            memory_repository=self.memories,
            local_tz=timezone.utc,
            output_quality_orchestrator=self.orchestrator,
            reply_quality_gate_enabled=reply_quality_gate_enabled,
            reply_quality_gate_max_retries=reply_quality_gate_max_retries,
        )
        self.conversation: Conversation | None = None

    async def open_scene(self) -> StorySceneSession:
        """A side-story scene mid-play: opening narration, one player line."""
        conversation = Conversation.start(character_id="c1", source=SOURCE_WEB)
        await self.conversations.save(conversation)
        await self.conversations.append_messages(
            conversation.id,
            expected_next_position=0,
            messages=[
                Message(
                    role=MessageRole.ASSISTANT,
                    content=OPENING_NARRATION,
                    kind=MessageKind.SCENE_NARRATION,
                    created_at=NOW,
                ),
                Message(
                    role=MessageRole.ASSISTANT,
                    content="……你來了。",
                    created_at=NOW,
                ),
                Message(
                    role=MessageRole.USER,
                    content="我只是路過。",
                    created_at=NOW,
                ),
            ],
        )
        self.conversation = await self.conversations.get(conversation.id)
        session = StorySceneSession.open_scene(
            character_id="c1",
            conversation_id=conversation.id,
            source_layer=SCENE_LAYER_SIDE_STORY,
            title="把話說完",
            dramatic_question="她要承認自己練得不夠嗎？",
            opened_at=NOW,
        )
        await self.sessions.add(session)
        return session

    async def thread_messages(self) -> list[Message]:
        assert self.conversation is not None
        conversation = await self.conversations.get(self.conversation.id)
        return list(conversation.messages) if conversation else []


# ── the opening ──────────────────────────────────────────────────────


async def test_a_clean_opening_is_published_unchanged() -> None:
    fx = await _opening_fixture(opener=_SequenceOpener(), gate=_ScriptedGate())

    opening = await fx.service.open_scene(_character(), now=NOW)

    assert opening.narration.content == OPENING_NARRATION
    assert fx.opener.calls == 1
    assert len(await fx.thread_messages()) == 2
    assert fx.orchestrator.counters.total(
        SCENE_OPENING_QUALITY_SURFACE, OUTCOME_PASS,
    ) == 1
    # FC1 — one label per hook point: raising the curtain never reports as
    # the wrap-up that has not happened yet.
    assert fx.orchestrator.counters.total(
        SCENE_CLOSING_QUALITY_SURFACE, OUTCOME_PASS,
    ) == 0


async def test_the_judge_reads_both_halves_of_the_opening() -> None:
    """One body, labelled — register cannot be judged without the labels."""
    gate = _ScriptedGate()
    fx = await _opening_fixture(opener=_SequenceOpener(), gate=gate)

    await fx.service.open_scene(_character(), now=NOW)

    context = gate.contexts[0]
    assert OPENING_NARRATION in context.response_text
    assert "……你來了。我以為今天只有我一個人。" in context.response_text
    assert "旁白" in context.response_text and "Mio" in context.response_text
    # the material the opening dramatises is what novelty is judged against
    assert any("頂樓的練習" in line for line in context.known_material)
    assert context.operator_primary_language == "zh-TW"


async def test_a_soft_failure_is_regenerated_and_the_retry_is_published() -> None:
    """Soft axes are opinions: the second draft ships, the scene opens."""
    second = _draft("風停了，她把弓放回琴盒，抬頭看見你。")
    gate = _ScriptedGate(_soft(), _passes())
    fx = await _opening_fixture(
        opener=_SequenceOpener(_draft(), second), gate=gate,
    )

    opening = await fx.service.open_scene(_character(), now=NOW)

    assert fx.opener.calls == 2
    assert opening.narration.content == second.narration
    assert fx.orchestrator.counters.total(
        SCENE_OPENING_QUALITY_SURFACE, OUTCOME_SOFT_RECOVERED,
    ) == 1


async def test_a_hard_failure_the_regeneration_clears_opens_the_scene() -> None:
    gate = _ScriptedGate(_hard(), _passes())
    fx = await _opening_fixture(
        opener=_SequenceOpener(_draft(), _draft("乾淨的第二稿。")), gate=gate,
    )

    opening = await fx.service.open_scene(_character(), now=NOW)

    assert opening.narration.content == "乾淨的第二稿。"
    assert fx.orchestrator.counters.total(
        SCENE_OPENING_QUALITY_SURFACE, OUTCOME_HARD_RECOVERED,
    ) == 1


async def test_a_hard_failure_that_survives_the_regeneration_fails_the_action() -> None:
    """The curtain stays down, and the failure writes nothing at all."""
    gate = _ScriptedGate(_hard(), _hard())
    fx = await _opening_fixture(opener=_SequenceOpener(), gate=gate)

    with pytest.raises(SceneOpenFailed):
        await fx.service.open_scene(_character(), now=NOW)

    assert fx.opener.calls == 2
    assert await fx.sessions.get_open_for_character("c1") is None
    assert await fx.thread_messages() == []
    assert fx.orchestrator.counters.total(
        SCENE_OPENING_QUALITY_SURFACE, OUTCOME_HARD_SKIPPED,
    ) == 1


async def test_an_opener_that_answers_nothing_twice_still_fails_once() -> None:
    """A regeneration that produces no draft is a disposal, not a crash."""
    gate = _ScriptedGate(_hard())
    fx = await _opening_fixture(
        opener=_SequenceOpener(_draft(), None), gate=gate,
    )

    with pytest.raises(SceneOpenFailed):
        await fx.service.open_scene(_character(), now=NOW)

    assert await fx.sessions.get_open_for_character("c1") is None


async def test_without_an_orchestrator_the_opening_is_never_reviewed() -> None:
    """Self-host and every un-wired deployment keep the path they had."""
    fx = await _opening_fixture(opener=_SequenceOpener(), gate=None)

    opening = await fx.service.open_scene(_character(), now=NOW)

    assert opening.narration.content == OPENING_NARRATION
    assert fx.opener.calls == 1


# ── QG7b: the container-wired knob ────────────────────────────────────


async def test_reply_quality_gate_disabled_publishes_the_opening_unreviewed() -> None:
    """``enabled=False`` behaves exactly like no orchestrator at all.

    Before QG7b the opening's ``enabled`` flag was hardcoded ``True`` — a
    deployment that turned the container's novelty gate off still ran a
    judge against every 起幕 opening. Wired now, so the same hard verdict
    that would otherwise fail the action never gets read.
    """
    gate = _ScriptedGate(_hard(), _hard())
    fx = await _opening_fixture(
        opener=_SequenceOpener(), gate=gate, reply_quality_gate_enabled=False,
    )

    opening = await fx.service.open_scene(_character(), now=NOW)

    assert opening.narration.content == OPENING_NARRATION
    assert fx.opener.calls == 1
    assert gate.contexts == []


async def test_reply_quality_gate_max_retries_zero_disposes_the_opening_without_a_second_draft() -> None:
    """``max_retries=0`` is "review only" — a hard failure fails the action
    on the first verdict, and the opener is never asked for a retry."""
    gate = _ScriptedGate(_hard())
    fx = await _opening_fixture(
        opener=_SequenceOpener(),
        gate=gate,
        reply_quality_gate_max_retries=0,
    )

    with pytest.raises(SceneOpenFailed):
        await fx.service.open_scene(_character(), now=NOW)

    assert fx.opener.calls == 1
    assert fx.orchestrator.counters.total(
        SCENE_OPENING_QUALITY_SURFACE, OUTCOME_HARD_SKIPPED,
    ) == 1


# ── red line 2: a withheld opening is never charged for ──────────────


async def test_a_withheld_opening_refunds_the_whole_charge() -> None:
    """§3.4 #2 — nobody pays for an opening they never saw.

    The gate skip is the first 起幕 refusal that arrives *after* the
    Gateway has already served a covered call, so the naive reading of the
    wallet's own rule ("a failure after a covered call settles rather than
    refunding work already served") would bill the player for a scene that
    does not exist. It does not, because the covered call is only folded
    onto the charge once the opening is in the thread.
    """
    gate = _ScriptedGate(_hard(), _hard())
    fx = await _opening_fixture(
        opener=_SequenceOpener(covered=True), gate=gate, billed=True,
    )

    with pytest.raises(SceneOpenFailed):
        await fx.service.open_scene(_character(), now=NOW)

    assert fx.client.settled == []
    assert fx.client.released == ["chg-1"]
    assert await fx.sessions.get_open_for_character("c1") is None


async def test_a_delivered_opening_still_settles_its_charge() -> None:
    """The other half of the deferral: a real opening is still paid for."""
    fx = await _opening_fixture(
        opener=_SequenceOpener(covered=True),
        gate=_ScriptedGate(),
        billed=True,
    )

    await fx.service.open_scene(_character(), now=NOW)

    assert fx.client.settled == ["chg-1"]
    assert fx.client.released == []


# ── the wrap-up ──────────────────────────────────────────────────────


def _closing(narration: str = CLOSING_NARRATION) -> StorySceneClosingDraft:
    return StorySceneClosingDraft(
        resolved=True,
        closing_narration=narration,
        canon_summary=CANON_SUMMARY,
    )


async def _closing_fixture(**kwargs) -> _ClosingFixture:  # noqa: ANN003
    return _ClosingFixture(**kwargs)


async def test_a_clean_wrap_up_is_appended_to_the_thread() -> None:
    fx = await _closing_fixture(
        closer=_SequenceCloser(_closing()), gate=_ScriptedGate(),
    )
    session = await fx.open_scene()

    closing = await fx.service.close_scene(
        _character(), session=session, mode=SCENE_CLOSE_MANUAL, now=NOW,
    )

    assert closing.closing_narration is not None
    assert closing.closing_narration.content == CLOSING_NARRATION
    assert closing.canon_landed is True


async def test_the_wrap_up_reports_under_its_own_surface_label() -> None:
    """FC1 — the two writes of a scene are two hook points, not one.

    They shared ``story_scene`` before this, so a hard-skip streak named
    the *feature* and left the operator to work out which of the two
    writers — the opener on a button press, or the closer on the idle
    sweep — was actually breaking. The pair still adds up to the feature's
    rate, because these are Prometheus labels rather than metric names.
    """
    fx = await _closing_fixture(
        closer=_SequenceCloser(_closing()), gate=_ScriptedGate(),
    )
    session = await fx.open_scene()

    await fx.service.close_scene(
        _character(), session=session, mode=SCENE_CLOSE_MANUAL, now=NOW,
    )

    assert SCENE_CLOSING_QUALITY_SURFACE != SCENE_OPENING_QUALITY_SURFACE
    assert fx.orchestrator.counters.total(
        SCENE_CLOSING_QUALITY_SURFACE, OUTCOME_PASS,
    ) == 1
    assert fx.orchestrator.counters.total(
        SCENE_OPENING_QUALITY_SURFACE, OUTCOME_PASS,
    ) == 0


async def test_a_soft_failure_regenerates_the_wrap_up() -> None:
    fx = await _closing_fixture(
        closer=_SequenceCloser(_closing(), _closing("她把燈關了。")),
        gate=_ScriptedGate(_soft(), _passes()),
    )
    session = await fx.open_scene()

    closing = await fx.service.close_scene(
        _character(), session=session, mode=SCENE_CLOSE_MANUAL, now=NOW,
    )

    assert fx.closer.calls == 2
    assert closing.closing_narration.content == "她把燈關了。"


async def test_a_hard_failure_that_survives_leaves_the_scene_unnarrated() -> None:
    """No wrap-up in the thread — and every other step still runs.

    Deliberately the *same* answer an absent draft already produced: the
    session closes, canon degrades to the opening narration, and the row
    carries the tag that tells an operator the writer failed here. A new
    fail-soft path would be one the idle sweep walks and nobody tests.
    """
    fx = await _closing_fixture(
        closer=_SequenceCloser(_closing()),
        gate=_ScriptedGate(_hard(), _hard()),
    )
    session = await fx.open_scene()

    closing = await fx.service.close_scene(
        _character(), session=session, mode=SCENE_CLOSE_MANUAL, now=NOW,
    )

    assert closing.closing_narration is None
    assert closing.session.status == SCENE_CLOSED
    # the thread still holds only what the scene itself produced
    narrations = [
        message for message in await fx.thread_messages()
        if message.kind is MessageKind.SCENE_NARRATION
    ]
    assert [message.content for message in narrations] == [OPENING_NARRATION]
    # canon landed, degraded and labelled as such
    memories = await fx.memories.query("c1")
    scene_memories = [
        item for item in memories if DEGRADED_TAG in item.tags
    ]
    assert len(scene_memories) == 1
    assert scene_memories[0].content == OPENING_NARRATION


async def test_a_wrap_up_with_no_narration_never_reaches_the_judge() -> None:
    """A writer that honestly declined is not a defect to be gated.

    ``closing_narration`` may be empty even on a resolved close, and that
    draft's canon summary is still the best record of the scene there is —
    reviewing an empty body would have thrown it away.
    """
    gate = _ScriptedGate()
    fx = await _closing_fixture(
        closer=_SequenceCloser(
            StorySceneClosingDraft(
                resolved=True, closing_narration="", canon_summary=CANON_SUMMARY,
            ),
        ),
        gate=gate,
    )
    session = await fx.open_scene()

    closing = await fx.service.close_scene(
        _character(), session=session, mode=SCENE_CLOSE_MANUAL, now=NOW,
    )

    assert gate.contexts == []
    assert closing.closing_narration is None
    memories = await fx.memories.query("c1")
    assert any(item.content == CANON_SUMMARY for item in memories)


async def test_the_judge_reads_the_scene_frame_as_known_material() -> None:
    gate = _ScriptedGate()
    fx = await _closing_fixture(
        closer=_SequenceCloser(_closing()), gate=gate,
    )
    session = await fx.open_scene()

    await fx.service.close_scene(
        _character(), session=session, mode=SCENE_CLOSE_MANUAL, now=NOW,
    )

    context = gate.contexts[0]
    assert CLOSING_NARRATION in context.response_text
    assert any("把話說完" in line for line in context.known_material)
    # the player's own last line is what the register axes read
    assert context.latest_user_message == "我只是路過。"


async def test_without_an_orchestrator_the_wrap_up_is_never_reviewed() -> None:
    fx = await _closing_fixture(
        closer=_SequenceCloser(_closing()), gate=None,
    )
    session = await fx.open_scene()

    closing = await fx.service.close_scene(
        _character(), session=session, mode=SCENE_CLOSE_MANUAL, now=NOW,
    )

    assert closing.closing_narration.content == CLOSING_NARRATION
    assert fx.closer.calls == 1


# ── QG7b: the container-wired knob reaches the wrap-up too ────────────


async def test_reply_quality_gate_disabled_appends_the_wrap_up_unreviewed() -> None:
    """The same flag that gates the opening also has to gate the wrap-up —
    they are handed down from one ``StorySceneService`` construction, and a
    knob that only reached one end would leave the timeout sweep running a
    judge the container turned off."""
    gate = _ScriptedGate(_hard(), _hard())
    fx = await _closing_fixture(
        closer=_SequenceCloser(_closing()),
        gate=gate,
        reply_quality_gate_enabled=False,
    )
    session = await fx.open_scene()

    closing = await fx.service.close_scene(
        _character(), session=session, mode=SCENE_CLOSE_MANUAL, now=NOW,
    )

    assert closing.closing_narration is not None
    assert closing.closing_narration.content == CLOSING_NARRATION
    assert fx.closer.calls == 1
    assert gate.contexts == []


async def test_reply_quality_gate_max_retries_zero_disposes_the_wrap_up_without_a_second_draft() -> None:
    """``max_retries=0``: the first hard verdict disposes straight to the
    existing unnarrated-degrade path, and the closer is never asked again."""
    gate = _ScriptedGate(_hard())
    fx = await _closing_fixture(
        closer=_SequenceCloser(_closing()),
        gate=gate,
        reply_quality_gate_max_retries=0,
    )
    session = await fx.open_scene()

    closing = await fx.service.close_scene(
        _character(), session=session, mode=SCENE_CLOSE_MANUAL, now=NOW,
    )

    assert closing.closing_narration is None
    assert fx.closer.calls == 1
    assert fx.orchestrator.counters.total(
        SCENE_CLOSING_QUALITY_SURFACE, OUTCOME_HARD_SKIPPED,
    ) == 1
