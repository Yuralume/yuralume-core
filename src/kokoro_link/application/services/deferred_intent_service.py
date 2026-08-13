"""Deferred-intent application service (HUMANIZATION_ROADMAP §3.4).

Thin façade over :class:`DeferredIntentRepositoryPort` plus the feature
flag in :class:`HumanizationSettings`. The dispatcher records a motive
whenever the proactive intention judge skips a slot with a usable inner
motive; the same dispatcher (on the next tick) asks the service for
active motives and folds them into the next ``ProactiveContext`` so the
LLM can re-evaluate "is the timing right *now*?".

Why a separate service rather than calling the repo from the dispatcher
directly:

- The feature flag lives here, not on every caller.
- ``record_if_useful`` encapsulates the "is this motive even worth
  keeping?" decision (empty motive → drop, judge explicitly said the
  blocker is permanent → drop) so the dispatcher stays a coordinator.
- Future extensions (richer TTL policy, per-trigger override) plug in
  here without touching the dispatcher again.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timezone

from kokoro_link.bootstrap.settings import HumanizationSettings
from kokoro_link.contracts.deferred_intent import (
    DeferredIntentRepositoryPort,
)
from kokoro_link.contracts.proactive_intention import (
    ProactiveIntentionDecision,
)
from kokoro_link.domain.entities.deferred_intent import DeferredIntent

_LOGGER = logging.getLogger(__name__)


_DEFAULT_TTL_MINUTES = 24 * 60


class DeferredIntentService:
    def __init__(
        self,
        *,
        repository: DeferredIntentRepositoryPort,
        settings: HumanizationSettings,
        ttl_minutes: int = _DEFAULT_TTL_MINUTES,
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._ttl_minutes = max(1, int(ttl_minutes))

    @property
    def enabled(self) -> bool:
        return self._settings.deferred_intent_enabled

    async def record_if_useful(
        self,
        *,
        character_id: str,
        operator_id: str,
        trigger: str,
        decision: ProactiveIntentionDecision,
        revisit_at: datetime | None = None,
        now: datetime | None = None,
    ) -> DeferredIntent | None:
        """Persist a motive worth re-evaluating; drop otherwise.

        ``revisit_at`` is the already-parsed, already-validated UTC form
        of ``decision.revisit_at_iso`` — the caller owns timezone
        resolution because only it knows the operator's zone, and it
        passes ``None`` for anything unparseable or already past.

        Returns the stored row when written, ``None`` when feature off,
        motive empty, or storage failed.
        """
        if not self.enabled:
            return None
        inner = (decision.inner_motive or "").strip()
        if not inner:
            return None

        intent = DeferredIntent.new(
            character_id=character_id,
            operator_id=operator_id,
            trigger=trigger,
            inner_motive=inner,
            conversation_purpose=decision.conversation_purpose,
            expected_reply=decision.expected_reply,
            risk=decision.risk,
            best_timing=decision.best_timing,
            reason=decision.reason,
            revisit_at=revisit_at,
            ttl_minutes=self._ttl_minutes,
            now=now,
        )
        try:
            return await self._repository.add(intent)
        except Exception:
            _LOGGER.exception(
                "deferred_intent add failed (char=%s op=%s trigger=%s)",
                character_id, operator_id, trigger,
            )
            return None

    async def list_active(
        self,
        character_id: str,
        operator_id: str,
        *,
        now: datetime | None = None,
        limit: int = 5,
    ) -> list[DeferredIntent]:
        if not self.enabled:
            return []
        ref = now or datetime.now(timezone.utc)
        try:
            await self._repository.gc_expired(now=ref)
        except Exception:
            _LOGGER.exception("deferred_intent gc_expired failed")
        try:
            return await self._repository.list_active_for(
                character_id, operator_id, now=ref, limit=limit,
            )
        except Exception:
            _LOGGER.exception(
                "deferred_intent list_active_for failed (char=%s op=%s)",
                character_id, operator_id,
            )
            return []

    async def list_due(
        self,
        character_id: str,
        operator_id: str,
        *,
        now: datetime | None = None,
        limit: int = 5,
    ) -> list[DeferredIntent]:
        """Motives whose ``revisit_at`` alarm has rung.

        Runs on the cheap side of the proactive gate — once per tick,
        before any LLM budget is spent — so unlike ``list_active`` it
        does **not** trigger a GC sweep; the ``expires_at`` predicate in
        the query already keeps stale rows out of the answer.
        """
        if not self.enabled:
            return []
        ref = now or datetime.now(timezone.utc)
        try:
            return await self._repository.list_due_for(
                character_id, operator_id, now=ref, limit=limit,
            )
        except Exception:
            _LOGGER.exception(
                "deferred_intent list_due_for failed (char=%s op=%s)",
                character_id, operator_id,
            )
            return []

    async def clear_revisit_many(self, intent_ids: list[str]) -> int:
        """Spend the alarms: after one exemption they must not fire
        again, or a single parked motive would keep the character out of
        cooldown forever. Per-row failures are logged, never fatal."""
        if not intent_ids:
            return 0
        cleared = 0
        for intent_id in intent_ids:
            try:
                if await self._repository.clear_revisit(intent_id):
                    cleared += 1
            except Exception:
                _LOGGER.exception(
                    "deferred_intent clear_revisit failed (id=%s)", intent_id,
                )
        return cleared

    async def restore_revisit_many(
        self, intents: Sequence[DeferredIntent],
    ) -> int:
        """Give back the alarms spent on a tick that never produced a
        judgement (upstream failure, unusable model output).

        Takes the **pre-spend snapshots** rather than ids: each row's own
        appointment is the only value worth restoring, and the caller
        already holds it. Rows that carried no alarm are skipped instead
        of having one invented. Per-row failures are logged, never fatal
        — a lost restore degrades to the pre-fix behaviour."""
        restorable = [i for i in intents if i.revisit_at is not None]
        if not restorable:
            return 0
        restored = 0
        for intent in restorable:
            try:
                if await self._repository.restore_revisit(
                    intent.id, revisit_at=intent.revisit_at,
                ):
                    restored += 1
            except Exception:
                _LOGGER.exception(
                    "deferred_intent restore_revisit failed (id=%s)", intent.id,
                )
        return restored

    async def mark_consumed_many(
        self,
        intent_ids: list[str],
        *,
        now: datetime | None = None,
    ) -> int:
        """Mark all rows the dispatcher knows have just been folded into
        a successful proactive message. Per-row failures are logged but
        do not abort the batch."""
        if not intent_ids:
            return 0
        ref = now or datetime.now(timezone.utc)
        flipped = 0
        for intent_id in intent_ids:
            try:
                if await self._repository.mark_consumed(intent_id, now=ref):
                    flipped += 1
            except Exception:
                _LOGGER.exception(
                    "deferred_intent mark_consumed failed (id=%s)", intent_id,
                )
        return flipped
