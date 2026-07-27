"""Per-conversation mutual exclusion for one player chat turn.

The chat hot path (``POST /chat/messages`` and ``/chat/messages/stream`` →
``ChatService``) historically had **no** per-conversation mutex at all. Two
turns that overlap on the same conversation do not lose data — the SA
repository's CAS append and the ``save()`` stale-version merge preserve both
tails — but they still produce three user-visible defects:

1. the earlier turn's messages can be reflowed *after* the later turn's, so the
   transcript reads out of order;
2. each turn's prompt was assembled before the other's rows existed, so the
   character answers the same moment twice, as if it had forgotten;
3. every post-turn side effect (memory extraction, state suggestion, schedule /
   arc adjustment, goal review) runs twice for one exchange.

A process-local ``asyncio.Lock`` cannot fix this under the hosted 2×api
round-robin topology, so the guard is a *lease*: the same TTL + fencing-epoch
contract the background coordinator, Creator Studio and the encounter pair
already use (``background_runtime_leases`` under postgres, an in-process
implementation for self-host). The self-host process therefore gets real
in-process exclusion from the same code path — the single-process deployment
had the identical bug and is fixed by the same mechanism.

Two deliberate differences from :class:`StudioExecutionLease`'s own usage:

* **owner id is per TURN, not per process.** The studio lease's process-wide
  ``owner_id`` exists so recovery can hand a target to the runner it spawns; a
  same-process re-acquire renews the same epoch and therefore *succeeds*. That
  is exactly the wrong answer here — two turns inside one api replica must
  exclude each other — so every turn mints its own owner and a sibling turn
  sees a live lease held by a different owner.
* **no waiting.** ``acquire`` either wins immediately or raises
  :class:`ConversationBusyError`; the route maps that to ``409``. Queuing a
  second turn behind a multi-minute streamed reply would just convert a visible
  conflict into an invisible timeout.
"""

from __future__ import annotations

from uuid import uuid4

from kokoro_link.application.services.studio_execution_lease import (
    StudioExecutionLease,
    StudioLeaseSession,
)

CONVERSATION_BUSY_CODE = "conversation_busy"
"""Structured error code carried by the route's 409 body."""

_LEASE_NAME_PREFIX = "chat:conversation:"
"""Lease namespace. Distinct from ``studio:`` / ``story_event:`` so an operator
reading ``background_runtime_leases`` can tell chat turns apart at a glance."""

DEFAULT_TURN_LEASE_TTL_SECONDS = 180
"""Lease window for one player turn.

A turn is *not* required to finish inside this window — the session heartbeat
renews every ``ttl/3`` (60s), and a streamed turn with a tool cycle can run for
minutes. The TTL only bounds how long a **crashed** replica keeps a
conversation locked, so it is sized against two facts rather than against turn
duration:

* it must comfortably exceed any plausible event-loop stall between heartbeats
  (a sync embedder batch, a greenlet DB round-trip) — 60s of slack over the
  heartbeat interval is generous;
* it must stay *below* the 300s upstream LLM read timeout
  (``infrastructure/llm/*._REQUEST_TIMEOUT``), so a conversation stranded by a
  hard kill is reclaimable before the player's own in-flight request gives up —
  their natural retry then succeeds instead of hitting a stale 409.
"""


DEFAULT_TURN_LEASE_MAX_LIFETIME_SECONDS = 900
"""Hard cap on how long one turn's heartbeat keeps renewing.

The streaming turn's lease is handed from ``send_message_stream`` to the
finalizer, so its release depends on someone finishing or aborting the stream.
If that owner is ever dropped without either (a response object that is
constructed but never iterated, say), an unbounded heartbeat would pin the
conversation for the life of the process — strictly worse than the TTL it was
meant to bound. After this many seconds the heartbeat stops and the lease ages
out normally. No real turn comes close: the upstream read timeout is 300s and a
tool cycle with a retry still lands far under 15 minutes.
"""


class ConversationBusyError(RuntimeError):
    """Another turn is already in flight on this conversation.

    Raised instead of queueing: the caller (route) maps it to ``409`` with the
    structured ``conversation_busy`` code so the client can distinguish it from
    the existing 403 / 404 / 429 chat outcomes.
    """

    code = CONVERSATION_BUSY_CODE

    def __init__(self, conversation_id: str) -> None:
        super().__init__(
            f"conversation {conversation_id} already has a turn in flight",
        )
        self.conversation_id = conversation_id


class ChatTurnLease:
    """Mints a fresh single-turn lease holder per conversation turn."""

    __slots__ = ("_port", "_ttl", "_interval", "_max_lifetime")

    def __init__(
        self,
        port,  # noqa: ANN001 - BackgroundCoordinatorLeasePort
        *,
        ttl_seconds: int = DEFAULT_TURN_LEASE_TTL_SECONDS,
        heartbeat_interval_seconds: float | None = None,
        max_lifetime_seconds: float | None = (
            DEFAULT_TURN_LEASE_MAX_LIFETIME_SECONDS
        ),
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ChatTurnLease.ttl_seconds must be > 0")
        self._port = port
        self._ttl = ttl_seconds
        self._interval = heartbeat_interval_seconds
        self._max_lifetime = max_lifetime_seconds

    @classmethod
    def from_studio_lease(
        cls,
        lease: StudioExecutionLease | None,
        *,
        ttl_seconds: int = DEFAULT_TURN_LEASE_TTL_SECONDS,
        heartbeat_interval_seconds: float | None = None,
    ) -> "ChatTurnLease | None":
        """Share the container's already-built lease *backend*.

        ``None`` in → ``None`` out, so a caller with no lease wiring keeps the
        historical unguarded behaviour verbatim.
        """
        if lease is None:
            return None
        return cls(
            lease.port,
            ttl_seconds=ttl_seconds,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
        )

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    def session(self, conversation_id: str) -> StudioLeaseSession:
        """An unentered session for ``conversation_id`` with a fresh owner."""
        holder = StudioExecutionLease(
            self._port,
            owner_id=f"chat-turn-{uuid4().hex}",
            ttl_seconds=self._ttl,
            name_prefix=_LEASE_NAME_PREFIX,
        )
        return StudioLeaseSession(
            holder,
            conversation_id,
            heartbeat_interval_seconds=self._interval,
            max_lifetime_seconds=self._max_lifetime,
        )

    async def acquire(self, conversation_id: str) -> StudioLeaseSession:
        """Claim the conversation for this turn.

        The session is returned already entered (heartbeat running); the caller
        owns its release via :func:`release_turn_lease`. Raises
        :class:`ConversationBusyError` when a sibling turn holds it.
        """
        session = self.session(conversation_id)
        await session.__aenter__()
        if not session.acquired:
            await session.__aexit__(None, None, None)
            raise ConversationBusyError(conversation_id)
        return session


async def release_turn_lease(session: StudioLeaseSession | None) -> None:
    """Release a session obtained from :meth:`ChatTurnLease.acquire`.

    Idempotent by construction at the call sites that matter: the streaming
    path releases from the finalizer *and* from the route's ``finally``, and a
    second call on an already-exited session only cancels an absent heartbeat
    and re-issues an owner-matched release that the backend treats as a no-op.
    ``None`` (lease-less wiring) is a no-op.
    """
    if session is None:
        return
    await session.__aexit__(None, None, None)
