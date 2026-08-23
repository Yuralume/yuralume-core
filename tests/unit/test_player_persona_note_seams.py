"""PP3 — the player's declared persona reaches every surface that speaks to them.

One rule, six seams, and the same two assertions at each: a declared note
shows up in the prompt, and no declaration leaves the prompt without a
trace. The "no trace" half is checked against
:data:`PLAYER_PERSONA_NOTE_HEADER` rather than against the note text,
because a seam that renders an empty header (or a stray blank block) is
still a seam that changed a prompt for a player who declared nothing.

The seventh test group is the one that is not about rendering at all: the
proactive / release path fans one composed message out to web, the local
binding and the hosted channel together, so the note has to answer to the
least private of them. Those tests pin the gate, not the block.

The last group runs the rule backwards. "Every surface that speaks to
*them*" is a boundary, not a wish list: LumeGram composes a public post
with no player in the room, and a character-to-character encounter is two
characters talking to each other. Neither may carry the owner's declared
identity, and neither has a red light of its own to say so — the block is
four lines any future edit could paste in "for consistency". The encounter
half of that guard lives beside the encounter prompt harness in
``test_encounter_speaker_contexts.py``.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.chat_assist_service import ChatAssistService
from kokoro_link.contracts.feed import FeedComposerInput
from kokoro_link.contracts.feed_comment_reply import FeedCommentReplyInput
from kokoro_link.contracts.pending_follow_up_composer import (
    PendingFollowUpComposeInput,
)
from kokoro_link.contracts.proactive import ProactiveContext
from kokoro_link.contracts.scheduled_promise_composer import (
    ScheduledPromiseComposeInput,
)
from kokoro_link.contracts.story_scene import (
    StorySceneClosingContext,
    StorySceneMaterial,
    StorySceneOpeningContext,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.feed_comment import FeedComment
from kokoro_link.domain.entities.feed_post import FeedPost, FeedKind, FeedSource
from kokoro_link.domain.entities.messaging_account import MessagingAccount
from kokoro_link.domain.entities.pending_follow_up import PendingFollowUpMessage
from kokoro_link.domain.entities.player_persona_note import PlayerPersonaNote
from kokoro_link.domain.entities.story_scene_session import (
    SCENE_CLOSE_TIMEOUT,
    SCENE_LAYER_BEAT,
    StorySceneSession,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.busy.llm_follow_up_composer import (
    _build_prompt as build_follow_up_prompt,
)
from kokoro_link.infrastructure.busy.llm_scheduled_promise_composer import (
    _build_prompt as build_promise_prompt,
)
from kokoro_link.infrastructure.feed.llm_comment_reply import (
    _build_prompt as build_comment_reply_prompt,
)
from kokoro_link.infrastructure.feed.llm_composer import (
    _build_prompt as build_feed_post_prompt,
)
from kokoro_link.infrastructure.post_turn.llm_processor import (
    _build_prompt as build_post_turn_prompt,
)
from kokoro_link.infrastructure.proactive.llm_decider import (
    _build_prompt as build_proactive_prompt,
)
from kokoro_link.infrastructure.prompt.player_persona_note_lines import (
    PLAYER_PERSONA_NOTE_HEADER,
)
from kokoro_link.infrastructure.story.llm_scene_closer import (
    _build_prompt as build_scene_closer_prompt,
)
from kokoro_link.infrastructure.story.llm_scene_opener import (
    _build_prompt as build_scene_opener_prompt,
)


NOTE = "我是隱瞞身分的超能力者，白天在同一間事務所上班。"
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 6, 1)


def _character() -> Character:
    return Character.create(
        name="Mio",
        summary="在咖啡店打工的大學生",
        personality=["溫柔"],
        interests=["吉他"],
        speaking_style="輕柔",
        boundaries=[],
        state=CharacterState(
            emotion="平靜", affection=60, fatigue=20, trust=70, energy=80,
        ),
    )


def _assert_injected(prompt: str) -> None:
    assert PLAYER_PERSONA_NOTE_HEADER in prompt
    assert NOTE in prompt


def _assert_traceless(prompt: str) -> None:
    assert PLAYER_PERSONA_NOTE_HEADER not in prompt


# ── seam 1: proactive ────────────────────────────────────────────────


def _proactive_context(note: str) -> ProactiveContext:
    return ProactiveContext(
        character=_character(),
        trigger=ProactiveTrigger.TICK,
        now=NOW,
        current_activity=None,
        upcoming_activities=[],
        schedule=None,
        idle_minutes=90.0,
        sent_today=0,
        last_proactive_at=None,
        recent_memories_text="",
        active_goals_text="",
        player_persona_note=note,
    )


def test_proactive_compose_prompt_carries_the_declaration() -> None:
    _assert_injected(build_proactive_prompt(_proactive_context(NOTE)))


def test_proactive_compose_prompt_is_traceless_without_one() -> None:
    _assert_traceless(build_proactive_prompt(_proactive_context("")))


# ── seam 2: busy-defer follow-up and scheduled promise ───────────────


def _follow_up_payload(note: str) -> PendingFollowUpComposeInput:
    return PendingFollowUpComposeInput(
        character=_character(),
        queued_messages=(
            PendingFollowUpMessage(
                content="剛剛那個怎麼樣了？", queued_at=NOW - timedelta(hours=1),
            ),
        ),
        brief_reply="等我開完會再回你",
        defer_reason="會議中",
        queued_at=NOW - timedelta(hours=1),
        just_finished_activity=None,
        current_activity=None,
        recent_dialogue_summary=None,
        now=NOW,
        player_persona_note=note,
    )


def _promise_payload(note: str) -> ScheduledPromiseComposeInput:
    return ScheduledPromiseComposeInput(
        character=_character(),
        promise_intent="叫使用者起床",
        promise_text="明天十點叫我起床",
        scheduled_for=NOW,
        current_activity=None,
        just_finished_activity=None,
        recent_dialogue_summary=None,
        now=NOW,
        player_persona_note=note,
    )


def test_follow_up_prompt_carries_the_declaration() -> None:
    _assert_injected(build_follow_up_prompt(_follow_up_payload(NOTE)))


def test_follow_up_prompt_is_traceless_without_one() -> None:
    _assert_traceless(build_follow_up_prompt(_follow_up_payload("")))


def test_scheduled_promise_prompt_carries_the_declaration() -> None:
    _assert_injected(build_promise_prompt(_promise_payload(NOTE)))


def test_scheduled_promise_prompt_is_traceless_without_one() -> None:
    _assert_traceless(build_promise_prompt(_promise_payload("")))


# ── seam 3: 起幕 opener and closer ───────────────────────────────────

SCENE_NOTE = "她在這場戲裡站在門邊，還沒決定要不要進來。"
"""The *other* note: a per-scene staging remark that happens to share the
``operator_note`` name. The tests below keep both present at once so a
future edit cannot quietly collapse them into one another."""


def _opening_context(note: str) -> StorySceneOpeningContext:
    return StorySceneOpeningContext(
        character=_character(),
        material=StorySceneMaterial(
            layer=SCENE_LAYER_BEAT,
            title="頂樓的告白",
            summary="她終於把話說出口",
            arc_id="arc-1",
            beat_id="beat-1",
            operator_position="present",
            operator_note=SCENE_NOTE,
        ),
        today=TODAY,
        player_persona_note=note,
    )


def _closing_context(note: str) -> StorySceneClosingContext:
    return StorySceneClosingContext(
        character=_character(),
        session=StorySceneSession.open_scene(
            character_id="c1",
            conversation_id="conv-1",
            source_layer=SCENE_LAYER_BEAT,
            beat_id="beat-1",
            title="把話說完",
            operator_position="present",
            operator_note=SCENE_NOTE,
            opened_at=NOW,
        ),
        mode=SCENE_CLOSE_TIMEOUT,
        transcript=("玩家：我只是路過。",),
        player_lines=("我只是路過。",),
        today=TODAY,
        player_persona_note=note,
    )


def test_scene_opener_prompt_carries_the_declaration() -> None:
    prompt = build_scene_opener_prompt(_opening_context(NOTE))
    _assert_injected(prompt)
    # The scene's own staging note survives untouched alongside it.
    assert SCENE_NOTE in prompt


def test_scene_opener_prompt_is_traceless_without_one() -> None:
    prompt = build_scene_opener_prompt(_opening_context(""))
    _assert_traceless(prompt)
    assert SCENE_NOTE in prompt


def test_scene_closer_prompt_carries_the_declaration() -> None:
    prompt = build_scene_closer_prompt(_closing_context(NOTE))
    _assert_injected(prompt)
    assert SCENE_NOTE in prompt


def test_scene_closer_prompt_is_traceless_without_one() -> None:
    prompt = build_scene_closer_prompt(_closing_context(""))
    _assert_traceless(prompt)
    assert SCENE_NOTE in prompt


# ── seam 4: LumeGram comment reply ───────────────────────────────────


def _comment_reply_payload(note: str) -> FeedCommentReplyInput:
    character = _character()
    post = FeedPost.create(
        character_id=character.id,
        kind=FeedKind.MOOD,
        content_text="今天的天空好藍。",
        source=FeedSource.manual(),
    )
    return FeedCommentReplyInput(
        character=character,
        post=post,
        user_comments=(
            FeedComment.create(post_id=post.id, content_text="哪裡拍的？"),
        ),
        player_persona_note=note,
    )


def test_comment_reply_prompt_carries_the_declaration() -> None:
    _assert_injected(build_comment_reply_prompt(_comment_reply_payload(NOTE)))


def test_comment_reply_prompt_is_traceless_without_one() -> None:
    _assert_traceless(build_comment_reply_prompt(_comment_reply_payload("")))


# ── seam 5: 發話輔助 ─────────────────────────────────────────────────


class _StubNoteRepository:
    def __init__(self, note: str | None) -> None:
        self._note = note
        self.calls: list[tuple[str, str]] = []

    async def get(self, *, character_id: str, operator_id: str):  # noqa: ANN201
        self.calls.append((character_id, operator_id))
        if not self._note:
            return None
        return PlayerPersonaNote.create(
            character_id=character_id, operator_id=operator_id, note=self._note,
        )

    async def upsert(self, note) -> None:  # pragma: no cover - unused
        raise AssertionError("read-only surface must never write")

    async def delete(self, *, character_id: str, operator_id: str) -> bool:
        raise AssertionError("read-only surface must never delete")


def _assist_service(note: str | None) -> ChatAssistService:
    return ChatAssistService(
        character_service=object(),  # type: ignore[arg-type]
        active_llm_provider=object(),  # type: ignore[arg-type]
        player_persona_note_repository=_StubNoteRepository(note),
    )


@pytest.mark.asyncio
async def test_chat_assist_prompt_carries_the_declaration_and_its_own_rail() -> None:
    service = _assist_service(NOTE)

    prompt = await service._build_prompt(_character(), user_id="user-1", count=3)

    _assert_injected(prompt)
    # This surface writes the *player's* lines, so it earns an extra
    # instruction the character-facing seams must not get.
    assert "建議台詞" in prompt


@pytest.mark.asyncio
async def test_chat_assist_prompt_is_traceless_without_one() -> None:
    service = _assist_service(None)

    prompt = await service._build_prompt(_character(), user_id="user-1", count=3)

    _assert_traceless(prompt)


# ── seam 6: post-turn extraction ─────────────────────────────────────


def _post_turn_prompt(note: str) -> str:
    return build_post_turn_prompt(
        character=_character(),
        user_message="我今天又用了一次能力。",
        assistant_message="……小心一點。",
        recent_messages=[],
        now=NOW,
        player_persona_note=note,
    )


def test_post_turn_prompt_tells_the_extractor_to_skip_declared_facts() -> None:
    prompt = _post_turn_prompt(NOTE)
    _assert_injected(prompt)
    assert "不需再抽成記憶" in prompt


def test_post_turn_prompt_is_traceless_without_one() -> None:
    prompt = _post_turn_prompt("")
    _assert_traceless(prompt)
    assert "不需再抽成記憶" not in prompt


# ── the gate: who may hear a declaration on an outbound channel ──────


def _account(*senders: str) -> MessagingAccount:
    return MessagingAccount.create(
        character_id="c1",
        platform=Platform.TELEGRAM,
        credentials={"bot_token": "t"},
        allowed_sender_refs=tuple(senders),
    )


def _dispatcher(note: str):  # noqa: ANN201
    from kokoro_link.application.services.proactive_dispatcher import (
        ProactiveDispatcher,
    )

    return ProactiveDispatcher.__new__(ProactiveDispatcher), _StubNoteRepository(note)


async def _load_note(account: MessagingAccount | None, note: str = NOTE) -> str:
    dispatcher, repository = _dispatcher(note)
    dispatcher._player_persona_note_repository = repository
    character = _character()
    return await dispatcher._load_player_persona_note(character, account)


@pytest.mark.asyncio
async def test_proactive_note_survives_a_single_identified_recipient() -> None:
    assert await _load_note(_account("tg-user-1")) == NOTE


@pytest.mark.asyncio
async def test_proactive_note_is_blanked_for_a_multi_sender_account() -> None:
    """A group / shared chat is not the account owner's stage."""
    assert await _load_note(_account("tg-user-1", "tg-user-2")) == ""


