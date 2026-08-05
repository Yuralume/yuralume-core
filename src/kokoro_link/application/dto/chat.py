from datetime import datetime

from pydantic import BaseModel, Field

from kokoro_link.application.dto.character import CharacterStatePayload, state_to_payload
from kokoro_link.contracts.repositories import ConversationMessagePage
from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageAttachment,
    MessageKind,
)
from kokoro_link.domain.entities.story_scene_session import StorySceneSession
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.presence_frame import (
    AccessContext,
    ChatChannel,
    ChatSurface,
    PresenceFrame,
    VisibilityMode,
)


DEFAULT_CONVERSATION_PAGE_SIZE = 50
"""How many messages the chat panel loads when a character is opened (IV10).

Sized against the measurement that motivated the change: a thread with 316
messages rendered in full, of which 39 carried pictures totalling 228 MB of
decoded bitmaps and 30 of those were never on screen. Fifty is comfortably more
than a screenful on any layout, so the first page still ends with the reader
already scrolled past its top."""

MAX_CONVERSATION_PAGE_SIZE = 200
"""Server-side clamp, mirroring the feed's. Keeps an over-eager client from
asking for the whole thread again through the front door."""


class PresenceFramePayload(BaseModel):
    """Wire shape of the turn's interaction surface.

    The field shape is deliberately frozen across the stage-access judge's
    retirement: ``access_context`` still accepts the legacy verdict values
    (an already-deployed self-hosted frontend keeps sending them, and
    stored turn metadata replays them) and ``co_presence_reason`` /
    ``stage_access_note`` are still parsed. Folding the legacy values onto
    ``PLAYER_DECLARED`` happens once, in ``PresenceFrame.__post_init__``.
    """

    surface: ChatSurface
    channel: ChatChannel
    visibility: VisibilityMode
    display_name: str | None = None
    access_context: AccessContext | None = None
    co_presence_reason: str | None = None
    stage_access_note: str | None = None

    @classmethod
    def web_stage(cls) -> "PresenceFramePayload":
        return cls.from_domain(PresenceFrame.web_stage())

    @classmethod
    def web_dm(cls) -> "PresenceFramePayload":
        return cls.from_domain(PresenceFrame.web_dm())

    @classmethod
    def from_domain(cls, frame: PresenceFrame) -> "PresenceFramePayload":
        return cls(
            surface=frame.surface,
            channel=frame.channel,
            visibility=frame.visibility,
            display_name=frame.display_name,
            access_context=frame.access_context,
            co_presence_reason=frame.co_presence_reason,
            stage_access_note=frame.stage_access_note,
        )

    def to_domain(self, *, has_attachments: bool = False) -> PresenceFrame:
        frame = PresenceFrame(
            surface=self.surface,
            channel=self.channel,
            visibility=self.visibility,
            display_name=self.display_name or _default_presence_display_name(
                self.channel,
            ),
            access_context=self.access_context or _default_access_context(self.surface),
            co_presence_reason=self.co_presence_reason,
            stage_access_note=self.stage_access_note,
        )
        return frame.with_attachment_visibility(has_attachments=has_attachments)


class SendChatMessageRequest(BaseModel):
    character_id: str
    conversation_id: str | None = None
    provider_id: str | None = None
    model_id: str | None = None
    """Optional legacy override for the main chat model.

    The player UI no longer chooses LLM models; omitted values mean the
    backend resolves ``FEATURE_CHAT`` through admin/global model routing.
    Admin/debug callers may still pass explicit values to override that
    route for one request.
    """
    message: str = Field(min_length=1)
    attachment_urls: list[str] = Field(default_factory=list)
    """Server-relative URLs (e.g. ``/uploads/chat-uploads/abc.png``) of
    images the user attached to this turn. Uploaded separately via
    ``POST /api/v1/chat/uploads`` so the streaming send endpoint stays
    JSON-only. Empty list = plain text turn."""
    operator_persona_enabled: bool = True
    """Whether this turn may update / inject operator persona.

    Web chat is single-operator and leaves this on. External messaging
    channels turn it off unless the account is locked to exactly one
    allowed sender, preventing multi-user chats from contaminating the
    default operator persona.
    """
    presence_frame: PresenceFramePayload | None = None
    """Structured context describing this turn's interaction surface.

    Omitted means legacy web message interaction. A stage frame is the
    player *declaring* a same-place interaction — no prior verdict is
    required and none is consulted; the character answers from its own
    schedule inside the narrative.
    """
    quoted_price_cr: float | None = Field(default=None, ge=0)
    """The price this client had on screen for one chat turn (R9).

    The charge is bound to what the *player* was shown, not to whatever this
    replica's price cache happens to hold — under several hosted replicas, or
    right after a back-office edit, those are not the same number and only the
    first one was ever agreed to. Omitted (older client, or no unambiguous
    price to quote) falls back to the process cache, so nothing regresses.

    Under-reporting gains nothing: the User service answers ``409
    price_changed`` when the quote does not match the published price, it never
    charges the lower number.
    """
    quoted_image_price_cr: float | None = Field(default=None, ge=0)
    """The same, for the ``image_chat_tool`` charge a turn may spawn.

    Carried on the turn rather than raised by its own request because the
    picture is decided by the model mid-turn — there is no moment where the
    client could be asked again."""

    def resolved_presence_frame(self) -> PresenceFrame:
        has_attachments = bool(self.attachment_urls)
        if self.presence_frame is None:
            return PresenceFrame.web_dm(has_attachments=has_attachments)
        return self.presence_frame.to_domain(has_attachments=has_attachments)


