"""A client that walks away is an audience leaving, not a cancellation.

Before detached completion, closing the tab mid-reply cancelled the generation
at whatever ``await`` it happened to be sitting on. The user turn was already
persisted (``send_message_stream`` saves it pre-LLM) and on a token-metered
hosted tier the upstream had already been paid for — so the player was left with
their own message, a spent turn, and no reply, forever. Reloading showed the
gap; nothing ever filled it.

What the code now guarantees, and what each test below pins:

1. the generation runs to completion and ``finalizer.finish`` persists the
   assistant message, so reloading the conversation shows the reply;
2. the charge **settles** rather than releasing — spending credits and
   producing a message are the same event, in both directions;
3. the conversation lease and the drain slot are held until that message lands,
   because drain waits for the turn to end, not for the socket to close;
4. and the whole thing is bounded: a turn that cannot finish inside the lease
   TTL is cancelled and released, so a disconnect can never strand a
   conversation.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from kokoro_link.application.dto.chat import SendChatMessageRequest
from kokoro_link.application.services import chat_stream_relay
from kokoro_link.application.services.chat_stream_relay import (
    TurnStreamRelay,
    wait_for_pending_turn_completions,
)

from tests.unit.chat_stream_route_harness import (
    FAKE_IMAGE_URL,
    USER_ID,
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


async def _read(frames) -> dict | str:  # noqa: ANN001
    return frame_payload(await frames.__anext__())


# ── plain token streaming ──────────────────────────────────────────────


async def test_a_mid_stream_disconnect_still_lands_the_assistant_message() -> None:
    model = GatedStreamModel()
    fixture = StreamRouteFixture(model=model)
    character_id = await fixture.seed_character()

    response = await fixture.open_stream(character_id, "講到一半我就走了")
    frames = response.body_iterator
    assert (await _read(frames))["conversation_id"]
    assert (await _read(frames))["token"] == "前半段。"

    # The player closes the tab here — Starlette closes the body iterator.
    await frames.aclose()
    # …and the rest of the reply arrives from upstream afterwards.
    model.gate.set()
    await wait_for_pending_turn_completions()

    replies = await fixture.assistant_messages(character_id)
    assert [m.content for m in replies] == [model.full_text]


async def test_that_turn_settles_rather_than_refunding() -> None:
    """Credits spent and a message delivered are one event, in both directions.

    Releasing here used to look generous; what it actually did was refund a turn
    whose upstream tokens had already been billed, on a turn that produced
    nothing. Now it produces something, so it is paid for.
    """
    model = GatedStreamModel()
    fixture = StreamRouteFixture(model=model)
    character_id = await fixture.seed_character()

    response = await fixture.open_stream(character_id, "扣了點就要有訊息")
    frames = response.body_iterator
    await _read(frames)
    await frames.aclose()
    model.gate.set()
    await wait_for_pending_turn_completions()

    assert fixture.billing.settles == 1
    assert fixture.drain.active_turns == 0
    assert len(await fixture.assistant_messages(character_id)) == 1


async def test_the_lease_is_held_until_the_reply_lands_then_freed() -> None:
    """Drain's contract, applied where the player feels it.

    The conversation stays claimed while the detached turn is still writing —
    a sibling turn started now would reflow the transcript around it — and is
    free the moment the message is persisted.
    """
    model = GatedStreamModel()
    fixture = StreamRouteFixture(model=model)
    character_id = await fixture.seed_character()

    response = await fixture.open_stream(character_id, "還在寫就別插隊")
    frames = response.body_iterator
    conversation_id = (await _read(frames))["conversation_id"]
    await frames.aclose()

    assert fixture.drain.active_turns == 1

    model.gate.set()
    await wait_for_pending_turn_completions()

    retry = await fixture.chat_service.send_message(
        SendChatMessageRequest(
            character_id=character_id,
            conversation_id=conversation_id,
            message="我回來了",
            provider_id="fake",
        ),
        current_user_id=USER_ID,
    )
    assert retry.assistant_message is not None
    assert fixture.drain.active_turns == 0


# ── tool path ──────────────────────────────────────────────────────────


async def test_a_disconnect_mid_tool_cycle_still_lands_the_image() -> None:
    """The tool path is where a disconnect costs the most.

    An image cycle is tens of seconds of generation the player already paid
    for. Cancelling it threw that away at the moment they were most likely to
    look away — a picture takes long enough that switching apps is normal.
    """
    model = GatedToolModel([_TOOL_CALL, _TOOL_REPLY])
    fixture = StreamRouteFixture(model=model, tools=True)
    character_id = await fixture.seed_character(allowed_tools=["fake_image"])

    response = await fixture.open_stream(character_id, "傳一張你現在的照片給我")
    frames = response.body_iterator
    assert (await _read(frames))["conversation_id"]
    # The tool cycle is running; the UI is showing its progress frame.
    assert (await _read(frames))["tool_activity"]["status"] == "started"

    await frames.aclose()
    # The second hop only answers after the client is long gone.
    model.gate.set()
    await wait_for_pending_turn_completions()

    replies = await fixture.assistant_messages(character_id)
    assert [m.content for m in replies] == [_TOOL_REPLY]
    assert [a.url for a in replies[0].attachments] == [FAKE_IMAGE_URL]
    assert fixture.billing.settles == 1
    assert fixture.drain.active_turns == 0


# ── the hard cap ───────────────────────────────────────────────────────


async def test_a_detached_turn_that_never_ends_is_released_not_stranded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detaching must not be able to pin a conversation forever.

    A wedged upstream after a disconnect has nobody left to notice it, so the
    watchdog is the only thing between it and a conversation the player cannot
    use until the process restarts. Nothing reclaims that lease on its own —
    its heartbeat keeps renewing it up to the 900s max lifetime — so "wait for
    it to expire" is not a fallback that exists; the cancel below is the whole
    mechanism.
    """
    monkeypatch.setattr(
        chat_stream_relay, "DETACHED_TURN_TIMEOUT_SECONDS", 0.05,
    )
    model = GatedStreamModel(stall=True)
    fixture = StreamRouteFixture(model=model)
    character_id = await fixture.seed_character()

    response = await fixture.open_stream(character_id, "上游卡死了")
    frames = response.body_iterator
    conversation_id = (await _read(frames))["conversation_id"]
    await frames.aclose()
    await wait_for_pending_turn_completions()

    # Old-path cleanup, just deferred: released rather than settled, because
    # this turn genuinely produced nothing.
    assert fixture.billing.settles == 0
    assert fixture.billing.releases == 1
    assert fixture.drain.active_turns == 0
    assert await fixture.assistant_messages(character_id) == []

    retry = await fixture.chat_service.send_message(
        SendChatMessageRequest(
            character_id=character_id,
            conversation_id=conversation_id,
            message="再試一次",
            provider_id="fake",
        ),
        current_user_id=USER_ID,
    )
    assert retry.assistant_message is not None


