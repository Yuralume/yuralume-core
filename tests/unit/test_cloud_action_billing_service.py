"""AP2 — action-level charging policy.

The service is the single place that decides *whether* a player action is
billed, so these tests are the guard rails around the three ways it must stay
invisible (token-billed tier, no hosted tenant, background work) and the two
ways it must fail (soft on an outage, loud on out-of-credits).
"""

from __future__ import annotations

import httpx
import pytest

from kokoro_link.application.services.cloud_action_billing_service import (
    CloudActionBillingService,
    NullActionBillingService,
)
from kokoro_link.contracts.cloud_action_billing import (
    ACTION_CHAT,
    ACTION_FEED_POST_OVERAGE,
    ACTION_KIND_QUOTA_OVERAGE,
    ActionCharge,
    ActionChargeUnavailable,
    ActionPriceChanged,
    client_quoted_price,
    client_quoted_price_scope,
    prepaid_action,
    prepaid_action_scope,
)
from kokoro_link.contracts.generation_trigger import (
    GenerationTrigger,
    generation_trigger_scope,
)
from kokoro_link.contracts.interaction_context import (
    current_interaction,
    interaction_headers,
    interaction_scope,
    mark_interaction_call_served,
)
from kokoro_link.domain.value_objects.account_runtime_profile import (
    BILLING_SHAPE_ACTION_FIXED,
    DEFAULT_ACCOUNT_RUNTIME_PROFILE,
    AccountRuntimeProfile,
)
from kokoro_link.infrastructure.llm.cloud_refusal import (
    INSUFFICIENT_CREDITS_CODE,
    ExpectedCloudRefusal,
)

_ACTION_TIER = AccountRuntimeProfile(
    name="standard", billing_shape=BILLING_SHAPE_ACTION_FIXED,
)

_OVERAGE_TIER = AccountRuntimeProfile(
    name="standard",
    billing_shape=BILLING_SHAPE_ACTION_FIXED,
    overage_enabled=True,
)


class _StubProfiles:
    def __init__(self, profile: AccountRuntimeProfile) -> None:
        self._profile = profile

    async def resolve_for_operator(self, operator_id: str):
        return self._profile


class _StubOperator:
    def __init__(self, tenant_id: str | None) -> None:
        self.cloud_tenant_id = tenant_id


class _StubOperatorRepository:
    def __init__(self, tenant_id: str | None) -> None:
        self._tenant_id = tenant_id

    async def get(self, operator_id: str):
        return _StubOperator(self._tenant_id)