class MessageAttachmentResponse(BaseModel):
    kind: str
    url: str
    mime_type: str = "application/octet-stream"
    caption: str | None = None

    @classmethod
    def from_domain(cls, att: MessageAttachment) -> "MessageAttachmentResponse":
        return cls(
            kind=att.kind,
            url=att.url,
            mime_type=att.mime_type,
            caption=att.caption,
        )


class ChatMessageResponse(BaseModel):
    role: str
    content: str
    attachments: list[MessageAttachmentResponse] = Field(default_factory=list)
    turn_record_id: str | None = None
    kind: str = MessageKind.CHAT.value
    """Message category, so the client can render non-dialogue rows
    differently. Added for 起幕 (SC1): story-scene narration is an
    ``assistant`` row like any other, and without this field a reloaded
    thread has no way to tell it apart from the character speaking."""

    @classmethod
    def from_domain(
        cls,
        message: Message,
        *,
        turn_record_id: str | None = None,
    ) -> "ChatMessageResponse":
        return cls(
            role=message.role.value,
            content=message.content,
            attachments=[
                MessageAttachmentResponse.from_domain(a) for a in message.attachments
            ],
            turn_record_id=turn_record_id,
            kind=message.kind.value,
        )


class SuggestedActionResponse(BaseModel):
    """One in-scene action chip (起幕 / SC1-C).

    An object rather than a bare string because chips are expected to
    grow a stable identifier / analytics tag, and widening an object is
    backwards-compatible where changing ``list[str]`` is not.

    Defined here — not in ``dto/story_scene`` where it started — because
    chips are returned from two surfaces: the scene-open response *and*
    every in-scene chat reply. ``dto/story_scene`` already imports this
    module, so this is the one direction that keeps a single shape
    without a cycle; it re-exports the name, so its own importers are
    unaffected.
    """

    text: str


class StorySceneSessionResponse(BaseModel):
    """Wire shape of one 起幕 scene session.

    Defined here — not in ``dto/story_scene`` where it started — for the
    same reason :class:`SuggestedActionResponse` moved: the session is
    returned from two surfaces now. The scene routes answer with it, and
    an in-scene chat reply carries the *closed* session on the turn whose
    verdict ended the scene (SC1-D), so the player sees the wrap-up
    without a reload. ``dto/story_scene`` already imports this module, so
    this is the one direction that keeps a single shape without a cycle;
    it re-exports the name, so its own importers are unaffected.
    """

    id: str
    character_id: str
    conversation_id: str
    status: str
    """``open`` | ``closed``."""
    source_layer: str
    """Which waterfall layer supplied the material: ``beat`` |
    ``new_season`` | ``side_story``."""
    arc_id: str | None = None
    beat_id: str | None = None
    title: str = ""
    location: str | None = None
    mood: str | None = None
    """The scene frame the UI draws around the thread."""
    scene_type: str | None = None
    dramatic_question: str | None = None
    opened_at: datetime
    last_activity_at: datetime
    closed_at: datetime | None = None
    closed_reason: str | None = None
    """``resolved`` | ``manual`` | ``timeout``; ``null`` while open."""

    @classmethod
    def from_domain(
        cls, session: StorySceneSession,
    ) -> "StorySceneSessionResponse":
        return cls(
            id=session.id,
            character_id=session.character_id,
            conversation_id=session.conversation_id,
            status=session.status,
            source_layer=session.source_layer,
            arc_id=session.arc_id,
            beat_id=session.beat_id,
            title=session.title,
            location=session.location,
            mood=session.mood,
            scene_type=session.scene_type,
            dramatic_question=session.dramatic_question,
            opened_at=session.opened_at,
            last_activity_at=session.last_activity_at,
            closed_at=session.closed_at,
            closed_reason=session.closed_reason,
        )


