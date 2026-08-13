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
    REVISIT_GRACE_MINUTES,
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


def test_new_carries_no_revisit_alarm_by_default():
    """The ordinary parked motive has no clock attached — every field
    added by T2 must be invisible to a motive that never named a time."""
    intent = DeferredIntent.new(
        character_id=_CHAR, operator_id=_OP, trigger="tick",
        inner_motive="想說話", now=_NOW,
    )
    assert intent.revisit_at is None
    assert intent.is_due_at(_NOW) is False
    assert intent.is_due_at(_NOW + timedelta(hours=5)) is False


def test_is_due_only_after_the_alarm_and_while_still_active():
    alarm = _NOW + timedelta(minutes=8)
    intent = DeferredIntent.new(
        character_id=_CHAR, operator_id=_OP, trigger="tick",
        inner_motive="七點半一起上線核對任務",
        revisit_at=alarm, ttl_minutes=60, now=_NOW,
    )
    assert intent.is_due_at(_NOW) is False
    assert intent.is_due_at(alarm) is True
    assert intent.is_due_at(alarm + timedelta(minutes=1)) is True
    # An alarm floors the row's lifetime at its own ring plus the grace
    # window — a row must never die before the moment it exists to catch
    # (F2-2). Past *that* expiry it is no longer live, alarm or not.
    assert intent.expires_at == alarm + timedelta(minutes=REVISIT_GRACE_MINUTES)
    assert intent.is_due_at(intent.expires_at) is False
    # A consumed row never comes due again either.
    assert intent.marked_consumed(now=_NOW).is_due_at(alarm) is False


def test_new_stretches_the_ttl_so_a_late_appointment_can_still_ring():
    """F2-2 — an appointment beyond the default TTL used to be born dead:
    the row expired hours before its own alarm, and ``list_due_for``
    (which requires the row to still be live) never returned it. The TTL
    now floors at the alarm plus a grace window."""
    alarm = _NOW + timedelta(hours=25)
    intent = DeferredIntent.new(
        character_id=_CHAR, operator_id=_OP, trigger="tick",
        inner_motive="明晚八點再一起看",
        revisit_at=alarm, now=_NOW,
    )
    assert intent.expires_at > alarm
    assert intent.is_active_at(alarm) is True
    assert intent.is_due_at(alarm) is True


def test_new_keeps_the_plain_ttl_for_an_alarm_inside_it():
    """Control — the stretch only ever extends, never shortens, and an
    ordinary same-evening appointment keeps the untouched 24h window."""
    intent = DeferredIntent.new(
        character_id=_CHAR, operator_id=_OP, trigger="tick",
        inner_motive="七點半一起上線",
        revisit_at=_NOW + timedelta(hours=2), now=_NOW,
    )
    assert intent.expires_at == _NOW + timedelta(hours=24)


def test_without_revisit_keeps_the_motive_and_drops_the_alarm():
    intent = DeferredIntent.new(
        character_id=_CHAR, operator_id=_OP, trigger="tick",
        inner_motive="七點半一起上線核對任務",
        revisit_at=_NOW + timedelta(minutes=8), now=_NOW,
    )
    spent = intent.without_revisit()
    assert spent.revisit_at is None
    assert spent.inner_motive == "七點半一起上線核對任務"
    assert spent.status == STATUS_ACTIVE
    # Immutable copy semantics — the original still carries its alarm.
    assert intent.revisit_at is not None


def test_with_revisit_puts_the_alarm_back():
    intent = DeferredIntent.new(
        character_id=_CHAR, operator_id=_OP, trigger="tick",
        inner_motive="七點半一起上線核對任務",
        revisit_at=_NOW + timedelta(minutes=8), now=_NOW,
    )
    alarm = intent.revisit_at
    spent = intent.without_revisit()
    restored = spent.with_revisit(alarm)
    assert restored.revisit_at == alarm
    assert restored.inner_motive == intent.inner_motive
    # Immutable copy semantics — the spent instance stays spent.
    assert spent.revisit_at is None


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


@pytest.mark.asyncio
async def test_repo_list_due_only_returns_rung_alarms():
    repo = InMemoryDeferredIntentRepository()
    await repo.add(DeferredIntent.new(
        character_id=_CHAR, operator_id=_OP, trigger="tick",
        inner_motive="no-alarm", now=_NOW - timedelta(minutes=30),
    ))
    await repo.add(DeferredIntent.new(
        character_id=_CHAR, operator_id=_OP, trigger="tick",
        inner_motive="not-yet", revisit_at=_NOW + timedelta(minutes=5),
        now=_NOW - timedelta(minutes=30),
    ))
    await repo.add(DeferredIntent.new(
        character_id=_CHAR, operator_id=_OP, trigger="tick",
        inner_motive="due", revisit_at=_NOW - timedelta(minutes=1),
        now=_NOW - timedelta(minutes=30),
    ))
    # Same alarm, other character → isolated.
    await repo.add(DeferredIntent.new(
        character_id="char-B", operator_id=_OP, trigger="tick",
        inner_motive="other-char", revisit_at=_NOW - timedelta(minutes=1),
        now=_NOW - timedelta(minutes=30),
    ))

    due = await repo.list_due_for(_CHAR, _OP, now=_NOW)

    assert [i.inner_motive for i in due] == ["due"]