# ── relay unit-level guarantees ────────────────────────────────────────


class _RecordingFinalizer:
    def __init__(self) -> None:
        self.finished: list[str] = []
        self.releases = 0

    @property
    def conversation_id(self) -> str:
        return "conv-relay"

    async def finish(self, assistant_text: str):  # noqa: ANN201
        self.finished.append(assistant_text)
        return assistant_text

    async def release_turn_lease(self) -> None:
        self.releases += 1


async def test_the_timeout_never_double_releases_a_finished_turn() -> None:
    """Ownership is one-way: ``finish`` releases, or the relay does — not both.

    The old shape had both the route's ``finally`` and a shielded ``finish``
    able to release the same turn, which is how a settle could race a release.
    """
    finalizer = _RecordingFinalizer()

    async def _one_chunk():  # noqa: ANN202
        yield "說完了。"

    relay = TurnStreamRelay(_one_chunk(), finalizer).start()
    assert [event async for event in relay.frames()]
    relay.detach()
    await wait_for_pending_turn_completions()

    assert finalizer.finished == ["說完了。"]
    # Exactly the one release ``finish`` itself performs — the relay adds none.
    assert finalizer.releases == 0
    # And a turn that ended before its transport did arms no watchdog: every
    # ordinary turn would otherwise log a "transport gone" line and spawn a
    # task to wait 180s on something already finished.
    assert relay._watchdog_task is None  # noqa: SLF001


async def test_a_slow_transport_gets_every_frame_in_order() -> None:
    """A full buffer must suspend the turn, never drop or reorder frames.

    The buffer replaces what a blocked ``yield`` used to do, so its full case
    has to behave the same way: backpressure, not a drop policy.
    """
    finalizer = _RecordingFinalizer()

    async def _many():  # noqa: ANN202
        for index in range(5):
            yield f"第{index}塊"

    relay = TurnStreamRelay(_many(), finalizer, frame_buffer=1).start()
    received = [event async for event in relay.frames()]

    assert [event.text for event in received[:5]] == [
        f"第{index}塊" for index in range(5)
    ]
    assert finalizer.finished == ["第0塊第1塊第2塊第3塊第4塊"]


