"""``AccessContext`` semantics after the stage-access judge retired.

Stage turns carry exactly one live value — ``player_declared`` — but the
old verdict strings must keep *parsing*: historical turn metadata already
stores them, and already-deployed self-hosted frontends still send them.
Both paths collapse to ``PLAYER_DECLARED`` so there is one narrative
block to maintain rather than two behaviours.
"""

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

_LEGACY_STAGE_VALUES = (
    "remote_stage",
    "public_encounter",
    "invited_visit",
    "scheduled_meetup",
    "established_routine",
    "not_plausible",
)


def _stage_payload(**overrides: object) -> PresenceFramePayload:
    fields: dict[str, object] = {
        "surface": ChatSurface.WEB_STAGE,
        "channel": ChatChannel.KOKORO_STAGE,
        "visibility": VisibilityMode.VIRTUAL_SAME_SPACE,
    }
    fields.update(overrides)
    return PresenceFramePayload(**fields)  # type: ignore[arg-type]


def test_web_stage_factory_defaults_to_player_declared() -> None:
    frame = PresenceFrame.web_stage()

    assert frame.access_context is AccessContext.PLAYER_DECLARED
    assert frame.to_metadata()["access_context"] == "player_declared"


def test_stage_payload_without_access_context_defaults_to_player_declared() -> None:
    frame = _stage_payload().to_domain()

    assert frame.access_context is AccessContext.PLAYER_DECLARED


def test_legacy_chat_request_defaults_to_text_message_access() -> None:
    frame = SendChatMessageRequest(
        character_id="char-1",
        message="嗨",
    ).resolved_presence_frame()

    assert frame.surface is ChatSurface.WEB_DM
    assert frame.access_context is AccessContext.TEXT_MESSAGE_ONLY
    assert frame.to_metadata()["access_context"] == "text_message_only"


def test_web_dm_defaults_to_text_message_only_access() -> None:
    frame = PresenceFramePayload(
        surface=ChatSurface.WEB_DM,
        channel=ChatChannel.KOKORO_DM,
        visibility=VisibilityMode.TEXT_ONLY,
    ).to_domain()

    assert frame.access_context is AccessContext.TEXT_MESSAGE_ONLY


@pytest.mark.parametrize("legacy", _LEGACY_STAGE_VALUES)
def test_legacy_stage_value_folds_to_player_declared(legacy: str) -> None:
    frame = PresenceFrame(
        surface=ChatSurface.WEB_STAGE,
        channel=ChatChannel.KOKORO_STAGE,
        visibility=VisibilityMode.VIRTUAL_SAME_SPACE,
        display_name="站內同場互動",
        access_context=AccessContext(legacy),
    )

    assert frame.access_context is AccessContext.PLAYER_DECLARED
    assert frame.to_metadata()["access_context"] == "player_declared"


@pytest.mark.parametrize("legacy", _LEGACY_STAGE_VALUES)
def test_legacy_stage_value_survives_the_dto_path(legacy: str) -> None:
    """A deployed frontend / stored turn payload must not blow up."""
    frame = _stage_payload(access_context=legacy).to_domain()

    assert frame.access_context is AccessContext.PLAYER_DECLARED
    assert frame.to_metadata()["access_context"] == "player_declared"


def test_stage_payload_keeps_legacy_field_shape() -> None:
    frame = _stage_payload(
        access_context="scheduled_meetup",
        co_presence_reason="晚上七點已約在車站見面",
        stage_access_note="雙方已有明確約定。",
    ).to_domain()

    assert frame.co_presence_reason == "晚上七點已約在車站見面"
    assert frame.stage_access_note == "雙方已有明確約定。"
    assert frame.to_metadata() == {
        "surface": "web_stage",
        "channel": "kokoro_stage",
        "visibility": "virtual_same_space",
        "display_name": "站內同場互動",
        "access_context": "player_declared",
        "co_presence_reason": "晚上七點已約在車站見面",
        "stage_access_note": "雙方已有明確約定。",
    }


def test_stage_payload_round_trips_player_declared() -> None:
    payload = PresenceFramePayload.from_domain(PresenceFrame.web_stage())

    assert payload.access_context is AccessContext.PLAYER_DECLARED
    assert payload.to_domain().access_context is AccessContext.PLAYER_DECLARED


def test_non_stage_surface_is_still_forced_to_text_message_only() -> None:
    frame = PresenceFramePayload.from_domain(
        PresenceFrame.messaging(platform=Platform.TELEGRAM),
    ).model_copy(
        update={"access_context": AccessContext.PLAYER_DECLARED},
    ).to_domain()

    assert frame.surface is ChatSurface.MESSAGING
    assert frame.access_context is AccessContext.TEXT_MESSAGE_ONLY


def test_stage_surface_keeps_explicit_text_message_only() -> None:
    """Only the *stage* verdict values fold; an explicit "text only"
    declaration keeps the phone-texting framing."""
    frame = _stage_payload(access_context="text_message_only").to_domain()

    assert frame.access_context is AccessContext.TEXT_MESSAGE_ONLY


def test_invalid_access_context_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _stage_payload(access_context="inside_private_room")
