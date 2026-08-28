"""Pending follow-up dispatcher — tick-time release of deferred replies.

Runs from ``ProactiveScheduler._tick_all`` after the scheduler has
ensured today's schedule and (optionally) refreshed rest recovery, so
``ScheduleService.resolve_current`` returns the post-tick view of the
day.

Per-character flow:

1. Find queued ``PendingFollowUp`` rows whose ``scheduled_for`` has
   passed.
2. For each row, double-gate on the character's *current* busy_score —
   high busy means the LLM's original "I'll get back to you" promise
   still holds; we leave the row queued until the next tick.
3. Force-release at-cap rows regardless of busy_score (the user has
   already stacked up :data:`MAX_QUEUED_MESSAGES` follow-ups —
   continuing to defer makes the eventual reply unworkably long).
4. Mark the row ``resolving`` (so a crashing dispatcher mid-call can't
   be retried while a stale row sits in flight), call the LLM composer
   for the full reply, then call
   ``ProactiveDispatcher.deliver_pre_composed`` to fan out to web SSE +
   Telegram / LINE bindings — pinned to the binding resolved BEFORE the
   compose, because that resolution is also what decided whether the
   message may carry the player's own declaration.

   That compose goes through :class:`ComposerToolLoop` when one is
   wired (PF1), so a promise to *do* something — look a fact up, take a
   picture — actually runs the tool before the message is written, and
   ships whatever it produced as attachments on the same delivery. With
   no loop injected the step is the single plain ``compose`` it always
   was.
5. On success → ``resolved``; on any failure → flip back to ``queued``
   with ``last_error`` set so the next tick retries.

   With one exception (F1): a compose the HV1 honesty gate withheld does
   not flip back *still due*. An honesty park changes nothing about the
   row, so an immediate re-queue asks the same model the same question on
   the next tick — forever, if it is systematically dishonest — and,
   because ``list_due`` is ordered by ``scheduled_for`` and limited, the
   permanently-overdue row also holds the head of the queue against newer
   promises. So the release instant moves forward, and a park the *model*
   caused is charged against a per-row budget whose ceiling ends the row
   loudly instead of retrying it silently. A park caused by our own judge
   being unreachable is delayed but never charged — see
   :func:`_honesty_park_disposition`.

   The QG4 quality gate withholding a round takes the same "delayed, not
   charged" road (RC): its refusal also leaves the row unchanged, so the
   next tick would re-run the identical prompt and be refused identically
   — at two composes and two judge calls per tick, forever. It is only
   delayed and never given up on, because giving up needs a per-row
   attempt count and the only one that exists belongs to the honesty
   budget, whose exhaustion alarms about a lying model. See
   :data:`QUALITY_SKIP_RETRY_SECONDS`.

Failure isolation: every step is wrapped so a single bad row does not
break the loop. Other characters / other tick steps must keep working.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo

from kokoro_link.application.services.composer_tool_loop import (
    ComposedMessage,
    ComposerToolLoop,
)
from kokoro_link.application.services.outcome_claim_audit import (
    OUTCOME_CLAIM_PARK_JUDGE_UNAVAILABLE,
    OUTCOME_CLAIM_PARK_MODEL_REOFFENDED,
    outcome_claim_audit_scope,
    outcome_claim_audit_summary,
    outcome_claim_park_kind,
)
from kokoro_link.application.services.outcome_claim_guard import (
    OutcomeClaimGuard,
)
from kokoro_link.application.services.proactive_dispatcher import (
    ProactiveDispatcher,
)
from kokoro_link.application.services.busy_defer_policy import (
    BUSY_FOLLOW_UP_RELEASE_CEILING,
)
from kokoro_link.application.services.schedule_service import ScheduleService
from kokoro_link.contracts.dialogue_summarizer import DialogueSummarizerPort
from kokoro_link.contracts.messaging import OutboundAttachment
from kokoro_link.contracts.observability import (
    TurnRecorderPort,
    TurnRecordingDraft,
)
from kokoro_link.contracts.pending_follow_up import (
    PendingFollowUpRepositoryPort,
)
from kokoro_link.contracts.pending_follow_up_composer import (
    PendingFollowUpComposeInput,
    PendingFollowUpComposerPort,
)
from kokoro_link.contracts.repositories import (
    CharacterRepositoryPort,
    ConversationRepositoryPort,
)
from kokoro_link.contracts.scheduled_promise_composer import (
    ScheduledPromiseComposeInput,
    ScheduledPromiseComposerPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import MessageContentMode
from kokoro_link.domain.entities.pending_follow_up import (
    PendingFollowUp,
    PendingFollowUpKind,
    PendingFollowUpStatus,
)
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity
from kokoro_link.domain.value_objects.content_flow import (
    CONTENT_TOLERANCE_FRONTIER,
    sanitize_messages_for_tolerance,
)
from kokoro_link.domain.value_objects.timezone import timezone_for_id
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger

_LOGGER = logging.getLogger(__name__)

_BUSY_RELEASE_CEILING = BUSY_FOLLOW_UP_RELEASE_CEILING
"""Below this current busy_score, the dispatcher considers the
character free enough to honour the deferred reply. Mirror of
``_BUSY_DECIDER_INVOKE_FLOOR`` in ``chat_service``: if the floor caused
the decider to skip a defer in the first place, the same floor should
let us release it now."""


HONESTY_PARK_ATTEMPT_LIMIT = 3
"""How many model-caused honesty parks one row may collect before the
promise is given up on (F1 / plan D5 「總次數上限」).

Three, because a park is not one sample: each one already contains a
blocked compose *and* a correction pass that named the exact overclaim.
Three rows of that is nine model turns telling the same model the same
thing, which is enough evidence that the next tick will not go
differently. The alternative — no ceiling — is what shipped, and it means
a systematically dishonest row re-composes forever."""

HONESTY_REOFFENCE_RETRY_SECONDS = 300.0
"""Interval floor after a park the model caused (D5 「間隔下限」).

Short, because a re-offence is per-row and a fresh sample of the same
prompt genuinely may come back honest — the player is owed this message.
Not *zero*, because zero is the current behaviour: the row lands back on
``queued`` still due, so the very next tick re-runs it, and the whole
budget above would burn off in the time it takes the scheduler to loop."""

HONESTY_JUDGE_OUTAGE_RETRY_SECONDS = 900.0
"""Interval floor after a park **our judge** caused.

Longer than the re-offence one on purpose: an unreachable judge is a
deployment-wide condition, not a row-specific one, so every parked row is
about to retry against the same dead upstream. Retrying them all on every
tick is precisely the "豁免≠無限快速重試燒上游" D5 warns about, and it
buys nothing — the outage counter, not the retry rate, is what gets this
fixed."""


QUALITY_SKIP_RETRY_SECONDS = HONESTY_JUDGE_OUTAGE_RETRY_SECONDS
"""Interval floor after **our quality gate** withheld the composed prose (RC).

