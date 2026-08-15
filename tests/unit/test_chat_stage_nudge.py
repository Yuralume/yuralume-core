"""示意（「讓{角色}先開口」）—— SN1 後端行為.

The player presses a button instead of typing. Three shapes of turn have
to behave, and one hole has to stay closed:

1. supplement → an ordinary user message wrapped as ``*動作*``, then a reply
2. no supplement → **no player message at all**, only the reply
3. supplement that already carries ``*`` → stored verbatim, not double-wrapped

and the ``max_messages_per_session`` ceiling must count the silent
turn, which writes nothing on the player side and used to slip past a
counter that only looked at user messages.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.dto.chat import (
    PresenceFramePayload,
    SendChatMessageRequest,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.chat_service import (
    ChatRuntimeLimitExceeded,
    ChatService,
)
from kokoro_link.application.services.stage_nudge import (
    StageNudgeTurn,
    wrap_supplement,
)
from kokoro_link.application.services.turn_undo_service import TurnUndoService
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageKind,
    MessageRole,
)
from kokoro_link.domain.entities.pending_follow_up import (
    PendingFollowUp,
    PendingFollowUpMessage,
    PendingFollowUpStatus,
)
from kokoro_link.domain.entities.story_scene_session import (
    SCENE_LAYER_SIDE_STORY,
    StorySceneSession,
)
from kokoro_link.domain.value_objects.account_runtime_profile import (
    AccountRuntimeProfile,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.presence_frame import (
    ChatChannel,
    ChatSurface,
    VisibilityMode,
)
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.post_turn.null_processor import NullPostTurnProcessor
from kokoro_link.infrastructure.prompt.default import (
    LATEST_USER_MESSAGE_MARKER,
    DefaultPromptContextBuilder,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_pending_follow_ups import (
    InMemoryPendingFollowUpRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_story_scene_sessions import (
    InMemoryStorySceneSessionRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_turn_journals import (
    InMemoryTurnJournalRepository,
)
from kokoro_link.infrastructure.state.simple import SimpleStateEngine


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------


class _StaticRuntimeProfileResolver:
    def __init__(self, profile: AccountRuntimeProfile) -> None:
        self._profile = profile

    async def resolve_for_operator(self, operator_id: str) -> AccountRuntimeProfile:
        return self._profile


class _RecordingPromptBuilder:
    """Captures every ``build`` kwarg so a test can assert on the flag."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def build(self, **kwargs) -> str:  # noqa: ANN003
        self.calls.append(dict(kwargs))
        return f"最新使用者訊息：{kwargs.get('latest_user_message', '')}"


