"""HV4 — chat's side of the outcome-claim gate: audit after, repair later.

Every other surface HV covers composes the whole message, then decides
whether it may ship. Chat cannot: the reply streams token by token
(``api/routes/chat.py``'s ``{'token': item}`` loop), so by the time any
judge could answer, the sentence claiming a photo that was never rendered
is already on the player's screen. §3.6 of the plan therefore inverts the
shape for this one surface — **fail-open, audit, and then make good** —
and D6 fixes the strength of the third part at *100%*: a caught lie is
never quietly dropped.

That last requirement is what decides the whole design here. "Make good"
cannot be an ``asyncio`` task holding the claim in memory, because the
process that noticed is the process that would forget: a deploy, a
restart, a worker rotation, and the character never comes back to it. So
the repair is a **row** — an ordinary ``SCHEDULED_PROMISE``
``PendingFollowUp``, the same durable mechanism the character already
uses to keep 「晚點傳照片給你」 — and every property this ticket needs
falls out of reusing it rather than being built again:

* **it survives restarts and processes**, because that is what the table
  is for, and the distributed release job is minted the same way the
  promise writer mints one;
* **its release is exempt from the tier background multiplier (D5)** —
  ``PENDING_FOLLOW_UP_RELEASE_KIND`` is already declared
  ``KnobGate.NONE``, so a low-tier player's repair is not stretched out
  by a cadence knob. It is equally *not* dormancy-exempt, which is also
  what D5 asks for: nobody is waiting on a repair for a player who has
  not been seen in a week;
* **it cannot recurse**, because releasing it runs through
  ``ComposerToolLoop`` — the gate-型 path HV1 already covers. A repair
  that overclaims a second time is blocked and re-composed there rather
  than shipped;
* **the three fulfilment endings are PF's, not new ones**: the tool runs
  and its output ships as attachments; the tool fails and the failure
  fact reaches pass 2 so the character says so; the capability is off on
  this deployment (``BG_CAP_IMAGE=0``) and the promise is kept in words.
  「兌現」 means the account is settled, not that a picture exists;
* **undo takes it back**, because the row is stamped with the turn's
  ``turn_record_id`` and TU4's ``PendingFollowUpRestoreStep`` deletes
  anchored rows by identity. The reverse direction is equally handled and
  equally important: the row is absent from the turn's *pre-turn*
  snapshot, so undo's restore pass has nothing to write back — a repair
  for a turn that no longer exists must not rise from the dead.

One repair per conversation, not one per lie (F5)
--------------------------------------------------
A row per caught lie is right until the same capability fails several
turns running — the image tool is down, the player asks again, and the
character overclaims each time. Three audits, three rows, three due times
minutes apart, and the player receives a burst of near-identical
apologies. That spends exactly the credibility this whole feature is
here to protect, so a claim caught while the conversation still has an
unreleased repair open is **merged into it** instead.

Merged, never skipped. D6 puts a number on this — a caught lie is owed
100% of the time — so "one is already open, drop this one" is not an
available answer. Every path below either lands the new claim in the
existing row or opens a row of its own; the fallbacks are all in that
second direction.

Only into a row older than this turn (B-3)
-------------------------------------------
Merging moves a claim into a row the *turn does not own*, and undo is
keyed on ownership. The row carries one ``turn_record_id``; the claims
inside it can come from several turns. That is safe in one direction
only, and the direction is decided by whether the target row was already
there when this turn began:

* **older than the turn** — the turn's pre-turn journal snapshotted it
  (``list_open_for_conversation``, every open row, any kind), so TU4's
  restore pass rewinds the merge when this turn is undone. And the row's
  own anchor is on an *earlier* turn, which undo cannot reach until this
  one has been undone first — undo is last-turn-only. Both directions
  hold.
* **born inside this turn's own window** — the snapshot does not name
  it, so nothing rewinds the merge; and if its anchor is a *later* turn
  than ours, undoing that later turn deletes the row by anchor and takes
  our claim with it, while our turn is still on the player's screen.

The second shape is not hypothetical. Both audits are fire-and-forget
tasks behind a judge call of unbounded latency, so they can finish out
of order: turn N+1's audit lands first and opens the row, turn N's slow
audit then merges into it, and one undo of N+1 destroys the repair owed
for a lie still in the transcript. That is a D6 breach, and it is one
F5 introduced — before merging existed, the second audit simply opened
its own row.

So ``turn_started_at`` is a precondition of merging, not a hint. Without
it (no journal on this deployment) there is no undo to be consistent
with, and the filter stands down.

Concurrency discipline (PF's three rounds of repair, §6.3)
----------------------------------------------------------
The lesson there was that a blind snapshot upsert loses to whoever writes
last, and that ownership must be judged per transition. Before F5 this
writer stayed out of that entirely — it only ever ``add``ed a new row
with an id nothing else knew yet. Merging is a read-modify-write, which
walks straight back into it: two audits of the same conversation can read
one intent and the second ``save`` would erase the first one's claim.

So the merge is not a ``save``. It goes through
``coalesce_promise_intent``, a compare-and-swap whose predicate is the
intent the caller read, and whose other two predicates (still ``queued``,
still not due) close the same window against the *release* path's blind
row-level write. Whoever loses the swap is told so and opens its own row.
The trade is deliberate and one-directional: an occasional second repair
row is a mild redundancy, a lost claim is the failure the ticket exists
to prevent.

The one shared decision this writer makes — "has this turn been undone" —
is asked of the TU2 tombstone immediately before the write, exactly where
``_do_post_turn`` asks it and for exactly the same reason.

Never silent
------------
A judge that found a lie and then failed to write the repair is worse
than no judge, because it produces a clean-looking audit. Every path that
ends without a row records why: on the counters
(``chat_repair_missed`` is an alert line), in the log at ERROR, and in
the audit turn record's ``repair_status``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final

from kokoro_link.application.services.outcome_claim_audit import (
    outcome_claim_audit_scope,
    outcome_claim_audit_summary,
)
from kokoro_link.application.services.outcome_claim_guard import (
    OutcomeClaimGuard,
)
from kokoro_link.application.services.undone_turn_gate import UndoneTurnGate
from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.observability import (
    TurnRecorderPort,
    TurnRecordingDraft,
)
from kokoro_link.contracts.outcome_claim import OutcomeClaimEvidence
from kokoro_link.contracts.pending_follow_up import (
    PendingFollowUpRepositoryPort,
)
from kokoro_link.contracts.prompt import ToolOutcomeMessage
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.pending_follow_up import (
    HONESTY_REPAIR_DEFER_REASON,
    PendingFollowUp,
    PendingFollowUpStatus,
)
from kokoro_link.infrastructure.prompt.outcome_claim_honesty import (
    merge_repair_promise_intent,
    render_repair_promise_intent,
)

_LOGGER = logging.getLogger(__name__)

CHAT_HONESTY_TURN_KIND: Final = "chat_outcome_claim"
"""``TurnRecord.kind`` for one chat post-turn audit.

