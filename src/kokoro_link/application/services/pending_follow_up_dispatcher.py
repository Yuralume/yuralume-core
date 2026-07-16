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
5. On success → ``resolved``; on any failure → flip back to ``queued``
   with ``last_error`` set so the next tick retries.

Failure isolation: every step is wrapped so a single bad row does not
break the loop. Other characters / other tick steps must keep working.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, tzinfo

from kokoro_link.application.services.proactive_dispatcher import (
    ProactiveDispatcher,
)
from kokoro_link.application.services.busy_defer_policy import (
    BUSY_FOLLOW_UP_RELEASE_CEILING,
)
from kokoro_link.application.services.schedule_service import ScheduleService
from kokoro_link.contracts.dialogue_summarizer import DialogueSummarizerPort
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
        operator_persona_service=None,  # noqa: ANN001 - optional app service
        operator_profile_service=None,  # noqa: ANN001 - optional; resolves primary_language
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
        self._operator_persona_service = operator_persona_service
        # FRONTEND_I18N_PLAN — pin the deferred-reply language to the
        # same operator language chat / proactive use. Optional so the
        # legacy single-user wiring keeps working with "zh-TW" default.
        self._operator_profile_service = operator_profile_service
        self._local_tz = local_tz or timezone.utc
        self._tick_limit = max(1, tick_limit)

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

    async def _maybe_release(
        self, row: PendingFollowUp, *, now: datetime,
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
            output = await self._composer.compose(compose_input)
        except Exception:
            _LOGGER.exception(
                "pending-follow-up composer crashed id=%s", row.id,
            )
            await self._repository.save(
                resolving.marked_failed(error="composer crashed", now=now),
            )
            return False
        body = (output.content_text or "").strip()
        if not body:
            # Composer fail-soft → leave queued for the next tick (no
            # cap on retries; if the model is fundamentally stuck the
            # operator will see a permanently-queued row and investigate).
            await self._repository.save(
                resolving.marked_failed(error="empty compose", now=now),
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
            output = await self._scheduled_promise_composer.compose(
                compose_input,
            )
        except Exception:
            _LOGGER.exception(
                "scheduled-promise composer crashed id=%s", row.id,
            )
            await self._repository.save(
                resolving.marked_failed(error="composer crashed", now=now),
            )
            return False
        body = (output.content_text or "").strip()
        if not body:
            await self._repository.save(
                resolving.marked_failed(error="empty compose", now=now),
            )
            return False

        return await self._deliver_and_resolve(
            row=resolving,
            body=body,
            trigger=ProactiveTrigger.SCHEDULED_PROMISE,
            reason=f"scheduled-promise release: {resolving.promise_intent[:60]}",
            character_id=character.id,
            now=now,
        )

    async def _deliver_and_resolve(
        self,
        *,
        row: PendingFollowUp,
        body: str,
        trigger: ProactiveTrigger,
        reason: str,
        character_id: str,
        now: datetime,
    ) -> bool:
        """Common tail for both kinds: fan out via deliver_pre_composed
        and flip status to resolved/failed."""
        try:
            attempt = await self._proactive_dispatcher.deliver_pre_composed(
                character_id=character_id,
                text=body,
                trigger=trigger,
                reason=reason,
                now=now,
            )
        except Exception:
            _LOGGER.exception(
                "pending-follow-up deliver_pre_composed crashed id=%s",
                row.id,
            )
            await self._repository.save(
                row.marked_failed(error="delivery raised", now=now),
            )
            return False

        if attempt is None or attempt.outcome.value != "sent":
            outcome_text = (
                f"deliver={attempt.outcome.value if attempt else 'none'}"
            )
            await self._repository.save(
                row.marked_failed(error=outcome_text, now=now),
            )
            return False

        await self._repository.save(
            row.marked_resolved(message_text=body, now=now),
        )
        return True

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
