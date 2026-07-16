from pydantic import BaseModel, Field

from kokoro_link.application.dto.character import CharacterStatePayload, state_to_payload
from kokoro_link.domain.entities.conversation import Conversation, Message, MessageAttachment
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.presence_frame import (
    AccessContext,
    ChatChannel,
    ChatSurface,
    PresenceFrame,
    VisibilityMode,
)


class PresenceFramePayload(BaseModel):
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

    Omitted means legacy web message interaction. Stage requires an explicit
    Scene Access verdict with a real-world-plausible access context.
    """

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
        )


class ConversationResponse(BaseModel):
    id: str
    character_id: str
    messages: list[ChatMessageResponse]

    @classmethod
    def from_domain(cls, conversation: Conversation) -> "ConversationResponse":
        return cls(
            id=conversation.id,
            character_id=conversation.character_id,
            messages=[ChatMessageResponse.from_domain(m) for m in conversation.messages],
        )


class ChatReplyResponse(BaseModel):
    conversation_id: str
    user_message: ChatMessageResponse
    assistant_message: ChatMessageResponse | None = None
    state: CharacterStatePayload

    @classmethod
    def build(
        cls,
        *,
        conversation_id: str,
        user_message: Message,
        assistant_message: Message | None,
        state: CharacterState,
        assistant_turn_record_id: str | None = None,
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
        )


def _default_presence_display_name(channel: ChatChannel) -> str:
    return {
        ChatChannel.KOKORO_STAGE: "站內同場互動",
        ChatChannel.KOKORO_DM: "站內私訊",
        ChatChannel.TELEGRAM: "Telegram",
        ChatChannel.LINE: "LINE",
    }.get(channel, "外部訊息")


def _default_access_context(surface: ChatSurface) -> AccessContext:
    if surface is ChatSurface.WEB_STAGE:
        return AccessContext.NOT_PLAUSIBLE
    return AccessContext.TEXT_MESSAGE_ONLY