@pytest.mark.asyncio
async def test_repo_clear_revisit_is_single_use():
    repo = InMemoryDeferredIntentRepository()
    intent = await repo.add(DeferredIntent.new(
        character_id=_CHAR, operator_id=_OP, trigger="tick",
        inner_motive="due", revisit_at=_NOW - timedelta(minutes=1),
        now=_NOW - timedelta(minutes=30),
    ))

    assert await repo.clear_revisit(intent.id) is True
    assert await repo.clear_revisit(intent.id) is False
    assert await repo.list_due_for(_CHAR, _OP, now=_NOW) == []
    # The motive survives; only the alarm was spent.
    assert repo.snapshot()[0].inner_motive == "due"
    assert repo.snapshot()[0].status == STATUS_ACTIVE


@pytest.mark.asyncio
async def test_repo_restore_revisit_re_arms_only_a_spent_live_row():
    """F2-3 — a tick that never produced a real judgement must be able to
    give the alarm back. Only a still-active row whose alarm is currently
    absent qualifies: a concurrent re-arm or consume wins."""
    repo = InMemoryDeferredIntentRepository()
    alarm = _NOW - timedelta(minutes=1)
    intent = await repo.add(DeferredIntent.new(
        character_id=_CHAR, operator_id=_OP, trigger="tick",
        inner_motive="due", revisit_at=alarm,
        now=_NOW - timedelta(minutes=30),
    ))

    # Still armed → nothing to restore.
    assert await repo.restore_revisit(intent.id, revisit_at=alarm) is False

    await repo.clear_revisit(intent.id)
    assert await repo.restore_revisit(intent.id, revisit_at=alarm) is True
    assert [i.inner_motive for i in
            await repo.list_due_for(_CHAR, _OP, now=_NOW)] == ["due"]

    # A consumed row is done, alarm or not.
    await repo.clear_revisit(intent.id)
    await repo.mark_consumed(intent.id, now=_NOW)
    assert await repo.restore_revisit(intent.id, revisit_at=alarm) is False
    assert await repo.restore_revisit("missing", revisit_at=alarm) is False


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
async def test_service_records_the_parsed_revisit_alarm():
    svc, repo = _service()
    alarm = _NOW + timedelta(minutes=8)
    await svc.record_if_useful(
        character_id=_CHAR, operator_id=_OP, trigger="tick",
        decision=_decision(), revisit_at=alarm, now=_NOW,
    )
    assert repo.snapshot()[0].revisit_at == alarm


@pytest.mark.asyncio
async def test_service_list_due_respects_feature_flag():
    svc, repo = _service(enabled=False)
    await repo.add(DeferredIntent.new(
        character_id=_CHAR, operator_id=_OP, trigger="tick",
        inner_motive="due", revisit_at=_NOW - timedelta(minutes=1),
        now=_NOW - timedelta(minutes=30),
    ))
    assert await svc.list_due(_CHAR, _OP, now=_NOW) == []


@pytest.mark.asyncio
async def test_service_clear_revisit_many_counts_cleared():
    svc, repo = _service()
    a = await repo.add(DeferredIntent.new(
        character_id=_CHAR, operator_id=_OP, trigger="tick",
        inner_motive="A", revisit_at=_NOW - timedelta(minutes=1), now=_NOW,
    ))
    b = await repo.add(DeferredIntent.new(
        character_id=_CHAR, operator_id=_OP, trigger="tick",
        inner_motive="B", now=_NOW,
    ))
    cleared = await svc.clear_revisit_many([a.id, b.id, "missing"])
    # Only A carried an alarm to spend.
    assert cleared == 1
    assert await svc.list_due(_CHAR, _OP, now=_NOW) == []


@pytest.mark.asyncio
async def test_service_restore_revisit_many_re_arms_the_snapshots():
    """The dispatcher hands back the *pre-clear* snapshots, so the service
    only needs to re-apply each row's own alarm. Rows that never carried
    one are skipped rather than invented."""
    svc, repo = _service()
    alarm = _NOW - timedelta(minutes=1)
    armed = await repo.add(DeferredIntent.new(
        character_id=_CHAR, operator_id=_OP, trigger="tick",
        inner_motive="A", revisit_at=alarm, now=_NOW - timedelta(minutes=30),
    ))
    plain = await repo.add(DeferredIntent.new(
        character_id=_CHAR, operator_id=_OP, trigger="tick",
        inner_motive="B", now=_NOW,
    ))
    await svc.clear_revisit_many([armed.id, plain.id])
    assert await svc.list_due(_CHAR, _OP, now=_NOW) == []

    restored = await svc.restore_revisit_many([armed, plain])

    assert restored == 1
    assert [i.inner_motive for i in
            await svc.list_due(_CHAR, _OP, now=_NOW)] == ["A"]


@pytest.mark.asyncio
async def test_service_records_an_appointment_beyond_the_default_ttl():
    """F2-2 end-to-end through the record path: 「明晚八點再一起」 is 25h
    out, past the default TTL — it must still be due when it rings."""
    svc, _ = _service()
    alarm = _NOW + timedelta(hours=25)
    await svc.record_if_useful(
        character_id=_CHAR, operator_id=_OP, trigger="tick",
        decision=_decision(), revisit_at=alarm, now=_NOW,
    )
    due = await svc.list_due(_CHAR, _OP, now=alarm)
    assert [i.inner_motive for i in due] == ["想說最近看的書"]


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