Deliberately the judge-outage floor rather than the re-offence one, and
deliberately an alias rather than a fourth number: a quality hard-skip is
the same *shape* of park as a judge outage. Our own gate refused the
round, nothing about the row changed, and the retry is a fresh sample of
the identical prompt — which on the QG4 seam costs two composes and two
judge calls every time it is taken. Before this the release instant was
left in the past, so "every time" meant every tick, forever, for a
composer that could not write prose this judge would accept.

Note what this floor deliberately does **not** do: it never cancels the
promise. Ending a row needs a per-row attempt count, and the only places
to keep one are a new column (a migration this ticket may not add) or the
HV1 honesty budget — and that budget is what
``park_retries_exhausted`` alarms on, so lending it to a stylistic defect
would make an alert that means "the model kept lying" fire for a message
that was merely unreadable. Bounding the *rate* is what is available
without either, and it is what the burn actually needed."""


@dataclass(frozen=True, slots=True)
class _HonestyParkDisposition:
    """What to do about a round the honesty gate withheld.

    Computed once, *before* the audit row is written, so the audit can
    record the decision rather than only the park that prompted it — the
    give-up is the terminal link of the park-reason chain, and a chain
    whose last link is missing cannot answer "how many promises did this
    actually cost".
    """

    kind: str
    """Which class of park (``OUTCOME_CLAIM_PARK_*``)."""
    attempts: int
    """The row's model-caused park count **after** this round. Unchanged
    from the row's own value for a judge-outage park."""
    charges_attempt: bool
    """Whether :attr:`attempts` is a *charge* against the row's budget or
    merely its unchanged current value. Carried as its own field so the
    "a judge outage costs the promise nothing" rule lives in exactly one
    place — the classifier — instead of being re-derived from
    :attr:`kind` at every write site."""
    give_up: bool
    """The attempt ceiling is reached: cancel instead of re-queueing."""
    retry_at: datetime
    """Earliest instant the next attempt may run. Meaningless — and
    unread — when :attr:`give_up`."""


def _honesty_park_disposition(
    *,
    metadata: dict[str, object] | None,
    row: PendingFollowUp,
    now: datetime,
) -> _HonestyParkDisposition | None:
    """Classify an empty compose, or ``None`` if the gate had no part in it.

    ``None`` is the important answer: it is what every pre-F1 empty
    compose gets — a composer fail-soft, a tool that produced nothing —
    and it routes to the untouched "back to queued, retry next tick"
    behaviour those paths have always had.
    """
    kind = outcome_claim_park_kind(metadata)
    if kind == OUTCOME_CLAIM_PARK_MODEL_REOFFENDED:
        attempts = row.honesty_park_attempts + 1
        return _HonestyParkDisposition(
            kind=kind,
            attempts=attempts,
            charges_attempt=True,
            give_up=attempts >= HONESTY_PARK_ATTEMPT_LIMIT,
            retry_at=now + timedelta(seconds=HONESTY_REOFFENCE_RETRY_SECONDS),
        )
    if kind == OUTCOME_CLAIM_PARK_JUDGE_UNAVAILABLE:
        return _HonestyParkDisposition(
            kind=kind,
            # Deliberately NOT incremented: our judge failing is not this
            # promise's fault, and a deployment-wide outage must not be
            # able to cancel every outstanding promise it touches.
            attempts=row.honesty_park_attempts,
            charges_attempt=False,
            give_up=False,
            retry_at=now + timedelta(
                seconds=HONESTY_JUDGE_OUTAGE_RETRY_SECONDS,
            ),
        )
    return None