async def test_a_transport_that_leaves_mid_buffer_does_not_wedge_the_turn() -> None:
    """The deadlock this design could have had, pinned.

    A turn parked on a full buffer whose reader has gone would wait forever —
    holding the lease, the charge and the drain slot — which is a worse failure
    than the one detached completion set out to fix.
    """
    finalizer = _RecordingFinalizer()

    async def _many():  # noqa: ANN202
        for index in range(30):
            yield f"第{index}塊"

    relay = TurnStreamRelay(_many(), finalizer, frame_buffer=1).start()
    stream = relay.frames()
    assert (await stream.__anext__()).text == "第0塊"
    # Let the turn task fill the buffer and park on it before walking away.
    await asyncio.sleep(0)
    await stream.aclose()
    relay.detach()

    await asyncio.wait_for(wait_for_pending_turn_completions(), timeout=5)
    assert finalizer.finished == ["".join(f"第{i}塊" for i in range(30))]


async def test_a_detached_failure_leaves_a_trace(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A log line is the only witness a detached turn has left.

    Nobody is reading the outcome any more: ``_publish`` drops it and the turn
    task ends normally, so without this line a ``finish`` that failed to write
    the DB is byte-for-byte identical to a turn that went perfectly. It is not
    the same event at all — on hosted, releasing a charge that already covered
    a Gateway call *settles* it, so the player has paid for a message that does
    not exist, and nothing anywhere says so.
    """

    class _FailingFinalizer(_RecordingFinalizer):
        async def finish(self, assistant_text: str):  # noqa: ANN201
            raise RuntimeError("finish 寫 DB 失敗")

    finalizer = _FailingFinalizer()

    async def _one_chunk():  # noqa: ANN202
        yield "說完了。"

    relay = TurnStreamRelay(_one_chunk(), finalizer).start()
    relay.detach()
    with caplog.at_level(logging.ERROR, logger=chat_stream_relay.__name__):
        await wait_for_pending_turn_completions()

    failures = [
        record for record in caplog.records
        if record.levelno >= logging.ERROR and "conv-relay" in record.getMessage()
    ]
    assert failures, "a detached turn failed and said nothing"
    # The flag, because "the transport is gone" is what turns an error the
    # route would have rendered into one nobody will ever see; and the
    # traceback, because the next reader of this line has no request to look at.
    assert "detached=True" in failures[0].getMessage()
    assert failures[0].exc_info is not None


async def test_a_transport_that_stops_reading_without_leaving_still_completes() -> None:
    """The stall no ``finally`` covers.

    A reader that goes away runs the route's ``finally`` and calls ``detach``.
    A reader that merely stops consuming — still connected, never cancelled,
    never closed — runs nothing at all, so a turn parked on a full buffer would
    hold the lease, the charge and the drain slot for the life of the process,
    with no watchdog armed because nothing ever said the transport was gone.
    Past the same timeout the watchdog uses, "connected but taking no frame" is
    read as gone: the turn detaches itself, drops the frames nobody is
    collecting, and finishes for the transcript.
    """
    finalizer = _RecordingFinalizer()

    async def _many():  # noqa: ANN202
        for index in range(10):
            yield f"第{index}塊"

    relay = TurnStreamRelay(
        _many(), finalizer, timeout_seconds=0.2, frame_buffer=1,
    ).start()
    stream = relay.frames()
    assert (await stream.__anext__()).text == "第0塊"
    # …and the reader stops here. No ``aclose``, no ``detach``, no cancel.

    await asyncio.wait_for(wait_for_pending_turn_completions(), timeout=5)

    assert relay.detached is True
    # Armed exactly as a real disconnect would have — a turn that also wedged
    # upstream is bounded from here rather than left running.
    assert relay._watchdog_task is not None  # noqa: SLF001
    # The reply still lands in full: what the reader missed is the live frames,
    # not the message.
    assert finalizer.finished == ["".join(f"第{i}塊" for i in range(10))]
    # ``finish`` ran, so it owns the release and the relay adds none.
    assert finalizer.releases == 0

    await stream.aclose()


async def test_a_detached_relay_stops_buffering_frames() -> None:
    """Memory of a detached turn must not grow with its reply length."""
    finalizer = _RecordingFinalizer()
    gate = asyncio.Event()

    async def _long_reply():  # noqa: ANN202
        yield "第一塊"
        await gate.wait()
        for _ in range(50):
            yield "又一塊"

    relay = TurnStreamRelay(_long_reply(), finalizer, frame_buffer=4).start()
    stream = relay.frames()
    assert (await stream.__anext__()).text == "第一塊"
    await stream.aclose()
    relay.detach()
    gate.set()
    await wait_for_pending_turn_completions()

    assert finalizer.finished == ["第一塊" + "又一塊" * 50]
    assert relay._queue.qsize() <= 4  # noqa: SLF001 - the bound is the point
