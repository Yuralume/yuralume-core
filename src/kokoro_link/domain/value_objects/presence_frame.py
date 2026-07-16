"""Conversation surface and co-presence context.

PresenceFrame tells the LLM what kind of interaction the current turn
belongs to: same-site stage interaction, web private message, or an
external messaging channel. It is deliberately semantic context, not a
branch table for scripted wording.
"""

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from kokoro_link.domain.value_objects.platform import Platform


class ChatSurface(StrEnum):
    WEB_STAGE = "web_stage"
    WEB_DM = "web_dm"
    MESSAGING = "messaging"


class ChatChannel(StrEnum):
    KOKORO_STAGE = "kokoro_stage"
    KOKORO_DM = "kokoro_dm"
    TELEGRAM = "telegram"
    LINE = "line"
    UNKNOWN = "unknown"


class VisibilityMode(StrEnum):
    VIRTUAL_SAME_SPACE = "virtual_same_space"
    TEXT_ONLY = "text_only"
    TEXT_AND_ATTACHMENTS = "text_and_attachments"


class AccessContext(StrEnum):
    # Legacy compatibility only. New Scene Access verdicts should use
    # concrete, real-world-plausible contexts or text_message_only.
    REMOTE_STAGE = "remote_stage"
    PUBLIC_ENCOUNTER = "public_encounter"
    INVITED_VISIT = "invited_visit"
    SCHEDULED_MEETUP = "scheduled_meetup"
    ESTABLISHED_ROUTINE = "established_routine"
    TEXT_MESSAGE_ONLY = "text_message_only"
    NOT_PLAUSIBLE = "not_plausible"


@dataclass(frozen=True, slots=True)
class PresenceFrame:
    surface: ChatSurface
    channel: ChatChannel
    visibility: VisibilityMode
    display_name: str
    access_context: AccessContext = AccessContext.NOT_PLAUSIBLE
    co_presence_reason: str | None = None
    stage_access_note: str | None = None

    def __post_init__(self) -> None:
        surface = ChatSurface(self.surface)
        channel = ChatChannel(self.channel)
        visibility = VisibilityMode(self.visibility)
        access_context = AccessContext(self.access_context)
        if surface is not ChatSurface.WEB_STAGE:
            access_context = AccessContext.TEXT_MESSAGE_ONLY
        object.__setattr__(self, "surface", surface)
        object.__setattr__(self, "channel", channel)
        object.__setattr__(self, "visibility", visibility)
        object.__setattr__(self, "access_context", access_context)

    @classmethod
    def web_stage(
        cls,
        *,
        has_attachments: bool = False,
        access_context: AccessContext = AccessContext.NOT_PLAUSIBLE,
        co_presence_reason: str | None = None,
        stage_access_note: str | None = None,
    ) -> "PresenceFrame":
        visibility = (
            VisibilityMode.TEXT_AND_ATTACHMENTS
            if has_attachments else VisibilityMode.VIRTUAL_SAME_SPACE
        )
        return cls(
            surface=ChatSurface.WEB_STAGE,
            channel=ChatChannel.KOKORO_STAGE,
            visibility=visibility,
            display_name="站內同場互動",
            access_context=access_context,
            co_presence_reason=co_presence_reason,
            stage_access_note=stage_access_note,
        )

    @classmethod
    def web_dm(cls, *, has_attachments: bool = False) -> "PresenceFrame":
        return cls(
            surface=ChatSurface.WEB_DM,
            channel=ChatChannel.KOKORO_DM,
            visibility=_message_visibility(has_attachments),
            display_name="站內私訊",
            access_context=AccessContext.TEXT_MESSAGE_ONLY,
        )

    @classmethod
    def messaging(
        cls,
        *,
        platform: Platform | None = None,
        has_attachments: bool = False,
    ) -> "PresenceFrame":
        channel = _channel_for_platform(platform)
        return cls(
            surface=ChatSurface.MESSAGING,
            channel=channel,
            visibility=_message_visibility(has_attachments),
            display_name=_display_name_for_channel(channel),
            access_context=AccessContext.TEXT_MESSAGE_ONLY,
        )

    def with_attachment_visibility(self, *, has_attachments: bool) -> "PresenceFrame":
        if not has_attachments or self.visibility is VisibilityMode.TEXT_AND_ATTACHMENTS:
            return self
        return replace(self, visibility=VisibilityMode.TEXT_AND_ATTACHMENTS)

    def to_metadata(self) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "surface": self.surface.value,
            "channel": self.channel.value,
            "visibility": self.visibility.value,
            "display_name": self.display_name,
            "access_context": self.access_context.value,
        }
        if self.co_presence_reason:
            metadata["co_presence_reason"] = self.co_presence_reason
        if self.stage_access_note:
            metadata["stage_access_note"] = self.stage_access_note
        return metadata


def _message_visibility(has_attachments: bool) -> VisibilityMode:
    if has_attachments:
        return VisibilityMode.TEXT_AND_ATTACHMENTS
    return VisibilityMode.TEXT_ONLY


def _channel_for_platform(platform: Platform | None) -> ChatChannel:
    if platform is None:
        return ChatChannel.UNKNOWN
    if platform == Platform.TELEGRAM:
        return ChatChannel.TELEGRAM
    if platform == Platform.LINE:
        return ChatChannel.LINE
    return ChatChannel.UNKNOWN


def _display_name_for_channel(channel: ChatChannel) -> str:
    return {
        ChatChannel.TELEGRAM: "Telegram",
        ChatChannel.LINE: "LINE",
        ChatChannel.KOKORO_DM: "站內私訊",
        ChatChannel.KOKORO_STAGE: "站內同場互動",
    }.get(channel, "外部訊息")

