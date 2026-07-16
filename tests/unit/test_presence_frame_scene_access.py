import pytest
from pydantic import ValidationError

from kokoro_link.application.dto.chat import PresenceFramePayload, SendChatMessageRequest
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.domain.value_objects.presence_frame import (
    AccessContext,
    ChatChannel,
    ChatSurface,
    PresenceFrame,
    VisibilityMode,
)


def test_legacy_chat_request_defaults_to_text_message_access() -> None:
    frame = SendChatMessageRequest(
        character_id="char-1",
        message="嗨",
    ).resolved_presence_frame()

    assert frame.surface is ChatSurface.WEB_DM
    assert frame.access_context is AccessContext.TEXT_MESSAGE_ONLY
    assert frame.to_metadata()["access_context"] == "text_message_only"


def test_web_stage_payload_without_gate_defaults_to_not_plausible() -> None:
    frame = PresenceFramePayload(
        surface=ChatSurface.WEB_STAGE,
        channel=ChatChannel.KOKORO_STAGE,
        visibility=VisibilityMode.VIRTUAL_SAME_SPACE,
    ).to_domain()

    assert frame.access_context is AccessContext.NOT_PLAUSIBLE


def test_web_dm_defaults_to_text_message_only_access() -> None:
    frame = PresenceFramePayload(
        surface=ChatSurface.WEB_DM,
        channel=ChatChannel.KOKORO_DM,
        visibility=VisibilityMode.TEXT_ONLY,
    ).to_domain()

    assert frame.access_context is AccessContext.TEXT_MESSAGE_ONLY


def test_messaging_frame_ignores_non_text_access_context() -> None:
    frame = PresenceFramePayload.from_domain(
        PresenceFrame.messaging(platform=Platform.TELEGRAM),
    ).model_copy(update={"access_context": AccessContext.PUBLIC_ENCOUNTER}).to_domain()

    assert frame.surface is ChatSurface.MESSAGING
    assert frame.access_context is AccessContext.TEXT_MESSAGE_ONLY


def test_stage_payload_round_trips_scene_access_fields() -> None:
    frame = PresenceFramePayload(
        surface=ChatSurface.WEB_STAGE,
        channel=ChatChannel.KOKORO_STAGE,
        visibility=VisibilityMode.VIRTUAL_SAME_SPACE,
        access_context=AccessContext.SCHEDULED_MEETUP,
        co_presence_reason="晚上七點已約在車站見面",
        stage_access_note="雙方已有明確約定。",
    ).to_domain()

    assert frame.access_context is AccessContext.SCHEDULED_MEETUP
    assert frame.co_presence_reason == "晚上七點已約在車站見面"
    assert frame.stage_access_note == "雙方已有明確約定。"
    assert frame.to_metadata() == {
        "surface": "web_stage",
        "channel": "kokoro_stage",
        "visibility": "virtual_same_space",
        "display_name": "站內同場互動",
        "access_context": "scheduled_meetup",
        "co_presence_reason": "晚上七點已約在車站見面",
        "stage_access_note": "雙方已有明確約定。",
    }


def test_invalid_access_context_is_rejected() -> None:
    with pytest.raises(ValidationError):
        PresenceFramePayload(
            surface=ChatSurface.WEB_STAGE,
            channel=ChatChannel.KOKORO_STAGE,
            visibility=VisibilityMode.VIRTUAL_SAME_SPACE,
            access_context="inside_private_room",
        )
