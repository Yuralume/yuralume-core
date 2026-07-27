"""Deferral seam for per-kind due jobs (HOSTED_CORE_SCALING §4.3.3).

The :class:`NextDueCalculator` carries an optional ``deferral_check`` hook that,
given a candidate ``due_at``, may return a LATER instant so the chain rewrites
its next occurrence forward instead of claiming + executing a job that a known
gate would no-op anyway (§4.3.3 — 順延 by rewriting ``due_at``, never retry in
place). This module wires that hook to the **existing** apply-time gates so the
distributed path only pre-applies a gate the tick body already enforces — never
a NEW suppression that would diverge from embedded (red-line / §14 parity).

Currently wired:

* ``feed_compose`` **daily-cap** — the feed composer no-ops once a character has
  published ``feed_daily_limit`` posts on its operator-local day
  (``FeedComposerService._gate_passes``). When that cap is already exhausted at
  the candidate's local date, the chain is pushed to the next local midnight (when
  the count resets). The VISIBLE outcome is identical either way (no post), so this
  is a pure claim/execute-cost saving, not a behaviour change.

Deliberately NOT deferred:

* ``proactive_evaluate`` on **quiet hours** — the embedded proactive path does not
  gate pings on the quiet-hours window (only dreams / chat do), so deferring it
  here would make distributed quieter than embedded and break §14 cadence parity.
  Quiet-hours pre-application would only be correct once the embedded proactive
  path itself adopts it.

The seam is intentionally generic: a new gate is added by extending
:meth:`DueJobDeferral.check` with another kind branch that mirrors an existing
apply-time gate.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone, tzinfo

from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.contracts.due_jobs import FEED_COMPOSE_KIND
from kokoro_link.contracts.feed import FeedPostRepositoryPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.timezone import timezone_for_id

_LOGGER = logging.getLogger(__name__)


class DueJobDeferral:
    """Pre-applies existing apply-time gates to a candidate due instant."""

    def __init__(
        self,
        *,
        feed_post_repository: FeedPostRepositoryPort | None = None,
        operator_profile_service=None,  # noqa: ANN001 - optional tz resolver
    ) -> None:
        self._feed_repo = feed_post_repository
        self._operator_profile_service = operator_profile_service

    async def check(
        self, character: Character, kind: str, candidate: datetime,
    ) -> datetime | None:
        """``DeferralCheck`` entry point — a later instant, or ``None`` to keep.

        Fail-soft: any resolution error yields ``None`` (no deferral) so the
        apply-time gate remains the sole authority (the double-guard)."""
        candidate = ensure_utc(candidate)
        if kind == FEED_COMPOSE_KIND:
            return await self._defer_feed_compose(character, candidate)
        return None

    async def _defer_feed_compose(
        self, character: Character, candidate: datetime,
    ) -> datetime | None:
        if self._feed_repo is None:
            return None
        limit = getattr(character, "feed_daily_limit", 0) or 0
        if limit <= 0:
            # Feed disabled — apply-time already no-ops; cadence unchanged.
            return None
        local_tz = await self._resolve_operator_timezone(character)
        local = candidate.astimezone(local_tz)
        local_date = local.date()
        try:
            count = await self._feed_repo.count_on_date(
                character.id, on=local_date, local_tz=local_tz,
            )
        except Exception:
            _LOGGER.exception(
                "due-job deferral: feed count failed character=%s", character.id,
            )
            return None
        if count < limit:
            return None  # cap not yet reached at the candidate — let it run
        # Cap exhausted for the candidate's local day → next local midnight, when
        # ``count_on_date`` resets to 0 and a post can publish again.
        next_local_midnight = datetime(
            local_date.year, local_date.month, local_date.day, tzinfo=local_tz,
        ) + timedelta(days=1)
        return next_local_midnight.astimezone(timezone.utc)

    async def _resolve_operator_timezone(self, character: Character) -> tzinfo:
        service = self._operator_profile_service
        if service is None:
            return timezone.utc
        user_id = getattr(character, "user_id", None) or "default"
        try:
            operator = await service.get_for_user(user_id)
            return timezone_for_id(getattr(operator, "timezone_id", None))
        except Exception:
            return timezone.utc


__all__ = ["DueJobDeferral"]
