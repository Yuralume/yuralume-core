"""BDD for the 起幕 wrap-up core (SC1-D).

Three routes share one close — the in-turn verdict, the player's
「結束場景」 button, and (SC1-E) the idle sweep — so these tests are about
what the shared core guarantees regardless of who called it:

* the scene lands as canon by the route its material came from: a beat
  is *realized* through the existing chain (StoryEvent + memory + arc
  status), an ad-hoc side story becomes a plain episodic memory;
* closing is a compare-and-set, so two closers produce one wrap-up;
* a scene always ends. A writer that fails, refuses, or was never wired
  cannot leave the player stuck inside a scene they cannot re-open —
  the session closes and canon degrades to what is honestly known.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.story_arc_service import StoryArcService
from kokoro_link.application.services.story_event_service import (
    StoryEventService,
)
from kokoro_link.application.services.story_gacha import StoryGachaService
from kokoro_link.application.services.story_scene_service import (
    SceneNotOpen,
    SceneSessionMismatch,
    StorySceneService,
)
from kokoro_link.contracts.story import StoryEventExpanderPort
from kokoro_link.contracts.story_arc import StoryArcPlannerPort
from kokoro_link.contracts.story_scene import (
    StorySceneClosingContext,
    StorySceneClosingDraft,
    StorySceneCloserPort,
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
    BEAT_REALIZED,
    SCENE_ENCOUNTER,
    StoryArc,
    StoryArcBeat,
    TENSION_SETUP,
)
from kokoro_link.domain.entities.story_scene_session import (
    SCENE_CLOSE_MANUAL,
    SCENE_CLOSE_RESOLVED,
    SCENE_CLOSE_TIMEOUT,
    SCENE_CLOSED,
    SCENE_LAYER_BEAT,
    SCENE_LAYER_NEW_SEASON,
    SCENE_LAYER_SIDE_STORY,
    StorySceneSession,
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


# ── doubles ──────────────────────────────────────────────────────────


class _StubCloser(StorySceneCloserPort):
    def __init__(
        self,
        draft: StorySceneClosingDraft | None = None,
        *,
        raises: Exception | None = None,
    ) -> None:
        self._draft = draft
        self._raises = raises
        self.contexts: list[StorySceneClosingContext] = []

    async def write_closing(self, context):  # noqa: ANN001
        self.contexts.append(context)
        if self._raises is not None:
            raise self._raises
        return self._draft


class _UnusedOpener(StorySceneOpenerPort):
    async def write_opening(self, context):  # noqa: ANN001
        raise AssertionError("the wrap-up never opens a scene")


class _UnusedPlanner(StoryArcPlannerPort):
    async def plan_arc(self, **kwargs):  # noqa: ANN003
        raise AssertionError("the wrap-up never plans an arc")


class _UnusedExpander(StoryEventExpanderPort):
    async def expand(self, **kwargs):  # noqa: ANN003
        raise AssertionError("realization narratives come from the wrap-up")


def _resolved_draft(**overrides) -> StorySceneClosingDraft:  # noqa: ANN003
    base = dict(
        resolved=True,
        closing_narration=CLOSING_NARRATION,
        canon_summary=CANON_SUMMARY,
    )
    base.update(overrides)
    return StorySceneClosingDraft(**base)


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


# ── fixture ──────────────────────────────────────────────────────────


class _Fixture:
    def __init__(self, closer: StorySceneCloserPort) -> None:
        self.sessions = InMemoryStorySceneSessionRepository()
        self.conversations = InMemoryConversationRepository()
        self.memories = InMemoryMemoryRepository()
        self.events = InMemoryStoryEventRepository()
        self.arcs_repo = InMemoryStoryArcRepository()
        self.arc_service = StoryArcService(
            repository=self.arcs_repo, planner=_UnusedPlanner(),
        )
        self.event_service = StoryEventService(
            gacha=StoryGachaService(
                seed_repository=InMemoryStorySeedRepository(),
                event_repository=self.events,
            ),
            expander=_UnusedExpander(),
            event_repository=self.events,
            memory_repository=self.memories,
            local_tz=timezone.utc,
            arc_service=self.arc_service,
        )
        self.closer = closer
        self.service = StorySceneService(
            sessions=self.sessions,
            conversations=self.conversations,
            opener=_UnusedOpener(),
            material_providers=(),
            closer=closer,
            story_arc_service=self.arc_service,
            story_event_service=self.event_service,
            memory_repository=self.memories,
            local_tz=timezone.utc,
        )
        self.conversation: Conversation | None = None
        self.arc: StoryArc | None = None

    async def with_arc(self) -> StoryArcBeat:
        arc = StoryArc.create(
            character_id="c1",
            title="夏日的獨奏會",
            premise="她要準備一場獨奏會",
            theme="custom",
            start_date=TODAY,
            end_date=TODAY + timedelta(days=21),
        )
        beat = StoryArcBeat.create(
            arc_id=arc.id,
            sequence=0,
            scheduled_date=TODAY,
            title="第一顆",
            summary="頂樓的那次相遇",
            tension=TENSION_SETUP,
            scene_type=SCENE_ENCOUNTER,
            location="頂樓",
            dramatic_question="她會說出口嗎？",
        )
        self.arc = arc.with_beats([beat])
        await self.arcs_repo.add(self.arc)
        return self.arc.beats[0]

    async def open_scene(
        self,
        *,
        layer: str = SCENE_LAYER_BEAT,
        beat_id: str | None = None,
        arc_id: str | None = None,
        player_lines: tuple[str, ...] = ("我只是路過。",),
    ) -> StorySceneSession:
        conversation = Conversation.start(character_id="c1", source=SOURCE_WEB)
        await self.conversations.save(conversation)
        messages = [
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
        ]
        for line in player_lines:
            messages.append(
                Message(
                    role=MessageRole.USER, content=line, created_at=NOW,
                ),
            )
        await self.conversations.append_messages(
            conversation.id, expected_next_position=0, messages=messages,
        )
        self.conversation = await self.conversations.get(conversation.id)
        session = StorySceneSession.open_scene(
            character_id="c1",
            conversation_id=conversation.id,
            source_layer=layer,
            arc_id=arc_id,
            beat_id=beat_id,
            title="頂樓的獨奏",
            location="頂樓天台",
            mood="欲言又止",
            scene_type=SCENE_ENCOUNTER,
            dramatic_question="她會說出口嗎？",
            opened_at=NOW,
        )
        await self.sessions.add(session)
        return session

    async def thread(self) -> list[Message]:
        assert self.conversation is not None
        stored = await self.conversations.get(self.conversation.id)
        return list(stored.messages)


# ── canon: the three material layers ─────────────────────────────────


@pytest.mark.parametrize(
    "layer", [SCENE_LAYER_BEAT, SCENE_LAYER_NEW_SEASON],
    ids=["beat", "new-season"],
)
async def test_a_beat_backed_scene_realizes_its_beat(layer: str) -> None:
    """Layers 1 and 2 land through the *existing* realization chain, so a
    player-pulled beat writes the same history a scheduled one does."""
    fx = _Fixture(_StubCloser(_resolved_draft()))
    beat = await fx.with_arc()
    session = await fx.open_scene(
        layer=layer, beat_id=beat.id, arc_id=fx.arc.id,
    )

    closing = await fx.service.close_scene(
        _character(), session=session, mode=SCENE_CLOSE_MANUAL, now=NOW,
    )

    assert closing.canon_landed is True
    # 1. the beat is realized and points at its event
    realized = (await fx.arcs_repo.get(fx.arc.id)).find_beat(beat.id)
    assert realized.status == BEAT_REALIZED
    # 2. a StoryEvent carries what was actually performed
    events = await fx.events.get_for_day("c1", TODAY.isoformat())
    assert [e.narrative for e in events] == [CANON_SUMMARY]
    assert events[0].arc_beat_id == beat.id
    assert realized.realized_event_id == events[0].id
    # 3. and it is in the character's memory
    memories = await fx.memories.query("c1")
    assert any(m.content == CANON_SUMMARY for m in memories)


async def test_a_side_story_scene_lands_as_a_plain_episodic_memory() -> None:
    """Layer 3 built no arc, so there is no beat to realize — and no
    StoryEvent either. It happened; the character remembers it."""
    fx = _Fixture(_StubCloser(_resolved_draft()))
    session = await fx.open_scene(layer=SCENE_LAYER_SIDE_STORY)

    closing = await fx.service.close_scene(
        _character(), session=session, mode=SCENE_CLOSE_MANUAL, now=NOW,
    )

    assert closing.canon_landed is True
    memories = await fx.memories.query("c1")
    assert [m.content for m in memories] == [CANON_SUMMARY]
    assert "story_event" in memories[0].tags
    assert "side_story" in memories[0].tags
    # no arc was involved, so nothing pretends one was
    assert await fx.events.get_for_day("c1", TODAY.isoformat()) == []


# ── the shared close ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "mode",
    [SCENE_CLOSE_MANUAL, SCENE_CLOSE_TIMEOUT, SCENE_CLOSE_RESOLVED],
    ids=["manual", "timeout", "resolved"],
)
async def test_every_mode_closes_the_row_with_its_own_reason(
    mode: str,
) -> None:
    fx = _Fixture(_StubCloser(_resolved_draft()))
    session = await fx.open_scene(layer=SCENE_LAYER_SIDE_STORY)

    closing = await fx.service.close_scene(
        _character(), session=session, mode=mode, now=NOW,
    )

    assert closing.session.status == SCENE_CLOSED
    assert closing.session.closed_reason == mode
    stored = await fx.sessions.get(session.id)
    assert stored.status == SCENE_CLOSED and stored.closed_reason == mode
    # and the character is free to raise the curtain again
    assert await fx.sessions.get_open_for_character("c1") is None


async def test_the_wrap_up_lands_in_the_thread_as_scene_narration() -> None:
    fx = _Fixture(_StubCloser(_resolved_draft()))
    session = await fx.open_scene(layer=SCENE_LAYER_SIDE_STORY)

    closing = await fx.service.close_scene(
        _character(), session=session, mode=SCENE_CLOSE_MANUAL, now=NOW,
    )

    assert closing.closing_narration is not None
    assert closing.closing_narration.kind is MessageKind.SCENE_NARRATION
    thread = await fx.thread()
    assert thread[-1].content == CLOSING_NARRATION
    assert thread[-1].kind is MessageKind.SCENE_NARRATION
    assert thread[-1].role is MessageRole.ASSISTANT


async def test_the_writer_reads_the_scene_and_the_players_own_lines() -> None:
    """The transcript is the model's material; the player lines are the
    evidence its citations are checked against (§3.4 #1)."""
    fx = _Fixture(_StubCloser(_resolved_draft()))
    session = await fx.open_scene(
        layer=SCENE_LAYER_SIDE_STORY, player_lines=("我只是路過。", "……好啦。"),
    )

    await fx.service.close_scene(
        _character(), session=session, mode=SCENE_CLOSE_TIMEOUT, now=NOW,
    )

    context = fx.closer.contexts[0]
    assert context.mode == SCENE_CLOSE_TIMEOUT
    assert f"旁白：{OPENING_NARRATION}" in context.transcript
    assert "玩家：我只是路過。" in context.transcript
    assert f"{_character().name}：……你來了。" in context.transcript
    assert context.player_lines == ("我只是路過。", "……好啦。")


async def test_an_earlier_scene_in_the_same_thread_is_not_read_back() -> None:
    """Scoped by ``opened_at``: the previous scene's own closing narration
    must not be handed to this scene's writer as if it were part of it."""
    fx = _Fixture(_StubCloser(_resolved_draft()))
    session = await fx.open_scene(layer=SCENE_LAYER_SIDE_STORY)
    # rewind the session so the thread it was opened against predates it
    later = replace(session, opened_at=NOW + timedelta(hours=1))
    await fx.sessions.save(later)

    await fx.service.close_scene(
        _character(), session=later, mode=SCENE_CLOSE_MANUAL, now=NOW,
    )

    assert fx.closer.contexts[0].transcript == ()
    assert fx.closer.contexts[0].player_lines == ()


# ── idempotency ──────────────────────────────────────────────────────


async def test_a_second_close_changes_nothing() -> None:
    """Two closers racing is ordinary — one wrap-up, one canon write."""
    fx = _Fixture(_StubCloser(_resolved_draft()))
    session = await fx.open_scene(layer=SCENE_LAYER_SIDE_STORY)
    first = await fx.service.close_scene(
        _character(), session=session, mode=SCENE_CLOSE_RESOLVED, now=NOW,
    )

    second = await fx.service.close_scene(
        _character(), session=session, mode=SCENE_CLOSE_MANUAL, now=NOW,
    )

    assert first.already_closed is False
    assert second.already_closed is True
    assert second.closing_narration is None
    # the original reason survives: whoever closed it first decided why
    assert second.session.closed_reason == SCENE_CLOSE_RESOLVED
    # and neither the thread nor canon was written twice
    thread = await fx.thread()
    assert [m.content for m in thread].count(CLOSING_NARRATION) == 1
    assert len(await fx.memories.query("c1")) == 1


# ── degradation: the scene always ends ───────────────────────────────


@pytest.mark.parametrize(
    "closer",
    [
        _StubCloser(None),
        _StubCloser(raises=RuntimeError("upstream exploded")),
    ],
    ids=["refused-or-unavailable", "crashed"],
)
async def test_a_forced_close_still_closes_when_the_writer_gives_nothing(
    closer: _StubCloser,
) -> None:
    """The wrap-up's obligation is that the player is never stuck, not
    that prose always exists (§3.4 #2 — the one price bought the opening).
    """
    fx = _Fixture(closer)
    session = await fx.open_scene(layer=SCENE_LAYER_SIDE_STORY)

    closing = await fx.service.close_scene(
        _character(), session=session, mode=SCENE_CLOSE_TIMEOUT, now=NOW,
    )

    assert closing.session.status == SCENE_CLOSED
    assert closing.closing_narration is None
    thread = await fx.thread()
    assert all(m.content != CLOSING_NARRATION for m in thread)


async def test_canon_degrades_to_the_scenes_own_opening_narration() -> None:
    """The minimal factual record. It is the only prose about this scene
    known to be real and known to contain no player actions, which is
    what makes it usable where an assembled sentence would not be."""
    fx = _Fixture(_StubCloser(None))
    session = await fx.open_scene(layer=SCENE_LAYER_SIDE_STORY)

    closing = await fx.service.close_scene(
        _character(), session=session, mode=SCENE_CLOSE_TIMEOUT, now=NOW,
    )

    assert closing.canon_landed is True
    memories = await fx.memories.query("c1")
    assert [m.content for m in memories] == [OPENING_NARRATION]
    # visible in the row, not only in a log line
    assert "story_scene_unnarrated" in memories[0].tags


async def test_a_degraded_beat_scene_still_advances_the_arc() -> None:
    """The alternative to a thin record is a pending beat the next 起幕
    would re-enact, in a thread where the player can still read the
    first attempt."""
    fx = _Fixture(_StubCloser(None))
    beat = await fx.with_arc()
    session = await fx.open_scene(beat_id=beat.id, arc_id=fx.arc.id)

    await fx.service.close_scene(
        _character(), session=session, mode=SCENE_CLOSE_TIMEOUT, now=NOW,
    )

    realized = (await fx.arcs_repo.get(fx.arc.id)).find_beat(beat.id)
    assert realized.status == BEAT_REALIZED
    events = await fx.events.get_for_day("c1", TODAY.isoformat())
    assert [e.narrative for e in events] == [OPENING_NARRATION]


async def test_an_unwired_closer_still_ends_the_scene() -> None:
    """Self-host with no model: 「結束場景」 must still work."""
    fx = _Fixture(_StubCloser(None))
    fx.service = StorySceneService(
        sessions=fx.sessions,
        conversations=fx.conversations,
        opener=_UnusedOpener(),
        material_providers=(),
        closer=None,
        memory_repository=fx.memories,
        local_tz=timezone.utc,
    )
    session = await fx.open_scene(layer=SCENE_LAYER_SIDE_STORY)

    closing = await fx.service.close_scene(
        _character(), session=session, mode=SCENE_CLOSE_MANUAL, now=NOW,
    )

    assert closing.session.status == SCENE_CLOSED
    assert closing.closing_narration is None


async def test_an_unwritable_thread_does_not_reopen_the_scene() -> None:
    fx = _Fixture(_StubCloser(_resolved_draft()))
    session = await fx.open_scene(layer=SCENE_LAYER_SIDE_STORY)

    async def _always_conflict(*args, **kwargs):  # noqa: ANN002, ANN003
        from kokoro_link.contracts.repositories import AppendResult
        return AppendResult(ok=False, conflict=True)

    fx.conversations.append_messages = _always_conflict  # type: ignore[assignment]

    closing = await fx.service.close_scene(
        _character(), session=session, mode=SCENE_CLOSE_MANUAL, now=NOW,
    )

    assert closing.session.status == SCENE_CLOSED
    assert closing.closing_narration is None
    # canon is a separate write and is not held hostage by the thread
    assert closing.canon_landed is True


async def test_a_canon_write_that_fails_does_not_reopen_the_scene() -> None:
    """Past the close, everything is fail-soft: the row already says the
    player is free, and un-saying that would be worse than a lost memory.
    """
    fx = _Fixture(_StubCloser(_resolved_draft()))
    session = await fx.open_scene(layer=SCENE_LAYER_SIDE_STORY)

    async def _explode(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("memory store down")

    fx.memories.add_many = _explode  # type: ignore[assignment]

    closing = await fx.service.close_scene(
        _character(), session=session, mode=SCENE_CLOSE_MANUAL, now=NOW,
    )

    assert closing.session.status == SCENE_CLOSED
    assert closing.canon_landed is False


# ── the in-turn verdict ──────────────────────────────────────────────


async def test_an_unresolved_verdict_changes_nothing() -> None:
    fx = _Fixture(_StubCloser(StorySceneClosingDraft(resolved=False)))
    session = await fx.open_scene(layer=SCENE_LAYER_SIDE_STORY)

    closing = await fx.service.close_if_resolved(
        _character(), session=session, now=NOW,
    )

    assert closing is None
    live = await fx.sessions.get_open_for_character("c1")
    assert live is not None and live.id == session.id
    # nothing was written anywhere: no wrap-up in the thread, no canon
    assert all(m.content != CLOSING_NARRATION for m in await fx.thread())
    assert await fx.memories.query("c1") == []


async def test_a_failed_verdict_leaves_the_scene_running() -> None:
    """The failure direction is "keep playing" — a crashed writer must
    never end a scene the player is still inside."""
    fx = _Fixture(_StubCloser(raises=RuntimeError("upstream exploded")))
    session = await fx.open_scene(layer=SCENE_LAYER_SIDE_STORY)

    assert await fx.service.close_if_resolved(
        _character(), session=session, now=NOW,
    ) is None
    assert await fx.sessions.get_open_for_character("c1") is not None


async def test_a_resolved_verdict_closes_the_scene_with_one_model_call(
) -> None:
    """The verdict already wrote the wrap-up; closing must not ask again."""
    fx = _Fixture(_StubCloser(_resolved_draft()))
    session = await fx.open_scene(layer=SCENE_LAYER_SIDE_STORY)

    closing = await fx.service.close_if_resolved(
        _character(), session=session, now=NOW,
    )

    assert closing is not None
    assert closing.session.closed_reason == SCENE_CLOSE_RESOLVED
    assert closing.closing_narration.content == CLOSING_NARRATION
    assert len(fx.closer.contexts) == 1
    assert fx.closer.contexts[0].mode == SCENE_CLOSE_RESOLVED


async def test_the_verdict_reads_the_conversation_it_was_handed() -> None:
    """The caller's snapshot already contains the reply that just landed;
    re-reading would cost a round trip and could miss it."""
    fx = _Fixture(_StubCloser(StorySceneClosingDraft(resolved=False)))
    session = await fx.open_scene(layer=SCENE_LAYER_SIDE_STORY)
    fresh = (await fx.conversations.get(session.conversation_id)).append(
        Message(role=MessageRole.USER, content="剛剛才說的話", created_at=NOW),
    )

    await fx.service.close_if_resolved(
        _character(), session=session, conversation=fresh, now=NOW,
    )

    assert "玩家：剛剛才說的話" in fx.closer.contexts[0].transcript


async def test_losing_the_close_race_reports_no_wrap_up_to_the_turn() -> None:
    """Someone else's narration is already in the thread; claiming it as
    this turn's would show the player a wrap-up nobody wrote here."""
    fx = _Fixture(_StubCloser(_resolved_draft()))
    session = await fx.open_scene(layer=SCENE_LAYER_SIDE_STORY)
    await fx.sessions.close(session.id, reason=SCENE_CLOSE_MANUAL, at=NOW)

    assert await fx.service.close_if_resolved(
        _character(), session=session, now=NOW,
    ) is None


# ── the player's button ──────────────────────────────────────────────


async def test_end_scene_closes_whichever_scene_is_running() -> None:
    fx = _Fixture(_StubCloser(_resolved_draft()))
    session = await fx.open_scene(layer=SCENE_LAYER_SIDE_STORY)

    closing = await fx.service.end_scene(_character(), now=NOW)

    assert closing.session.id == session.id
    assert closing.session.closed_reason == SCENE_CLOSE_MANUAL
    assert fx.closer.contexts[0].mode == SCENE_CLOSE_MANUAL


async def test_end_scene_without_a_live_scene_is_refused() -> None:
    fx = _Fixture(_StubCloser(_resolved_draft()))

    with pytest.raises(SceneNotOpen) as caught:
        await fx.service.end_scene(_character(), now=NOW)

    assert caught.value.code == "no_open_scene"


async def test_a_stale_tab_cannot_close_the_scene_opened_after_it() -> None:
    fx = _Fixture(_StubCloser(_resolved_draft()))
    session = await fx.open_scene(layer=SCENE_LAYER_SIDE_STORY)

    with pytest.raises(SceneSessionMismatch) as caught:
        await fx.service.end_scene(
            _character(), session_id="a-scene-that-ended", now=NOW,
        )

    assert caught.value.code == "scene_session_mismatch"
    assert caught.value.running == session.id
    live = await fx.sessions.get_open_for_character("c1")
    assert live is not None and live.id == session.id


async def test_end_scene_accepts_the_matching_session_id() -> None:
    fx = _Fixture(_StubCloser(_resolved_draft()))
    session = await fx.open_scene(layer=SCENE_LAYER_SIDE_STORY)

    closing = await fx.service.end_scene(
        _character(), session_id=session.id, now=NOW,
    )

    assert closing.session.status == SCENE_CLOSED


# ── the repository fence ─────────────────────────────────────────────


async def test_close_is_a_compare_and_set_on_the_repository() -> None:
    sessions = InMemoryStorySceneSessionRepository()
    session = StorySceneSession.open_scene(
        character_id="c1",
        conversation_id="conv-1",
        source_layer=SCENE_LAYER_SIDE_STORY,
        opened_at=NOW,
    )
    await sessions.add(session)

    won = await sessions.close(session.id, reason=SCENE_CLOSE_MANUAL, at=NOW)
    lost = await sessions.close(
        session.id, reason=SCENE_CLOSE_TIMEOUT, at=NOW + timedelta(hours=1),
    )

    assert won is not None and won.closed_reason == SCENE_CLOSE_MANUAL
    assert lost is None
    assert (await sessions.get(session.id)).closed_reason == SCENE_CLOSE_MANUAL


async def test_closing_a_session_that_never_existed_is_not_an_error() -> None:
    sessions = InMemoryStorySceneSessionRepository()

    assert await sessions.close(
        "gone", reason=SCENE_CLOSE_TIMEOUT, at=NOW,
    ) is None


async def test_the_sql_backend_fences_the_close_the_same_way() -> None:
    """The in-memory store is single-process; the fence that matters in
    production is the ``WHERE status = 'open'`` clause."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from kokoro_link.infrastructure.persistence.engine import (
        build_session_factory,
    )
    from kokoro_link.infrastructure.persistence.models import (
        StorySceneSessionRow,
    )
    from kokoro_link.infrastructure.persistence.sa_story_scene_session_repository import (  # noqa: E501
        SAStorySceneSessionRepository,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda s: StorySceneSessionRow.__table__.create(s),
            )
        sessions = SAStorySceneSessionRepository(build_session_factory(engine))
        session = StorySceneSession.open_scene(
            character_id="c1",
            conversation_id="conv-1",
            source_layer=SCENE_LAYER_BEAT,
            opened_at=NOW,
        )
        await sessions.add(session)

        won = await sessions.close(
            session.id, reason=SCENE_CLOSE_RESOLVED, at=NOW + timedelta(minutes=5),
        )
        lost = await sessions.close(
            session.id, reason=SCENE_CLOSE_TIMEOUT, at=NOW + timedelta(hours=9),
        )

        assert won is not None
        assert won.status == SCENE_CLOSED
        assert won.closed_reason == SCENE_CLOSE_RESOLVED
        # the idle clock is monotonic across the close, as in the entity
        assert won.last_activity_at == NOW + timedelta(minutes=5)
        assert lost is None
        stored = await sessions.get(session.id)
        assert stored.closed_reason == SCENE_CLOSE_RESOLVED
        assert await sessions.get_open_for_character("c1") is None
    finally:
        await engine.dispose()


async def test_a_player_pull_that_never_closed_stays_pending() -> None:
    """Sanity anchor for the degradation tests above: without a close the
    beat is untouched, so a realized beat there really is the wrap-up's
    doing."""
    fx = _Fixture(_StubCloser(_resolved_draft()))
    beat = await fx.with_arc()
    await fx.open_scene(beat_id=beat.id, arc_id=fx.arc.id)

    still = (await fx.arcs_repo.get(fx.arc.id)).find_beat(beat.id)
    assert still.status == BEAT_PENDING