@pytest.mark.asyncio
async def test_proactive_note_is_blanked_for_an_open_allowlist() -> None:
    """An empty allowlist means "accept anyone" — same defect, same answer."""
    assert await _load_note(_account()) == ""


@pytest.mark.asyncio
async def test_proactive_note_survives_a_web_only_character() -> None:
    """No external binding at all: only the owner's own session is reachable."""
    assert await _load_note(None) == NOTE


class _GateStub:
    """Stands in for the proactive dispatcher's *pinned* verdict.

    The verdict the release path consults is
    ``persona_disclosure_allowed_for(sink)`` — pure, and answered from the
    snapshot the send will use. The unpinned ``persona_disclosure_allowed``
    is offered too, and always says *yes*: it is the door that must stay
    shut, so a test that simply omitted it could not tell a closed door
    from an absent one.
    """

    def __init__(self, allowed: bool | Exception) -> None:
        self._allowed = allowed
        self.sinks_seen: list[object] = []
        self.unpinned_asks = 0

    def persona_disclosure_allowed_for(self, sink) -> bool:  # noqa: ANN001
        self.sinks_seen.append(sink)
        if isinstance(self._allowed, Exception):
            raise self._allowed
        return self._allowed

    async def persona_disclosure_allowed(self, character_id: str) -> bool:
        self.unpinned_asks += 1
        return True


