"""Event-driven post-turn processing as a distributed one-shot job (§5 / §11).

After a chat turn completes, the post-turn processor extracts long-term memories,
refines character state, applies schedule / arc adjustments and persists any
"I'll message you at X" promises. In the embedded (self-host) runtime this stays a
fire-and-forget in-process task off the chat write point — **byte-identical, § red
line**. In the distributed (hosted) runtime the chat write point instead enqueues a
single one-shot ``post_turn`` job so the LLM extraction runs on a worker instead of
inside the API request, freeing the request thread.

Two collaborators live here — the feature is one cohesive unit:

* :class:`PostTurnEnqueuer` — the single distributed enqueue path off the chat write
  point. It reads the *current* coordinator epoch via the lease (the API process
  never holds the coordinator lease) and enqueues under it; when no live lease exists
  (no leader) it returns ``False`` so the caller falls back to in-process execution —
  **the work is never silently dropped**. The queue's active-idempotency index
  (``post_turn:{turn_record_id}``) makes a duplicate enqueue a no-op.
* :class:`PostTurnHandler` — the worker-side apply. It parses the id-only payload and
  delegates to the shared :meth:`ChatService.run_post_turn_for_record`, which re-reads
  the conversation to rebuild the turn text (§3.3 re-verify: a deleted / frozen
  character or a missing conversation is skipped) and runs the SAME ``_do_post_turn``
  implementation the embedded path uses (抽共用而非複製).

**Payload red line** (§3.1 / §11): the job payload carries ids / flags / an enum
ONLY — never the user or assistant message text, prompt, or any secret. The worker
re-loads the conversation by id and rebuilds ``user_text`` / ``assistant_text`` /
``prior_messages`` from it.

**No reconciler** — unlike ``pending_follow_up_release`` (whose release instant is in
the future and can be missed), a post-turn job is due *immediately* and its miss
fallback is direct in-process execution at the write point, so there is no due-row to
sweep later. A one-shot immediate job with an in-process fallback needs no safety-net
scan.

**Distributed-only by construction** — the embedded scheduler never wires any of this;
its post-turn stays the in-process ``_do_post_turn`` task off the chat write point.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Protocol

from kokoro_link.contracts.background_jobs import (
    COORDINATOR_LEASE_NAME,
    BackgroundCoordinatorLeasePort,
    BackgroundJobQueuePort,
    BackgroundJobSpec,
    ClaimedJob,
)
from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.due_jobs import POST_TURN_KIND, kind_spec

_LOGGER = logging.getLogger(__name__)

_POST_TURN_PRIORITY: int = (
    kind_spec(POST_TURN_KIND).priority  # type: ignore[union-attr]
)


#: Bounded re-attempts when an enqueue raises (commit outcome unknown). The
#: idempotency key makes each retry converge: if the earlier attempt actually
#: committed, the retry sees the active row (→ ALREADY_ACTIVE); if it did not, the
#: retry inserts (→ INSERTED). Exhausting the budget while still raising leaves the
#: outcome AMBIGUOUS.
_ENQUEUE_MAX_ATTEMPTS = 3


class PostTurnEnqueueOutcome(Enum):
    """Tri(+1)-state result of a distributed post-turn enqueue (§11).

    Collapsing all of these to a single ``False`` (the old bool) mis-classified an
    already-active duplicate and a commit-unknown ambiguity as "no job exists" and
    fell back to in-process execution — double-running the turn's non-idempotent
    emotion / promise / schedule writes when a worker was (or might be) already on
    it. The caller falls back ONLY on :attr:`should_fallback`."""

    #: A new job row was inserted — the worker will run this turn. No fallback.
    INSERTED = "inserted"
    #: An active job for this turn already exists (idempotency collision) — a worker
    #: owns it. No fallback.
    ALREADY_ACTIVE = "already_active"
    #: No live coordinator (transient leadership gap / embedded / fenced out) — no
    #: job row exists and none will run it, so the caller runs it in-process.
    NO_LEADER = "no_leader"
    #: The enqueue raised and the commit outcome stayed unknown after a bounded
    #: retry. A row MAY exist; to avoid double-running non-idempotent writes the
    #: caller drops this best-effort post-turn rather than fall back.
    AMBIGUOUS = "ambiguous"

    @property
    def should_fallback(self) -> bool:
        """Whether the caller should run the post-turn in-process instead.

        Only when we KNOW no worker owns the turn (``NO_LEADER``). ``INSERTED`` /
        ``ALREADY_ACTIVE`` mean a worker owns it; ``AMBIGUOUS`` prefers dropping one
        best-effort post-turn over a double non-idempotent write."""
        return self is PostTurnEnqueueOutcome.NO_LEADER


def _idempotency_key(turn_record_id: str) -> str:
    """One active post-turn job per chat turn.

    ``turn_record_id`` is a fresh uuid minted once per completed turn, so the key is
    globally unique per turn — a duplicate enqueue (e.g. a write-point retry) collapses
    to the single active job."""
    return f"post_turn:{turn_record_id}"


class PostTurnRunnerCallback(Protocol):
    """The shared id-only post-turn entry (``ChatService.run_post_turn_for_record``).

    Kept as a structural type so the handler does not import ``ChatService`` (avoids an
    application-layer import cycle) and stays trivially fakeable in tests."""

    async def __call__(
        self,
        *,
        turn_record_id: str,
        conversation_id: str,
        character_id: str,
        assistant_index: int,
        persona_enabled: bool,
        content_mode: str,
        has_user_message: bool = True,
        private_memory_ids: tuple[str, ...] = (),
        now: datetime | None = None,
    ) -> Mapping[str, Any]:
        ...


class PostTurnEnqueuer:
    """Enqueue one immediate post-turn job for a completed chat turn (distributed-only).

    The API process does not hold the coordinator lease, so the fencing epoch is read
    from the lease's ``current()`` — the true epoch of whatever coordinator is live. No
    live lease (no leader / embedded self-host) → no enqueue and the caller runs the
    post-turn in-process instead. The enqueue itself fences atomically against the
    epoch, so even a briefly-stale read can never insert under a superseded coordinator.
    """

    def __init__(
        self,
        *,
        queue: BackgroundJobQueuePort,
        coordinator_lease: BackgroundCoordinatorLeasePort,
        clock: ClockPort | None = None,
    ) -> None:
        self._queue = queue
        self._lease = coordinator_lease
        self._clock = clock

    async def enqueue(
        self,
        *,
        turn_record_id: str,
        conversation_id: str,
        character_id: str,
        assistant_index: int,
        persona_enabled: bool,
        content_mode: str,
        has_user_message: bool = True,
        private_memory_ids: tuple[str, ...] = (),
        operator_id: str | None = None,
        tenant_id: str | None = None,
        now: datetime | None = None,
    ) -> PostTurnEnqueueOutcome:
        """Enqueue the post-turn job and classify the outcome (§11).

        Never raises: a failure on the foreground chat path must not break the turn.
        Returns a :class:`PostTurnEnqueueOutcome`; the caller falls back to in-process
        execution ONLY on ``NO_LEADER`` (``should_fallback``). An ``AMBIGUOUS`` return
        (commit unknown after a bounded retry) is deliberately NOT a fallback — the
        turn's writes are non-idempotent and a double run is worse than dropping one
        best-effort post-turn."""
        resolved_now = self._resolve_now(now)
        epoch = await self._live_epoch(resolved_now, turn_record_id)
        if epoch is None:
            # No coordinator (embedded / transient gap / expired lease) → no row
            # exists and none will run it; the caller runs it in-process.
            return PostTurnEnqueueOutcome.NO_LEADER
        spec = BackgroundJobSpec(
            kind=POST_TURN_KIND,
            idempotency_key=_idempotency_key(turn_record_id),
            due_at=resolved_now,
            fencing_epoch=epoch,
            priority=_POST_TURN_PRIORITY,
            tenant_id=tenant_id,
            operator_id=operator_id,
            character_id=character_id,
            # Payload carries ids / flags ONLY (no chat content — §3.1 / §11); the
            # handler re-loads the conversation and rebuilds the turn text.
            payload={
                "turn_record_id": turn_record_id,
                "conversation_id": conversation_id,
                "character_id": character_id,
                "assistant_index": int(assistant_index),
                "persona_enabled": bool(persona_enabled),
                "content_mode": content_mode,
                # SN1: a silent 示意 turn has no user message, so the worker
                # must not walk backwards and adopt the *previous* turn's
                # line as this turn's input. Absent on jobs enqueued before
                # SN1, where the handler's default (``True``) is correct.
                "has_user_message": bool(has_user_message),
                # KB8: the ``private`` memory ids this turn's reply prompt
                # carried. Ids, not content — the §3.1 / §11 rail is about
                # chat text and secrets, and the worker still re-reads
                # every row it names. They cannot be re-derived worker-side
                # because memory selection is a live ranked pick: re-running
                # it later answers "what would be recalled now", not "what
                # was recalled then". Absent on pre-KB8 jobs, where the
                # handler's default (empty) correctly flips nothing.
                "private_memory_ids": [
                    str(item_id) for item_id in private_memory_ids if item_id
                ],
            },
        )
        last_exc: Exception | None = None
        for _attempt in range(_ENQUEUE_MAX_ATTEMPTS):
            try:
                job_id = await self._queue.enqueue(spec, now=resolved_now)
            except Exception as exc:  # commit outcome unknown — retry to converge
                last_exc = exc
                _LOGGER.warning(
                    "post-turn enqueue ambiguous (attempt %d) turn=%s character=%s: %r",
                    _attempt + 1, turn_record_id, character_id, exc,
                )
                continue
            if job_id is not None:
                return PostTurnEnqueueOutcome.INSERTED
            # ``None`` = an active row already exists (idempotency collision) OR the
            # lease fenced the insert out. Disambiguate via the lease so a real
            # duplicate is not mistaken for "no job".
            return await self._classify_none(epoch, resolved_now, turn_record_id)
        # Bounded retries exhausted while still raising: outcome genuinely unknown.
        _LOGGER.warning(
            "post-turn enqueue unresolved after %d attempts turn=%s character=%s: %r",
            _ENQUEUE_MAX_ATTEMPTS, turn_record_id, character_id, last_exc,
        )
        return PostTurnEnqueueOutcome.AMBIGUOUS

    async def _live_epoch(
        self, now: datetime, turn_record_id: str,
    ) -> int | None:
        """The current coordinator epoch if a leader holds a still-live lease at
        ``now``, else ``None`` (no leader / expired / read error)."""
        try:
            current = await self._lease.current(COORDINATOR_LEASE_NAME)
        except Exception:
            _LOGGER.exception(
                "post-turn enqueue: lease read failed turn=%s", turn_record_id,
            )
            return None
        if current is None:
            return None
        _owner, epoch, lease_until = current
        if ensure_utc(lease_until) < now:
            return None
        return epoch

    async def _classify_none(
        self, epoch: int, now: datetime, turn_record_id: str,
    ) -> PostTurnEnqueueOutcome:
        """Classify an enqueue that returned ``None`` under ``epoch``.

        ``None`` is either an idempotency collision (an active row exists → a worker
        owns the turn) or a fence-out (the lease expired / a new leader bumped the
        epoch → no row). Re-read the lease: an unchanged epoch still live at ``now``
        proves the row exists (the queue only fences when the epoch is not live), so
        it is a collision — ``ALREADY_ACTIVE``. Anything else is a fence-out —
        ``NO_LEADER``, safe to run in-process (nothing was inserted). A read error
        cannot confirm a row, so it degrades to ``AMBIGUOUS`` (no double run)."""
        try:
            current = await self._lease.current(COORDINATOR_LEASE_NAME)
        except Exception:
            _LOGGER.exception(
                "post-turn enqueue: lease re-read failed turn=%s", turn_record_id,
            )
            return PostTurnEnqueueOutcome.AMBIGUOUS
        if current is None:
            return PostTurnEnqueueOutcome.NO_LEADER
        _owner, cur_epoch, lease_until = current
        if cur_epoch == epoch and ensure_utc(lease_until) >= now:
            return PostTurnEnqueueOutcome.ALREADY_ACTIVE
        return PostTurnEnqueueOutcome.NO_LEADER

    def _resolve_now(self, now: datetime | None) -> datetime:
        if now is not None:
            return ensure_utc(now)
        if self._clock is not None:
            return ensure_utc(self._clock.now())
        return datetime.now(timezone.utc)


def _payload_ids(value: Any) -> tuple[str, ...]:
    """Read an id list off a job payload, tolerating anything else.

    A payload is JSON that has round-tripped through a queue row, so a
    field can be absent (pre-KB8 job), null, or — after a bad write —
    the wrong shape entirely. None of those is worth failing a job over
    when the consequence of ignoring them is simply that this turn
    discloses nothing.
    """
    if not isinstance(value, list):
        return ()
    return tuple(
        item.strip() for item in value
        if isinstance(item, str) and item.strip()
    )


@dataclass(frozen=True, slots=True)
class PostTurnHandlerResult:
    """Outcome of handling one post-turn job (observability / test assertions)."""

    executed: bool
    reason: str


class PostTurnHandler:
    """Worker-side apply for a ``post_turn`` job.

    Parses the id-only payload and delegates to the shared runner callback
    (``ChatService.run_post_turn_for_record``), which owns the §3.3 re-verify
    (conversation / character existence + frozen / subscription gate) and runs the
    SAME ``_do_post_turn`` body the embedded path uses. The body is fail-soft (it
    swallows every side-effect error internally and returns a dict), so a handled
    error never fails the job into a retry — only a hard worker crash mid-body would
    reclaim it. See the rerun-safety notes on ``run_post_turn_for_record``."""

    def __init__(
        self,
        *,
        runner: PostTurnRunnerCallback,
        clock: ClockPort | None = None,
    ) -> None:
        self._runner = runner
        self._clock = clock

    async def handle(
        self, job: ClaimedJob, *, now: datetime | None = None,
    ) -> PostTurnHandlerResult:
        resolved_now = self._resolve_now(now)
        payload = job.payload or {}
        turn_record_id = payload.get("turn_record_id")
        conversation_id = payload.get("conversation_id")
        character_id = payload.get("character_id")
        assistant_index = payload.get("assistant_index")
        if (
            not isinstance(turn_record_id, str) or not turn_record_id
            or not isinstance(conversation_id, str) or not conversation_id
            or not isinstance(character_id, str) or not character_id
            or not isinstance(assistant_index, int)
        ):
            return PostTurnHandlerResult(executed=False, reason="bad_payload")
        persona_enabled = bool(payload.get("persona_enabled", True))
        content_mode = payload.get("content_mode")
        if not isinstance(content_mode, str) or not content_mode:
            content_mode = "normal"
        result = await self._runner(
            turn_record_id=turn_record_id,
            conversation_id=conversation_id,
            character_id=character_id,
            assistant_index=assistant_index,
            persona_enabled=persona_enabled,
            content_mode=content_mode,
            has_user_message=bool(payload.get("has_user_message", True)),
            private_memory_ids=_payload_ids(payload.get("private_memory_ids")),
            now=resolved_now,
        )
        reason = str((result or {}).get("post_turn_skipped") or "executed")
        return PostTurnHandlerResult(
            executed=reason == "executed", reason=reason,
        )

    def _resolve_now(self, now: datetime | None) -> datetime:
        if now is not None:
            return ensure_utc(now)
        if self._clock is not None:
            return ensure_utc(self._clock.now())
        return datetime.now(timezone.utc)
