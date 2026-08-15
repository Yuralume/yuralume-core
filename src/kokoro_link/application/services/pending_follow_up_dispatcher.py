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
   Telegram / LINE bindings.

   That compose goes through :class:`ComposerToolLoop` when one is
   wired (PF1), so a promise to *do* something — look a fact up, take a
   picture — actually runs the tool before the message is written, and
   ships whatever it produced as attachments on the same delivery. With
   no loop injected the step is the single plain ``compose`` it always
   was.
5. On success → ``resolved``; on any failure → flip back to ``queued``
   with ``last_error`` set so the next tick retries.

Failure isolation: every step is wrapped so a single bad row does not
break the loop. Other characters / other tick steps must keep working.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone, tzinfo

from kokoro_link.application.services.composer_tool_loop import (
    ComposedMessage,
    ComposerToolLoop,
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
        operator_profile_service=None,  # noqa: ANN001 - optional; resolves primary_language
        visible_slot_port=None,  # noqa: ANN001 - VisibleSlotPort | None (avoid import cycle)
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
            character, conversation_id=resolving.conversation_id,
        )
        persona_lines = await self._safe_operator_persona_lines(character.id)
        operator_language = await self._resolve_operator_language(character)
        local_tz = await self._resolve_operator_timezone(character)
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
            operator_primary_language=operator_language,
        )
        try:
            composed = await self._compose(
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
        if composed.deferred_capability:
            return await self._park_for_capability(
                row=resolving, composed=composed, now=now,
            )
        body = composed.content_text
        if not body:
            # Composer fail-soft → leave queued for the next tick (no
            # cap on retries; if the model is fundamentally stuck the
            # operator will see a permanently-queued row and investigate).
            await self._fail_back_to_queued(
                resolving, error="empty compose", now=now,
            )
            return False

        return await self._deliver_and_resolve(
            row=resolving,
            body=body,
            trigger=ProactiveTrigger.PENDING_FOLLOW_UP,
            reason=(
                f"follow-up release after {resolving.defer_reason or 'busy'}"
            ),
            character_id=character.id,
            attachments=composed.attachments,
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
            character, conversation_id=resolving.conversation_id,
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
        compose_input = ScheduledPromiseComposeInput(
            character=character,
            promise_intent=resolving.promise_intent,
            promise_text=promise_text,
            scheduled_for=resolving.scheduled_for,
            current_activity=current_activity,
            just_finished_activity=just_finished,
            recent_dialogue_summary=summary,
            now=now,
            operator_persona_lines=tuple(persona_lines),
            operator_primary_language=operator_language,
            local_tz=local_tz,
            promise_content_mode=promise_content_mode,
            promise_safe_summary=promise_safe_summary,
        )
        try:
            composed = await self._compose(
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
        if composed.deferred_capability:
            return await self._park_for_capability(
                row=resolving, composed=composed, now=now,
            )
        body = composed.content_text
        if not body:
            await self._fail_back_to_queued(
                resolving, error="empty compose", now=now,
            )
            return False

        return await self._deliver_and_resolve(
            row=resolving,
            body=body,
            trigger=ProactiveTrigger.SCHEDULED_PROMISE,
            reason=f"scheduled-promise release: {resolving.promise_intent[:60]}",
            character_id=character.id,
            attachments=composed.attachments,
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
    ) -> ComposedMessage:
        """Compose the outbound body, with tools when the loop is wired.

        The single funnel both kinds go through, so promise fulfilment
        and busy-defer follow-up can never drift into two different
        tool protocols. Without a loop this is exactly the pre-PF1
        single ``compose(payload)`` call."""
        if self._tool_loop is None:
            output = await compose(payload)
            return ComposedMessage(
                content_text=(getattr(output, "content_text", "") or "").strip(),
            )
        schedule = (
            self._capability_scheduler(row=row, now=now)
            if defer_capabilities
            else None
        )
        return await self._tool_loop.run(
            character=character,
            payload=payload,
            compose=compose,
            conversation_id=conversation_id,
            recent_dialogue=recent_dialogue,
            schedule_capability=schedule,
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
        now: datetime,
    ) -> bool:
        """Common tail for both kinds: fan out via deliver_pre_composed
        and flip status to resolved/failed.

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
        self, character: Character, *, conversation_id: str,
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
        try:
            return await self._dialogue_summarizer.summarize(
                character=character, messages=messages,
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