A kind of its own rather than more keys on the ``chat`` record: the audit
lands seconds later from a background task, and folding it into a row
another writer owns would mean a read-modify-write race for an
observability field. Queryable through the existing
``list_recent(character_id=…, kind=…)``."""

DEFAULT_REPAIR_DELAY_SECONDS: Final = 120.0
"""How long after the dishonest turn the repair comes due.

Not zero, and not long. Immediate would have the character interrupt the
very exchange it is apologising inside — and would race the player's own
undo of the turn that caused it. Long would break the thing that makes a
repair read as the character noticing rather than as a scheduled
apology. Two minutes is "a beat later", and the release path treats it
like any other promise from there."""

REPAIR_STATUS_NOT_NEEDED: Final = "not_needed"
"""The judge cleared the turn. The overwhelmingly common outcome."""
REPAIR_STATUS_NO_VERDICT: Final = "no_verdict"
"""The judge could not answer. Chat fails **open** (§3.6): the reply has
already been delivered, so there is nothing to withhold and no evidence
of a lie to repair. The guard's own outage streak carries the alarm."""
REPAIR_STATUS_QUEUED: Final = "queued"
REPAIR_STATUS_COALESCED: Final = "coalesced"
"""F5. The claim joined a repair this conversation already owed rather
than opening a second row. Owed just as durably — the distinction is
recorded because "the character will come back once about several
claims" and "the character will come back once about one claim" are the
same promise, while a *missing* row is not."""
REPAIR_STATUS_TURN_UNDONE: Final = "turn_undone"
"""The player reversed the turn while the audit was upstream. Not a
failure: the claim is gone from the transcript, so owning it would be the
character apologising for something the player can no longer see."""
REPAIR_STATUS_NO_REPOSITORY: Final = "no_repository"
REPAIR_STATUS_WRITE_FAILED: Final = "write_failed"