class _RecordingClient:
    def __init__(
        self,
        *,
        charge: ActionCharge | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._charge = charge or ActionCharge(charge_id="chg-1", price_cr=3.0)
        self._error = error
        self.charges: list[dict] = []
        self.settled: list[str] = []
        self.released: list[str] = []
        self.probed_releases: list[bool] = []
        """``settle_if_probed`` as sent per release — the flag that tells
        the ledger "Core cannot know what was served, you decide"."""

    async def charge(self, **kwargs) -> ActionCharge:
        self.charges.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._charge

    async def settle(self, charge_id: str) -> None:
        self.settled.append(charge_id)

    async def release(
        self, charge_id: str, *, settle_if_probed: bool = False,
    ) -> None:
        self.released.append(charge_id)
        self.probed_releases.append(settle_if_probed)


class _StubQuotes:
    """The price list Core showed the player, as the charge path sees it."""

    def __init__(
        self,
        prices: dict[tuple[str, str], float] | None = None,
        *,
        warm_prices: dict[tuple[str, str], float] | None = None,
        warm_error: Exception | None = None,
    ) -> None:
        self._prices = prices or {}
        self._warm_prices = warm_prices
        self._warm_error = warm_error
        self.invalidated = 0
        self.warmed = 0

    def quoted_price_cr(self, *, tier_name: str, action_key: str) -> float | None:
        return self._prices.get((tier_name, action_key))

    def invalidate(self) -> None:
        self.invalidated += 1
        self._prices = {}

    async def warm(self) -> None:
        self.warmed += 1
        if self._warm_error is not None:
            raise self._warm_error
        if self._warm_prices is not None:
            self._prices = dict(self._warm_prices)


async def _no_sleep(_delay: float) -> None:
    """Retries are about ordering, not wall-clock time."""
    return None


def _service(
    client: _RecordingClient,
    *,
    profile: AccountRuntimeProfile = _ACTION_TIER,
    tenant_id: str | None = "tenant-1",
    pricing: _StubQuotes | None = None,
    close_retry_delays: tuple[float, ...] = (0.0, 0.0, 0.0),
) -> CloudActionBillingService:
    return CloudActionBillingService(
        client=client,
        profile_resolver=_StubProfiles(profile),
        operator_profiles=_StubOperatorRepository(tenant_id),
        pricing=pricing,
        close_retry_delays=close_retry_delays,
        sleep=_no_sleep,
    )


def _served(handle):
    """Simulate the Gateway waiving (and delivering) one call under ``handle``.

    Every foreground action does this for real before it settles; a settle
    without it is now a refund, so tests about the *transport* of a settle have
    to say so explicitly.
    """
    assert handle is not None
    with interaction_scope(handle.context):
        mark_interaction_call_served()
    return handle


def _refusal() -> ExpectedCloudRefusal:
    request = httpx.Request("POST", "https://user.example/charge")
    return ExpectedCloudRefusal(
        "402",
        request=request,
        response=httpx.Response(402, request=request),
        code=INSUFFICIENT_CREDITS_CODE,
        reason="螢火不足",
    )


@pytest.mark.asyncio
async def test_action_charges_scopes_and_settles() -> None:
    client = _RecordingClient()
    service = _service(client)

    async with service.action(ACTION_CHAT, operator_id="op-1") as handle:
        assert handle is not None
        assert current_interaction() is not None
        assert interaction_headers()  # the Gateway sees the covering charge
        mark_interaction_call_served()  # ...and waives that call

    assert client.charges[0]["tenant_id"] == "tenant-1"
    assert client.charges[0]["action_key"] == ACTION_CHAT
    assert client.charges[0]["interaction_id"] == handle.interaction_id
    assert client.settled == ["chg-1"]
    assert client.released == []
    assert current_interaction() is None
    assert service.counters.charged == 1


@pytest.mark.asyncio
async def test_action_uses_server_owned_interaction_id_when_provided() -> None:
    """A durable external turn must keep one billing correlation on retry."""
    client = _RecordingClient()
    service = _service(client)

    async with service.action(
        ACTION_CHAT,
        operator_id="op-1",
        interaction_id="external-turn-stable-1",
    ) as handle:
        assert handle is not None
        assert handle.interaction_id == "external-turn-stable-1"
        mark_interaction_call_served()

    assert client.charges[0]["interaction_id"] == "external-turn-stable-1"


@pytest.mark.asyncio
async def test_a_failed_action_releases_the_whole_charge() -> None:
    client = _RecordingClient()
    service = _service(client)

    with pytest.raises(RuntimeError):
        async with service.action(ACTION_CHAT, operator_id="op-1"):
            raise RuntimeError("generation blew up")

    assert client.released == ["chg-1"]
    assert client.settled == []


@pytest.mark.asyncio
async def test_token_billed_tier_is_never_charged_or_scoped() -> None:
    client = _RecordingClient()
    service = _service(client, profile=DEFAULT_ACCOUNT_RUNTIME_PROFILE)

    async with service.action(ACTION_CHAT, operator_id="op-1") as handle:
        assert handle is None
        assert interaction_headers() == {}

    assert client.charges == []


@pytest.mark.asyncio
async def test_operator_without_a_hosted_tenant_is_not_charged() -> None:
    client = _RecordingClient()
    service = _service(client, tenant_id=None)

    async with service.action(ACTION_CHAT, operator_id="op-1") as handle:
        assert handle is None
    assert client.charges == []


@pytest.mark.asyncio
async def test_background_work_is_never_charged() -> None:
    """R1–R4: the player pays for what they asked for, nothing else."""
    client = _RecordingClient()
    service = _service(client)

    with generation_trigger_scope(GenerationTrigger.BACKGROUND):
        async with service.action(ACTION_CHAT, operator_id="op-1") as handle:
            assert handle is None
    assert client.charges == []


@pytest.mark.asyncio
async def test_quota_overage_is_charged_even_from_background_work() -> None:
    """AP4: the player authorised this one in advance, at the switch.

    ``feed_post_overage`` funds an autonomous post, so the background gate
    above must not apply — otherwise the feature could never fire at all.
    """
    client = _RecordingClient()
    service = _service(client, profile=_OVERAGE_TIER)

    with generation_trigger_scope(GenerationTrigger.BACKGROUND):
        handle = await service.begin(
            ACTION_FEED_POST_OVERAGE,
            operator_id="op-1",
            action_kind=ACTION_KIND_QUOTA_OVERAGE,
        )

    assert handle is not None
    assert client.charges[0]["action_kind"] == ACTION_KIND_QUOTA_OVERAGE


@pytest.mark.asyncio
async def test_quota_overage_is_refused_by_a_tier_that_never_opened_it() -> None:
    client = _RecordingClient()
    service = _service(client, profile=_ACTION_TIER)  # overage_enabled=False

    handle = await service.begin(
        ACTION_FEED_POST_OVERAGE,
        operator_id="op-1",
        action_kind=ACTION_KIND_QUOTA_OVERAGE,
    )

    assert handle is None
    assert client.charges == []


@pytest.mark.asyncio
async def test_quota_overage_still_requires_action_fixed_billing() -> None:
    """A token-billed tier cannot sell an overage, however its switch is set.

    Its extra generation is already metered per Gateway call, so an overage
    price on top would bill the same work twice — and the User service refuses
    the charge anyway, which would leave the player facing a switch that can
    only ever answer "try again later".
    """
    client = _RecordingClient()
    service = _service(
        client,
        profile=AccountRuntimeProfile(name="standard", overage_enabled=True),
    )

    handle = await service.begin(
        ACTION_FEED_POST_OVERAGE,
        operator_id="op-1",
        action_kind=ACTION_KIND_QUOTA_OVERAGE,
    )

    assert handle is None
    assert client.charges == []


@pytest.mark.asyncio
async def test_wallet_outage_runs_the_action_uncharged() -> None:
    client = _RecordingClient(error=ActionChargeUnavailable("down"))
    service = _service(client)

    async with service.action(ACTION_CHAT, operator_id="op-1") as handle:
        assert handle is None
        assert interaction_headers() == {}

    assert service.counters.charge_failed == 1


@pytest.mark.asyncio
async def test_missing_back_office_price_runs_uncharged() -> None:
    client = _RecordingClient(charge=ActionCharge(charge_id="", no_charge=True))
    service = _service(client)

    async with service.action(ACTION_CHAT, operator_id="op-1") as handle:
        assert handle is None
    assert client.settled == []
    assert service.counters.no_price == 1


@pytest.mark.asyncio
async def test_insufficient_credits_propagates_before_any_work() -> None:
    client = _RecordingClient(error=_refusal())
    service = _service(client)

    ran = False
    with pytest.raises(ExpectedCloudRefusal) as excinfo:
        async with service.action(ACTION_CHAT, operator_id="op-1"):
            ran = True

    assert ran is False
    assert excinfo.value.code == INSUFFICIENT_CREDITS_CODE
    assert service.counters.refused == 1


@pytest.mark.asyncio
async def test_a_charge_is_closed_exactly_once() -> None:
    client = _RecordingClient()
    service = _service(client)

    handle = _served(await service.begin(ACTION_CHAT, operator_id="op-1"))
    await service.settle(handle)
    await service.release(handle)
    await service.settle(handle)

    assert client.settled == ["chg-1"]
    assert client.released == []


@pytest.mark.asyncio
async def test_settle_failure_is_swallowed_and_counted() -> None:
    class _FailingSettle(_RecordingClient):
        async def settle(self, charge_id: str) -> None:
            raise ActionChargeUnavailable("down")

    client = _FailingSettle()
    service = _service(client)
    handle = _served(await service.begin(ACTION_CHAT, operator_id="op-1"))
    await service.settle(handle)
    await service.wait_for_pending_closes()
    assert service.counters.settle_failed == 1


# -- closing a charge must never be *claimed* without landing (C3) -----


@pytest.mark.asyncio
async def test_a_settle_that_fails_leaves_the_handle_open_and_retries() -> None:
    """A reservation Core failed to close is still open on the User side.

    Marking the handle closed would strand the player's money with nothing left
    to retry, which is why the failure path keeps it open and retries off the
    request path instead.
    """

    class _FlakySettle(_RecordingClient):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        async def settle(self, charge_id: str) -> None:
            self.attempts += 1
            if self.attempts < 3:
                raise ActionChargeUnavailable("down")
            await super().settle(charge_id)

    client = _FlakySettle()
    service = _service(client)
    handle = _served(await service.begin(ACTION_CHAT, operator_id="op-1"))

    await service.settle(handle)
    assert handle.closed is False  # not closed — the attempt failed
    assert handle.close_pending is True

    await service.wait_for_pending_closes()

    assert client.settled == ["chg-1"]
    assert handle.closed is True
    assert handle.close_pending is False
    assert service.counters.close_retried == 1
    assert service.counters.close_recovered == 1
    assert service.counters.close_abandoned == 0


@pytest.mark.asyncio
async def test_a_settle_that_never_lands_stays_open_for_the_sweeper() -> None:
    """Bounded retries, then hand over — Core does not own reconciliation."""

    class _DeadSettle(_RecordingClient):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        async def settle(self, charge_id: str) -> None:
            self.attempts += 1
            raise ActionChargeUnavailable("down")

    client = _DeadSettle()
    service = _service(client, close_retry_delays=(0.0, 0.0, 0.0))
    handle = _served(await service.begin(ACTION_CHAT, operator_id="op-1"))

    await service.settle(handle)
    await service.wait_for_pending_closes()

    assert handle is not None
    assert client.attempts == 4  # one inline, three bounded retries
    assert handle.closed is False
    assert handle.close_pending is False
    assert service.counters.close_abandoned == 1


@pytest.mark.asyncio
async def test_a_failed_release_retries_the_refund_too() -> None:
    class _FlakyRelease(_RecordingClient):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        async def release(
            self, charge_id: str, *, settle_if_probed: bool = False,
        ) -> None:
            self.attempts += 1
            if self.attempts < 2:
                raise ActionChargeUnavailable("down")
            await super().release(
                charge_id, settle_if_probed=settle_if_probed,
            )

    client = _FlakyRelease()
    service = _service(client)
    handle = await service.begin(ACTION_CHAT, operator_id="op-1")

    await service.release(handle)
    await service.wait_for_pending_closes()

    assert client.released == ["chg-1"]
    assert service.counters.release_failed == 1
    assert service.counters.close_recovered == 1


@pytest.mark.asyncio
async def test_a_close_being_retried_is_not_started_a_second_time() -> None:
    """Open is not an invitation to double-close while a retry is in flight."""

    class _SlowSettle(_RecordingClient):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        async def settle(self, charge_id: str) -> None:
            self.attempts += 1
            raise ActionChargeUnavailable("down")

    client = _SlowSettle()
    service = _service(client, close_retry_delays=(0.0,))
    handle = _served(await service.begin(ACTION_CHAT, operator_id="op-1"))

    await service.settle(handle)
    await service.settle(handle)  # ignored: a close is already pending
    await service.release(handle)  # likewise

    await service.wait_for_pending_closes()
    assert client.attempts == 2  # inline + one retry, nothing re-entered
    assert client.released == []


@pytest.mark.asyncio
async def test_null_service_is_inert() -> None:
    service = NullActionBillingService()
    async with service.action(ACTION_CHAT, operator_id="op-1") as handle:
        assert handle is None
    await service.settle(None)
    await service.release(None)


# -- refund vs settle (the abandoned-stream hole) ----------------------


@pytest.mark.asyncio
async def test_release_before_any_covered_call_refunds_in_full() -> None:
    """Nothing was served upstream, so the player gets everything back."""
    client = _RecordingClient()
    service = _service(client)

    handle = await service.begin(ACTION_CHAT, operator_id="op-1")
    await service.release(handle)

    assert client.released == ["chg-1"]
    assert client.settled == []


@pytest.mark.asyncio
async def test_release_after_a_covered_call_settles_instead() -> None:
    """A turn abandoned mid-generation must not become a free generation.

    Under ``action_fixed`` the Gateway waives per-call billing for everything
    inside the action, so once a call has been served nobody else will ever
    charge for those tokens. Refunding here would make "send, then hit stop" an
    unlimited free-chat loop.
    """
    client = _RecordingClient()
    service = _service(client)

    handle = await service.begin(ACTION_CHAT, operator_id="op-1")
    assert handle is not None
    with interaction_scope(handle.context):
        mark_interaction_call_served()
    await service.release(handle)

    assert client.settled == ["chg-1"]
    assert client.released == []
    assert service.counters.settled_after_use == 1


@pytest.mark.asyncio
async def test_a_body_that_raises_after_a_covered_call_still_settles() -> None:
    client = _RecordingClient()
    service = _service(client)

    with pytest.raises(RuntimeError):
        async with service.action(ACTION_CHAT, operator_id="op-1"):
            mark_interaction_call_served()
            raise RuntimeError("finish failed after the model answered")

    assert client.settled == ["chg-1"]
    assert client.released == []


@pytest.mark.asyncio
async def test_settle_after_release_is_still_single_shot() -> None:
    client = _RecordingClient()
    service = _service(client)

    handle = await service.begin(ACTION_CHAT, operator_id="op-1")
    await service.release(handle)
    await service.settle(handle)

    assert client.released == ["chg-1"]
    assert client.settled == []


# -- a success the Gateway never covered (C2') -------------------------


@pytest.mark.asyncio
async def test_a_success_without_a_covered_call_is_refunded_not_settled() -> None:
    """The Gateway billed those calls itself, so the fixed charge is a second
    bill for the same work — the player must not pay both."""
    client = _RecordingClient()
    service = _service(client)

    async with service.action(ACTION_CHAT, operator_id="op-1") as handle:
        assert handle is not None  # ran, produced a reply, no covered header

    assert client.released == ["chg-1"]
    assert client.settled == []
    assert service.counters.released_uncovered == 1


@pytest.mark.asyncio
async def test_a_partially_covered_action_still_settles_once() -> None:
    """One covered call is enough: that work is only ever paid for here."""
    client = _RecordingClient()
    service = _service(client)

    handle = _served(await service.begin(ACTION_CHAT, operator_id="op-1"))
    await service.settle(handle)

    assert client.settled == ["chg-1"]
    assert service.counters.released_uncovered == 0


@pytest.mark.asyncio
async def test_an_action_that_does_not_deliver_through_the_gateway_settles() -> None:
    """``feed_post_overage``'s post is composed by background work, which the
    Gateway never waives — yet the post *is* published, so it is owed."""
    client = _RecordingClient()
    service = _service(client, profile=_OVERAGE_TIER)

    handle = await service.begin(
        ACTION_FEED_POST_OVERAGE,
        operator_id="op-1",
        action_kind=ACTION_KIND_QUOTA_OVERAGE,
    )
    await service.settle(handle, gateway_delivered=False)

    assert client.settled == ["chg-1"]
    assert client.released == []
    assert service.counters.released_uncovered == 0


@pytest.mark.asyncio
async def test_an_uncovered_zero_charge_closes_without_billing_anyone() -> None:
    """A ``no_charge`` handle carries no money either way, so the refund path
    is a formality — but it must still close the reservation exactly once."""
    client = _RecordingClient(
        charge=ActionCharge(charge_id="chg-free", price_cr=0.0, no_charge=True),
    )
    service = _service(client)

    async with service.action(ACTION_CHAT, operator_id="op-1") as handle:
        assert handle is not None

    assert client.released == ["chg-free"]
    assert client.settled == []


# -- quote binding (C1) ------------------------------------------------


@pytest.mark.asyncio
async def test_the_charge_carries_the_price_core_quoted() -> None:
    client = _RecordingClient()
    quotes = _StubQuotes({("standard", ACTION_CHAT): 3.0})
    service = _service(client, pricing=quotes)

    await service.begin(ACTION_CHAT, operator_id="op-1")

    assert client.charges[0]["expected_price_cr"] == 3.0


@pytest.mark.asyncio
async def test_a_cold_cache_warms_once_and_binds_the_fetched_price() -> None:
    """A fresh process must not demote priced actions to unbound charges.

    The User service refuses an unquoted charge for a priced action, so an
    unbound cold-start charge would fail closed into a free run. Warming once
    gives the charge the same published number the SPA would be served.
    """
    client = _RecordingClient()
    quotes = _StubQuotes(warm_prices={("standard", ACTION_CHAT): 3.0})
    service = _service(client, pricing=quotes)

    await service.begin(ACTION_CHAT, operator_id="op-1")

    assert quotes.warmed == 1
    assert client.charges[0]["expected_price_cr"] == 3.0


@pytest.mark.asyncio
async def test_a_warm_quote_does_not_refetch_the_price_list() -> None:
    client = _RecordingClient()
    quotes = _StubQuotes({("standard", ACTION_CHAT): 3.0})
    service = _service(client, pricing=quotes)

    await service.begin(ACTION_CHAT, operator_id="op-1")

    assert quotes.warmed == 0


@pytest.mark.asyncio
async def test_an_unquoted_action_charges_unbound_rather_than_blocking() -> None:
    """A pricing outage is not a reason to refuse the player's turn."""
    client = _RecordingClient()
    quotes = _StubQuotes(warm_error=RuntimeError("pricing down"))
    service = _service(client, pricing=quotes)

    handle = await service.begin(ACTION_CHAT, operator_id="op-1")

    assert handle is not None
    assert quotes.warmed == 1
    assert client.charges[0]["expected_price_cr"] is None


@pytest.mark.asyncio
async def test_a_price_change_refuses_the_action_and_drops_the_stale_quote() -> None:
    """C1: nothing is reserved, and nothing may re-quote the old number."""
    quotes = _StubQuotes({("standard", ACTION_CHAT): 3.0})
    client = _RecordingClient(
        error=ActionPriceChanged(
            action_key=ACTION_CHAT, expected_price_cr=3.0, current_price_cr=4.0,
        ),
    )
    service = _service(client, pricing=quotes)

    ran = False
    with pytest.raises(ActionPriceChanged) as excinfo:
        async with service.action(ACTION_CHAT, operator_id="op-1"):
            ran = True

    assert ran is False
    assert excinfo.value.current_price_cr == 4.0
    assert quotes.invalidated == 1
    assert service.counters.price_changed == 1
    assert client.settled == []
    assert client.released == []


@pytest.mark.asyncio
async def test_a_background_overage_price_change_denies_quietly() -> None:
    """The background caller sees a refusal it can swallow, not a free run.

    ``QuotaOverageService`` maps any charge failure to a denial, so the post
    simply does not happen — which is the only safe answer for a price the
    player was never shown and cannot re-confirm.
    """
    quotes = _StubQuotes()
    client = _RecordingClient(
        error=ActionPriceChanged(action_key=ACTION_FEED_POST_OVERAGE),
    )
    service = _service(client, profile=_OVERAGE_TIER, pricing=quotes)

    with generation_trigger_scope(GenerationTrigger.BACKGROUND):
        with pytest.raises(ActionPriceChanged):
            await service.begin(
                ACTION_FEED_POST_OVERAGE,
                operator_id="op-1",
                action_kind=ACTION_KIND_QUOTA_OVERAGE,
            )

    assert quotes.invalidated == 1


@pytest.mark.asyncio
async def test_an_unpriced_action_runs_covered_under_its_zero_charge() -> None:
    """A ``no_charge`` answer now names a real zero-amount charge.

    Keeping the handle keeps the interaction header on every call the action
    makes, so the whole action is *covered* (free) instead of falling back to
    per-call billing — which would charge the player for an action the back
    office never priced.
    """
    client = _RecordingClient(
        charge=ActionCharge(charge_id="chg-free", price_cr=0.0, no_charge=True),
    )
    service = _service(client)

    async with service.action(ACTION_CHAT, operator_id="op-1") as handle:
        assert handle is not None
        assert handle.price_cr == 0.0
        assert interaction_headers()
        mark_interaction_call_served()

    assert client.settled == ["chg-free"]
    assert service.counters.no_price == 1
    assert service.counters.charged == 0


# -- the price the player actually saw (R9) ----------------------------


@pytest.mark.asyncio
async def test_the_client_quote_wins_over_this_replica_s_cache() -> None:
    """The binding number has to be the one on the *player's* screen.

    Core's cache is only a proxy for that, and under several replicas (or right
    after a refresh) it can hold a price this player was never shown.
    """
    client = _RecordingClient()
    quotes = _StubQuotes({("standard", ACTION_CHAT): 3.0})
    service = _service(client, pricing=quotes)

    await service.begin(
        ACTION_CHAT, operator_id="op-1", quoted_price_cr=2.5,
    )

    assert client.charges[0]["expected_price_cr"] == 2.5


@pytest.mark.asyncio
async def test_a_client_with_no_quote_falls_back_to_the_cache() -> None:
    """Older clients, and screens with nothing quotable, must not regress."""
    client = _RecordingClient()
    quotes = _StubQuotes({("standard", ACTION_CHAT): 3.0})
    service = _service(client, pricing=quotes)

    await service.begin(ACTION_CHAT, operator_id="op-1", quoted_price_cr=None)

    assert client.charges[0]["expected_price_cr"] == 3.0


@pytest.mark.asyncio
async def test_a_client_quote_never_reaches_the_price_list() -> None:
    """A quoted number is a claim about what was displayed, not a price
    source: it must not warm, refresh or seed the cache."""
    client = _RecordingClient()
    quotes = _StubQuotes()
    service = _service(client, pricing=quotes)

    await service.begin(ACTION_CHAT, operator_id="op-1", quoted_price_cr=2.5)

    assert quotes.warmed == 0
    assert client.charges[0]["expected_price_cr"] == 2.5


@pytest.mark.asyncio
async def test_a_nonsense_client_quote_is_ignored() -> None:
    client = _RecordingClient()
    quotes = _StubQuotes({("standard", ACTION_CHAT): 3.0})
    service = _service(client, pricing=quotes)

    await service.begin(
        ACTION_CHAT, operator_id="op-1", quoted_price_cr=float("nan"),
    )

    assert client.charges[0]["expected_price_cr"] == 3.0


def test_a_client_quote_scope_carries_only_real_numbers() -> None:
    with client_quoted_price_scope(
        {ACTION_CHAT: 2.5, ACTION_FEED_POST_OVERAGE: None},
    ):
        assert client_quoted_price(ACTION_CHAT) == 2.5
        assert client_quoted_price(ACTION_FEED_POST_OVERAGE) is None
    assert client_quoted_price(ACTION_CHAT) is None


# -- prepaid actions (AP4 overage replaces the base price) -------------


@pytest.mark.asyncio
async def test_a_prepaid_action_is_declared_and_withdrawn_by_scope() -> None:
    client = _RecordingClient()
    service = _service(client)
    handle = await service.begin(ACTION_CHAT, operator_id="op-1")
    assert handle is not None

    with prepaid_action_scope(ACTION_CHAT, handle.context):
        assert prepaid_action(ACTION_CHAT) is handle.context
        with prepaid_action_scope(ACTION_CHAT, None):
            assert prepaid_action(ACTION_CHAT) is None
    assert prepaid_action(ACTION_CHAT) is None


# -- releases Core is not entitled to decide (settle_if_probed) --------


@pytest.mark.asyncio
async def test_a_probed_release_carries_the_flag_to_the_ledger() -> None:
    """A handle Core rebuilt cannot answer "was anything served?".

    Its usage record is empty because this process did not make the calls, not
    because none were made. The flag says exactly that, and the User service
    resolves it from the covered-call probe it still holds.
    """
    client = _RecordingClient()
    service = _service(client)
    handle = await service.begin(ACTION_CHAT, operator_id="op-1")

    await service.release(handle, settle_if_probed=True)

    assert client.released == ["chg-1"]
    assert client.probed_releases == [True]
    assert client.settled == []


@pytest.mark.asyncio
async def test_a_probed_release_does_not_second_guess_the_ledger() -> None:
    """Even a *locally* consumed handle defers when the flag is asked for.

    The local "consumed ⇒ settle" shortcut exists because a live handle's usage
    record is authoritative. Once the caller has declared it is not, running the
    shortcut anyway would settle on the very evidence that was disclaimed.
    """
    client = _RecordingClient()
    service = _service(client)
    handle = _served(await service.begin(ACTION_CHAT, operator_id="op-1"))

    await service.release(handle, settle_if_probed=True)

    assert client.settled == []
    assert client.released == ["chg-1"]
    assert client.probed_releases == [True]


@pytest.mark.asyncio
async def test_an_ordinary_release_never_sets_the_flag() -> None:
    # The live-handle path is unchanged: Core knows the answer and gives it.
    client = _RecordingClient()
    service = _service(client)
    handle = await service.begin(ACTION_CHAT, operator_id="op-1")

    await service.release(handle)

    assert client.probed_releases == [False]


@pytest.mark.asyncio
async def test_a_probed_release_keeps_the_flag_across_retries() -> None:
    """A retry that drops the flag would ask the ledger the wrong question."""

    class _FlakyRelease(_RecordingClient):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        async def release(
            self, charge_id: str, *, settle_if_probed: bool = False,
        ) -> None:
            self.attempts += 1
            if self.attempts < 2:
                raise ActionChargeUnavailable("down")
            await super().release(
                charge_id, settle_if_probed=settle_if_probed,
            )

    client = _FlakyRelease()
    service = _service(client)
    handle = await service.begin(ACTION_CHAT, operator_id="op-1")

    await service.release(handle, settle_if_probed=True)
    await service.wait_for_pending_closes()

    assert client.released == ["chg-1"]
    assert client.probed_releases == [True]
