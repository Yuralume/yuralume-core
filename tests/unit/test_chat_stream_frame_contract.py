"""The SSE wire format, frame by frame, for a stream nobody abandons.

Detached completion moved the whole turn off the response generator and behind
a queue. That is a large change underneath a format the frontend parses by
hand, and every part of it is invisible to the tests that only assert "a
``done`` frame arrived somewhere": a dropped ``tool_activity``, a reordered
token, a second ``[DONE]``, a ``conversation_id`` that stopped coming first
would all still pass those.

So this file asserts the *sequence*, on both paths, with a scripted model —
what the client actually receives, in order, and nothing else.
"""

from __future__ import annotations

import pytest

from tests.unit.chat_stream_route_harness import (
    FAKE_IMAGE_URL,
    GatedStreamModel,
    GatedToolModel,
    StreamRouteFixture,
    frame_payload,
)

pytestmark = pytest.mark.asyncio

_TOOL_CALL = (
    '```json\n{"tool": "fake_image", "args": {"scene": "窗邊", '
    '"caption": "現在的我"}}\n```'
)
_TOOL_REPLY = "我剛畫了一張現在的我，希望你喜歡～"


async def _collect(response) -> list:  # noqa: ANN001
    return [frame_payload(chunk) async for chunk in response.body_iterator]


async def test_the_plain_path_frame_sequence_is_unchanged() -> None:
    model = GatedStreamModel(chunks=("一。", "二。", "三。"))
    model.gate.set()  # nobody is stalling this turn
    fixture = StreamRouteFixture(model=model)
    character_id = await fixture.seed_character()

    response = await fixture.open_stream(character_id, "從頭聽到尾")
    frames = await _collect(response)

    # Frame one binds the conversation before a single token arrives — the
    # frontend needs it to route everything that follows.
    assert list(frames[0]) == ["conversation_id"]
    assert [f["token"] for f in frames[1:4]] == ["一。", "二。", "三。"]

    done = frames[4]
    assert done["done"] is True
    assert done["response"]["assistant_message"]["content"] == "一。二。三。"
    # ``mode='json'`` — a raw datetime here would crash ``json.dumps``
    # mid-stream and leave the UI stuck on "傳送中" with no final event.
    assert isinstance(done["response"]["state"]["last_active_at"], (str, type(None)))

    assert frames[5] == "[DONE]"
    assert len(frames) == 6


async def test_the_tool_path_still_puts_every_activity_frame_before_the_text() -> None:
    model = GatedToolModel([_TOOL_CALL, _TOOL_REPLY])
    model.gate.set()
    fixture = StreamRouteFixture(model=model, tools=True)
    character_id = await fixture.seed_character(allowed_tools=["fake_image"])

    response = await fixture.open_stream(character_id, "傳一張你現在的照片給我")
    frames = await _collect(response)

    assert list(frames[0]) == ["conversation_id"]
    assert [f["tool_activity"] for f in frames[1:3]] == [
        {"tool": "fake_image", "status": "started"},
        {"tool": "fake_image", "status": "finished"},
    ]
    assert frames[3]["token"] == _TOOL_REPLY

    done = frames[4]
    assert done["done"] is True
    message = done["response"]["assistant_message"]
    assert message["content"] == _TOOL_REPLY
    assert [a["url"] for a in message["attachments"]] == [FAKE_IMAGE_URL]

    assert frames[5] == "[DONE]"
    assert len(frames) == 6
