"""BDD for 起幕 — the player-pulled story scene service (SC1-A).

Covers the first waterfall layer end to end (beat chosen → opening
written → messages in the thread → session open) plus the refusals and
the atomicity the plan calls red lines: no scene while one is running, no
row when the opening fails, and a player pull that never spends the
autonomous scene writer's retry budget.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.beat_retry_policy import BeatRetryPolicy
from kokoro_link.application.services.chat_turn_lease import (
    ChatTurnLease,
    ConversationBusyError,
    release_turn_lease,
)
from kokoro_link.application.services.story_arc_service import StoryArcService
from kokoro_link.application.services.story_scene_material import (
    PendingBeatSceneMaterialProvider,
)
from kokoro_link.application.services.story_scene_service import (
    SCENE_OPEN_PLAY_RESULT,
    SCENE_OPEN_PLAY_SOURCE,
    SceneAlreadyOpen,
    SceneMaterialUnavailable,
    SceneOpenFailed,
    StorySceneService,
)
from kokoro_link.contracts.story_arc import StoryArcPlannerPort
from kokoro_link.contracts.story_scene import (
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
    BEAT_PENDING,
    SCENE_ENCOUNTER,
    StoryArc,
    StoryArcBeat,
    TENSION_SETUP,
    is_failed_play_result,
)
from kokoro_link.domain.entities.story_scene_session import (
    SCENE_LAYER_BEAT,
    SCENE_OPEN,
)
from kokoro_link.domain.value_objects.account_runtime_profile import (
    DEFAULT_ACCOUNT_RUNTIME_PROFILE,
)
from tests.unit._runtime_profiles import (
    RESTRICTIVE_ACCOUNT_RUNTIME_PROFILE,
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


NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 6, 1)


# ── doubles ──────────────────────────────────────────────────────────


class _UnusedPlanner(StoryArcPlannerPort):
    async def plan_arc(self, **kwargs):  # noqa: ANN003
        raise AssertionError("SC1-A never plans an arc")


class _StubOpener(StorySceneOpenerPort):
    def __init__(
        self,
        draft: StorySceneOpeningDraft | None = None,
        *,
        raises: Exception | None = None,
    ) -> None:
        self._draft = draft
        self._raises = raises
        self.contexts: list = []

    async def write_opening(self, context):  # noqa: ANN001
        self.contexts.append(context)
        if self._raises is not None:
            raise self._raises
        return self._draft


class _StubProfileResolver:
    def __init__(self, profile) -> None:  # noqa: ANN001
        self._profile = profile

    async def resolve_for_operator(self, operator_id: str):  # noqa: ANN201
        return self._profile


# ── fixtures ─────────────────────────────────────────────────────────


def _character(user_id: str = "u1") -> Character:
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
    )


def _beat(
    arc_id: str,
    *,
    sequence: int,
    scheduled: date,
    title: str,
    failures: int = 0,
    operator_position: str | None = "central",
    operator_note: str | None = "她要向你坦白",
) -> StoryArcBeat:
    return StoryArcBeat.create(
        arc_id=arc_id,
        sequence=sequence,
        scheduled_date=scheduled,
        title=title,
        summary=f"summary for {title}",
        tension=TENSION_SETUP,
        scene_type=SCENE_ENCOUNTER,
        location="頂樓",
        dramatic_question="她會說出口嗎？",
        scene_characters=["學姊"],
        play_failure_count=failures,
        last_play_failure_at=NOW - timedelta(minutes=1) if failures else None,
        operator_position=operator_position,
        operator_note=operator_note,
    )


def _arc(beats: list[StoryArcBeat] | None = None) -> StoryArc:
    arc = StoryArc.create(
        character_id="c1",
        title="夏日的獨奏會",
        premise="她要準備一場獨奏會",
        theme="custom",
        start_date=TODAY,
        end_date=TODAY + timedelta(days=21),
    )
    if beats is None:
        beats = [_beat(arc.id, sequence=0, scheduled=TODAY, title="第一顆")]
    else:
        beats = [
            StoryArcBeat.create(
                arc_id=arc.id,
                sequence=b.sequence,
                scheduled_date=b.scheduled_date,
                title=b.title,
                summary=b.summary,
                tension=b.tension,
                scene_type=b.scene_type,
                location=b.location,
                dramatic_question=b.dramatic_question,
                scene_characters=list(b.scene_characters),
                play_failure_count=b.play_failure_count,
                last_play_failure_at=b.last_play_failure_at,
                operator_position=b.operator_position,
                operator_note=b.operator_note,
            )
            for b in beats
        ]
    return arc.with_beats(beats)


_DRAFT = StorySceneOpeningDraft(
    narration="頂樓的風把譜架吹得直響，她已經站在那裡很久了。",
    character_line="……你來了。我以為今天只有我一個人。",
    title="頂樓的獨奏",
    location="頂樓天台",
    mood="欲言又止",
)


class _Fixture:
    def __init__(
        self,
        *,
        arc: StoryArc | None,
        opener: StorySceneOpenerPort,
        turn_lease: ChatTurnLease | None = None,
        retry_policy: BeatRetryPolicy | None = None,
        quota_guard=None,  # noqa: ANN001
    ) -> None:
        self.arcs_repo = InMemoryStoryArcRepository()
        self.arc = arc
        self.conversations = InMemoryConversationRepository()
        self.sessions = InMemoryStorySceneSessionRepository()
        self.arc_service = StoryArcService(
            repository=self.arcs_repo,
            planner=_UnusedPlanner(),
        )
        provider_kwargs = {"story_arc_service": self.arc_service}
        if retry_policy is not None:
            provider_kwargs["retry_policy"] = retry_policy
        self.opener = opener
        self.service = StorySceneService(
            sessions=self.sessions,
            conversations=self.conversations,
            opener=opener,
            material_providers=(
                PendingBeatSceneMaterialProvider(**provider_kwargs),
            ),
            story_arc_service=self.arc_service,
            turn_lease=turn_lease,
            quota_guard=quota_guard,
        )

    async def setup(self) -> None:
        if self.arc is not None:
            await self.arcs_repo.add(self.arc)


async def _fixture(**kwargs) -> _Fixture:  # noqa: ANN003
    fixture = _Fixture(**kwargs)
    await fixture.setup()
    return fixture


# ── the happy path ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_scene_plays_the_next_beat_and_lands_the_opening() -> None:
    arc = _arc()
    fx = await _fixture(arc=arc, opener=_StubOpener(_DRAFT))

    opening = await fx.service.open_scene(_character(), now=NOW)

    # session
    assert opening.session.status == SCENE_OPEN
    assert opening.session.source_layer == SCENE_LAYER_BEAT
    assert opening.session.arc_id == arc.id
    assert opening.session.beat_id == arc.beats[0].id
    assert opening.session.title == "頂樓的獨奏"
    assert opening.session.location == "頂樓天台"
    assert opening.session.mood == "欲言又止"
    # the material the later turns need travels on the row, not a lookup
    assert opening.session.dramatic_question == "她會說出口嗎？"
    assert opening.session.scene_type == SCENE_ENCOUNTER
    # OP2-D: the beat's player position/note travels onto the session too,
    # so the closer (which reads only the session) can frame from it
    assert opening.session.operator_position == "central"
    assert opening.session.operator_note == "她要向你坦白"

    # messages: narration is a distinguishable kind, the line is ordinary
    assert opening.narration.kind is MessageKind.SCENE_NARRATION
    assert opening.character_message.kind is MessageKind.CHAT
    assert opening.character_message.role is MessageRole.ASSISTANT

    stored = await fx.conversations.get(opening.session.conversation_id)
    assert [m.content for m in stored.messages] == [
        _DRAFT.narration, _DRAFT.character_line,
    ]
    assert stored.source == SOURCE_WEB

    # and it is now the character's live scene, readable by anyone
    live = await fx.service.current_scene("c1")
    assert live is not None and live.id == opening.session.id


@pytest.mark.asyncio
async def test_opening_appends_to_the_existing_web_thread() -> None:
    """The scene is a frame inside the ordinary thread, not a new one."""
    fx = await _fixture(arc=_arc(), opener=_StubOpener(_DRAFT))
    existing = Conversation.start(character_id="c1", source=SOURCE_WEB).append(
        Message(role=MessageRole.USER, content="嗨"),
    )
    await fx.conversations.save(existing)

    opening = await fx.service.open_scene(_character(), now=NOW)

    assert opening.session.conversation_id == existing.id
    stored = await fx.conversations.get(existing.id)
    assert [m.content for m in stored.messages] == [
        "嗨", _DRAFT.narration, _DRAFT.character_line,
    ]


@pytest.mark.asyncio
async def test_narration_is_persistable_and_stays_in_llm_history() -> None:
    """Two decisions in one round trip through the real SQL adapter.

    1. ``scene_narration`` fits the ``String(16)`` ``messages.kind``
       column — a longer member would truncate or blow up only in
       production, since the in-memory repository has no width.
    2. Narration is **ordinary history**. It states facts the scene
       established, so a later turn that could not see it would
       contradict the story it is playing inside; only ``tool_only`` is
       filtered out of the LLM context path.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    from kokoro_link.infrastructure.persistence.engine import (
        build_session_factory,
    )
    from kokoro_link.infrastructure.persistence.models import (
        ConversationRow,
        MessageRow,
    )
    from kokoro_link.infrastructure.persistence.sa_conversation_repository import (
        SAConversationRepository,
    )

    width = MessageRow.__table__.c.kind.type.length
    assert all(len(kind.value) <= width for kind in MessageKind)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(lambda s: ConversationRow.__table__.create(s))
            await conn.run_sync(lambda s: MessageRow.__table__.create(s))
        conversations = SAConversationRepository(build_session_factory(engine))
        fx = await _fixture(arc=_arc(), opener=_StubOpener(_DRAFT))
        fx.service._conversations = conversations  # noqa: SLF001

        opening = await fx.service.open_scene(_character(), now=NOW)

        stored = await conversations.get(opening.session.conversation_id)
        assert [m.kind for m in stored.messages] == [
            MessageKind.SCENE_NARRATION, MessageKind.CHAT,
        ]
        history = await conversations.recent_messages_for_character(
            "c1", limit=10, exclude_tool_only=True,
        )
        assert [m.content for m in history] == [
            _DRAFT.narration, _DRAFT.character_line,
        ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_material_context_reaches_the_opener() -> None:
    arc = _arc()
    opener = _StubOpener(_DRAFT)
    fx = await _fixture(arc=arc, opener=opener)

    await fx.service.open_scene(_character(), now=NOW)

    material = opener.contexts[0].material
    assert material.layer == SCENE_LAYER_BEAT
    assert material.beat_id == arc.beats[0].id
    assert material.arc_premise == arc.premise
    assert material.dramatic_question == "她會說出口嗎？"
    assert material.scene_characters == ("學姊",)
    assert material.operator_position == "central"
    assert material.operator_note == "她要向你坦白"


@pytest.mark.asyncio
async def test_an_unjudged_beat_reaches_the_opener_as_none() -> None:
    """No author decided a position for this beat — the material must
    say ``None`` (unjudged), never a guess, so the opener derives a
    framing from the rest of the material instead."""
    arc = _arc([
        _beat(
            "x", sequence=0, scheduled=TODAY, title="沒人標記過",
            operator_position=None, operator_note=None,
        ),
    ])
    opener = _StubOpener(_DRAFT)
    fx = await _fixture(arc=arc, opener=opener)

    await fx.service.open_scene(_character(), now=NOW)

    material = opener.contexts[0].material
    assert material.operator_position is None
    assert material.operator_note is None


# ── beat selection ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_future_scheduled_beat_is_served_immediately() -> None:
    """「我現在就要劇情」 — ``scheduled_date`` is not a gate (§3.1)."""
    arc = _arc([
        _beat("x", sequence=0, scheduled=TODAY + timedelta(days=9), title="下週"),
    ])
    fx = await _fixture(arc=arc, opener=_StubOpener(_DRAFT))

    opening = await fx.service.open_scene(_character(), now=NOW)

    assert opening.session.beat_id == arc.beats[0].id


@pytest.mark.asyncio
async def test_exhausted_beat_is_walked_past_without_being_written_to() -> None:
    policy = BeatRetryPolicy(
        max_attempts=2,
        initial_backoff=timedelta(hours=1),
        maximum_backoff=timedelta(hours=24),
    )
    arc = _arc([
        _beat("x", sequence=0, scheduled=TODAY, title="燒光了", failures=2),
        _beat("x", sequence=1, scheduled=TODAY, title="還能演", failures=0),
    ])
    fx = await _fixture(
        arc=arc, opener=_StubOpener(_DRAFT), retry_policy=policy,
    )

    opening = await fx.service.open_scene(_character(), now=NOW)

    assert opening.session.beat_id == arc.beats[1].id
    # retiring the dead beat belongs to the background sweep that owns
    # that write — a player tap must not mutate arc status as a side
    # effect of reading.
    reloaded = await fx.arcs_repo.get(arc.id)
    assert reloaded.beats[0].status == BEAT_PENDING


@pytest.mark.asyncio
async def test_backoff_does_not_block_a_player_pull() -> None:
    """Backoff throttles autonomous retries; a player is not a retry."""
    policy = BeatRetryPolicy(
        max_attempts=5,
        initial_backoff=timedelta(hours=6),
        maximum_backoff=timedelta(hours=24),
    )
    arc = _arc([
        _beat("x", sequence=0, scheduled=TODAY, title="剛失敗過", failures=1),
    ])
    beat = arc.beats[0]
    # the policy would refuse the autonomous scanner right now …
    assert policy.allows(beat, now=NOW) is False
    fx = await _fixture(
        arc=arc, opener=_StubOpener(_DRAFT), retry_policy=policy,
    )

    # … and still hands the beat to the player who asked for it.
    opening = await fx.service.open_scene(_character(), now=NOW)
    assert opening.session.beat_id == beat.id


@pytest.mark.asyncio
async def test_play_attempt_is_recorded_without_charging_the_retry_budget(
) -> None:
    arc = _arc()
    fx = await _fixture(arc=arc, opener=_StubOpener(_DRAFT))

    await fx.service.open_scene(_character(), now=NOW)

    beat = (await fx.arcs_repo.get(arc.id)).beats[0]
    assert beat.play_attempt_count == 1
    assert beat.last_play_attempt_source == SCENE_OPEN_PLAY_SOURCE
    assert beat.last_play_attempt_result == SCENE_OPEN_PLAY_RESULT
    # the whole point: the budget only spends failures (SC0 / §10 #3)
    assert is_failed_play_result(SCENE_OPEN_PLAY_RESULT) is False
    assert beat.play_failure_count == 0
    assert beat.last_play_failure_at is None


# ── refusals ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_second_open_while_a_scene_is_running_is_refused() -> None:
    fx = await _fixture(
        arc=_arc([
            _beat("x", sequence=0, scheduled=TODAY, title="一"),
            _beat("x", sequence=1, scheduled=TODAY, title="二"),
        ]),
        opener=_StubOpener(_DRAFT),
    )
    await fx.service.open_scene(_character(), now=NOW)

    with pytest.raises(SceneAlreadyOpen) as caught:
        await fx.service.open_scene(_character(), now=NOW)

    assert caught.value.code == "scene_in_progress"


@pytest.mark.asyncio
async def test_no_active_arc_reports_no_material() -> None:
    fx = await _fixture(arc=None, opener=_StubOpener(_DRAFT))

    with pytest.raises(SceneMaterialUnavailable) as caught:
        await fx.service.open_scene(_character(), now=NOW)

    assert caught.value.code == "no_material"
    # SC1-B fills this gap; nothing was written in the meantime.
    assert await fx.sessions.get_open_for_character("c1") is None


@pytest.mark.asyncio
async def test_arc_with_only_realized_beats_reports_no_material() -> None:
    arc = _arc()
    arc = arc.with_beats([arc.beats[0].with_status("realized")])
    fx = await _fixture(arc=arc, opener=_StubOpener(_DRAFT))

    with pytest.raises(SceneMaterialUnavailable):
        await fx.service.open_scene(_character(), now=NOW)


@pytest.mark.asyncio
async def test_a_busy_conversation_blocks_the_opening() -> None:
    lease = ChatTurnLease(InMemoryBackgroundCoordinatorLease(), ttl_seconds=60)
    fx = await _fixture(
        arc=_arc(), opener=_StubOpener(_DRAFT), turn_lease=lease,
    )
    conversation = Conversation.start(character_id="c1", source=SOURCE_WEB)
    await fx.conversations.save(conversation)
    # someone else's turn already holds the thread
    held = await lease.acquire(conversation.id)
    try:
        with pytest.raises(ConversationBusyError):
            await fx.service.open_scene(_character(), now=NOW)
    finally:
        await release_turn_lease(held)

    assert await fx.sessions.get_open_for_character("c1") is None


# ── atomicity ────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "opener",
    [
        _StubOpener(None),
        _StubOpener(raises=RuntimeError("upstream exploded")),
    ],
    ids=["no-draft", "writer-crashed"],
)
async def test_a_failed_opening_writes_nothing(opener) -> None:  # noqa: ANN001
    arc = _arc()
    fx = await _fixture(arc=arc, opener=opener)

    with pytest.raises(SceneOpenFailed) as caught:
        await fx.service.open_scene(_character(), now=NOW)

    assert caught.value.code == "scene_open_failed"
    assert await fx.sessions.get_open_for_character("c1") is None
    conversation = await fx.conversations.latest_for_character(
        "c1", source=SOURCE_WEB,
    )
    assert conversation is None or conversation.messages == []
    # and the beat is untouched — no attempt, no failure
    beat = (await fx.arcs_repo.get(arc.id)).beats[0]
    assert beat.play_attempt_count == 0
    assert beat.play_failure_count == 0