_PINNED_SINK = object()
"""Any non-``None`` snapshot: the stub's verdict is what's under test here,
not how a real sink is shaped (that lives in the binding-pin suite)."""


def _release_dispatcher(note: str, gate):  # noqa: ANN001, ANN201
    from kokoro_link.application.services.pending_follow_up_dispatcher import (
        PendingFollowUpDispatcher,
    )

    dispatcher = PendingFollowUpDispatcher.__new__(PendingFollowUpDispatcher)
    dispatcher._player_persona_note_repository = _StubNoteRepository(note)
    dispatcher._proactive_dispatcher = gate
    return dispatcher


@pytest.mark.asyncio
async def test_release_note_follows_the_proactive_gate() -> None:
    allowed = _release_dispatcher(NOTE, _GateStub(True))
    blocked = _release_dispatcher(NOTE, _GateStub(False))

    assert await allowed._safe_player_persona_note(
        _character(), sink=_PINNED_SINK,
    ) == NOTE
    assert await blocked._safe_player_persona_note(
        _character(), sink=_PINNED_SINK,
    ) == ""


@pytest.mark.asyncio
async def test_release_note_fails_closed_when_the_gate_cannot_answer() -> None:
    raising = _release_dispatcher(NOTE, _GateStub(RuntimeError("boom")))
    absent = _release_dispatcher(NOTE, object())

    assert await raising._safe_player_persona_note(
        _character(), sink=_PINNED_SINK,
    ) == ""
    assert await absent._safe_player_persona_note(
        _character(), sink=_PINNED_SINK,
    ) == ""