class PendingFollowUpDispatcher:
    def __init__(
        self,
        *,
        repository: PendingFollowUpRepositoryPort,
        composer: PendingFollowUpComposerPort,
        proactive_dispatcher: ProactiveDispatcher,
        character_repository: CharacterRepositoryPort,
        conversation_repository: ConversationRepositoryPort | None = None,
        schedule_service: ScheduleService | None = None,
        dialogue_summarizer: DialogueSummarizerPort | None = None,
        scheduled_promise_composer: ScheduledPromiseComposerPort | None = None,
        tool_loop: ComposerToolLoop | None = None,
        operator_persona_service=None,  # noqa: ANN001 - optional app service
        player_persona_note_repository=None,  # noqa: ANN001 - PlayerPersonaNoteRepositoryPort | None
        operator_profile_service=None,  # noqa: ANN001 - optional; resolves primary_language
        visible_slot_port=None,  # noqa: ANN001 - VisibleSlotPort | None (avoid import cycle)
        turn_recorder: TurnRecorderPort | None = None,
        outcome_claim_guard: OutcomeClaimGuard | None = None,
        local_tz: tzinfo | None = None,
        tick_limit: int = 25,
    ) -> None:
        self._repository = repository
        self._composer = composer
        self._proactive_dispatcher = proactive_dispatcher
        self._characters = character_repository
        self._conversations = conversation_repository
        self._schedule_service = schedule_service
        self._dialogue_summarizer = dialogue_summarizer
        self._scheduled_promise_composer = scheduled_promise_composer
        # PF1 — two-pass compose→tool→compose. ``None`` (no tool registry,
        # fake provider, self-host without tools) means every compose stays
        # a single plain call, byte-identical to the pre-PF1 dispatcher.
        # Shared by BOTH kinds on purpose: PF2 only has to teach the
        # busy-defer adapter to read available_tools / emit tool_calls.
        self._tool_loop = tool_loop
        self._operator_persona_service = operator_persona_service
        # PP3 — the player's declared identity for this pair. Read only;
        # this dispatcher never writes one.
        self._player_persona_note_repository = player_persona_note_repository
        # FRONTEND_I18N_PLAN — pin the deferred-reply language to the
        # same operator language chat / proactive use. Optional so the
        # legacy single-user wiring keeps working with "zh-TW" default.
        self._operator_profile_service = operator_profile_service
        # P3-Dedup — at-most-once visible send for the DISTRIBUTED release path. A
        # reclaimed release job (worker crashed after the send, before marking the
        # row resolved) would otherwise re-compose and re-send a duplicate promise.
        # ``None`` (no DB / embedded no-op wiring) fails open, so the single-process
        # embedded tick is byte-identical (its send always wins the claim first).
        self._visible_slot_port = visible_slot_port
        # PF3 — where a fulfilment's capability-bound half (today: the GPU) gets
        # handed to so it runs under the deployment's ceiling instead of wherever
        # the composer happened to ask for it. Set by the container only when a
        # distributed queue exists; ``None`` — and any embedded release, leader or
        # not — runs the tool inline exactly as PF1 did.
        self._capability_enqueuer = None
        # HV3 — write-only, fire-and-forget, mirrors ChatService /
        # ProactiveDispatcher's own optional turn-recorder wiring. ``None``
        # (older container wiring, most tests) makes the honesty-gate
        # audit write in ``_record_honesty_audit`` a no-op.
        self._turn_recorder = turn_recorder
        # F1 — the same shared guard the tool loop asks for verdicts, held
        # here only to raise ONE counter: the alert line for a promise that
        # ran out of honesty retries. Reached through the container rather
        # than through ``tool_loop`` because a counter is not the loop's to
        # lend out, and ``None`` (no judge route) simply means no park of
        # that class can happen in the first place.
        self._outcome_claim_guard = outcome_claim_guard
        self._local_tz = local_tz or timezone.utc
        self._tick_limit = max(1, tick_limit)

    def set_capability_release_enqueuer(self, enqueuer) -> None:  # noqa: ANN001
        """Wire the PF3 capability hand-off (:class:`PendingFollowUpReleaseEnqueuer`).

        A setter, not a constructor argument, because the queue and the coordinator
        lease are built long after the dispatcher in the container — and because a
        deployment without them must keep working untouched."""
        self._capability_enqueuer = enqueuer

    async def tick(self, *, now: datetime | None = None) -> int:
        """Process all due rows. Returns the number of rows resolved."""
        when = now or datetime.now(timezone.utc)
        try:
            due = await self._repository.list_due(
                now=when, limit=self._tick_limit,
            )
        except Exception:
            _LOGGER.exception("pending-follow-up tick: list_due crashed")
            return 0
        if due:
            _LOGGER.info(
                "busy-defer tick: %d due rows", len(due),
            )
        resolved = 0
        for row in due:
            try:
                released = await self._maybe_release(row, now=when)
            except Exception:
                _LOGGER.exception(
                    "pending-follow-up tick: row crashed id=%s",
                    row.id,
                )
                released = False
            if released:
                resolved += 1
        return resolved

    async def release_row(
        self, row: PendingFollowUp, *, now: datetime,
        defer_capabilities: bool = False,
    ) -> bool:
        """Public entry for the distributed event-driven release path.

        The Phase 5 ``pending_follow_up_release`` worker handler hands one claimed
        row here; the busy-score / frozen re-gating, compose and fan-out are the
        SAME as the embedded tick (抽共用而非複製) — this only exposes the existing
        per-row release as a callable so the handler need not re-implement it.

        ``defer_capabilities`` (PF3) is the caller telling us it did NOT claim a
        provider slot: if the composer reaches for a capability-bound tool, queue
        that half instead of running it here. It defaults to False so the embedded
        tick — which has no queue to defer to — is untouched."""
        return await self._maybe_release(
            row, now=now, defer_capabilities=defer_capabilities,
        )

    async def _maybe_release(
        self, row: PendingFollowUp, *, now: datetime,
        defer_capabilities: bool = False,
    ) -> bool:
        character = await self._characters.get(row.character_id)
        if character is None:
            await self._repository.save(row.cancelled(now=now))
            return False
        if character.frozen:
            # A frozen character emits no background messages
            # (CHARACTER_FREEZE_PLAN). Leave the promise queued — it
            # releases naturally on the next tick once foreground chat
            # unfreezes the character, so the user still gets their reply.
            return False

        schedule, current_activity, just_finished = (
            await self._resolve_schedule(character, now=now)
        )

        if row.kind == PendingFollowUpKind.SCHEDULED_PROMISE:
            # Scheduled-promise rows do **not** wait for busy_score to
            # drop — the user asked for a message at this specific time,
            # not "when you're free". If the character happens to be
            # mid-meeting at 10am, we still send (the LLM composer
            # weaves in "正在開會但記得叫你起床" as natural context).
            return await self._release_scheduled_promise(
                row=row,
                character=character,
                current_activity=current_activity,
                just_finished=just_finished,
                now=now,
                defer_capabilities=defer_capabilities,
            )

        force = row.is_at_cap
        if not force and _is_still_busy(current_activity):
            # Promise still holds — leave the row queued. Next tick will
            # re-check; once the activity ends, current_activity flips
            # to ``just_finished`` and we release.
            _LOGGER.info(
                "busy-defer tick: keeping queued id=%s — still busy "
                "(busy_score=%.2f activity=%s)",
                row.id,
                current_activity.busy_score if current_activity else 0.0,
                current_activity.category if current_activity else "?",
            )
            return False
        _LOGGER.info(
            "busy-defer tick: releasing id=%s force=%s current_busy=%.2f "
            "queued_msgs=%d",
            row.id, force,
            current_activity.busy_score if current_activity else 0.0,
            len(row.messages),
        )

        # Claim the row first so a crash mid-LLM doesn't leave a sibling
        # tick (or the next tick) firing twice.
        resolving = row.marked_resolving(now=now)
        try:
            await self._repository.save(resolving)
        except Exception:
            _LOGGER.exception(
                "pending-follow-up: mark resolving failed id=%s", row.id,
            )
            return False

        recent_summary = await self._safe_summarize(
            character, conversation_id=resolving.conversation_id, now=now,
        )
        persona_lines = await self._safe_operator_persona_lines(character.id)
        operator_language = await self._resolve_operator_language(character)
        local_tz = await self._resolve_operator_timezone(character)
        # Resolved BEFORE the compose and carried to the send — the note in
        # the prompt and the endpoint that receives it must be the same
        # decision (see ``_resolve_release_target``).
        sink, player_persona_note = await self._resolve_release_target(character)
        compose_input = PendingFollowUpComposeInput(
            character=character,
            queued_messages=resolving.messages,
            brief_reply=resolving.brief_reply,
            defer_reason=resolving.defer_reason,
            queued_at=resolving.queued_at,
            just_finished_activity=just_finished,
            current_activity=current_activity,
            recent_dialogue_summary=recent_summary,
            now=now,
            local_tz=local_tz,
            operator_persona_lines=tuple(persona_lines),
            player_persona_note=player_persona_note,
            operator_primary_language=operator_language,
        )
        try:
            composed, outcome_claim_metadata = await self._compose(
                character=character,
                payload=compose_input,
                compose=self._composer.compose,
                conversation_id=resolving.conversation_id,
                recent_dialogue=recent_summary or "",
                row=resolving,
                defer_capabilities=defer_capabilities,
                now=now,
            )
        except Exception:
            _LOGGER.exception(
                "pending-follow-up composer crashed id=%s", row.id,
            )
            await self._fail_back_to_queued(
                resolving, error="composer crashed", now=now,
            )
            return False
        # F1 — decided before the audit write so the audit can carry the
        # decision, not just the park that prompted it. ``None`` for every
        # round the gate had no part in, which is what keeps a plain
        # composer fail-soft on its original path.
        disposition = (
            _honesty_park_disposition(
                metadata=outcome_claim_metadata, row=resolving, now=now,
            )
            if not composed.content_text and not composed.deferred_capability
            else None
        )
        # HV3 — written whenever the honesty gate looked at this round,
        # BEFORE the deferred/empty checks below: a park is exactly the
        # outcome that otherwise leaves no trace (neither branch reaches
        # ``_deliver_and_resolve``), and it is the outcome a dishonesty
        # rate most needs visible.
        await self._record_honesty_audit(
            character_id=character.id,
            row=resolving,
            metadata=outcome_claim_metadata,
            disposition=disposition,
        )
        if composed.deferred_capability:
            return await self._park_for_capability(
                row=resolving, composed=composed, now=now,
            )
        body = composed.content_text
        if not body:
            return await self._park_empty_compose(
                row=resolving, disposition=disposition, now=now,
                quality_skipped=composed.quality_skipped,
            )

        return await self._deliver_and_resolve(
            row=resolving,
            body=body,
            trigger=ProactiveTrigger.PENDING_FOLLOW_UP,
            reason=(
                f"follow-up release after {resolving.defer_reason or 'busy'}"
            ),
            character_id=character.id,
            attachments=composed.attachments,
            sink=sink,
            now=now,
        )

    async def _release_scheduled_promise(
        self,
        *,
        row: PendingFollowUp,
        character: Character,
        current_activity: ScheduleActivity | None,
        just_finished: ScheduleActivity | None,
        now: datetime,
        defer_capabilities: bool = False,
    ) -> bool:
        """Release a ``kind=SCHEDULED_PROMISE`` row.

        No busy-score check — the user asked for *this specific time*,
        not "when you're free". A null composer (operator hasn't wired
        scheduled-promise routing yet) cancels the row so it doesn't
        loop forever; everything else fail-softs into a retry.
        """
        if self._scheduled_promise_composer is None:
            _LOGGER.warning(
                "scheduled-promise tick: no composer wired — cancelling "
                "row id=%s to avoid infinite retry", row.id,
            )
            await self._repository.save(
                row.cancelled(now=now),
            )
            return False
        _LOGGER.info(
            "scheduled-promise tick: releasing id=%s intent=%r",
            row.id, row.promise_intent[:80],
        )
        resolving = row.marked_resolving(now=now)
        try:
            await self._repository.save(resolving)
        except Exception:
            _LOGGER.exception(
                "scheduled-promise: mark resolving failed id=%s", row.id,
            )
            return False

        summary = await self._safe_summarize(
            character, conversation_id=resolving.conversation_id, now=now,
        )
        persona_lines = await self._safe_operator_persona_lines(character.id)
        # The "promise_text" is the original user-side wording captured
        # at queue time (entity invariant: at least one message in the
        # row). It's optional context — composer falls back to intent
        # alone when blank.
        promise_text = (
            resolving.messages[0].content
            if resolving.messages else ""
        )
        promise_content_mode = (
            resolving.messages[0].content_mode
            if resolving.messages else MessageContentMode.NORMAL
        )
        promise_safe_summary = (
            resolving.messages[0].safe_summary
            if resolving.messages else ""
        )
        operator_language = await self._resolve_operator_language(character)
        local_tz = await self._resolve_operator_timezone(character)
        # Same single resolution as the busy-defer path above.
        sink, player_persona_note = await self._resolve_release_target(character)
        compose_input = ScheduledPromiseComposeInput(
            character=character,
            promise_intent=resolving.promise_intent,
            promise_text=promise_text,
            promise_made_at=resolving.queued_at,
            scheduled_for=resolving.scheduled_for,
            current_activity=current_activity,
            just_finished_activity=just_finished,
            recent_dialogue_summary=summary,
            now=now,
            operator_persona_lines=tuple(persona_lines),
            player_persona_note=player_persona_note,
            operator_primary_language=operator_language,
            local_tz=local_tz,
            promise_content_mode=promise_content_mode,
            promise_safe_summary=promise_safe_summary,
        )
        try:
            composed, outcome_claim_metadata = await self._compose(
                character=character,
                payload=compose_input,
                compose=self._scheduled_promise_composer.compose,
                conversation_id=resolving.conversation_id,
                recent_dialogue=summary or "",
                row=resolving,
                defer_capabilities=defer_capabilities,
                now=now,
            )
        except Exception:
            _LOGGER.exception(
                "scheduled-promise composer crashed id=%s", row.id,
            )
            await self._fail_back_to_queued(
                resolving, error="composer crashed", now=now,
            )
            return False
        # F1 — see the mirror comment in ``_maybe_release``.
        disposition = (
            _honesty_park_disposition(
                metadata=outcome_claim_metadata, row=resolving, now=now,
            )
            if not composed.content_text and not composed.deferred_capability
            else None
        )
        # HV3 — see the mirror comment in ``_maybe_release``.
        await self._record_honesty_audit(
            character_id=character.id,
            row=resolving,
            metadata=outcome_claim_metadata,
            disposition=disposition,
        )
        if composed.deferred_capability:
            return await self._park_for_capability(
                row=resolving, composed=composed, now=now,
            )
        body = composed.content_text
        if not body:
            return await self._park_empty_compose(
                row=resolving, disposition=disposition, now=now,
                quality_skipped=composed.quality_skipped,
            )

        return await self._deliver_and_resolve(
            row=resolving,
            body=body,
            trigger=ProactiveTrigger.SCHEDULED_PROMISE,
            reason=f"scheduled-promise release: {resolving.promise_intent[:60]}",
            character_id=character.id,
            attachments=composed.attachments,
            sink=sink,
            now=now,
        )

    async def _compose(
        self,
        *,
        character: Character,
        payload,  # noqa: ANN001 - one of the two composer input dataclasses
        compose,  # noqa: ANN001 - the matching composer port's ``compose``
        conversation_id: str | None,
        recent_dialogue: str,
        row: PendingFollowUp,
        defer_capabilities: bool,
        now: datetime,
    ) -> tuple[ComposedMessage, dict[str, object] | None]:
        """Compose the outbound body, with tools when the loop is wired.

        The single funnel both kinds go through, so promise fulfilment
        and busy-defer follow-up can never drift into two different
        tool protocols. Without a loop this is exactly the pre-PF1
        single ``compose(payload)`` call.

        The second element of the return (HV3) is the HV1 honesty gate's
        conclusion for this round, folded by
        ``outcome_claim_audit_summary`` — ``None`` when the round never
        reached the gate (no loop, or no tools this character may call),
        so a caller wired without a judge records nothing extra."""
        if self._tool_loop is None:
            output = await compose(payload)
            return ComposedMessage(
                content_text=(getattr(output, "content_text", "") or "").strip(),
            ), None
        schedule = (
            self._capability_scheduler(row=row, now=now)
            if defer_capabilities
            else None
        )
        with outcome_claim_audit_scope():
            composed = await self._tool_loop.run(
                character=character,
                payload=payload,
                compose=compose,
                conversation_id=conversation_id,
                recent_dialogue=recent_dialogue,
                schedule_capability=schedule,
            )
            outcome_claim_metadata = outcome_claim_audit_summary()
        return composed, outcome_claim_metadata

    async def _park_empty_compose(
        self,
        *,
        row: PendingFollowUp,
        disposition: _HonestyParkDisposition | None,
        now: datetime,
        quality_skipped: bool = False,
    ) -> bool:
        """An empty composed body. How it is retried depends on *why*.

        Four outcomes, and each split exists because the single outcome it
        replaced was wrong for the case it splits off:

        * ``disposition is None`` and no quality skip — the gates had no
          part in this (composer fail-soft, a tool that produced nothing).
          **Untouched**: back to ``queued``, still due, retried next tick.
          That is also the contract the PF3 image park depends on, which is
          why this branch is byte-identical to what it replaced.
        * ``quality_skipped`` — the QG4 band withheld the prose (RC). Back
          to ``queued``, but not due for a while: nothing about the row
          changed, so the next tick would re-run the identical prompt and
          be refused identically, at two composes and two judge calls a
          time. Charged to nothing — see
          :data:`QUALITY_SKIP_RETRY_SECONDS` for why this path delays but
          never cancels.
        * a park the judge's own unavailability caused — back to
          ``queued``, but not due for a while. Nothing is known about this
          row, so nothing is charged to it; the delay is about not
          hammering a dead upstream with every parked promise on the
          deployment, and about not letting perpetually-overdue rows hold
          the head of ``list_due`` against newer ones.
        * a park the model caused — same delayed re-queue, but the row's
          attempt budget is charged, and at the ceiling the promise is
          ended rather than re-offered forever.

        Returns ``False`` throughout: nothing was delivered in any of them.
        """
        if disposition is None:
            if quality_skipped:
                return await self._park_quality_skip(row=row, now=now)
            await self._fail_back_to_queued(
                row, error="empty compose", now=now,
            )
            return False
        if disposition.give_up:
            return await self._give_up_after_honesty_parks(
                row=row, disposition=disposition, now=now,
            )
        budget = (
            f"attempt {disposition.attempts}/{HONESTY_PARK_ATTEMPT_LIMIT}"
            if disposition.charges_attempt else "uncharged"
        )
        error = f"honesty park ({disposition.kind}); {budget}"
        wrote = await self._write_release_outcome(
            row,
            lambda fresh: fresh.honesty_parked(
                error=error,
                retry_at=disposition.retry_at,
                attempts=(
                    disposition.attempts if disposition.charges_attempt
                    else None
                ),
                now=now,
            ),
            still_ours=_a_retry_write_is_still_ours,
        )
        if wrote:
            _LOGGER.info(
                "pending-follow-up: honesty park id=%s kind=%s (%s) — next "
                "attempt at %s",
                row.id, disposition.kind, budget,
                disposition.retry_at.isoformat(),
            )
        return False

    async def _park_quality_skip(
        self, *, row: PendingFollowUp, now: datetime,
    ) -> bool:
        """Back off a row whose prose the quality gate keeps withholding.

        The same transition an uncharged honesty park uses
        (:meth:`PendingFollowUp.honesty_parked` with ``attempts=None``) —
        because "back to queued, but not before ``retry_at``" is exactly
        what is wanted and inventing a second transition that did the same
        thing would give the row two ways to mean one state. What it must
        NOT do is charge ``honesty_park_attempts``: that budget cancels the
        promise and its exhaustion is an alert line about a **lying**
        model, and this round said nothing false — it said nothing at all.
        ``last_error`` therefore names the quality gate, so an operator
        reading the row is never told the honesty gate withheld it.

        Returns ``False``: nothing was delivered."""
        retry_at = now + timedelta(seconds=QUALITY_SKIP_RETRY_SECONDS)
        wrote = await self._write_release_outcome(
            row,
            lambda fresh: fresh.honesty_parked(
                error="quality park (output_quality hard_skipped); uncharged",
                retry_at=retry_at,
                attempts=None,
                now=now,
            ),
            still_ours=_a_retry_write_is_still_ours,
        )
        if wrote:
            _LOGGER.info(
                "pending-follow-up: quality park id=%s — the output-quality "
                "gate withheld this round's prose; next attempt at %s "
                "(nothing charged to the honesty budget)",
                row.id, retry_at.isoformat(),
            )
        return False

    async def _give_up_after_honesty_parks(
        self,
        *,
        row: PendingFollowUp,
        disposition: _HonestyParkDisposition,
        now: datetime,
    ) -> bool:
        """End a promise the model lied about on every allowed attempt.

        The loud half of the ceiling. A cancelled row simply stops being
        due, so without the ``ERROR`` line and the alert-line counter the
        only trace an operator would ever get is a promise that quietly
        never arrived — the exact failure mode HV exists to end, re-entered
        through our own retry policy."""
        error = (
            "honesty gate: model re-claimed unsupported outcomes on all "
            f"{disposition.attempts} attempts"
        )
        wrote = await self._write_release_outcome(
            row,
            lambda fresh: fresh.honesty_retries_exhausted(
                error=error, attempts=disposition.attempts, now=now,
            ),
            still_ours=_a_retry_write_is_still_ours,
        )
        if not wrote:
            # Somebody else reached a decision about this promise while we
            # composed; theirs is newer and we do not overwrite it — and we
            # do not raise an alarm for a promise we did not actually drop.
            return False
        if self._outcome_claim_guard is not None:
            self._outcome_claim_guard.record_park_retries_exhausted()
            if row.is_honesty_repair:
                # B-2: this row is chat's own HV4 repair promise (F5) —
                # a caught lie the chat auditor already durably owed.
                # Cancelling it here is the *correct*, previously
                # ratified behaviour (a compensation that keeps lying
                # should not ship), so this does not touch that
                # decision. But D6's alert line counts repairs that
                # never land, and a compensation row that gives up is
                # exactly that: the lie was caught, and now nobody is
                # coming back to make good on it either. Without this
                # the D6 board would show a clean 100% while a caught
                # lie quietly went unrepaired a second time.
                self._outcome_claim_guard.record_chat_repair_missed()
        _LOGGER.error(
            "pending-follow-up: GIVING UP on id=%s character=%s kind=%s — the "
            "honesty gate withheld %d consecutive composes because the model "
            "kept claiming outcomes no tool produced. The promise will NOT be "
            "delivered. Check the composer's honesty prompt rules and the "
            "outcome_claim_judge route before assuming the model is at fault.",
            row.id, row.character_id, row.kind.value, disposition.attempts,
        )
        return False

    async def _record_honesty_audit(
        self,
        *,
        character_id: str,
        row: PendingFollowUp,
        metadata: dict[str, object] | None,
        disposition: _HonestyParkDisposition | None = None,
    ) -> None:
        """Fold this round's HV1 honesty-gate conclusion into a turn record.

        Written whenever the gate actually looked at something this round
        (``metadata`` non-``None``) — deliberately including a park: a
        parked round never reaches ``_deliver_and_resolve`` (S7 made this
        an invariant rather than a usual case — ``ComposerToolLoop._park``
        now only records ``parked`` when the round truly sent nothing; a
        blocked round whose tool had already produced deliverable
        attachments ships them with a fallback line and is *not* a park,
        so it reaches ``_deliver_and_resolve`` same as any other body),
        so without this write the exact rounds a dishonesty rate (謊稱率)
        most needs — the ones the gate stopped — would leave no trace at
        all. Never
        persists ``message_text`` or the judge's ``unsupported_claims``
        phrases, only counts and status strings (see the log
        de-identification rule in ``outcome_claim_audit``'s module
        docstring). Fire-and-forget and exception-safe, same contract as
        every other ``turn_recorder.record`` call site in this codebase.

        ``disposition`` (F1) closes the park-reason chain: a park says the
        message was withheld, and this says what that cost the promise —
        including the one terminal outcome, giving up, which is otherwise
        indistinguishable from a row that simply stopped being due. Counts
        and status strings only, same redline as everything else here."""
        if self._turn_recorder is None or metadata is None:
            return
        judge_metadata = dict(metadata)
        if disposition is not None:
            judge_metadata["park_disposition"] = {
                "kind": disposition.kind,
                "attempts": disposition.attempts,
                "attempt_limit": HONESTY_PARK_ATTEMPT_LIMIT,
                # Without this an ``attempts`` that stayed put reads
                # identically to one that was never charged — and a
                # dishonesty rate computed off the first would quietly
                # count judge outages as model offences.
                "charged": disposition.charges_attempt,
                "gave_up": disposition.give_up,
                "retry_at": (
                    "" if disposition.give_up
                    else disposition.retry_at.isoformat()
                ),
            }
        try:
            await self._turn_recorder.record(TurnRecordingDraft(
                character_id=character_id,
                kind="promise_fulfilment",
                post_turn_refs={
                    "pending_follow_up_id": row.id,
                    "pending_follow_up_kind": row.kind.value,
                    "outcome_claim_judge": judge_metadata,
                },
            ))
        except Exception:
            _LOGGER.exception(
                "pending-follow-up: honesty audit record failed id=%s",
                row.id,
            )

    def _capability_scheduler(self, *, row: PendingFollowUp, now: datetime):  # noqa: ANN202
        """The hand-off closure the tool loop consults, or ``None``.

        ``None`` when nothing can take the work — the loop then runs every tool
        inline, which is the whole of the embedded behaviour."""
        if self._capability_enqueuer is None:
            return None

        async def schedule(capability: str) -> bool:
            return await self._defer_capability(
                row=row, capability=capability, now=now,
            )

        return schedule

    async def _defer_capability(
        self, *, row: PendingFollowUp, capability: str, now: datetime,
    ) -> bool:
        """Ask the queue to take over this fulfilment's ``capability`` half."""
        enqueuer = self._capability_enqueuer
        if enqueuer is None:
            return False
        try:
            return await enqueuer.defer_capability(
                row, capability=capability, now=now,
            )
        except Exception:
            _LOGGER.exception(
                "pending-follow-up: %s hand-off crashed id=%s — running inline",
                capability, row.id,
            )
            return False

    async def _park_for_capability(
        self, *, row: PendingFollowUp, composed: ComposedMessage, now: datetime,
    ) -> bool:
        """The fulfilment is queued elsewhere: leave the row releasable.

        ``marked_failed`` is the existing "back to queued, try again" transition —
        the same one an empty compose takes — so the row keeps its place and the
        job that owns the capability slot picks it up. Nothing was sent, so this
        returns False; the promise is not broken, only rescheduled, and the error
        text says so for anyone reading the row."""
        parked = await self._write_release_outcome(
            row,
            lambda fresh: fresh.marked_failed(
                error=f"deferred to the {composed.deferred_capability} queue",
                now=now,
            ),
            still_ours=_a_retry_write_is_still_ours,
        )
        if parked:
            _LOGGER.info(
                "pending-follow-up: parked id=%s until the %s slot is free",
                row.id, composed.deferred_capability,
            )
        return False

    async def _fail_back_to_queued(
        self, row: PendingFollowUp, *, error: str, now: datetime,
    ) -> bool:
        """Flip this release's row back to ``queued`` for the next attempt."""
        return await self._write_release_outcome(
            row,
            lambda fresh: fresh.marked_failed(error=error, now=now),
            still_ours=_a_retry_write_is_still_ours,
        )

    async def _mark_resolved(
        self, row: PendingFollowUp, *, body: str, now: datetime,
    ) -> bool:
        """Record this release's message as delivered."""
        return await self._write_release_outcome(
            row,
            lambda fresh: fresh.marked_resolved(message_text=body, now=now),
            still_ours=_a_delivery_write_is_still_ours,
        )

    async def _write_release_outcome(
        self,
        row: PendingFollowUp,
        transition: Callable[[PendingFollowUp], PendingFollowUp],
        *,
        still_ours: Callable[[PendingFollowUpStatus], bool],
    ) -> bool:
        """Apply a post-compose transition onto a **re-read** row.

        EVERY write this release makes after its compose goes through here,
        because every one of them was computed from a snapshot taken before
        it — and PF3 keeps two executors alive over the same row on purpose
        (the reconciler re-mints the text half every sweep while the image
        half waits for its slot), while the repository's ``save`` is a
        version-less upsert. A stale snapshot written back can therefore put
        a row the image half already resolved back on ``queued`` with its
        ``resolved_at`` / ``resolved_message`` erased: the message the player
        already has, recorded as never sent — and the next sweep composes and
        renders it all over again.

        ``still_ours`` is **per transition, and the asymmetry is deliberate**,
        because the claim that puts a row on ``resolving`` is itself a
        version-less upsert: two executors can both see ``resolving`` and
        neither can tell whose it is. So each write abandons on what would
        actually outrank the fact it carries:

        * :meth:`_mark_resolved` after a confirmed send —
          :func:`_a_delivery_write_is_still_ours`. Gives up only on a
          **terminal** status (``resolved`` / ``cancelled``): somebody else
          already filed a final answer for this promise and re-stamping it
          with our body would record the wrong sentence as the one the player
          received. ``queued`` does NOT stop it: that only means a concurrent
          executor pushed the row back for a retry, and "the message went out"
          is the stronger fact — leaving it queued is exactly the bug above,
          just entered through the other door.
        * :meth:`_fail_back_to_queued` and :meth:`_park_for_capability` —
          :func:`_a_retry_write_is_still_ours`. Gives up on anything but
          ``resolving``. All they carry is "this pass didn't finish", which
          has no strength to overwrite anyone's result; and if the row is
          already ``queued``, the retry they want is scheduled regardless.
        * :meth:`_park_empty_compose` and
          :meth:`_give_up_after_honesty_parks` — the same *retry* rule,
          the second one despite writing a terminal status. What it
          carries is "**our** composes kept being withheld", which is a
          statement about this executor's passes and nothing else; if a
          concurrent executor has meanwhile queued, resolved or cancelled
          the row, its answer is both newer and better informed, and
          cancelling over it would drop a promise on the strength of a
          budget the row may no longer have spent.

        Returns whether the write happened, so a caller can keep its logging
        honest. It deliberately does NOT swallow ``save`` errors: a repository
        failure is the caller's to handle exactly as it was before."""
        fresh = await self._reread_writable(row, still_ours=still_ours)
        if fresh is None:
            return False
        await self._repository.save(transition(fresh))
        return True

    async def _reread_writable(
        self,
        row: PendingFollowUp,
        *,
        still_ours: Callable[[PendingFollowUpStatus], bool],
    ) -> PendingFollowUp | None:
        """Re-load ``row``, or ``None`` when ``still_ours`` rejects its status.

        Whatever the caller's rule is, it is applied to the status as it
        stands NOW rather than to the pre-compose snapshot — another executor
        may have reached a decision about this promise while we were
        composing, and its decision is strictly newer than ours.

        A read failure is treated as "not ours": we would be writing blind."""
        try:
            fresh = await self._repository.get(row.id)
        except Exception:
            _LOGGER.exception(
                "pending-follow-up: re-read before write-back failed id=%s — "
                "leaving the row untouched", row.id,
            )
            return None
        if fresh is None:
            _LOGGER.info(
                "pending-follow-up: row gone before write-back id=%s", row.id,
            )
            return None
        if not still_ours(fresh.status):
            _LOGGER.info(
                "pending-follow-up: row moved on to %s before write-back "
                "id=%s — not overwriting it", fresh.status.value, row.id,
            )
            return None
        return fresh

    async def _deliver_and_resolve(
        self,
        *,
        row: PendingFollowUp,
        body: str,
        trigger: ProactiveTrigger,
        reason: str,
        character_id: str,
        attachments: tuple[OutboundAttachment, ...] = (),
        sink=None,  # noqa: ANN001 - ResolvedProactiveSink | None (no import cycle)
        now: datetime,
    ) -> bool:
        """Common tail for both kinds: fan out via deliver_pre_composed
        and flip status to resolved/failed.

        ``sink`` is the endpoint this release was gated against, pinned all
        the way from :meth:`_resolve_release_target` to the send so the
        message cannot land anywhere the gate did not authorise. It is only
        forwarded when we actually have one: a ``None`` sink means the
        dispatcher never gave us a resolver to begin with, and such a
        stand-in has no ``target`` parameter to receive either.

        ``attachments`` carries whatever a tool produced while the
        promise was being fulfilled (a generated photo, typically) into
        the pre-composed delivery path that has always accepted them —
        the promise "晚點傳照片給你" is only kept if the photo ships
        with the message.

        Guards the visible send with the at-most-once slot claim: claim BEFORE the
        send, so a reclaimed job whose predecessor already sent finds the slot taken
        and resolves WITHOUT re-sending; release the slot on a delivery failure so a
        genuine retry can re-claim (embedded stays byte-identical — its single
        runner always wins the claim, and a failure frees it exactly as before).

        Every status write here is a post-compose write like the ones above, so it
        goes through :meth:`_write_release_outcome`. The slot-taken branch shares
        the *delivery* ownership rule rather than the retry one, because a refused
        claim proves this row's visible send is already committed by somebody —
        as good a reason to end the row as our own send, and the only thing left
        uncertain is the wording. That is what the terminal check protects:
        whoever DID send owns ``resolved_message``, so once they have filed it our
        never-delivered ``body`` can no longer be stamped over it."""
        if not await self._claim_follow_up_slot(character_id, row.id):
            # Another executor already committed this row's send (crash-after-send
            # → reclaim). At-most-once: resolve without re-delivering.
            _LOGGER.info(
                "pending-follow-up: release slot already taken id=%s — "
                "resolving without resend", row.id,
            )
            await self._mark_resolved(row, body=body, now=now)
            return True
        try:
            attempt = await self._proactive_dispatcher.deliver_pre_composed(
                character_id=character_id,
                text=body,
                trigger=trigger,
                reason=reason,
                attachments=attachments,
                now=now,
                **({"target": sink} if sink is not None else {}),
            )
        except Exception:
            _LOGGER.exception(
                "pending-follow-up deliver_pre_composed crashed id=%s",
                row.id,
            )
            await self._release_follow_up_slot(character_id, row.id)
            await self._fail_back_to_queued(
                row, error="delivery raised", now=now,
            )
            return False

        if attempt is None or attempt.outcome.value != "sent":
            outcome_text = (
                f"deliver={attempt.outcome.value if attempt else 'none'}"
            )
            await self._release_follow_up_slot(character_id, row.id)
            await self._fail_back_to_queued(row, error=outcome_text, now=now)
            return False

        await self._mark_resolved(row, body=body, now=now)
        return True

    async def _claim_follow_up_slot(
        self, character_id: str, follow_up_id: str,
    ) -> bool:
        """Own this row's release slot. ``True`` when we own it (or the port is
        unwired / errored — fail OPEN so a claim failure never suppresses a
        legitimate single-runner send). ``False`` only when the slot is verifiably
        already owned by another executor."""
        if self._visible_slot_port is None:
            return True
        from kokoro_link.contracts.visible_slots import SLOT_KIND_FOLLOW_UP

        try:
            return await self._visible_slot_port.claim(
                character_id, SLOT_KIND_FOLLOW_UP, follow_up_id,
            )
        except Exception:
            _LOGGER.exception(
                "pending-follow-up: slot claim crashed; failing open id=%s",
                follow_up_id,
            )
            return True

    async def _release_follow_up_slot(
        self, character_id: str, follow_up_id: str,
    ) -> None:
        """Free the release slot after a failed delivery so a retry can re-claim.
        Never raises — a release failure only risks suppressing one retry, and the
        row stays ``failed``/``queued`` for the next reconcile either way."""
        if self._visible_slot_port is None:
            return
        from kokoro_link.contracts.visible_slots import SLOT_KIND_FOLLOW_UP

        try:
            await self._visible_slot_port.release(
                character_id, SLOT_KIND_FOLLOW_UP, follow_up_id,
            )
        except Exception:
            _LOGGER.exception(
                "pending-follow-up: slot release crashed id=%s", follow_up_id,
            )

    async def _resolve_schedule(
        self, character: Character, *, now: datetime,
    ) -> tuple[
        DailySchedule | None, ScheduleActivity | None, ScheduleActivity | None
    ]:
        if self._schedule_service is None:
            return None, None, None
        try:
            schedule = await self._schedule_service.ensure_schedule(character)
        except Exception:
            _LOGGER.exception(
                "pending-follow-up: ensure_schedule crashed character=%s",
                character.id,
            )
            return None, None, None
        if schedule is None:
            return None, None, None
        current, _, just_finished = self._schedule_service.resolve_current(
            schedule, now=now,
        )
        return schedule, current, just_finished

    async def _safe_summarize(
        self, character: Character, *, conversation_id: str, now: datetime,
    ) -> str | None:
        if (
            self._dialogue_summarizer is None
            or self._conversations is None
        ):
            return None
        try:
            conversation = await self._conversations.get(conversation_id)
        except Exception:
            _LOGGER.exception(
                "pending-follow-up: conversation load failed id=%s",
                conversation_id,
            )
            return None
        if conversation is None:
            return None
        messages = conversation.recent_messages(
            limit=40, exclude_tool_only=True,
        )
        if not messages:
            return None
        messages = sanitize_messages_for_tolerance(
            messages,
            content_tolerance=CONTENT_TOLERANCE_FRONTIER,
        )
        if not messages:
            return None
        # Resolved outside the try: a timezone lookup failure must not be
        # mistaken for a summariser failure and cost us the summary — the
        # resolver already falls back to the service default on its own.
        local_tz = await self._resolve_operator_timezone(character)
        try:
            return await self._dialogue_summarizer.summarize(
                character=character,
                messages=messages,
                now=now,
                local_tz=local_tz,
            )
        except Exception:
            _LOGGER.exception(
                "pending-follow-up: summarizer crashed character=%s",
                character.id,
            )
            return None

    async def _resolve_operator_language(self, character) -> str:  # noqa: ANN001
        """Resolve the character owner's pinned ``primary_language``,
        falling back to ``"zh-TW"`` if the service is unwired or the
        lookup fails. Matches the same shape used elsewhere in this
        codebase (see ``ProactiveDispatcher._load_operator_language``)
        so the four LLM surfaces — chat, proactive, busy-defer follow-
        up, scheduled-promise — all see the same language signal."""
        default = "zh-TW"
        service = self._operator_profile_service
        if service is None:
            return default
        user_id = getattr(character, "user_id", None) or "default"
        try:
            operator = await service.get_for_user(user_id)
        except Exception:  # pragma: no cover - defensive
            return default
        if operator is None:
            return default
        lang = getattr(operator, "primary_language", "") or ""
        return lang.strip() or default

    async def _resolve_operator_timezone(self, character) -> tzinfo:  # noqa: ANN001
        service = self._operator_profile_service
        if service is None:
            return self._local_tz
        user_id = getattr(character, "user_id", None) or "default"
        try:
            operator = await service.get_for_user(user_id)
        except Exception:  # pragma: no cover - defensive
            return self._local_tz
        if operator is None:
            return self._local_tz
        try:
            return timezone_for_id(getattr(operator, "timezone_id", "UTC"))
        except ValueError:
            return self._local_tz

    async def _resolve_release_target(self, character):  # noqa: ANN001, ANN201
        """Resolve, ONCE, where this release goes and what it may say.

        Both facts come out of a single
        ``ProactiveDispatcher.resolve_proactive_sink`` call because they are
        one decision wearing two hats: "may this message carry the owner's
        declared identity" is answered *from* the endpoint it will reach.
        Asking twice — gate now, endpoint again after the compose — is the
        defect this method exists to close: the compose is a tool loop that
        can run for tens of seconds, and any ``ChannelBinding.updated_at``
        bump inside that window (the owner flipping ``accepts_proactive``,
        a ``with_conversation`` self-heal) re-orders the candidates. A
        multi-speaker group binding could therefore receive a message that
        was cleared, and written, for the owner's own DM.

        Returns ``(sink, note)``; ``sink is None`` means we could not pin
        one — an older dispatcher stand-in without the resolver, or a lookup
        that failed. That leaves the delivery resolving for itself exactly
        as it did before, and the note fails closed.
        """
        sink = await self._resolve_delivery_sink(character.id)
        return sink, await self._safe_player_persona_note(character, sink=sink)

    async def _resolve_delivery_sink(self, character_id: str):  # noqa: ANN202
        resolver = getattr(
            self._proactive_dispatcher, "resolve_proactive_sink", None,
        )
        if resolver is None:
            return None
        try:
            return await resolver(character_id)
        except Exception:
            _LOGGER.exception(
                "pending-follow-up: sink resolution failed character=%s",
                character_id,
            )
            return None

    async def _disclosure_allowed(self, character_id: str, sink) -> bool:  # noqa: ANN001
        """Whether owner-attached text may ride this release.

        Fail-closed everywhere, **``sink is None`` included**. The verdict is
        read off the pinned snapshot so it and the send provably describe the
        same endpoint; an unpinned release has no endpoint to describe, and
        re-asking the dispatcher for an unpinned second opinion is precisely
        the two-lookup shape the pin exists to remove.

        That second lookup was not merely redundant, it reopened the leak:
        ``resolve_proactive_sink`` swallows its own exceptions and answers
        ``None``, so a transient query failure produced no pin — and the
        retry a moment later could well succeed, put the declaration in the
        prompt, and then leave ``deliver_pre_composed`` to resolve the
        endpoint for itself, including a group binding bumped mid-compose.
        No pin, no disclosure; the release still goes out, just without the
        owner's declared identity in it.

        A dispatcher that cannot answer at all (an older stand-in without
        :meth:`~ProactiveDispatcher.persona_disclosure_allowed_for`, a
        verdict that raised) is likewise not permission to disclose.
        """
        if sink is None:
            return False
        verdict = getattr(
            self._proactive_dispatcher, "persona_disclosure_allowed_for", None,
        )
        if verdict is None:
            return False
        try:
            return bool(verdict(sink))
        except Exception:
            _LOGGER.exception(
                "pending-follow-up: disclosure verdict failed character=%s",
                character_id,
            )
            return False

    async def _safe_player_persona_note(  # noqa: ANN001
        self, character, *, sink=None,
    ) -> str:
        """The player's declaration, when this release may carry it.

        The release is handed to ``ProactiveDispatcher.deliver_pre_composed``,
        which fans it out to the very same external binding a proactive
        ping would reach — so the gate is the proactive dispatcher's, asked
        rather than re-derived. ``sink`` is that gate's already-resolved
        answer (see :meth:`_resolve_release_target`); its absence is a
        refusal, not an invitation to look the answer up again.
        """
        repository = self._player_persona_note_repository
        if repository is None:
            return ""
        if not await self._disclosure_allowed(character.id, sink):
            return ""
        operator_id = getattr(character, "user_id", None) or DEFAULT_OPERATOR_ID
        try:
            row = await repository.get(
                character_id=character.id, operator_id=operator_id,
            )
        except Exception:
            _LOGGER.exception(
                "pending-follow-up: player persona note load failed character=%s",
                character.id,
            )
            return ""
        return row.note if row is not None else ""

    async def _safe_operator_persona_lines(self, character_id: str) -> list[str]:
        service = self._operator_persona_service
        if service is None:
            return []
        try:
            persona = await service.get_current(character_id, DEFAULT_OPERATOR_ID)
            return list(service.render_for_prompt(persona))
        except Exception:
            _LOGGER.exception(
                "pending-follow-up: operator persona render failed character=%s",
                character_id,
            )
            return []


