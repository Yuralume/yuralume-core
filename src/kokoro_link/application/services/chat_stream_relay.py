"""Complete one streamed chat turn independently of its SSE transport.

A player who closes the tab mid-reply is an **audience leaving**, not a
cancellation. Before this module the two were the same event: Starlette
cancels the ``StreamingResponse`` generator, the generator was also the thing
draining the token stream, so the cancellation landed inside
``token_stream.__anext__`` and killed the generation. The user turn was already
persisted (``send_message_stream`` saves it pre-LLM), the upstream tokens were
already billed on a token-metered tier, and the assistant reply was lost —
credits spent with no message to show for them.

The fix is to move ownership of the turn off the transport:

* a **turn task** (:meth:`TurnStreamRelay.start`) drains the token stream,
  accumulates the reply text and calls ``finalizer.finish`` itself;
* the route's SSE generator only forwards what the turn task publishes into a
  queue, so a client disconnect cancels the *forwarder* and nothing else.

Why a task rather than "catch ``CancelledError`` and keep draining": once a
cancellation has been delivered into an async generator's frame, that frame is
finished — it cannot be resumed by whoever catches the error. The drain has to
have been running somewhere else all along.

Ownership is single and one-way. Exactly one of two things releases the turn:

* ``finalizer.finish`` — reached whenever the token stream completes, whether or
  not anyone is still listening. It settles the action charge, releases the
  lease and drops the drain slot in its own ``finally``.
* :meth:`TurnStreamRelay._release_quietly` — the turn task's ``finally`` for
  every path that never reached ``finish`` (upstream error, refusal, the
  detach watchdog's timeout). This is the *old* route ``finally`` verbatim,
  moved to the one place that now knows whether ``finish`` started.

The route no longer releases anything, so the double-release window between
"route ``finally``" and "shielded finish" is gone by construction.

Detachment is bounded. A detached turn is given
:data:`DETACHED_TURN_TIMEOUT_SECONDS`, after which the watchdog cancels it and
the release above runs — unless ``finish`` has already started, which is the one
case the watchdog deliberately cannot touch (see :meth:`TurnStreamRelay._watch`).
That timeout is a decision to stop waiting, not a moment at which waiting stops
working: the turn's lease is heartbeated every ``ttl/3`` up to its 900s max
lifetime, so nothing reclaims the conversation on its own when the timeout
passes. The cancel is what frees it, and the worst case is the pre-existing
"abandoned turn" behaviour, just delayed by the timeout.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kokoro_link.application.services.chat_turn_lease import (
    DEFAULT_TURN_LEASE_TTL_SECONDS,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kokoro_link.application.dto.chat import ChatReplyResponse

_LOGGER = logging.getLogger(__name__)


DETACHED_TURN_TIMEOUT_SECONDS: float = DEFAULT_TURN_LEASE_TTL_SECONDS
"""How long a turn may keep running after its client went away.

The chosen ceiling on "the player closed the tab and their own conversation is
still busy". Read it as an **active give-up, not an expiry**: the lease this
turn holds is renewed by its heartbeat every ``ttl/3`` up to
``DEFAULT_TURN_LEASE_MAX_LIFETIME_SECONDS`` (900s), so at 180s nothing has
timed out — a sibling turn started then still gets ``conversation_busy``. Only
the watchdog's cancel below frees the conversation, which is exactly why the
watchdog has to exist.

Borrowed from the lease TTL rather than declared as its own number because the
two size the same thing: how long a conversation may look busy to its owner
while nobody is watching it be served. One number keeps the two answers from
drifting apart — change either and re-read both.

Doubles as the stall timeout for a transport that is still connected but has
stopped consuming (:meth:`TurnStreamRelay._publish_blocking`), on the same
reading: three minutes of taking no frame is an audience that has left.
"""

DEFAULT_FRAME_BUFFER = 512
"""Frames the relay may hold for a transport that is not keeping up.