@pytest.mark.asyncio
async def test_release_note_fails_closed_without_a_pinned_sink() -> None:
    """No pin is a refusal, never a licence to look the answer up again.

    ``resolve_proactive_sink`` swallows its own failures and answers
    ``None``, so "unpinned" and "the lookup just broke" are the same state
    here. Re-asking would let a transient failure be retried into a *yes*
    while the send is left to resolve its own endpoint — the very split the
    pin exists to close. A gate that would happily say yes must still be
    refused when there is nothing pinned to say yes *about*.
    """
    permissive = _GateStub(True)
    dispatcher = _release_dispatcher(NOTE, permissive)

    assert await dispatcher._safe_player_persona_note(_character()) == ""
    assert permissive.sinks_seen == []
    assert permissive.unpinned_asks == 0


# ── the boundary: surfaces that must stay traceless, note or no note ──


def test_lumegram_post_prompt_never_carries_the_declaration() -> None:
    """A feed post is written *to nobody*: it is published, not spoken.

    The declaration is a staging instruction for how the character treats
    the player in front of it, and the composer has no player in front of
    it — the post goes to whoever opens LumeGram. There is nothing to
    stage, and a lot to leak. The assertion is on the header rather than
    on the note text because there is no note to plant here: the whole
    point is that this surface has no channel for one, and adding a
    channel is what must go red.
    """
    character = _character()
    post_prompt = build_feed_post_prompt(
        FeedComposerInput(
            character=character,
            kind=FeedKind.MOOD,
            source=FeedSource.silence(),
            hint="說點今天的小事。",
            context_snippets=(),
            image_required=False,
        ),
    )

    # Cheap non-vacuity check: an absence assertion against an empty
    # string would pass forever.
    assert "說點今天的小事。" in post_prompt
    _assert_traceless(post_prompt)
