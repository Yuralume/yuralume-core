"""Per-replica drain switch for rolling deployments (GD1-A).

``HOSTED_PROMPT_PACK_DISTRIBUTION_PLAN`` §7.1 measured what a hosted
``--force-recreate`` costs today: the in-flight turn dies at the SIGKILL that
follows the grace period, its conversation lease stays held until the 180s TTL
lapses, and the action charge it reserved sits on the player's wallet until the
stale-reservation sweeper finds it. Crash-safety already guarantees none of that
*corrupts* anything — GD's job is to stop the deploy path from relying on it.

The mechanism is deliberately one boolean and one counter:

* ``draining`` — flipped once, never back. A replica that has been told to drain
  is on its way out of the process table; "un-drain" would only exist to paper
  over an aborted deploy, and a replica that flip-flopped would be a replica
  whose observed ``active_turns`` could not be trusted. Restart is the reset.
* ``active_turns`` — how many player turns are mid-flight *right now*. This is
  the number ``deploy.sh`` polls to zero (§7.3 GD1 step 3) before it recreates
  the container, so it must count exactly the work that a SIGKILL would destroy.

**Off by default, and off is the historical behaviour verbatim.** Self-host
never posts to the drain endpoint, so ``draining`` stays ``False`` for the life
of every self-host process and every path below reduces to an integer increment.

Why the state is an object on the container rather than a module global: a
module global would be shared by the several containers a test session builds in
one interpreter, so one test's drain would leak into the next. One instance per
container is one instance per process in production, which is the scope that
actually matters — a drain request addresses *this replica*.
"""

from __future__ import annotations

import asyncio

SERVER_DRAINING_CODE = "draining"
"""Structured error code carried by the 503 body when a new turn is refused."""


class ServerDrainingError(RuntimeError):
    """This replica is shutting down and will not start new turns.

    Belt-and-suspenders only (§7.3 GD1 step 2): the router has already removed
    this replica from the upstream pool before the drain request is sent, so in
    a healthy roll nothing ever reaches this. It exists so that a request that
    slips through the window is refused *before* it costs the player anything —
    no lease, no charge, no persisted turn — instead of being killed halfway.

    Mapped to ``503`` (not ``409``): 503 is the honest "this server, not your
    request" answer, and it is what a client should retry elsewhere on.
    """

    code = SERVER_DRAINING_CODE

    def __init__(
        self,
        message: str = "this replica is draining and is not accepting new turns",
    ) -> None:
        super().__init__(message)


class ActiveTurn:
    """One counted in-flight turn. :meth:`release` is idempotent.

    Handed out by :meth:`DrainState.try_enter_turn`. Idempotence is not decoration:
    the streaming chat path releases from the finalizer *and* from the route's
    ``finally``, exactly like the conversation lease it travels with, so a second
    release is the normal case rather than a bug.

    An instance built with ``None`` counts nothing — that is the shape a service
    with no drain wiring (bare test containers) gets, so callers never branch.
    """

    __slots__ = ("_state",)

    def __init__(self, state: "DrainState | None" = None) -> None:
        self._state = state

    def release(self) -> None:
        state, self._state = self._state, None
        if state is not None:
            state._turn_finished()

    def __enter__(self) -> "ActiveTurn":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        # Synchronous on purpose: decrementing must also happen on the failure
        # path, and a ``finally`` that cannot await is a ``finally`` that cannot
        # be skipped by a cancellation landing on an await point.
        self.release()
        return False


class DrainState:
    """Whether this replica is draining, and how many turns are still running."""

    __slots__ = ("_draining", "_event", "_active_turns")

    def __init__(self) -> None:
        self._draining = False
        # Wakes the long-lived SSE generators the instant drain is requested, so
        # they close within milliseconds instead of on their next 15s heartbeat.
        # Constructed here rather than lazily because ``asyncio.Event`` binds no
        # loop until something awaits it, and ``set()`` never binds one at all —
        # so the container may build this outside a running loop, as it does.
        self._event = asyncio.Event()
        self._active_turns = 0

    @property
    def draining(self) -> bool:
        return self._draining

    @property
    def active_turns(self) -> int:
        return self._active_turns

    def begin_drain(self) -> None:
        """Refuse new turns and wake everything waiting on the drain signal.

        Idempotent — the deploy script may retry the request, and a partially
        completed roll may send it twice. A second call is a no-op rather than a
        second round of SSE closes.
        """
        self._draining = True
        self._event.set()

    async def wait_draining(self) -> None:
        """Block until drain is requested; return immediately if it already was.

        The early return also keeps a connection that arrives *after* drain from
        binding the event to its loop, which matters only in tests that build
        several event loops around one container.
        """
        if self._draining:
            return
        await self._event.wait()

    def ensure_accepting(self) -> None:
        """Raise :class:`ServerDrainingError` once this replica is draining.

        The check *without* the count, for gates that stand in front of a turn
        rather than own one — the chat routes refuse here, before the ownership
        lookup whose rest-recovery refresh would otherwise persist state on the
        way to a 503. A caller that is about to run the turn itself must use
        :meth:`try_enter_turn` instead, or it re-opens the window this class
        exists to close.
        """
        if self._draining:
            raise ServerDrainingError()

    def try_enter_turn(self) -> ActiveTurn:
        """Admit one turn: refuse while draining, otherwise count it — atomically.

        The check and the increment are one synchronous statement pair with no
        ``await`` between them, and that is the whole point. When they were two
        calls with a turn's prelude in between (character load, action charge,
        lease acquire — all of them awaits), everything in that prelude was
        invisible to ``active_turns``: the deploy's poll could read ``0`` while
        a turn was three awaits away from reserving credits and claiming a
        conversation, recreate the container, and SIGKILL exactly the work GD
        exists to protect. Counting from the *admission decision* instead means
        a turn is either refused or visible, never neither.

        The cost of admitting earlier is that a turn later rejected as
        ``conversation_busy`` is briefly counted too. That is correct rather
        than merely tolerable: it *is* in-flight work on this replica for as
        long as it runs, and over-counting only ever makes a deploy wait.
        """
        if self._draining:
            raise ServerDrainingError()
        self._active_turns += 1
        return ActiveTurn(self)

    def _turn_finished(self) -> None:
        # Clamped at zero: the gauge is a deploy gate, and a counter that could
        # go negative would let a double-release hide a genuinely running turn.
        if self._active_turns > 0:
            self._active_turns -= 1