class ConversationResponse(BaseModel):
    """One thread as the chat panel receives it.

    A **superset** of the pre-IV10 shape: ``id`` / ``character_id`` /
    ``messages`` are unchanged, and the two pagination fields default to the
    values that describe "this is the whole thread", so a client that has never
    heard of them keeps working.
    """

    id: str
    character_id: str
    messages: list[ChatMessageResponse]
    has_more: bool = False
    """Whether older messages exist before ``messages[0]`` (IV10)."""
    next_before: int | None = None
    """``position`` of the oldest message in this page; pass back as ``before``
    to fetch the page before it. ``None`` when ``has_more`` is false so the
    client can short-circuit — the same contract the feed's cursor has."""

    @classmethod
    def from_domain(cls, conversation: Conversation) -> "ConversationResponse":
        """Whole-thread rendering: there is nothing older by construction."""
        return cls(
            id=conversation.id,
            character_id=conversation.character_id,
            messages=[ChatMessageResponse.from_domain(m) for m in conversation.messages],
            has_more=False,
            next_before=None,
        )

    @classmethod
    def from_page(cls, page: ConversationMessagePage) -> "ConversationResponse":
        return cls(
            id=page.conversation_id,
            character_id=page.character_id,
            messages=[ChatMessageResponse.from_domain(m) for m in page.messages],
            has_more=page.has_more,
            next_before=page.next_before,
        )


class ChatReplyResponse(BaseModel):
    conversation_id: str
    user_message: ChatMessageResponse
    assistant_message: ChatMessageResponse | None = None
    state: CharacterStatePayload
    suggested_actions: list[SuggestedActionResponse] = Field(
        default_factory=list,
    )
    """In-scene action chips for the *next* turn (起幕 / SC1-C).

    Always empty outside a live story scene — an ordinary chat turn never
    calls the chips writer at all. Streaming carries this in the ``done``
    frame's ``response`` object, which is the same model dumped here, so
    both transports expose one field rather than two shapes.
    """
    story_scene_session: StorySceneSessionResponse | None = None
    """The scene this turn *ended*, when its verdict wrapped one up (SC1-D).

    ``null`` on every other turn, including every turn of a scene that is
    still running — the client keeps rendering the scene frame it already
    has and only reacts when this arrives with ``status == "closed"``.
    Carried on the reply rather than left to the next state poll because
    otherwise the player has to reload to find out the scene ended."""
    story_scene_closing: ChatMessageResponse | None = None
    """The closing narration appended to the thread (``kind ==
    "scene_narration"``), when the writer produced one.

    Independently nullable from ``story_scene_session``: a scene can
    close without prose (writer unavailable, or a draft refused for
    inventing player actions), and the close is the load-bearing half."""

    @classmethod
    def build(
        cls,
        *,
        conversation_id: str,
        user_message: Message,
        assistant_message: Message | None,
        state: CharacterState,
        assistant_turn_record_id: str | None = None,
        suggested_actions: tuple[str, ...] = (),
        story_scene_session: StorySceneSession | None = None,
        story_scene_closing: Message | None = None,
    ) -> "ChatReplyResponse":
        return cls(
            conversation_id=conversation_id,
            user_message=ChatMessageResponse.from_domain(user_message),
            assistant_message=(
                ChatMessageResponse.from_domain(
                    assistant_message,
                    turn_record_id=assistant_turn_record_id,
                )
                if assistant_message is not None else None
            ),
            state=state_to_payload(state),
            suggested_actions=[
                SuggestedActionResponse(text=text)
                for text in suggested_actions
            ],
            story_scene_session=(
                StorySceneSessionResponse.from_domain(story_scene_session)
                if story_scene_session is not None else None
            ),
            story_scene_closing=(
                ChatMessageResponse.from_domain(story_scene_closing)
                if story_scene_closing is not None else None
            ),
        )


def _default_presence_display_name(channel: ChatChannel) -> str:
    return {
        ChatChannel.KOKORO_STAGE: "站內同場互動",
        ChatChannel.KOKORO_DM: "站內私訊",
        ChatChannel.TELEGRAM: "Telegram",
        ChatChannel.LINE: "LINE",
    }.get(channel, "外部訊息")


def _default_access_context(surface: ChatSurface) -> AccessContext:
    """Fill in the access context an older client did not send.

    Stage defaults to ``PLAYER_DECLARED``: switching to the stage tab *is*
    the declaration. The pre-2026-07-30 default was ``NOT_PLAUSIBLE``,
    which only made sense while a judge had to grant access first —
    keeping it would drop every stage turn into a blocking frame now that
    nothing grants anything.
    """
    if surface is ChatSurface.WEB_STAGE:
        return AccessContext.PLAYER_DECLARED
    return AccessContext.TEXT_MESSAGE_ONLY