@pytest.mark.asyncio
async def test_an_unwritable_thread_leaves_no_open_session() -> None:
    """A scene that never reached the thread must not lock the button."""
    fx = await _fixture(arc=_arc(), opener=_StubOpener(_DRAFT))

    async def _always_conflict(*args, **kwargs):  # noqa: ANN002, ANN003
        from kokoro_link.contracts.repositories import AppendResult
        return AppendResult(ok=False, conflict=True)

    fx.conversations.append_messages = _always_conflict  # type: ignore[assignment]

    with pytest.raises(SceneOpenFailed):
        await fx.service.open_scene(_character(), now=NOW)

    assert await fx.sessions.get_open_for_character("c1") is None


@pytest.mark.asyncio
async def test_a_broken_material_layer_falls_through_instead_of_aborting(
) -> None:
    class _Exploding:
        layer = "boom"

        async def resolve(self, character, *, today):  # noqa: ANN001
            raise RuntimeError("layer is broken")

    fx = await _fixture(arc=_arc(), opener=_StubOpener(_DRAFT))
    fx.service._material_providers = (  # noqa: SLF001 - wiring under test
        _Exploding(), *fx.service._material_providers,  # noqa: SLF001
    )

    opening = await fx.service.open_scene(_character(), now=NOW)

    assert opening.session.source_layer == SCENE_LAYER_BEAT