Backpressure, not a drop policy: while the client is connected a full buffer
suspends the turn task exactly like a blocked ``yield`` used to. Once the client
is gone the buffer stops being written to at all, so a detached turn's memory
cost is its collected reply text and nothing else.
"""


@dataclass(frozen=True, slots=True)
class TurnToken:
    """One chunk of assistant reply text."""

    text: str


@dataclass(frozen=True, slots=True)
class TurnFrame:
    """One non-text frame (tool activity), forwarded verbatim."""

    payload: dict


@dataclass(frozen=True, slots=True)
class TurnCompleted:
    """``finalizer.finish`` returned; the assistant message has landed."""

    response: "ChatReplyResponse"


@dataclass(frozen=True, slots=True)
class TurnFailed:
    """The turn raised. The transport decides how to render it."""

    error: BaseException


TurnStreamEvent = TurnToken | TurnFrame | TurnCompleted | TurnFailed

_SENTINEL: Any = object()
"""In-band end marker. A private object so it can never collide with a frame."""


# Tasks that outlive the request that created them. ``asyncio`` only holds a
# weak reference to a running task, so without this set a detached completion
# could be garbage-collected mid-turn — the exact failure this module exists to
# prevent. Entries remove themselves on completion.
_PENDING: set[asyncio.Task[Any]] = set()


def _track(task: asyncio.Task[Any]) -> asyncio.Task[Any]:
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
    return task


def pending_turn_completions() -> tuple[asyncio.Task[Any], ...]:
    """Every turn task / finalize task this process still has in flight."""
    return tuple(_PENDING)


async def wait_for_pending_turn_completions(
    *, timeout: float | None = None,
) -> None:
    """Await the detached work above (tests, and any future drain hook).

    Loops because finishing a turn task spawns its finalize task: waiting once
    would return while the DB write it started is still running.
    """
    loop = asyncio.get_running_loop()
    # A test suite runs many loops in one interpreter; a task left behind by a
    # closed one can never complete and cannot be awaited from here. Drop those
    # rather than hanging on them. Production has exactly one loop, so this is
    # a no-op there.
    _PENDING.difference_update(
        {task for task in _PENDING if task.get_loop().is_closed()},
    )
    while True:
        pending = [
            task
            for task in _PENDING
            if not task.done() and task.get_loop() is loop
        ]
        if not pending:
            return
        await asyncio.wait(pending, timeout=timeout)
        if timeout is not None:
            return


class TurnStreamRelay:
    """Runs one streamed turn to completion and publishes its frames.

    Constructed by the SSE route, which then does exactly three things with it:
    :meth:`start` it, iterate :meth:`frames`, and :meth:`detach` in a
    ``finally``. Everything about lease / charge / drain-slot ownership lives
    here, so "who released the turn" has one answer per path.
    """

    __slots__ = (
        "_detached",
        "_detached_event",
        "_finalize_task",
        "_finalizer",
        "_queue",
        "_timeout",
        "_token_stream",
        "_turn_task",
        "_watchdog_task",
    )

    def __init__(
        self,
        token_stream: AsyncIterator[str | dict],
        finalizer: Any,
        *,
        timeout_seconds: float | None = None,
        frame_buffer: int | None = None,
    ) -> None:
        self._token_stream = token_stream
        self._finalizer = finalizer
        # Both knobs are resolved here rather than as default arguments: a
        # default is evaluated once at import time, which would freeze the
        # module constant into the signature and make it unchangeable by
        # anything, tests included. Same reason for both, so same shape.
        self._timeout = (
            DETACHED_TURN_TIMEOUT_SECONDS
            if timeout_seconds is None
            else timeout_seconds
        )
        buffer = DEFAULT_FRAME_BUFFER if frame_buffer is None else frame_buffer
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=buffer)
        self._detached = False
        self._detached_event = asyncio.Event()
        self._turn_task: asyncio.Task[None] | None = None
        self._finalize_task: asyncio.Task[Any] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None

    # ── lifecycle ──────────────────────────────────────────────────────

    def start(self) -> "TurnStreamRelay":
        """Begin draining the token stream. Idempotent.

        Started by the route *before* the response is returned rather than on
        the first read: from here the turn's completion does not depend on
        anybody reading it, which is the whole point. A response object that is
        built and never iterated still finishes — immediately if the reply fits
        the frame buffer, and otherwise after the stalled-transport timeout in
        :meth:`_publish_blocking` gives up on the reader and detaches — instead
        of pinning the conversation until the lease's max lifetime.
        """
        if self._turn_task is None:
            task = asyncio.create_task(
                self._run(),
                name=f"chat-turn-{self._conversation_id}",
            )
            # Belt and braces: whatever happens inside ``_run``, the reader
            # must not be left waiting on a queue nobody will write to again.
            task.add_done_callback(self._wake_reader)
            self._turn_task = _track(task)
        return self

    async def frames(self) -> AsyncIterator[TurnStreamEvent]:
        """Yield published frames until the turn ends.

        Only ever iterated by the transport. Cancelling this iteration (client
        disconnect) touches nothing the turn owns.
        """
        while True:
            # Two independent end conditions, because the sentinel is published
            # with a *non-blocking* put — the turn's clean-up path must never be
            # able to park (see :meth:`_publish_final`) — and a non-blocking put
            # into a full buffer is dropped. "The turn task is done and the
            # buffer is drained" catches exactly that case, and catches it
            # without a race: a task that finishes while we are parked below
            # wakes us through ``_wake_reader``, whose put cannot find the queue
            # full (a parked reader implies an empty queue).
            if self._queue.empty() and self._turn_is_over():
                return
            event = await self._queue.get()
            if event is _SENTINEL:
                return
            yield event

    def detach(self) -> None:
        """The transport is gone; keep the turn running under a hard cap.

        Synchronous on purpose: it is called from the route generator's
        ``finally``, which may be running because of a cancellation — a
        ``finally`` that cannot await is a ``finally`` that cannot be skipped.
        """
        if self._detached:
            return
        self._detached = True
        # Wakes a turn task parked on a full buffer so it stops publishing.
        self._detached_event.set()
        task = self._turn_task
        if task is None or task.done():
            return
        _LOGGER.info(
            "chat stream transport gone; finishing conversation %s detached",
            self._conversation_id,
        )
        self._watchdog_task = _track(
            asyncio.create_task(
                self._watch(task),
                name=f"chat-turn-watchdog-{self._conversation_id}",
            ),
        )

    # ── introspection (tests, future drain hooks) ──────────────────────

    @property
    def detached(self) -> bool:
        return self._detached

    @property
    def completion(self) -> asyncio.Task[None] | None:
        """The turn task, or ``None`` before :meth:`start`."""
        return self._turn_task

    @property
    def finalize_task(self) -> asyncio.Task[Any] | None:
        """The ``finish`` task, once the token stream has been drained."""
        return self._finalize_task

    # ── internals ──────────────────────────────────────────────────────

    @property
    def _conversation_id(self) -> str:
        return self._finalizer.conversation_id

    async def _run(self) -> None:
        """Drain → finish → publish. The only owner of the turn."""
        finalize_started = False
        outcome: TurnStreamEvent | None = None
        collected: list[str] = []
        try:
            async for item in self._token_stream:
                # The tool path interleaves dict frames (e.g.
                # ``{"tool_activity": ...}``) with reply text. Dicts are
                # forwarded verbatim and never join ``collected`` — the
                # finalizer must only ever persist the reply text.
                if isinstance(item, str):
                    collected.append(item)
                    await self._publish(TurnToken(item))
                else:
                    await self._publish(TurnFrame(item))
            # An explicit task rather than ``shield(coro)`` so the reference
            # survives: a cancellation here (detach timeout) must leave the DB
            # write running and findable, not orphaned. Cancelling a finalize
            # mid-flight would tear a SQLAlchemy greenlet in half.
            self._finalize_task = _track(
                asyncio.create_task(
                    self._finalizer.finish("".join(collected)),
                    name=f"chat-turn-finish-{self._conversation_id}",
                ),
            )
            # Ownership transfers exactly here, on the line after the task that
            # will do the releasing exists and before the first ``await`` that
            # could hand control away. Flipping it earlier would strand the
            # lease if the task could not be created at all.
            finalize_started = True
            outcome = TurnCompleted(await asyncio.shield(self._finalize_task))
        except asyncio.CancelledError:
            # Two very different events reach this line and the difference is
            # the whole story for whoever reads the log: before ``finish`` the
            # turn really is lost, after it the cancel only ended the waiting —
            # ``finish`` is shielded, still running, and still the one that
            # releases.
            if finalize_started:
                _LOGGER.warning(
                    "chat turn for conversation %s was cancelled after finish "
                    "started; the assistant message still lands and finish "
                    "still owns the release",
                    self._conversation_id,
                )
            else:
                _LOGGER.warning(
                    "chat turn for conversation %s abandoned before it "
                    "completed",
                    self._conversation_id,
                )
            raise
        except BaseException as error:  # noqa: BLE001 - handed to the route
            outcome = TurnFailed(error)
            self._log_failure(error)
        finally:
            # Cleanup *before* publishing the outcome: the transport may raise
            # the failure out of the request the instant it reads it, and the
            # lease must already be free by then.
            if not finalize_started:
                await self._release_quietly()
            if outcome is not None:
                await self._publish(outcome)
            self._publish_final()

    async def _watch(self, task: asyncio.Task[None]) -> None:
        """Hard cap on a detached turn.

        Only a cap on the *generation*. Once ``finish`` has started the turn is
        no longer the relay's to cancel — see :meth:`_watch_finalize`.
        """
        try:
            await asyncio.wait_for(asyncio.shield(task), self._timeout)
            return
        except TimeoutError:
            pass
        except asyncio.CancelledError:
            raise
        except BaseException:  # noqa: BLE001 - a watchdog may not take the turn down
            # Reachable only by surprise: ``_run`` turns every failure of its
            # own into an outcome and never raises anything but
            # ``CancelledError``. Logged rather than swallowed because the
            # watchdog is the only thing bounding a turn whose audience left —
            # dying quietly here means the cap is gone and nothing outside
            # would ever say so.
            _LOGGER.exception(
                "chat turn watchdog for conversation %s stopped on an "
                "unexpected error; that turn is no longer bounded",
                self._conversation_id,
            )
            return
        finalize = self._finalize_task
        if finalize is not None:
            await self._watch_finalize(finalize)
            return
        _LOGGER.warning(
            "detached chat turn for conversation %s exceeded %ss before "
            "reaching finish; cancelling and releasing it",
            self._conversation_id,
            self._timeout,
        )
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _watch_finalize(self, finalize: asyncio.Task[Any]) -> None:
        """Second-level cap for a turn already handed over to ``finish``.

        Deliberately toothless, and that is the point being made rather than an
        omission. Cancelling the turn task here would not stop anything —
        ``finish`` runs in its own shielded task — and cancelling *that* would
        tear a SQLAlchemy greenlet in half, turning "the reply is late" into
        "the reply is half-written". So the only honest thing a cap can do at
        this stage is say the lease, charge and drain slot are still held, and
        by whom: the answer to "why is this conversation busy" has to be
        findable in the log, even when the answer is "it is still writing".
        """
        try:
            await asyncio.wait_for(asyncio.shield(finalize), self._timeout)
        except TimeoutError:
            _LOGGER.warning(
                "detached chat turn for conversation %s has been inside "
                "finish for %ss; leaving it to land — lease, charge and drain "
                "slot stay held until it does",
                self._conversation_id,
                self._timeout,
            )
        except asyncio.CancelledError:
            raise
        except BaseException:  # noqa: BLE001
            # ``_run`` is awaiting the same task and logs its failure there;
            # a second traceback here would only double-count it.
            return

    async def _release_quietly(self) -> None:
        """Free lease + charge + drain slot for a turn that never finished."""
        try:
            await self._finalizer.release_turn_lease()
        except Exception:  # pragma: no cover - defensive
            _LOGGER.exception(
                "chat turn lease release failed for conversation %s",
                self._conversation_id,
            )

    async def _publish(self, event: Any) -> None:
        """Hand one frame to the transport, or drop it once there isn't one."""
        if self._detached:
            return
        try:
            self._queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass
        await self._publish_blocking(event)

    async def _publish_blocking(self, event: Any) -> None:
        """Wait for buffer room, but never past the transport's lifetime.

        Three ways out, and the third is why the timeout is here. The reader
        takes a frame; or the reader leaves and :meth:`detach` wakes us; or
        neither happens — a transport still nominally connected that has not
        accepted a single frame in ``self._timeout`` seconds. That third case
        never reaches the route's ``finally``, so nothing else would ever call
        ``detach``, and the turn would sit on this queue holding the lease, the
        charge and the drain slot for the life of the process. Whatever the
        socket claims, an audience that has not moved in three minutes has
        left, so it is treated as one: detach, drop this frame, and let the
        turn finish for the transcript.
        """
        putter = asyncio.ensure_future(self._queue.put(event))
        gone = asyncio.ensure_future(self._detached_event.wait())
        try:
            await asyncio.wait(
                {putter, gone},
                return_when=asyncio.FIRST_COMPLETED,
                timeout=self._timeout,
            )
            if putter.done():
                putter.result()
            elif not self._detached:
                _LOGGER.warning(
                    "chat stream transport for conversation %s took no frame "
                    "in %ss; treating it as gone",
                    self._conversation_id,
                    self._timeout,
                )
                # Arms the watchdog too, so a turn that also stops making
                # progress upstream is bounded from here exactly as a real
                # disconnect would be.
                self.detach()
        finally:
            gone.cancel()
            if not putter.done():
                putter.cancel()

    def _publish_final(self) -> None:
        """End the reader's iteration without ever parking again.

        Synchronous, and non-blocking where :meth:`_publish` may wait, because
        this runs in ``_run``'s ``finally`` — which may be running *because the
        task was cancelled*. A clean-up path that can acquire a new reason to
        wait is a shutdown that never completes, and the wait it could acquire
        here is unbounded in the one case that matters (a full buffer whose
        reader has stopped reading but not left).

        Dropping the sentinel when the buffer is full is not a lost wake-up:
        :meth:`frames` also ends on "turn task done and buffer drained", which
        is precisely the state the reader arrives at afterwards.
        """
        if self._detached:
            return
        with suppress(asyncio.QueueFull):
            self._queue.put_nowait(_SENTINEL)

    def _log_failure(self, error: BaseException) -> None:
        """Record a turn that failed, loudly when nobody else can.

        A detached failure is the silent one: ``_publish`` drops the outcome,
        the task ends normally, and "the assistant message never landed" and
        "everything went fine" are byte-identical in the log. They are not the
        same event to the player — on hosted, a charge whose covered call was
        already served is settled rather than refunded (that is what stops
        "send, then abort" being free generation), so this is the shape of
        credits spent on a message nobody will ever see. Attached failures get
        one line and no traceback: the transport raises the same error out of
        the request, where it is logged in full, and an out-of-credits refusal
        travelling this path is routine rather than an incident.
        """
        if self._detached:
            _LOGGER.exception(
                "detached chat turn for conversation %s failed "
                "(detached=True); no assistant message will land",
                self._conversation_id,
                exc_info=error,
            )
            return
        _LOGGER.info(
            "chat turn for conversation %s failed (detached=False); "
            "the transport reports it: %r",
            self._conversation_id,
            error,
        )

    def _turn_is_over(self) -> bool:
        """Has the turn task finished? ``False`` before :meth:`start`."""
        task = self._turn_task
        return task is not None and task.done()

    def _wake_reader(self, _task: asyncio.Task[None]) -> None:
        """End :meth:`frames` even if ``_run`` died somewhere unexpected.

        A duplicate sentinel is harmless — the reader returns on the first one
        and never touches the queue again.
        """
        if self._detached:
            return
        with suppress(asyncio.QueueFull):
            self._queue.put_nowait(_SENTINEL)