_TERMINAL_STATUSES = frozenset(
    {PendingFollowUpStatus.RESOLVED, PendingFollowUpStatus.CANCELLED},
)
"""The statuses that record somebody's **final** answer for a promise.
Everything else (``queued``, ``resolving``) says the promise is still in
flight, which is why a delivery may write over them."""


def _a_retry_write_is_still_ours(status: PendingFollowUpStatus) -> bool:
    """Ownership test for the writes that say "this pass didn't finish".

    Only ``resolving`` — see :meth:`PendingFollowUpDispatcher._write_release_outcome`
    for why this is stricter than the delivery rule."""
    return status == PendingFollowUpStatus.RESOLVING


def _a_delivery_write_is_still_ours(status: PendingFollowUpStatus) -> bool:
    """Ownership test for the write that records a **confirmed send**.

    Anything non-terminal, ``queued`` included — see
    :meth:`PendingFollowUpDispatcher._write_release_outcome`."""
    return status not in _TERMINAL_STATUSES


def _is_still_busy(current_activity: ScheduleActivity | None) -> bool:
    """Mirror of ``_BUSY_DECIDER_INVOKE_FLOOR`` from chat_service.

    Returns True when the character is mid an activity whose
    ``busy_score`` would have triggered the original defer decision.
    Below the threshold we treat the character as releasable —
    matching the chat-side perf gate so defer/release are symmetric.
    """
    if current_activity is None:
        return False
    return current_activity.busy_score >= _BUSY_RELEASE_CEILING