@dataclass(frozen=True, slots=True)
class ChatHonestyAuditResult:
    """What one post-turn audit did. Returned for tests and callers that
    want to log; the production path is fire-and-forget."""

    audited: bool
    """False when the turn was never eligible (no tools offered this turn,
    or no text) — the judge was not called and nothing was recorded."""
    verdict_status: str = ""
    repair_status: str = REPAIR_STATUS_NOT_NEEDED
    repair_follow_up_id: str | None = None

    @property
    def repaired(self) -> bool:
        """Whether a durable row now owes this turn's claim.

        Both write outcomes count. A coalesced claim is carried by a row
        that already existed, which is a different *shape* of answer and
        not a weaker one — reading it as "not repaired" would make the
        D6 rate under-report itself the moment the feature started
        working."""
        return self.repair_status in (
            REPAIR_STATUS_QUEUED, REPAIR_STATUS_COALESCED,
        )


class ChatOutcomeClaimAuditor:
    """Judge a delivered chat reply, and owe a repair when it lied.

    Called off the chat write point as a background task, so it costs the
    player no latency and can never fail their turn: :meth:`audit` returns
    a result instead of raising, for every branch including a crash.
    """

    __slots__ = (
        "_guard", "_repository", "_turn_recorder", "_clock", "_repair_delay",
    )

    def __init__(
        self,
        *,
        guard: OutcomeClaimGuard,
        pending_follow_up_repository: PendingFollowUpRepositoryPort | None = None,
        turn_recorder: TurnRecorderPort | None = None,
        clock: ClockPort | None = None,
        repair_delay_seconds: float = DEFAULT_REPAIR_DELAY_SECONDS,
    ) -> None:
        self._guard = guard
        self._repository = pending_follow_up_repository
        self._turn_recorder = turn_recorder
        self._clock = clock
        self._repair_delay = timedelta(
            seconds=max(0.0, float(repair_delay_seconds)),
        )

    async def audit(
        self,
        *,
        character: Character,
        conversation_id: str,
        turn_record_id: str,
        assistant_text: str,
        offered_tools: tuple[str, ...],
        tool_outcomes: tuple[ToolOutcomeMessage, ...] = (),
        delivered_attachments: int = 0,
        operator_primary_language: str = "",
        content_mode: str = "normal",
        undone_turn_gate: UndoneTurnGate | None = None,
        release_enqueuer: object | None = None,
        turn_started_at: datetime | None = None,
        now: datetime | None = None,
    ) -> ChatHonestyAuditResult:
        """Judge one delivered chat reply; owe a repair if it overclaimed.

        ``turn_started_at`` is the instant this turn's pre-turn journal
        was taken. It is what makes an F5 merge reversible (see the
        module docstring): only a repair row that already existed then is
        in the snapshot undo would restore from. ``None`` = this
        deployment keeps no journal, so there is no undo to stay
        consistent with and the filter stands down.

        ``undone_turn_gate`` and ``release_enqueuer`` arrive per call
        rather than through the constructor because both are wired onto
        ``ChatService`` by a *setter* long after this service is built
        (the tombstone store and the coordinator lease do not exist yet at
        construction time). Taking them here keeps one instance of each
        shared with the write point instead of a second copy that can
        silently be the wrong one — the failure mode being an auditor
        whose gate answers "not undone" for every turn.

        Never raises an ``Exception`` — but a cancellation (F2: the
        process shutdown drain giving up on a still-running judge past its
        bound) is not one; it is re-raised after being recorded, below.
        """
        try:
            return await self._audit(
                character=character,
                conversation_id=conversation_id,
                turn_record_id=turn_record_id,
                assistant_text=assistant_text,
                offered_tools=offered_tools,
                tool_outcomes=tool_outcomes,
                delivered_attachments=delivered_attachments,
                operator_primary_language=operator_primary_language,
                content_mode=content_mode,
                undone_turn_gate=undone_turn_gate,
                release_enqueuer=release_enqueuer,
                turn_started_at=(
                    ensure_utc(turn_started_at)
                    if turn_started_at is not None else None
                ),
                now=self._resolve_now(now),
            )
        except asyncio.CancelledError:
            # ``CancelledError`` is a ``BaseException`` (3.8+), so the
            # ``except Exception`` below never sees it — a task killed
            # mid-judge (the shutdown drain's bound, or any other
            # cancellation) used to vanish with zero signal. Whatever this
            # audit might have found — including a repair it would have
            # owed — is now unowned, the same fate as a failed repair
            # write, so it is counted the same way. Re-raise: swallowing a
            # cancellation here would leave the task looking like it
            # finished normally to whatever cancelled it.
            self._guard.record_chat_repair_missed()
            _LOGGER.warning(
                "chat honesty audit cancelled mid-judge character=%s "
                "turn=%s — verdict lost, any repair it may have owed is "
                "now unowned",
                getattr(character, "id", "?"), turn_record_id,
            )
            raise
        except Exception:  # noqa: BLE001 - a background audit must not escape
            _LOGGER.exception(
                "chat honesty audit crashed character=%s turn=%s",
                getattr(character, "id", "?"), turn_record_id,
            )
            return ChatHonestyAuditResult(audited=False)

    async def _audit(
        self,
        *,
        character: Character,
        conversation_id: str,
        turn_record_id: str,
        assistant_text: str,
        offered_tools: tuple[str, ...],
        tool_outcomes: tuple[ToolOutcomeMessage, ...],
        delivered_attachments: int,
        operator_primary_language: str,
        content_mode: str,
        undone_turn_gate: UndoneTurnGate | None,
        release_enqueuer: object | None,
        turn_started_at: datetime | None,
        now: datetime,
    ) -> ChatHonestyAuditResult:
        text = (assistant_text or "").strip()
        if not offered_tools or not text:
            # No tools this turn means no tool the character could have
            # called and lied about calling — the prompt-side red line
            # (HV2's ``honesty_discipline`` section) is the whole defence
            # there, and paying for a judge call would buy nothing.
            return ChatHonestyAuditResult(audited=False)
        evidence = OutcomeClaimEvidence(
            offered_tools=tuple(offered_tools),
            outcomes=tuple(tool_outcomes),
            delivered_attachments=max(0, int(delivered_attachments)),
        )
        with outcome_claim_audit_scope():
            verdict = await self._guard.review(
                message_text=text,
                evidence=evidence,
                character=character,
                operator_primary_language=operator_primary_language,
            )
            judge_summary = outcome_claim_audit_summary()
        self._guard.record_chat_audit()

        if verdict.consistent:
            result = ChatHonestyAuditResult(
                audited=True,
                verdict_status=verdict.status,
                repair_status=REPAIR_STATUS_NOT_NEEDED,
            )
        elif verdict.unavailable:
            result = ChatHonestyAuditResult(
                audited=True,
                verdict_status=verdict.status,
                repair_status=REPAIR_STATUS_NO_VERDICT,
            )
        else:
            _LOGGER.warning(
                "chat honesty audit: delivered reply claimed %d unsupported "
                "outcome(s) character=%s turn=%s — owing a repair follow-up",
                len(verdict.unsupported_claims), character.id, turn_record_id,
            )
            try:
                repair_id, repair_status = await self._owe_repair(
                    character=character,
                    conversation_id=conversation_id,
                    turn_record_id=turn_record_id,
                    unsupported_claims=verdict.unsupported_claims,
                    content_mode=content_mode,
                    undone_turn_gate=undone_turn_gate,
                    release_enqueuer=release_enqueuer,
                    turn_started_at=turn_started_at,
                    now=now,
                )
            except Exception:
                # B-1: everything ``_owe_repair`` does runs only after the
                # judge has already found this turn dishonest — a caught
                # lie. Most of its own failure branches already count
                # ``chat_repair_missed`` for themselves (the repository
                # write, the missing store); the two calls that could
                # not — ``UndoneTurnGate.is_undone`` and
                # ``PendingFollowUp.new_promise`` — used to raise straight
                # through to :meth:`audit`'s top-level handler, which
                # returns ``audited=False`` without ever touching the
                # alert counter or writing the audit turn record. A caught
                # lie would vanish with only a log line. Catching here
                # instead keeps this on the same path as every other
                # "ended without a row" outcome: the counter, the ERROR
                # log, and (via the ``result``/``_record_audit`` below)
                # the turn record's ``repair_status``.
                self._guard.record_chat_repair_missed()
                _LOGGER.exception(
                    "chat honesty audit: owing the repair crashed "
                    "character=%s turn=%s — the claim is now unowned",
                    character.id, turn_record_id,
                )
                repair_id, repair_status = None, REPAIR_STATUS_WRITE_FAILED
            result = ChatHonestyAuditResult(
                audited=True,
                verdict_status=verdict.status,
                repair_status=repair_status,
                repair_follow_up_id=repair_id,
            )
        await self._record_audit(
            character_id=character.id,
            conversation_id=conversation_id,
            turn_record_id=turn_record_id,
            judge_summary=judge_summary,
            result=result,
        )
        return result

    async def _owe_repair(
        self,
        *,
        character: Character,
        conversation_id: str,
        turn_record_id: str,
        unsupported_claims: tuple[str, ...],
        content_mode: str,
        undone_turn_gate: UndoneTurnGate | None,
        release_enqueuer: object | None,
        turn_started_at: datetime | None,
        now: datetime,
    ) -> tuple[str | None, str]:
        """Make the claim durably owed. Returns ``(row id, status)``.

        Two ways to end owed: merged into the repair this conversation
        already has open (F5), or a new row. The order is not an
        optimisation — a new row is the *fallback*, taken whenever
        merging cannot be done safely, precisely so that no branch here
        can end with the claim owned by nobody."""
        gate = undone_turn_gate or UndoneTurnGate()
        if await gate.is_undone(turn_record_id):
            # TU2, asked here for the same reason ``_do_post_turn`` asks it
            # before *its* writes: this whole audit ran behind a model call
            # the player never waited for, and an undo landing in that
            # window has already deleted the message being repaired.
            _LOGGER.info(
                "chat honesty audit: turn %s was undone while the judge ran "
                "— no repair owed", turn_record_id,
            )
            return None, REPAIR_STATUS_TURN_UNDONE
        repository = self._repository
        if repository is None:
            self._guard.record_chat_repair_missed()
            _LOGGER.error(
                "chat honesty audit: a delivered reply was judged dishonest "
                "but this deployment has no pending-follow-up store, so the "
                "character can never come back to it (character=%s turn=%s)",
                character.id, turn_record_id,
            )
            return None, REPAIR_STATUS_NO_REPOSITORY
        coalesced = await self._coalesce_into_open_repair(
            repository=repository,
            character_id=character.id,
            conversation_id=conversation_id,
            unsupported_claims=unsupported_claims,
            turn_started_at=turn_started_at,
            now=now,
        )
        if coalesced is not None:
            return coalesced
        row = PendingFollowUp.new_promise(
            character_id=character.id,
            conversation_id=conversation_id,
            promise_intent=render_repair_promise_intent(unsupported_claims),
            scheduled_for=now + self._repair_delay,
            # Deliberately blank: the entity falls back to the intent for
            # its first queued message, and copying the reply in would
            # persist the dishonest sentence itself into a second table
            # for no reader that needs it.
            source_message_content="",
            source_content_mode=content_mode,
            # The anchor TU4 deletes by. Without it the row would be
            # identified by a time window that its own background writer
            # races — the exact defect the promise writer's anchor fixed.
            turn_record_id=turn_record_id,
            # The stamp that lets the *next* audit of this conversation
            # find this row and merge into it (F5). Set at mint time by
            # the machine, so recognising a repair row later is an
            # equality check on a field rather than a reading of the
            # prose in ``promise_intent``.
            defer_reason=HONESTY_REPAIR_DEFER_REASON,
            now=now,
        )
        try:
            await repository.add(row)
        except Exception:
            self._guard.record_chat_repair_missed()
            _LOGGER.exception(
                "chat honesty audit: repair follow-up write FAILED "
                "character=%s turn=%s — the claim is now unowned",
                character.id, turn_record_id,
            )
            return None, REPAIR_STATUS_WRITE_FAILED
        self._guard.record_chat_repair_queued()
        _LOGGER.info(
            "chat honesty audit: repair follow-up queued id=%s character=%s "
            "turn=%s due=%s",
            row.id, character.id, turn_record_id,
            row.scheduled_for.isoformat(),
        )
        await self._enqueue_release(release_enqueuer, row, now=now)
        return row.id, REPAIR_STATUS_QUEUED

    async def _coalesce_into_open_repair(
        self,
        *,
        repository: PendingFollowUpRepositoryPort,
        character_id: str,
        conversation_id: str,
        unsupported_claims: tuple[str, ...],
        turn_started_at: datetime | None,
        now: datetime,
    ) -> tuple[str | None, str] | None:
        """Fold the claim into an open repair. ``None`` = open a new row.

        Every ``None`` below is a fallback to the pre-F5 behaviour, which
        is the safe direction in all of them: a second repair row costs
        the player one extra apology, while any form of "give up here"
        would cost them an unowned lie. Nothing in this method may raise
        into the caller for the same reason — a store that answers the
        lookup badly must not be able to turn a repairable turn into an
        unrepaired one.
        """
        try:
            target = await self._open_repair_row(
                repository=repository,
                character_id=character_id,
                conversation_id=conversation_id,
                turn_started_at=turn_started_at,
                now=now,
            )
            if target is None:
                return None
            merged = merge_repair_promise_intent(
                target.promise_intent, unsupported_claims,
            )
            if merged is None:
                # The list is full. The claim gets its own row rather
                # than being squeezed out of this one.
                _LOGGER.info(
                    "chat honesty audit: repair row %s is full — opening a "
                    "second one for this claim", target.id,
                )
                return None
            if merged == target.promise_intent:
                # Every claim is already quoted in the row: the same
                # sentence, caught again. Nothing is dropped by writing
                # nothing — the row already owes exactly this.
                self._guard.record_chat_repair_coalesced()
                _LOGGER.info(
                    "chat honesty audit: claim already owed by repair row "
                    "%s — no second row", target.id,
                )
                return target.id, REPAIR_STATUS_COALESCED
            if not await repository.coalesce_promise_intent(
                target.id,
                expected_intent=target.promise_intent,
                new_intent=merged,
                now=now,
            ):
                # Lost the swap: another audit merged first, or the row
                # went to a release worker between the read and here.
                _LOGGER.info(
                    "chat honesty audit: repair row %s moved under the merge "
                    "— opening a row of our own instead", target.id,
                )
                return None
        except Exception:  # noqa: BLE001 - never lose the claim to this
            _LOGGER.exception(
                "chat honesty audit: coalesce into an open repair failed "
                "conversation=%s — falling back to a new row", conversation_id,
            )
            return None
        self._guard.record_chat_repair_coalesced()
        _LOGGER.info(
            "chat honesty audit: claim merged into repair row id=%s "
            "character=%s due=%s",
            target.id, character_id, target.scheduled_for.isoformat(),
        )
        # No release job is minted here on purpose: the row already has
        # one from when it was written, and a second job for the same row
        # is a second delivery attempt the release path would have to
        # de-duplicate. The due time is left alone too — a repair the
        # character already owes must not be pushed further away by
        # catching another claim.
        return target.id, REPAIR_STATUS_COALESCED

    @staticmethod
    async def _open_repair_row(
        *,
        repository: PendingFollowUpRepositoryPort,
        character_id: str,
        conversation_id: str,
        turn_started_at: datetime | None,
        now: datetime,
    ) -> PendingFollowUp | None:
        """The repair row this conversation already owes, if any.

        The filters are the merge's preconditions, restated as a query:

        * **this character's** — a conversation belongs to one, but undo
          and cascade code have been bitten before by trusting that
          rather than saying it;
        * **still ``queued``** — a ``resolving`` row is being composed
          right now and will be written back from a copy read before this
          merge, so anything added here would vanish;
        * **not yet due** — a due row may already be in a release
          worker's hands, which is the same loss one tick earlier;
        * **older than this turn** (B-3) — a row queued after this turn's
          journal snapshot is not in that snapshot, so undoing this turn
          cannot rewind a merge into it, and if its anchor is a *later*
          turn then undoing that turn deletes it and this turn's claim
          with it. The module docstring has the sequence. Skipped when
          the caller has no ``turn_started_at`` to offer: no journal
          means no undo means nothing to be consistent with.

        The newest is chosen rather than the oldest: rows are minted at
        ``now + delay``, so the newest is the one furthest from release
        and therefore the one with the most room left to accumulate.
        """
        rows = await repository.list_open_for_conversation(conversation_id)
        candidates = [
            row for row in rows
            if row.character_id == character_id
            and row.is_honesty_repair
            and row.status == PendingFollowUpStatus.QUEUED
            and ensure_utc(row.scheduled_for) > now
            and (
                turn_started_at is None
                or ensure_utc(row.queued_at) <= turn_started_at
            )
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda row: ensure_utc(row.queued_at))

    @staticmethod
    async def _enqueue_release(
        enqueuer: object | None, row: PendingFollowUp, *, now: datetime,
    ) -> None:
        """Mint the distributed due-time release job, if there is a queue.

        ``None`` on embedded / self-host, where the in-process scheduler
        tick picks the row up from ``list_due`` instead. A failure here
        costs latency, never the repair: the release reconciler re-enqueues
        any still-due row on its next sweep."""
        if enqueuer is None:
            return
        try:
            await enqueuer.enqueue(row, now=now)  # type: ignore[attr-defined]
        except Exception:
            _LOGGER.exception(
                "chat honesty audit: repair release enqueue raised id=%s "
                "— leaving it to the reconcile sweep", row.id,
            )

    async def _record_audit(
        self,
        *,
        character_id: str,
        conversation_id: str,
        turn_record_id: str,
        judge_summary: dict[str, object] | None,
        result: ChatHonestyAuditResult,
    ) -> None:
        """Land the HV3-shaped audit row for this chat turn.

        Written for every turn that reached the judge, including the ones
        it cleared: a dishonesty rate needs the denominator, and the
        rounds that produced no repair are exactly the ones that would
        otherwise leave no trace. Carries counts and status strings only —
        never the reply, never the judge's quoted claims (the
        de-identification rule in ``outcome_claim_audit``'s docstring)."""
        if self._turn_recorder is None or judge_summary is None:
            return
        try:
            await self._turn_recorder.record(TurnRecordingDraft(
                character_id=character_id,
                kind=CHAT_HONESTY_TURN_KIND,
                conversation_id=conversation_id,
                post_turn_refs={
                    "parent_turn_record_id": turn_record_id,
                    "outcome_claim_judge": judge_summary,
                    "repair_status": result.repair_status,
                    "repair_follow_up_id": result.repair_follow_up_id or "",
                },
            ))
        except Exception:
            _LOGGER.exception(
                "chat honesty audit: audit record failed turn=%s",
                turn_record_id,
            )

    def _resolve_now(self, now: datetime | None) -> datetime:
        if now is not None:
            return ensure_utc(now)
        if self._clock is not None:
            return ensure_utc(self._clock.now())
        return datetime.now(timezone.utc)


__all__ = [
    "CHAT_HONESTY_TURN_KIND",
    "DEFAULT_REPAIR_DELAY_SECONDS",
    "REPAIR_STATUS_COALESCED",
    "REPAIR_STATUS_NOT_NEEDED",
    "REPAIR_STATUS_NO_REPOSITORY",
    "REPAIR_STATUS_NO_VERDICT",
    "REPAIR_STATUS_QUEUED",
    "REPAIR_STATUS_TURN_UNDONE",
    "REPAIR_STATUS_WRITE_FAILED",
    "ChatHonestyAuditResult",
    "ChatOutcomeClaimAuditor",
]
