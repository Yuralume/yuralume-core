"""Unit tests for the DeferredIntent stack (HUMANIZATION_ROADMAP §3.4).

Three concerns covered here:

- ``DeferredIntent`` entity invariants (status / expires_at / new()).
- ``InMemoryDeferredIntentRepository`` query / GC semantics.
- ``DeferredIntentService`` glue (feature flag, ``record_if_useful``,
  ``list_active``, ``mark_consumed_many``).

The proactive dispatcher integration is exercised separately by
``test_proactive_dispatcher_*`` regression suites.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.deferred_intent_service import (
    DeferredIntentService,
)
from kokoro_link.bootstrap.settings import HumanizationSettings
from kokoro_link.contracts.proactive_intention import (
    ProactiveIntentionDecision,
)
from kokoro_link.domain.entities.deferred_intent import (
    STATUS_ACTIVE,
    STATUS_CONSUMED,
    STATUS_EXPIRED,
    DeferredIntent,
)
from kokoro_link.infrastructure.repositories.in_memory_deferred_intents import (
    InMemoryDeferredIntentRepository,
)


_CHAR = "char-A"
_OP = "default"
_NOW = datetime(2026, 5, 21, 4, 0, tzinfo=timezone.utc)


# ---- entity ----------------------------------------------------------------


def test_new_sets_status_active_with_future_ttl():
    intent = DeferredIntent.new(
        character_id=_CHAR,
        operator_id=_OP,
        trigger="tick",
        inner_motive="想分享今天看的書",
        ttl_minutes=60,
        now=_NOW,
    )
    assert intent.status == STATUS_ACTIVE
    assert intent.expires_at == _NOW + timedelta(minutes=60)
    assert intent.is_active_at(_NOW) is True
    assert intent.is_active_at(_NOW + timedelta(minutes=59)) is True
    assert intent.is_active_at(_NOW + timedelta(minutes=61)) is False


def test_new_rejects_zero_ttl():
    """Floor to 1 minute — never accept TTL=0 (would create instantly
    expired rows that just clutter the table)."""
    intent = DeferredIntent.new(
        character_id=_CHAR,
        operator_id=_OP,
        trigger="tick",
        inner_motive="想說話",
        ttl_minutes=0,
        now=_NOW,
    )
    assert intent.expires_at == _NOW + timedelta(minutes=1)


def test_status_must_be_valid():
    with pytest.raises(ValueError, match="status"):
        DeferredIntent(
            id="x",
            character_id=_CHAR,
            operator_id=_OP,
            trigger="tick",
            inner_motive="m",
            conversation_purpose="",
            expected_reply="",
            risk="",
            best_timing="",
            reason="",
            status="bogus",
            created_at=_NOW,
            expires_at=_NOW + timedelta(minutes=10),
        )


def test_marked_consumed_flips_status():
    intent = DeferredIntent.new(
        character_id=_CHAR,
        operator_id=_OP,
        trigger="tick",
        inner_motive="m",
        now=_NOW,
    )
    consumed = intent.marked_consumed(now=_NOW + timedelta(minutes=10))
    assert consumed.status == STATUS_CONSUMED
    assert consumed.consumed_at == _NOW + timedelta(minutes=10)
    # Original instance is immutable — verify copy semantics.
    assert intent.status == STATUS_ACTIVE
    assert intent.is_active_at(_NOW) is True


# ---- repository ------------------------------------------------------------


@pytest.mark.asyncio
async def test_repo_returns_only_active_for_pair():
    repo = InMemoryDeferredIntentRepository()
    await repo.add(DeferredIntent.new(
        character_id=_CHAR, operator_id=_OP, trigger="tick",
        inner_motive="m1", now=_NOW,
    ))
    # Different character → isolated.
    await repo.add(DeferredIntent.new(
        character_id="char-B", operator_id=_OP, trigger="tick",
        inner_motive="m2", now=_NOW,
    ))
    # Different operator → isolated.
    await repo.add(DeferredIntent.new(
        character_id=_CHAR, operator_id="other-op", trigger="tick",
        inner_motive="m3", now=_NOW,
    ))

    listed = await repo.list_active_for(_CHAR, _OP, now=_NOW)

    assert [i.inner_motive for i in listed] == ["m1"]


@pytest.mark.asyncio
async def test_repo_gc_expired_sweeps_past_ttl():
    repo = InMemoryDeferredIntentRepository()
    fresh = DeferredIntent.new(
        character_id=_CHAR, operator_id=_OP, trigger="tick",
        inner_motive="fresh", ttl_minutes=60, now=_NOW,
    )
    stale = DeferredIntent.new(
        character_id=_CHAR, operator_id=_OP, trigger="tick",
        inner_motive="stale", ttl_minutes=5,
        now=_NOW - timedelta(hours=1),
    )
    await repo.add(fresh)
    await repo.add(stale)

    swept = await repo.gc_expired(now=_NOW)

    assert swept == 1
    snap = {row.inner_motive: row.status for row in repo.snapshot()}
    assert snap == {"fresh": STATUS_ACTIVE, "stale": STATUS_EXPIRED}


@pytest.mark.asyncio
async def test_repo_mark_consumed_returns_true_only_for_active():
    repo = InMemoryDeferredIntentRepository()
    intent = await repo.add(DeferredIntent.new(
        character_id=_CHAR, operator_id=_OP, trigger="tick",
        inner_motive="m", now=_NOW,
    ))

    assert await repo.mark_consumed(intent.id, now=_NOW) is True
    # second call should not flip a consumed row again
    assert await repo.mark_consumed(intent.id, now=_NOW) is False


# ---- service --------------------------------------------------------------


def _service(*, enabled: bool = True) -> tuple[DeferredIntentService, InMemoryDeferredIntentRepository]:
    repo = InMemoryDeferredIntentRepository()
    svc = DeferredIntentService(
        repository=repo,
        settings=HumanizationSettings(deferred_intent_enabled=enabled),
    )
    return svc, repo


def _decision(*, inner_motive: str = "想說最近看的書",
              should_consume: bool = False) -> ProactiveIntentionDecision:
    return ProactiveIntentionDecision(
        should_consume_slot=should_consume,
        reason="現在對方剛說完話，再發一條太黏",
        inner_motive=inner_motive,
        conversation_purpose="想分享閱讀感受",
        expected_reply="對方回個短句或表情",
        risk="可能被視為刷存在感",
        best_timing="evening",
    )


@pytest.mark.asyncio
async def test_service_records_useful_motive():
    svc, repo = _service()
    stored = await svc.record_if_useful(
        character_id=_CHAR, operator_id=_OP, trigger="tick",
        decision=_decision(), now=_NOW,
    )
    assert stored is not None
    assert repo.snapshot()[0].inner_motive == "想說最近看的書"


@pytest.mark.asyncio
async def test_service_skips_empty_motive():
    svc, repo = _service()
    stored = await svc.record_if_useful(
        character_id=_CHAR, operator_id=_OP, trigger="tick",
        decision=_decision(inner_motive=""), now=_NOW,
    )
    assert stored is None
    assert repo.snapshot() == []


@pytest.mark.asyncio
async def test_service_disabled_short_circuits():
    svc, repo = _service(enabled=False)
    assert svc.enabled is False
    stored = await svc.record_if_useful(
        character_id=_CHAR, operator_id=_OP, trigger="tick",
        decision=_decision(), now=_NOW,
    )
    listed = await svc.list_active(_CHAR, _OP, now=_NOW)
    assert stored is None
    assert listed == []
    assert repo.snapshot() == []


@pytest.mark.asyncio
async def test_service_list_active_runs_gc_before_returning():
    svc, repo = _service()
    stale = DeferredIntent.new(
        character_id=_CHAR, operator_id=_OP, trigger="tick",
        inner_motive="stale", ttl_minutes=1,
        now=_NOW - timedelta(hours=1),
    )
    await repo.add(stale)
    listed = await svc.list_active(_CHAR, _OP, now=_NOW)
    assert listed == []
    # GC sweeps even when the visible list is empty.
    assert repo.snapshot()[0].status == STATUS_EXPIRED


@pytest.mark.asyncio
async def test_service_mark_consumed_many_counts_flips():
    svc, repo = _service()
    a = await repo.add(DeferredIntent.new(
        character_id=_CHAR, operator_id=_OP, trigger="tick",
        inner_motive="A", now=_NOW,
    ))
    b = await repo.add(DeferredIntent.new(
        character_id=_CHAR, operator_id=_OP, trigger="tick",
        inner_motive="B", now=_NOW,
    ))
    flipped = await svc.mark_consumed_many([a.id, b.id, "missing"], now=_NOW)
    assert flipped == 2
    statuses = {row.inner_motive: row.status for row in repo.snapshot()}
    assert statuses == {"A": STATUS_CONSUMED, "B": STATUS_CONSUMED}