# ── SC3-B quota guard wiring ─────────────────────────────────────────


class _RecordingGuard:
    """Stands in for StorySceneQuotaGuard: records calls, optionally raises."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self._raises = raises
        self.checked: list = []

    async def check(self, character, *, now=None):  # noqa: ANN001
        self.checked.append((character.id, now))
        if self._raises is not None:
            raise self._raises


@pytest.mark.asyncio
async def test_open_scene_consults_the_quota_guard_before_any_work() -> None:
    guard = _RecordingGuard()
    fx = await _fixture(
        arc=_arc(), opener=_StubOpener(_DRAFT), quota_guard=guard,
    )

    opening = await fx.service.open_scene(_character(), now=NOW)

    assert opening.session.status == SCENE_OPEN
    assert guard.checked == [("c1", NOW)]


@pytest.mark.asyncio
async def test_quota_guard_refusal_blocks_before_the_opener_is_called() -> None:
    from kokoro_link.application.services.story_scene_quota import (
        StorySceneDailyLimitReached,
    )

    opener = _StubOpener(_DRAFT)
    guard = _RecordingGuard(
        raises=StorySceneDailyLimitReached(limit=2, used=2),
    )
    fx = await _fixture(arc=_arc(), opener=opener, quota_guard=guard)

    with pytest.raises(StorySceneDailyLimitReached):
        await fx.service.open_scene(_character(), now=NOW)

    # refused before any material work or writes: no opener call, no
    # session row, and the beat was not marked attempted
    assert opener.contexts == []
    assert await fx.sessions.get_open_for_character("c1") is None
    arc = await fx.arcs_repo.get_active_for_character("c1")
    assert arc.beats[0].play_attempt_count == 0
