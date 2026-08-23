"""The two memos in front of the account runtime-profile resolver (NF4 cost).

The TTL memo bounds the steady-state read rate; the pass scope bounds one
reconcile pass. Both memoise the answer, neither may change it.
"""

from __future__ import annotations

import pytest

from kokoro_link.application.services.account_runtime_profile_cache import (
    CachedAccountRuntimeProfileResolver,
    ProfileResolutionScope,
)
from kokoro_link.domain.value_objects.account_runtime_profile import (
    DEFAULT_ACCOUNT_RUNTIME_PROFILE,
    AccountRuntimeProfile,
)

pytestmark = pytest.mark.asyncio

_FREE = AccountRuntimeProfile(name="free", background_dormancy_days=7)
_PAID = AccountRuntimeProfile(name="paid")


class _CountingResolver:
    def __init__(self, profile: AccountRuntimeProfile = _FREE) -> None:
        self.profile = profile
        self.calls: list[str] = []

    async def resolve_for_operator(self, operator_id: str) -> AccountRuntimeProfile:
        self.calls.append(operator_id)
        return self.profile


class _BrokenResolver:
    def __init__(self) -> None:
        self.calls = 0

    async def resolve_for_operator(self, operator_id: str) -> AccountRuntimeProfile:
        self.calls += 1
        raise RuntimeError("control plane down")


class _Ticker:
    """A monotonic clock the test drives by hand."""

    def __init__(self) -> None:
        self.value = 1_000.0

    def __call__(self) -> float:
        return self.value


# -- TTL memo --------------------------------------------------------------- #


async def test_repeat_resolutions_inside_the_ttl_cost_one_read() -> None:
    inner = _CountingResolver()
    clock = _Ticker()
    cached = CachedAccountRuntimeProfileResolver(
        inner, ttl_seconds=60.0, time_source=clock,
    )
    for _ in range(50):
        assert await cached.resolve_for_operator("op1") is _FREE
    assert inner.calls == ["op1"]


async def test_the_answer_is_refreshed_after_the_ttl() -> None:
    inner = _CountingResolver()
    clock = _Ticker()
    cached = CachedAccountRuntimeProfileResolver(
        inner, ttl_seconds=60.0, time_source=clock,
    )
    await cached.resolve_for_operator("op1")
    clock.value += 61.0
    inner.profile = _PAID  # the control plane moved this operator's tier
    assert await cached.resolve_for_operator("op1") is _PAID
    assert inner.calls == ["op1", "op1"]


async def test_operators_do_not_share_an_entry() -> None:
    inner = _CountingResolver()
    cached = CachedAccountRuntimeProfileResolver(inner, time_source=_Ticker())
    await cached.resolve_for_operator("op1")
    await cached.resolve_for_operator("op2")
    await cached.resolve_for_operator("op1")
    assert inner.calls == ["op1", "op2"]


async def test_failures_are_not_cached() -> None:
    """A blip must not be pinned for a whole TTL: the next call retries and the
    caller's own fail-open handling stays in charge of the answer."""
    inner = _BrokenResolver()
    cached = CachedAccountRuntimeProfileResolver(inner, time_source=_Ticker())
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await cached.resolve_for_operator("op1")
    assert inner.calls == 3


async def test_zero_ttl_disables_the_memo() -> None:
    inner = _CountingResolver()
    cached = CachedAccountRuntimeProfileResolver(inner, ttl_seconds=0.0)
    await cached.resolve_for_operator("op1")
    await cached.resolve_for_operator("op1")
    assert inner.calls == ["op1", "op1"]


async def test_entries_are_bounded() -> None:
    inner = _CountingResolver()
    cached = CachedAccountRuntimeProfileResolver(
        inner, max_entries=2, time_source=_Ticker(),
    )
    for operator in ("op1", "op2", "op3"):
        await cached.resolve_for_operator(operator)
    # op1 was evicted as least-recently-resolved and costs a second read.
    await cached.resolve_for_operator("op1")
    assert inner.calls == ["op1", "op2", "op3", "op1"]


async def test_invalidate_drops_one_operator() -> None:
    inner = _CountingResolver()
    cached = CachedAccountRuntimeProfileResolver(inner, time_source=_Ticker())
    await cached.resolve_for_operator("op1")
    cached.invalidate("op1")
    await cached.resolve_for_operator("op1")
    assert inner.calls == ["op1", "op1"]


# -- pass scope ------------------------------------------------------------- #


async def test_scope_resolves_each_operator_once() -> None:
    inner = _CountingResolver()
    scope = ProfileResolutionScope(inner)
    for _ in range(20):
        assert await scope.resolve("op1") is _FREE
        assert await scope.resolve("op2") is _FREE
    assert inner.calls == ["op1", "op2"]


async def test_scope_memoises_failure_as_no_policy_known() -> None:
    """Fail-open, once. A control plane that is down answers ``None`` for the
    rest of the pass instead of being re-hammered once per character."""
    inner = _BrokenResolver()
    scope = ProfileResolutionScope(inner)
    for _ in range(10):
        assert await scope.resolve("op1") is None
    assert inner.calls == 1


async def test_a_fresh_scope_starts_empty() -> None:
    """Staleness bound: one pass. The next pass re-reads."""
    inner = _CountingResolver(DEFAULT_ACCOUNT_RUNTIME_PROFILE)
    await ProfileResolutionScope(inner).resolve("op1")
    await ProfileResolutionScope(inner).resolve("op1")
    assert inner.calls == ["op1", "op1"]