class _CountingDecider:
    """Busy decider that only records that it was consulted.

    A silent 示意 must never reach it — that is the whole point of the
    "cancel, but do not open a new row" rule.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def decide(self, **kwargs) -> None:  # noqa: ANN003
        self.calls += 1
        raise AssertionError("busy decider consulted on a turn that forbids it")


def _wire(
    *,
    session_limit: int | None = None,
    prompt_builder=None,
    journal_repository=None,
    scene_sessions=None,
    pending_follow_ups=None,
    busy_reply_decider=None,
) -> tuple[ChatService, CharacterService, InMemoryConversationRepository]:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=InMemoryMemoryRepository(),
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=prompt_builder or DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        journal_repository=journal_repository,
        story_scene_sessions=scene_sessions,
        pending_follow_up_repository=pending_follow_ups,
        busy_reply_decider=busy_reply_decider,
        account_runtime_profile_resolver=(
            None if session_limit is None else _StaticRuntimeProfileResolver(
                AccountRuntimeProfile(
                    name="capped-test", max_messages_per_session=session_limit,
                ),
            )
        ),
    )
    character_service = CharacterService(
        character_repository, conversation_repository=conversation_repository,
    )
    return chat_service, character_service, conversation_repository


def _stage_frame() -> PresenceFramePayload:
    return PresenceFramePayload(
        surface=ChatSurface.WEB_STAGE,
        channel=ChatChannel.KOKORO_STAGE,
        visibility=VisibilityMode.VIRTUAL_SAME_SPACE,
    )


async def _drain(stream) -> str:  # noqa: ANN001
    return "".join([chunk async for chunk in stream])


# --------------------------------------------------------------------------
# the wrapping rule
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("我推開門進來", "*我推開門進來*"),
        ("  我推開門進來  ", "*我推開門進來*"),
        ("", ""),
        ("   ", ""),
        # Already carries the convention somewhere — left exactly as typed.
        ("*我推開門進來*", "*我推開門進來*"),
        ("我推開門 *小聲地* 說", "我推開門 *小聲地* 說"),
    ],
)
def test_supplement_wrapping(raw: str, expected: str) -> None:
    assert wrap_supplement(raw) == expected


def test_ordinary_turn_still_writes_its_user_message_even_when_empty() -> None:
    """A bare ``/pic`` strips to nothing yet is still the player speaking.

    Only the nudge flag may suppress the player row; the pre-SN1 behaviour
    for every other empty-after-cleaning turn is untouched.
    """
    ordinary = StageNudgeTurn.resolve("", stage_nudge=False)
    assert ordinary.writes_user_message is True
    assert StageNudgeTurn.resolve("", stage_nudge=True).writes_user_message is False


# --------------------------------------------------------------------------
# the request contract
# --------------------------------------------------------------------------


def test_empty_message_is_rejected_without_the_nudge_flag() -> None:
    with pytest.raises(ValidationError):
        SendChatMessageRequest(character_id="c1", message="")
    with pytest.raises(ValidationError):
        SendChatMessageRequest(character_id="c1")


def test_empty_message_is_accepted_on_a_nudge() -> None:
    payload = SendChatMessageRequest(character_id="c1", stage_nudge=True)
    assert payload.message == ""
    assert payload.stage_nudge is True


def test_nudge_defaults_to_off() -> None:
    assert SendChatMessageRequest(
        character_id="c1", message="hi",
    ).stage_nudge is False


@pytest.mark.parametrize("supplement", ["", "   "])
def test_silent_nudge_with_attachments_is_rejected(supplement: str) -> None:
    """No player message = nowhere to hang the picture.

    The attachment would still reach *this* turn's vision prompt, so the
    character comments on an image that never lands in history: reload and
    the thread shows the character discussing a picture nobody sent.
    Rejected at the contract instead — the official client never sends this
    combination, only a direct API caller can produce it.
    """
    with pytest.raises(ValidationError):
        SendChatMessageRequest(
            character_id="c1",
            message=supplement,
            stage_nudge=True,
            attachment_urls=["/uploads/chat-uploads/a.png"],
        )


def test_nudge_with_a_supplement_may_still_carry_attachments() -> None:
    """The supplement *is* a user message, so the picture has a home."""
    payload = SendChatMessageRequest(
        character_id="c1",
        message="我推開門進來",
        stage_nudge=True,
        attachment_urls=["/uploads/chat-uploads/a.png"],
    )
    assert payload.attachment_urls == ["/uploads/chat-uploads/a.png"]


def test_ordinary_turn_with_attachments_is_untouched() -> None:
    payload = SendChatMessageRequest(
        character_id="c1",
        message="看這張",
        attachment_urls=["/uploads/chat-uploads/a.png"],
    )
    assert payload.stage_nudge is False
    assert payload.attachment_urls == ["/uploads/chat-uploads/a.png"]


# --------------------------------------------------------------------------
# the three shapes of turn
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nudge_with_supplement_stores_it_as_a_wrapped_user_message() -> None:
    chat, characters, conversations = _wire()
    created = await characters.create_character(CreateCharacterRequest(name="Mio"))

    response = await chat.send_message(SendChatMessageRequest(
        character_id=created.id,
        message="我推開門進來",
        stage_nudge=True,
        presence_frame=_stage_frame(),
    ))

    conversation = await conversations.get(response.conversation_id)
    assert [(m.role, m.content, m.kind) for m in conversation.messages][:1] == [
        (MessageRole.USER, "*我推開門進來*", MessageKind.CHAT),
    ]
    assert conversation.messages[1].role is MessageRole.ASSISTANT
    assert response.user_message is not None
    assert response.user_message.content == "*我推開門進來*"
    assert response.assistant_message is not None


@pytest.mark.asyncio
async def test_nudge_supplement_that_already_has_asterisks_is_not_rewrapped() -> None:
    chat, characters, conversations = _wire()
    created = await characters.create_character(CreateCharacterRequest(name="Mio"))

    response = await chat.send_message(SendChatMessageRequest(
        character_id=created.id,
        message="我推開門 *小聲地* 說",
        stage_nudge=True,
        presence_frame=_stage_frame(),
    ))

    conversation = await conversations.get(response.conversation_id)
    assert conversation.messages[0].content == "我推開門 *小聲地* 說"


@pytest.mark.asyncio
async def test_silent_nudge_writes_only_the_character_reply() -> None:
    chat, characters, conversations = _wire()
    created = await characters.create_character(CreateCharacterRequest(name="Mio"))

    response = await chat.send_message(SendChatMessageRequest(
        character_id=created.id,
        message="",
        stage_nudge=True,
        presence_frame=_stage_frame(),
    ))

    conversation = await conversations.get(response.conversation_id)
    assert [m.role for m in conversation.messages] == [MessageRole.ASSISTANT]
    assert response.user_message is None
    assert response.assistant_message is not None


@pytest.mark.asyncio
async def test_silent_nudge_streams_and_finalises_without_a_user_message() -> None:
    chat, characters, conversations = _wire()
    created = await characters.create_character(CreateCharacterRequest(name="Mio"))

    stream, finalizer = await chat.send_message_stream(SendChatMessageRequest(
        character_id=created.id,
        message="",
        stage_nudge=True,
        presence_frame=_stage_frame(),
    ))
    text = await _drain(stream)
    response = await finalizer.finish(text)

    conversation = await conversations.get(response.conversation_id)
    assert [m.role for m in conversation.messages] == [MessageRole.ASSISTANT]
    assert response.user_message is None
    assert response.assistant_message is not None


@pytest.mark.asyncio
async def test_streamed_nudge_supplement_is_wrapped_too() -> None:
    chat, characters, conversations = _wire()
    created = await characters.create_character(CreateCharacterRequest(name="Mio"))

    stream, finalizer = await chat.send_message_stream(SendChatMessageRequest(
        character_id=created.id,
        message="我推開門進來",
        stage_nudge=True,
        presence_frame=_stage_frame(),
    ))
    response = await finalizer.finish(await _drain(stream))

    conversation = await conversations.get(response.conversation_id)
    assert conversation.messages[0].content == "*我推開門進來*"
    assert conversation.messages[1].role is MessageRole.ASSISTANT


# --------------------------------------------------------------------------
# the prompt
# --------------------------------------------------------------------------


def _neutral_state() -> CharacterState:
    return CharacterState(
        emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
    )


def _character() -> Character:
    return Character.create(
        name="Mio",
        summary="鄰居",
        personality=["溫和"],
        interests=["咖啡"],
        speaking_style="平實",
        boundaries=[],
        state=_neutral_state(),
    )


def _build_prompt(
    *,
    stage_nudge: bool,
    presence_frame=None,  # noqa: ANN001
    latest_user_message: str = "",
) -> str:
    return DefaultPromptContextBuilder().build(
        character=_character(),
        conversation=Conversation.start(character_id="c1"),
        recent_messages=[],
        memories=[],
        pending_state=_neutral_state(),
        latest_user_message=latest_user_message,
        stage_nudge=stage_nudge,
        presence_frame=presence_frame,
    )


def test_prompt_has_no_nudge_block_on_an_ordinary_turn() -> None:
    assert "玩家請你先開口" not in _build_prompt(stage_nudge=False)


def test_prompt_nudge_block_frames_the_turn_and_carries_the_red_line() -> None:
    prompt = _build_prompt(stage_nudge=True)
    assert "本輪玩家沒有對你說話" in prompt
    assert "請你主動開口" in prompt
    # The red line the plan pins verbatim: acknowledging what the player
    # declared is allowed, inventing more of it is not.
    assert "不得替玩家虛構新的行動或台詞" in prompt


def test_prompt_nudge_block_degrades_to_neutral_off_stage() -> None:
    """Not a gate — a DM frame still gets its turn, just framed neutrally."""
    from kokoro_link.domain.value_objects.presence_frame import PresenceFrame

    prompt = _build_prompt(stage_nudge=True, presence_frame=PresenceFrame.web_dm())
    assert "玩家請你先開口" in prompt
    assert "只是示意你先起頭" in prompt
    assert "不得替玩家虛構新的行動或台詞" in prompt


def test_silent_nudge_prompt_drops_the_empty_latest_user_message_line() -> None:
    """The two lines used to contradict each other.

    「本輪玩家沒有對你說話」 immediately followed by an empty
    「最新使用者訊息：」 reads as *a message arrived and it was blank*,
    which is exactly the turn shape most likely to draw 「你怎麼不說話」 —
    on the feature's single most common press.
    """
    prompt = _build_prompt(stage_nudge=True)
    assert "本輪玩家沒有對你說話" in prompt
    assert LATEST_USER_MESSAGE_MARKER not in prompt


def test_nudge_with_a_supplement_still_renders_the_latest_user_line() -> None:
    prompt = _build_prompt(stage_nudge=True, latest_user_message="*我推開門進來*")
    assert f"{LATEST_USER_MESSAGE_MARKER}*我推開門進來*" in prompt


def test_ordinary_empty_turn_keeps_its_latest_user_line() -> None:
    """A bare ``/pic`` cleans down to nothing and is still the player
    speaking — the pre-SN1 prompt for that turn must not move a byte."""
    assert LATEST_USER_MESSAGE_MARKER in _build_prompt(stage_nudge=False)


@pytest.mark.asyncio
async def test_chat_turn_passes_the_flag_to_the_prompt_builder() -> None:
    builder = _RecordingPromptBuilder()
    chat, characters, _ = _wire(prompt_builder=builder)
    created = await characters.create_character(CreateCharacterRequest(name="Mio"))

    await chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="", stage_nudge=True,
        presence_frame=_stage_frame(),
    ))
    assert builder.calls
    assert builder.calls[-1]["stage_nudge"] is True

    await chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="嗨",
        presence_frame=_stage_frame(),
    ))
    assert builder.calls[-1]["stage_nudge"] is False


# --------------------------------------------------------------------------
# inside a 起幕 scene
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nudge_inside_a_scene_keeps_the_frame_and_the_idle_clock() -> None:
    """A nudge pressed inside a live scene is an ordinary in-scene turn.

    Both halves matter: the scene frame still reaches the prompt (the
    character keeps playing the scene), and the idle clock still advances
    (the scene is not swept away under a player who is interacting).
    """
    sessions = InMemoryStorySceneSessionRepository()
    builder = _RecordingPromptBuilder()
    chat, characters, conversations = _wire(
        prompt_builder=builder, scene_sessions=sessions,
    )
    created = await characters.create_character(CreateCharacterRequest(name="Mio"))
    conversation = Conversation.start(character_id=created.id)
    await conversations.save(conversation)
    opened_at = datetime(2020, 1, 1, 9, 0, tzinfo=timezone.utc)
    await sessions.add(StorySceneSession.open_scene(
        character_id=created.id,
        conversation_id=conversation.id,
        source_layer=SCENE_LAYER_SIDE_STORY,
        title="把話說完",
        opened_at=opened_at,
    ))

    await chat.send_message(SendChatMessageRequest(
        character_id=created.id,
        conversation_id=conversation.id,
        message="", stage_nudge=True,
        presence_frame=_stage_frame(),
    ))

    live = await sessions.get_open_for_character(created.id)
    assert live is not None
    assert live.last_activity_at > opened_at
    assert builder.calls[-1]["story_scene"] is not None
    assert builder.calls[-1]["stage_nudge"] is True


# --------------------------------------------------------------------------
# the open busy-defer row
# --------------------------------------------------------------------------


def _open_busy_defer_row(*, character_id: str, conversation_id: str) -> PendingFollowUp:
    queued_at = datetime(2026, 8, 15, 9, 0, tzinfo=timezone.utc)
    return PendingFollowUp.new(
        character_id=character_id,
        conversation_id=conversation_id,
        first_message=PendingFollowUpMessage.new(
            content="晚餐想吃什麼", queued_at=queued_at,
        ),
        brief_reply="開會中，等等回你",
        defer_reason="會議中",
        scheduled_for=queued_at + timedelta(hours=1),
        now=queued_at,
    )


async def _seeded_nudge_fixture():  # noqa: ANN201
    pending = InMemoryPendingFollowUpRepository()
    decider = _CountingDecider()
    chat, characters, conversations = _wire(
        pending_follow_ups=pending, busy_reply_decider=decider,
    )
    created = await characters.create_character(CreateCharacterRequest(name="Mio"))
    conversation = Conversation.start(character_id=created.id)
    await conversations.save(conversation)
    await pending.add(_open_busy_defer_row(
        character_id=created.id, conversation_id=conversation.id,
    ))
    return chat, created, conversation, pending, decider


@pytest.mark.asyncio
async def test_silent_nudge_cancels_an_open_busy_defer_row() -> None:
    """Otherwise the dispatcher answers a second time, into a live scene.

    The character just replied in full to the nudge — it already caught up
    on everything the open row was owed. Leaving the row queued means the
    scheduler fires the deferred full reply later as well: the old
    duplicate-answer echo, reached through a button that never touches the
    decider.
    """
    chat, created, conversation, pending, decider = await _seeded_nudge_fixture()

    response = await chat.send_message(SendChatMessageRequest(
        character_id=created.id,
        conversation_id=conversation.id,
        message="", stage_nudge=True,
        presence_frame=_stage_frame(),
    ))

    assert await pending.find_open_for_conversation(conversation.id) is None
    rows = [row for row in pending._rows.values()]  # noqa: SLF001
    assert [row.status for row in rows] == [PendingFollowUpStatus.CANCELLED]
    # No player text exists, so nothing is appended for audit — the queue
    # entity refuses empty content and there is nothing honest to record.
    assert len(rows[0].messages) == 1
    # The turn itself is the ordinary immediate reply, not a new defer.
    assert decider.calls == 0
    assert response.user_message is None
    assert response.assistant_message is not None


@pytest.mark.asyncio
async def test_streamed_silent_nudge_cancels_an_open_busy_defer_row() -> None:
    chat, created, conversation, pending, decider = await _seeded_nudge_fixture()

    stream, finalizer = await chat.send_message_stream(SendChatMessageRequest(
        character_id=created.id,
        conversation_id=conversation.id,
        message="", stage_nudge=True,
        presence_frame=_stage_frame(),
    ))
    response = await finalizer.finish(await _drain(stream))

    assert await pending.find_open_for_conversation(conversation.id) is None
    assert decider.calls == 0
    assert response.user_message is None
    assert response.assistant_message is not None


@pytest.mark.asyncio
async def test_nudge_with_supplement_cancels_and_records_the_supplement() -> None:
    """The supplement is a real player message, so it is audited as one."""
    chat, created, conversation, pending, _ = await _seeded_nudge_fixture()

    await chat.send_message(SendChatMessageRequest(
        character_id=created.id,
        conversation_id=conversation.id,
        message="我推開門進來", stage_nudge=True,
        presence_frame=_stage_frame(),
    ))

    assert await pending.find_open_for_conversation(conversation.id) is None
    row = next(iter(pending._rows.values()))  # noqa: SLF001
    assert row.status == PendingFollowUpStatus.CANCELLED
    assert [m.content for m in row.messages] == ["晚餐想吃什麼", "*我推開門進來*"]


# --------------------------------------------------------------------------
# the per-session message ceiling
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_silent_nudges_consume_the_session_message_ceiling() -> None:
    """The hole SN1 must not open: a capped account pulling free replies.

    With no user message written, the pre-SN1 counter (user messages only)
    stayed at zero forever.
    """
    chat, characters, _ = _wire(session_limit=2)
    created = await characters.create_character(CreateCharacterRequest(name="Mio"))

    first = await chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="", stage_nudge=True,
        presence_frame=_stage_frame(),
    ))
    await chat.send_message(SendChatMessageRequest(
        character_id=created.id,
        conversation_id=first.conversation_id,
        message="", stage_nudge=True,
        presence_frame=_stage_frame(),
    ))
    with pytest.raises(ChatRuntimeLimitExceeded, match="session message limit"):
        await chat.send_message(SendChatMessageRequest(
            character_id=created.id,
            conversation_id=first.conversation_id,
            message="", stage_nudge=True,
            presence_frame=_stage_frame(),
        ))


@pytest.mark.asyncio
async def test_a_nudge_costs_the_same_slot_as_a_typed_turn() -> None:
    """One press = one turn = one slot, whichever shape the turn took."""
    chat, characters, _ = _wire(session_limit=2)
    created = await characters.create_character(CreateCharacterRequest(name="Mio"))

    first = await chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="嗨",
    ))
    await chat.send_message(SendChatMessageRequest(
        character_id=created.id,
        conversation_id=first.conversation_id,
        message="", stage_nudge=True,
        presence_frame=_stage_frame(),
    ))
    with pytest.raises(ChatRuntimeLimitExceeded, match="session message limit"):
        await chat.send_message(SendChatMessageRequest(
            character_id=created.id,
            conversation_id=first.conversation_id,
            message="再一句",
        ))


# --------------------------------------------------------------------------
# the distributed post-turn rebuild
# --------------------------------------------------------------------------


def _thread() -> Conversation:
    """[typed turn] + [a silent nudge's reply]."""
    base = datetime(2026, 8, 15, 9, 0, tzinfo=timezone.utc)
    conversation = Conversation.start(character_id="c1")
    return Conversation(
        id=conversation.id,
        character_id="c1",
        messages=[
            Message(role=MessageRole.USER, content="嗨", created_at=base),
            Message(role=MessageRole.ASSISTANT, content="嗨啊", created_at=base),
            Message(
                role=MessageRole.ASSISTANT,
                content="你回來啦",
                created_at=base + timedelta(minutes=5),
            ),
        ],
    )


def test_worker_rebuild_of_a_silent_nudge_does_not_borrow_the_previous_line() -> None:
    """The hosted worker rebuilds turn text from ids alone.

    Walking backwards for "the nearest user message" would hand this turn
    the *previous* turn's 「嗨」 and extract memories against the wrong
    input, which is precisely the mis-attribution the rebuild refuses to do
    everywhere else.
    """
    chat, _, _ = _wire()
    thread = _thread()

    rebuilt = chat._rebuild_post_turn_texts(  # noqa: SLF001 - unit under test
        thread, 2, has_user_message=False,
    )

    assert rebuilt is not None
    user_text, assistant_text, boundary = rebuilt
    assert user_text == ""
    assert assistant_text == "你回來啦"
    assert boundary == thread.messages[2].created_at


def test_worker_rebuild_of_an_ordinary_turn_is_unchanged() -> None:
    chat, _, _ = _wire()
    thread = _thread()

    rebuilt = chat._rebuild_post_turn_texts(thread, 1)  # noqa: SLF001

    assert rebuilt == ("嗨", "嗨啊", thread.messages[0].created_at)


# --------------------------------------------------------------------------
# undo
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_undo_of_a_silent_nudge_drops_only_that_reply() -> None:
    """An assistant-only turn is one message, and undo must take exactly it —
    not the typed turn underneath it."""
    journals = InMemoryTurnJournalRepository()
    chat, characters, conversations = _wire(journal_repository=journals)
    created = await characters.create_character(CreateCharacterRequest(name="Mio"))
    undo = TurnUndoService(
        journal_repository=journals,
        conversation_repository=conversations,
        character_repository=chat._character_repository,
        memory_repository=chat._memory_repository,
    )

    typed = await chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="嗨",
    ))
    await chat.send_message(SendChatMessageRequest(
        character_id=created.id,
        conversation_id=typed.conversation_id,
        message="", stage_nudge=True,
        presence_frame=_stage_frame(),
    ))
    conversation = await conversations.get(typed.conversation_id)
    assert len(conversation.messages) == 3

    result = await undo.undo_last_turn(typed.conversation_id)

    assert result.reverted_messages == 1
    conversation = await conversations.get(typed.conversation_id)
    assert [m.role for m in conversation.messages] == [
        MessageRole.USER, MessageRole.ASSISTANT,
    ]
    assert conversation.messages[0].content == "嗨"


@pytest.mark.asyncio
async def test_undo_of_a_nudge_with_supplement_takes_both_messages() -> None:
    journals = InMemoryTurnJournalRepository()
    chat, characters, conversations = _wire(journal_repository=journals)
    created = await characters.create_character(CreateCharacterRequest(name="Mio"))
    undo = TurnUndoService(
        journal_repository=journals,
        conversation_repository=conversations,
        character_repository=chat._character_repository,
        memory_repository=chat._memory_repository,
    )

    response = await chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="我推開門進來", stage_nudge=True,
        presence_frame=_stage_frame(),
    ))
    result = await undo.undo_last_turn(response.conversation_id)

    assert result.reverted_messages == 2
    conversation = await conversations.get(response.conversation_id)
    assert conversation.messages == []
