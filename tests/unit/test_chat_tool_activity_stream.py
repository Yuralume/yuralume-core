"""BDD: tool-activity frames on the streaming chat path.

Scenario: the character has tools, so the SSE stream can't carry real
tokens — but it must not sit silent for the whole (potentially 30–60s)
tool cycle either. The stream now interleaves ``{"tool_activity": ...}``
dict frames (started/finished per tool call) with the final reply text,
so the chat UI can show *which* tool is running instead of a generic
"maybe using a tool" guess.

Ordering contract pinned here: every activity frame precedes the reply
text, and the finalizer sees the resolved generation (text/attachments)
by the time the stream is drained — the route calls ``finish`` only
after draining, so this is what keeps persistence correct.
"""

from __future__ import annotations

import asyncio

import pytest

from kokoro_link.application.dto.chat import SendChatMessageRequest
from kokoro_link.application.services.chat_service import (
    _notify_tool_activity,
    _tool_cycle_stream,
)

from tests.unit.test_chat_tool_use import (
    _FAKE_IMAGE_URL,
    _build_chat_service,
    _seed_character,
)


async def _drain(stream) -> list:
    return [item async for item in stream]


@pytest.mark.asyncio
async def test_stream_surfaces_tool_activity_then_reply_text() -> None:
    chat, chars, model, invocations = _build_chat_service(
        replies=[
            '```json\n{"tool": "fake_image", "args": {"scene": "窗邊", "caption": "現在的我"}}\n```',
            "我剛畫了一張現在的我，希望你喜歡～",
        ],
    )
    character_id = await _seed_character(chars, allowed_tools=["fake_image"])

    token_stream, finalizer = await chat.send_message_stream(
        SendChatMessageRequest(
            character_id=character_id, message="傳一張你現在的照片給我",
        ),
    )
    items = await _drain(token_stream)

    dict_frames = [i for i in items if isinstance(i, dict)]
    text_chunks = [i for i in items if isinstance(i, str)]
    assert [f["tool_activity"] for f in dict_frames] == [
        {"tool": "fake_image", "status": "started"},
        {"tool": "fake_image", "status": "finished"},
    ]
    assert text_chunks == ["我剛畫了一張現在的我，希望你喜歡～"]
    # Every activity frame precedes the reply text.
    assert items.index(dict_frames[-1]) < items.index(text_chunks[0])

    # The finalizer got the generation late-bound before the text was
    # yielded — finishing persists the real reply with its attachment.
    response = await finalizer.finish("".join(text_chunks))
    assert response.assistant_message.content == "我剛畫了一張現在的我，希望你喜歡～"
    assert [a.url for a in response.assistant_message.attachments] == [
        _FAKE_IMAGE_URL,
    ]


@pytest.mark.asyncio
async def test_plain_reply_turn_emits_no_activity_frames() -> None:
    """A tool-enabled character answering without tools → text only.

    The UI drops its "maybe using a tool" hint entirely now, so a turn
    that never runs a tool must not emit a single activity frame.
    """
    chat, chars, model, invocations = _build_chat_service(
        replies=["今天過得怎麼樣？"],
    )
    character_id = await _seed_character(chars, allowed_tools=["fake_image"])

    token_stream, finalizer = await chat.send_message_stream(
        SendChatMessageRequest(character_id=character_id, message="嗨"),
    )
    items = await _drain(token_stream)

    assert all(isinstance(i, str) for i in items)
    response = await finalizer.finish("".join(items))
    assert response.assistant_message.content == "今天過得怎麼樣？"


@pytest.mark.asyncio
async def test_orchestrator_crash_still_emits_finished_frame() -> None:
    """The icon must clear even when the tool blows up mid-turn."""
    chat, chars, model, invocations = _build_chat_service(
        replies=[
            '```json\n{"tool": "fake_image", "args": {"scene": "x"}}\n```',
            "抱歉，剛剛好像失敗了。",
        ],
    )
    character_id = await _seed_character(chars, allowed_tools=["fake_image"])

    async def _boom(**kwargs):
        raise RuntimeError("orchestrator exploded")

    chat._tool_orchestrator.execute = _boom  # type: ignore[method-assign]

    token_stream, finalizer = await chat.send_message_stream(
        SendChatMessageRequest(character_id=character_id, message="畫一張"),
    )
    items = await _drain(token_stream)

    statuses = [
        i["tool_activity"]["status"] for i in items if isinstance(i, dict)
    ]
    assert statuses == ["started", "finished"]
    await finalizer.finish(
        "".join(i for i in items if isinstance(i, str)),
    )


@pytest.mark.asyncio
async def test_tool_cycle_stream_close_cancels_generation() -> None:
    """Abandoning the cycle must not leave the LLM call running.

    This used to be the client-disconnect case. It no longer is: since
    ``TurnStreamRelay`` owns the drain, a disconnect leaves this generator
    untouched and the cycle finishes (see
    ``test_chat_stream_detached_completion``). Closing it now means the turn
    was genuinely abandoned — the detach watchdog's timeout, or teardown —
    which is the one situation where killing a multi-minute image cycle is the
    right answer.
    """

    started = asyncio.Event()

    async def _never_finishes():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(_never_finishes())
    events: asyncio.Queue = asyncio.Queue()

    class _Finalizer:
        def attach_generation(self, generation) -> None:  # pragma: no cover
            raise AssertionError("must not attach on an abandoned stream")

    stream = _tool_cycle_stream(task, events, _Finalizer())
    events.put_nowait({"tool": "fake_image", "status": "started"})
    assert (await stream.__anext__()) == {
        "tool_activity": {"tool": "fake_image", "status": "started"},
    }
    await started.wait()

    await stream.aclose()
    await asyncio.sleep(0)
    assert task.cancelled() or task.cancelling()


@pytest.mark.asyncio
async def test_tool_cycle_stream_propagates_generation_failure() -> None:
    async def _fails():
        raise RuntimeError("upstream model died")

    task = asyncio.create_task(_fails())
    events: asyncio.Queue = asyncio.Queue()

    class _Finalizer:
        def attach_generation(self, generation) -> None:  # pragma: no cover
            raise AssertionError("must not attach on a failed cycle")

    stream = _tool_cycle_stream(task, events, _Finalizer())
    with pytest.raises(RuntimeError, match="upstream model died"):
        await _drain(stream)


def test_notify_tool_activity_is_fail_soft() -> None:
    def _bad_callback(event: dict) -> None:
        raise ValueError("queue full")

    # Must not raise — a UI hint can never break the turn.
    _notify_tool_activity(_bad_callback, "fake_image", "started")
    _notify_tool_activity(None, "fake_image", "started")
