"""AP4 — player-authorised quota overage purchases.

The invariant under test is the *layered* consent: a tier that opened overage
AND a player who turned that specific item on AND the price they agreed to AND
a daily ceiling that has not been spent. Missing any one of them, or a charge
that does not land, must leave the caller on the historical "over the limit
means no" path.

The price layer is the C6 review's: a switch may not be armed before there is a
price to agree to, the agreement is written to an append-only ledger, and a
later rise above it revokes the switch rather than quietly spending more.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from kokoro_link.application.services.cloud_action_billing_service import (
    ActionChargeHandle,
)
from kokoro_link.application.services.cloud_pricing_service import (
    PricingSnapshot,
)
from kokoro_link.application.services.scoped_preferences import (
    set_user_preference,
    user_preference_key,
)
from kokoro_link.application.services.quota_overage_service import (
    CHAT_IMAGE_OVERAGE,
    FEED_POST_OVERAGE,
    OVERAGE_CONSENT_PRICE_UNAVAILABLE,
    OVERAGE_CONSENT_TIER_CLOSED,
    OVERAGE_DENIED_DAILY_LIMIT,
    OVERAGE_DENIED_INSUFFICIENT_CREDITS,
    OVERAGE_DENIED_PLAYER_OFF,
    OVERAGE_DENIED_PRICE_CHANGED,
    OVERAGE_DENIED_TIER_OFF,
    OVERAGE_DENIED_UNAVAILABLE,
    OverageConsentRejected,
    OverageConsentUnrecordable,
    QuotaOverageService,
)
from kokoro_link.contracts.account_runtime_usage import (
    ACCOUNT_RUNTIME_EVENT_CHAT_IMAGE_OVERAGE,
    ACCOUNT_RUNTIME_EVENT_FEED_POST_OVERAGE,
    ACCOUNT_RUNTIME_EVENT_OVERAGE_CONSENT,
)
from kokoro_link.contracts.cloud_action_pricing import (
    ActionPrice,
    PublicPricing,
    TierPricing,
)
from kokoro_link.contracts.cloud_action_billing import (
    ACTION_KIND_QUOTA_OVERAGE,
)
from kokoro_link.domain.value_objects.account_runtime_profile import (
    BILLING_SHAPE_ACTION_FIXED,
    BILLING_SHAPE_TOKEN_FLOATING,
    AccountRuntimeProfile,
)
from kokoro_link.infrastructure.llm.cloud_refusal import (
    INSUFFICIENT_CREDITS_CODE,
    ExpectedCloudRefusal,
)
from kokoro_link.infrastructure.repositories.in_memory_account_runtime_usage import (
    InMemoryAccountRuntimeUsageRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_preferences import (
    InMemoryPreferencesRepository,
)

NOW = datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc)
OPERATOR = "cloud:acct-1"


class _StaticProfileResolver:
    def __init__(self, profile: AccountRuntimeProfile) -> None:
        self.profile = profile

    async def resolve_for_operator(self, operator_id: str) -> AccountRuntimeProfile:
        return self.profile


class _RecordingBilling:
    """Stands in for ``CloudActionBillingService`` at the seam that matters."""

    def __init__(
        self,
        *,
        handle: ActionChargeHandle | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._handle = handle
        self._error = error
        self.begun: list[tuple[str, str, str]] = []
        self.settled: list[str] = []
        self.settle_gateway_delivered: list[bool] = []
        self.released: list[str] = []

    async def begin(
        self, action_key: str, *, operator_id: str, action_kind: str,
    ) -> ActionChargeHandle | None:
        self.begun.append((action_key, operator_id, action_kind))
        if self._error is not None:
            raise self._error
        return self._handle

    async def settle(
        self,
        handle: ActionChargeHandle | None,
        *,
        gateway_delivered: bool = True,
    ) -> None:
        if handle is not None:
            self.settled.append(handle.charge_id)
            self.settle_gateway_delivered.append(gateway_delivered)

    async def release(self, handle: ActionChargeHandle | None) -> None:
        if handle is not None:
            self.released.append(handle.charge_id)


def _handle(charge_id: str = "chg-1") -> ActionChargeHandle:
    return ActionChargeHandle(
        action_key="chat_image_overage",
        charge_id=charge_id,
        interaction_id="int-1",
        price_cr=3.0,
    )


def _refusal(code: str = INSUFFICIENT_CREDITS_CODE) -> ExpectedCloudRefusal:
    request = httpx.Request("POST", "https://user.test/internal/charge")
    response = httpx.Response(402, request=request)
    return ExpectedCloudRefusal(
        "402", request=request, response=response, code=code,
    )


class _StaticPricing:
    """Stands in for ``CloudPricingService`` — one published list, no cache."""

    def __init__(self, snapshot: PricingSnapshot | None) -> None:
        self._snapshot = snapshot

    async def get_pricing(self, *, refresh: bool = False) -> PricingSnapshot | None:
        return self._snapshot


def _pricing(
    *,
    chat_image_cr: float | None = 8.0,
    feed_post_cr: float | None = 6.0,
    tier_name: str = "internal_test",
    stale: bool = False,
) -> _StaticPricing:
    actions = [
        ActionPrice(action_key=key, unit=unit, price_cr=price, overage=True)
        for key, unit, price in (
            ("chat_image_overage", "per_image", chat_image_cr),
            ("feed_post_overage", "per_post", feed_post_cr),
        )
        if price is not None
    ]
    return _StaticPricing(PricingSnapshot(
        pricing=PublicPricing(tiers=(
            TierPricing(
                tier_name=tier_name,
                billing_shape=BILLING_SHAPE_ACTION_FIXED,
                actions=tuple(actions),
            ),
        )),
        stale=stale,
    ))


def _profile(
    *,
    overage_enabled: bool = True,
    daily_overage_limit: int = 5,
    billing_shape: str = BILLING_SHAPE_ACTION_FIXED,
) -> AccountRuntimeProfile:
    return AccountRuntimeProfile(
        name="internal_test",
        billing_shape=billing_shape,
        overage_enabled=overage_enabled,
        daily_overage_limit=daily_overage_limit,
    )


def _service(
    *,
    profile: AccountRuntimeProfile | None = None,
    billing: _RecordingBilling | None = None,
    preferences: InMemoryPreferencesRepository | None = None,
    usage: InMemoryAccountRuntimeUsageRepository | None = None,
    pricing: _StaticPricing | None = None,
) -> QuotaOverageService:
    return QuotaOverageService(
        preferences=preferences or InMemoryPreferencesRepository(),
        profile_resolver=_StaticProfileResolver(profile or _profile()),
        usage_repository=usage or InMemoryAccountRuntimeUsageRepository(),
        action_billing=billing or _RecordingBilling(handle=_handle()),
        pricing=pricing if pricing is not None else _pricing(),
    )


async def _arm(
    preferences: InMemoryPreferencesRepository, *items,
) -> None:
    """Consent to ``items`` the way a player does: on a tier that sells them.

    Used by the tests whose subject is what happens *after* something changes
    under the player (the tier closes, the ledger goes away, the price moves).
    Those states can no longer be reached by writing the switch directly —
    which is the point of the C6 gate — so they are set up through the same
    door the player used.
    """
    service = _service(preferences=preferences)
    await service.write_settings(
        OPERATOR, **{f"{item.key}_enabled": True for item in items},
    )


# -- settings ---------------------------------------------------------


@pytest.mark.asyncio
async def test_settings_default_to_off_for_both_items() -> None:
    service = _service()

    settings = await service.read_settings(OPERATOR)

    assert settings.chat_image_enabled is False
    assert settings.feed_post_enabled is False
    assert settings.tier_overage_enabled is True
    assert settings.daily_limit == 5


@pytest.mark.asyncio
async def test_settings_write_is_partial_and_round_trips() -> None:
    preferences = InMemoryPreferencesRepository()
    service = _service(preferences=preferences)

    await service.write_settings(OPERATOR, chat_image_enabled=True)
    settings = await service.write_settings(OPERATOR, feed_post_enabled=True)

    assert settings.chat_image_enabled is True
    assert settings.feed_post_enabled is True
    assert (await service.read_settings(OPERATOR)).chat_image_enabled is True


@pytest.mark.asyncio
async def test_settings_are_scoped_per_player() -> None:
    preferences = InMemoryPreferencesRepository()
    service = _service(preferences=preferences)

    await service.write_settings(OPERATOR, chat_image_enabled=True)

    other = await service.read_settings("cloud:acct-2")
    assert other.chat_image_enabled is False


@pytest.mark.asyncio
async def test_settings_report_the_tier_gate_even_when_the_player_opted_in() -> None:
    # The tier closed overage after this player had already consented.
    preferences = InMemoryPreferencesRepository()
    await _arm(preferences, CHAT_IMAGE_OVERAGE)
    service = _service(
        preferences=preferences, profile=_profile(overage_enabled=False),
    )

    settings = await service.read_settings(OPERATOR)

    assert settings.chat_image_enabled is True
    assert settings.tier_overage_enabled is False


# -- priced consent (C6) ----------------------------------------------


@pytest.mark.asyncio
async def test_settings_quote_this_tier_s_price_for_each_item() -> None:
    settings = await _service().read_settings(OPERATOR)

    assert settings.chat_image_price_cr == 8.0
    assert settings.feed_post_price_cr == 6.0


@pytest.mark.asyncio
async def test_an_unreadable_price_is_reported_as_unknown_not_free() -> None:
    settings = await _service(
        pricing=_StaticPricing(None),
    ).read_settings(OPERATOR)

    assert settings.chat_image_price_cr is None
    assert settings.feed_post_price_cr is None


@pytest.mark.asyncio
async def test_a_switch_cannot_be_armed_before_there_is_a_price() -> None:
    """C6: no pre-arming. Consent is to a number, so there must be one."""
    preferences = InMemoryPreferencesRepository()
    service = _service(preferences=preferences, pricing=_StaticPricing(None))

    with pytest.raises(OverageConsentRejected) as raised:
        await service.write_settings(OPERATOR, chat_image_enabled=True)

    assert raised.value.reason == OVERAGE_CONSENT_PRICE_UNAVAILABLE
    assert raised.value.item_key == "chat_image"
    assert (await service.read_settings(OPERATOR)).chat_image_enabled is False


@pytest.mark.asyncio
async def test_a_stale_price_list_is_not_good_enough_to_agree_to() -> None:
    service = _service(pricing=_pricing(stale=True))

    with pytest.raises(OverageConsentRejected):
        await service.write_settings(OPERATOR, feed_post_enabled=True)


@pytest.mark.asyncio
async def test_a_tier_that_never_opened_overage_cannot_be_pre_armed() -> None:
    service = _service(profile=_profile(overage_enabled=False))

    with pytest.raises(OverageConsentRejected) as raised:
        await service.write_settings(OPERATOR, feed_post_enabled=True)

    assert raised.value.reason == OVERAGE_CONSENT_TIER_CLOSED


@pytest.mark.asyncio
async def test_a_zero_ceiling_cannot_be_pre_armed_either() -> None:
    service = _service(profile=_profile(daily_overage_limit=0))

    with pytest.raises(OverageConsentRejected) as raised:
        await service.write_settings(OPERATOR, chat_image_enabled=True)

    assert raised.value.reason == OVERAGE_CONSENT_TIER_CLOSED


@pytest.mark.asyncio
async def test_switching_off_is_never_refused() -> None:
    """Revoking consent may not depend on a tier, a price or a ledger."""
    preferences = InMemoryPreferencesRepository()
    await _arm(preferences, CHAT_IMAGE_OVERAGE)
    service = _service(
        preferences=preferences,
        profile=_profile(overage_enabled=False),
        pricing=_StaticPricing(None),
    )

    settings = await service.write_settings(OPERATOR, chat_image_enabled=False)

    assert settings.chat_image_enabled is False


@pytest.mark.asyncio
async def test_a_rejected_item_does_not_half_apply_the_other_one() -> None:
    preferences = InMemoryPreferencesRepository()
    service = _service(
        preferences=preferences,
        pricing=_pricing(feed_post_cr=None),
    )

    with pytest.raises(OverageConsentRejected):
        await service.write_settings(
            OPERATOR, chat_image_enabled=True, feed_post_enabled=True,
        )

    settings = await service.read_settings(OPERATOR)
    assert settings.chat_image_enabled is False
    assert settings.feed_post_enabled is False


@pytest.mark.asyncio
async def test_every_switch_move_lands_an_append_only_consent_row() -> None:
    usage = InMemoryAccountRuntimeUsageRepository()
    service = _service(usage=usage)

    await service.write_settings(OPERATOR, chat_image_enabled=True, now=NOW)
    await service.write_settings(OPERATOR, chat_image_enabled=False, now=NOW)

    trail = await usage.list_events(
        event_type=ACCOUNT_RUNTIME_EVENT_OVERAGE_CONSENT,
    )
    assert [event.resource_id for event in trail] == [
        "chat_image_overage:on:8.00",
        "chat_image_overage:off:-",
    ]
    assert all(event.occurred_at == NOW for event in trail)


@pytest.mark.asyncio
async def test_the_consent_row_names_the_price_exactly() -> None:
    """The audit row is the evidence of what was agreed to, so it may not
    round: ``%.6g`` used to turn 123456.78 into 123457, i.e. into a number the
    player never saw and never agreed to."""
    usage = InMemoryAccountRuntimeUsageRepository()
    preferences = InMemoryPreferencesRepository()
    service = _service(
        usage=usage,
        preferences=preferences,
        pricing=_pricing(chat_image_cr=123456.78),
    )

    await service.write_settings(OPERATOR, chat_image_enabled=True, now=NOW)

    trail = await usage.list_events(
        event_type=ACCOUNT_RUNTIME_EVENT_OVERAGE_CONSENT,
    )
    assert [event.resource_id for event in trail] == [
        "chat_image_overage:on:123456.78",
    ]
    # ...and the stored consent agrees with the row to the same digit, so the
    # later re-confirmation cannot fire on a rounding step.
    stored = await preferences.get(
        user_preference_key(OPERATOR, CHAT_IMAGE_OVERAGE.consented_price_key),
    )
    assert stored == 123456.78


@pytest.mark.asyncio
async def test_a_price_within_the_stored_precision_is_not_a_rise() -> None:
    """Consent is to a number at credit precision; noise below that is not a
    new price to re-confirm."""
    preferences = InMemoryPreferencesRepository()
    billing = _RecordingBilling(handle=_handle())
    await _service(
        preferences=preferences, pricing=_pricing(chat_image_cr=8.0),
    ).write_settings(OPERATOR, chat_image_enabled=True, now=NOW)

    grant = await _service(
        preferences=preferences,
        billing=billing,
        pricing=_pricing(chat_image_cr=8.000000001),
    ).authorise(CHAT_IMAGE_OVERAGE, operator_id=OPERATOR, now=NOW)

    assert grant.granted is True


@pytest.mark.asyncio
async def test_an_enable_that_cannot_be_recorded_does_not_happen() -> None:
    """No audit row, no consent — the switch stays where it was."""
    service = QuotaOverageService(
        preferences=InMemoryPreferencesRepository(),
        profile_resolver=_StaticProfileResolver(_profile()),
        usage_repository=None,
        action_billing=_RecordingBilling(handle=_handle()),
        pricing=_pricing(),
    )

    with pytest.raises(OverageConsentUnrecordable):
        await service.write_settings(OPERATOR, chat_image_enabled=True)

    assert (await service.read_settings(OPERATOR)).chat_image_enabled is False


# -- authorisation ----------------------------------------------------


@pytest.mark.asyncio
async def test_tier_with_overage_closed_never_charges() -> None:
    billing = _RecordingBilling(handle=_handle())
    preferences = InMemoryPreferencesRepository()
    await _arm(preferences, CHAT_IMAGE_OVERAGE)
    service = _service(
        preferences=preferences,
        profile=_profile(overage_enabled=False),
        billing=billing,
    )

    grant = await service.authorise(
        CHAT_IMAGE_OVERAGE, operator_id=OPERATOR, now=NOW,
    )

    assert grant.granted is False
    assert grant.denied_reason == OVERAGE_DENIED_TIER_OFF
    assert billing.begun == []


@pytest.mark.asyncio
async def test_player_who_never_opted_in_is_never_charged() -> None:
    billing = _RecordingBilling(handle=_handle())
    service = _service(billing=billing)

    grant = await service.authorise(
        CHAT_IMAGE_OVERAGE, operator_id=OPERATOR, now=NOW,
    )

    assert grant.denied_reason == OVERAGE_DENIED_PLAYER_OFF
    assert billing.begun == []


@pytest.mark.asyncio
async def test_the_two_items_consent_independently() -> None:
    billing = _RecordingBilling(handle=_handle())
    service = _service(billing=billing)
    await service.write_settings(OPERATOR, chat_image_enabled=True)

    granted = await service.authorise(
        CHAT_IMAGE_OVERAGE, operator_id=OPERATOR, now=NOW,
    )
    denied = await service.authorise(
        FEED_POST_OVERAGE, operator_id=OPERATOR, now=NOW,
    )

    assert granted.granted is True
    assert denied.denied_reason == OVERAGE_DENIED_PLAYER_OFF


@pytest.mark.asyncio
async def test_a_granted_overage_charges_as_quota_overage_and_is_counted() -> None:
    billing = _RecordingBilling(handle=_handle())
    usage = InMemoryAccountRuntimeUsageRepository()
    service = _service(billing=billing, usage=usage)
    await service.write_settings(OPERATOR, chat_image_enabled=True)

    grant = await service.authorise(
        CHAT_IMAGE_OVERAGE, operator_id=OPERATOR, now=NOW,
    )

    assert grant.granted is True
    assert billing.begun == [
        ("chat_image_overage", OPERATOR, ACTION_KIND_QUOTA_OVERAGE),
    ]
    assert await usage.count_events(
        operator_id=OPERATOR,
        event_type=ACCOUNT_RUNTIME_EVENT_CHAT_IMAGE_OVERAGE,
        since=NOW - timedelta(hours=24),
        until=NOW,
    ) == 1


@pytest.mark.asyncio
async def test_feed_overage_charges_its_own_action_key_and_counter() -> None:
    billing = _RecordingBilling(handle=_handle("chg-feed"))
    usage = InMemoryAccountRuntimeUsageRepository()
    service = _service(billing=billing, usage=usage)
    await service.write_settings(OPERATOR, feed_post_enabled=True)

    grant = await service.authorise(
        FEED_POST_OVERAGE, operator_id=OPERATOR, now=NOW,
    )

    assert grant.granted is True
    assert billing.begun[0][0] == "feed_post_overage"
    assert await usage.count_events(
        operator_id=OPERATOR,
        event_type=ACCOUNT_RUNTIME_EVENT_FEED_POST_OVERAGE,
        since=NOW - timedelta(hours=24),
        until=NOW,
    ) == 1


@pytest.mark.asyncio
async def test_daily_ceiling_stops_further_purchases() -> None:
    billing = _RecordingBilling(handle=_handle())
    usage = InMemoryAccountRuntimeUsageRepository()
    service = _service(
        profile=_profile(daily_overage_limit=2), billing=billing, usage=usage,
    )
    await service.write_settings(OPERATOR, chat_image_enabled=True)

    first = await service.authorise(
        CHAT_IMAGE_OVERAGE, operator_id=OPERATOR, now=NOW,
    )
    second = await service.authorise(
        CHAT_IMAGE_OVERAGE, operator_id=OPERATOR, now=NOW,
    )
    third = await service.authorise(
        CHAT_IMAGE_OVERAGE, operator_id=OPERATOR, now=NOW,
    )

    assert [first.granted, second.granted, third.granted] == [True, True, False]
    assert third.denied_reason == OVERAGE_DENIED_DAILY_LIMIT
    assert len(billing.begun) == 2


@pytest.mark.asyncio
async def test_the_ceiling_is_a_rolling_24h_window() -> None:
    billing = _RecordingBilling(handle=_handle())
    usage = InMemoryAccountRuntimeUsageRepository()
    service = _service(
        profile=_profile(daily_overage_limit=1), billing=billing, usage=usage,
    )
    await service.write_settings(OPERATOR, chat_image_enabled=True)

    await service.authorise(CHAT_IMAGE_OVERAGE, operator_id=OPERATOR, now=NOW)
    blocked = await service.authorise(
        CHAT_IMAGE_OVERAGE, operator_id=OPERATOR, now=NOW,
    )
    tomorrow = await service.authorise(
        CHAT_IMAGE_OVERAGE,
        operator_id=OPERATOR,
        now=NOW + timedelta(hours=24, seconds=1),
    )

    assert blocked.granted is False
    assert tomorrow.granted is True


@pytest.mark.asyncio
async def test_a_zero_ceiling_closes_overage_entirely() -> None:
    billing = _RecordingBilling(handle=_handle())
    preferences = InMemoryPreferencesRepository()
    await _arm(preferences, CHAT_IMAGE_OVERAGE)
    service = _service(
        preferences=preferences,
        profile=_profile(daily_overage_limit=0),
        billing=billing,
    )

    grant = await service.authorise(
        CHAT_IMAGE_OVERAGE, operator_id=OPERATOR, now=NOW,
    )

    assert grant.denied_reason == OVERAGE_DENIED_DAILY_LIMIT
    assert billing.begun == []


@pytest.mark.asyncio
async def test_insufficient_credits_denies_without_raising() -> None:
    billing = _RecordingBilling(error=_refusal())
    usage = InMemoryAccountRuntimeUsageRepository()
    service = _service(billing=billing, usage=usage)
    await service.write_settings(OPERATOR, chat_image_enabled=True)

    grant = await service.authorise(
        CHAT_IMAGE_OVERAGE, operator_id=OPERATOR, now=NOW,
    )

    assert grant.granted is False
    assert grant.denied_reason == OVERAGE_DENIED_INSUFFICIENT_CREDITS
    # A refused purchase must not eat the player's daily allowance.
    assert await usage.count_events(
        operator_id=OPERATOR,
        event_type=ACCOUNT_RUNTIME_EVENT_CHAT_IMAGE_OVERAGE,
        since=NOW - timedelta(hours=24),
        until=NOW,
    ) == 0


@pytest.mark.asyncio
async def test_an_outage_denies_instead_of_granting_a_free_slot() -> None:
    billing = _RecordingBilling(error=RuntimeError("boom"))
    service = _service(billing=billing)
    await service.write_settings(OPERATOR, chat_image_enabled=True)

    grant = await service.authorise(
        CHAT_IMAGE_OVERAGE, operator_id=OPERATOR, now=NOW,
    )

    assert grant.granted is False
    assert grant.denied_reason == OVERAGE_DENIED_UNAVAILABLE


@pytest.mark.asyncio
async def test_a_no_price_answer_denies_rather_than_granting_for_free() -> None:
    billing = _RecordingBilling(handle=None)
    service = _service(billing=billing)
    await service.write_settings(OPERATOR, chat_image_enabled=True)

    grant = await service.authorise(
        CHAT_IMAGE_OVERAGE, operator_id=OPERATOR, now=NOW,
    )

    assert grant.granted is False
    assert grant.denied_reason == OVERAGE_DENIED_UNAVAILABLE


@pytest.mark.asyncio
async def test_a_missing_usage_ledger_denies_rather_than_spending_unbounded() -> None:
    billing = _RecordingBilling(handle=_handle())
    preferences = InMemoryPreferencesRepository()
    await _arm(preferences, CHAT_IMAGE_OVERAGE)
    service = QuotaOverageService(
        preferences=preferences,
        profile_resolver=_StaticProfileResolver(_profile()),
        usage_repository=None,
        action_billing=billing,
        pricing=_pricing(),
    )

    grant = await service.authorise(
        CHAT_IMAGE_OVERAGE, operator_id=OPERATOR, now=NOW,
    )

    assert grant.denied_reason == OVERAGE_DENIED_UNAVAILABLE
    assert billing.begun == []


@pytest.mark.asyncio
async def test_settle_and_release_delegate_to_the_billing_service() -> None:
    billing = _RecordingBilling(handle=_handle("chg-9"))
    service = _service(billing=billing)
    await service.write_settings(OPERATOR, chat_image_enabled=True)
    grant = await service.authorise(
        CHAT_IMAGE_OVERAGE, operator_id=OPERATOR, now=NOW,
    )

    await service.settle(grant)
    await service.release(grant)

    assert billing.settled == ["chg-9"]
    # The default cross-check travels with it: an overage picture IS delivered
    # by a Gateway call, so a per-call fallback must not be billed twice.
    assert billing.settle_gateway_delivered == [True]
    # ``settle`` already closed it; the second close is a no-op at the
    # billing service, which owns single-shot closing.
    assert billing.released == ["chg-9"]


@pytest.mark.asyncio
async def test_settle_and_release_tolerate_a_denied_grant() -> None:
    billing = _RecordingBilling(handle=_handle())
    service = _service(billing=billing)

    denied = await service.authorise(
        CHAT_IMAGE_OVERAGE, operator_id=OPERATOR, now=NOW,
    )
    await service.settle(denied)
    await service.release(denied)
    await service.settle(None)
    await service.release(None)

    assert billing.settled == []
    assert billing.released == []


@pytest.mark.asyncio
async def test_a_token_billed_tier_cannot_sell_an_overage() -> None:
    """The wallet only books action charges on a fixed-price tier.

    Offering the switch on a ``token_floating`` tier would show the player a
    purchase that can only ever answer "try again later" — and if it did land,
    the same generation would be billed twice (overage price plus its own
    per-call token cost).
    """
    billing = _RecordingBilling(handle=_handle())
    preferences = InMemoryPreferencesRepository()
    await _arm(preferences, CHAT_IMAGE_OVERAGE)
    service = _service(
        preferences=preferences,
        profile=_profile(billing_shape=BILLING_SHAPE_TOKEN_FLOATING),
        billing=billing,
    )

    settings = await service.read_settings(OPERATOR)
    grant = await service.authorise(
        CHAT_IMAGE_OVERAGE, operator_id=OPERATOR, now=NOW,
    )

    assert settings.tier_overage_enabled is False
    assert grant.denied_reason == OVERAGE_DENIED_TIER_OFF
    assert billing.begun == []


@pytest.mark.asyncio
async def test_a_denied_charge_gives_the_daily_slot_back() -> None:
    """The ceiling counts purchases, not attempts."""
    billing = _RecordingBilling(error=_refusal())
    usage = InMemoryAccountRuntimeUsageRepository()
    service = _service(
        profile=_profile(daily_overage_limit=1), billing=billing, usage=usage,
    )
    await service.write_settings(OPERATOR, chat_image_enabled=True)

    denied = await service.authorise(
        CHAT_IMAGE_OVERAGE, operator_id=OPERATOR, now=NOW,
    )

    assert denied.denied_reason == OVERAGE_DENIED_INSUFFICIENT_CREDITS
    assert await usage.count_events(
        operator_id=OPERATOR,
        event_type=ACCOUNT_RUNTIME_EVENT_CHAT_IMAGE_OVERAGE,
        since=NOW - timedelta(hours=24),
    ) == 0


@pytest.mark.asyncio
async def test_the_ceiling_never_exceeds_the_limit_under_a_race() -> None:
    """Two ticks of the same operator cannot both take the last slot."""
    import asyncio

    billing = _RecordingBilling(handle=_handle())
    usage = InMemoryAccountRuntimeUsageRepository()
    service = _service(
        profile=_profile(daily_overage_limit=2), billing=billing, usage=usage,
    )
    await service.write_settings(OPERATOR, chat_image_enabled=True)

    grants = await asyncio.gather(*(
        service.authorise(CHAT_IMAGE_OVERAGE, operator_id=OPERATOR, now=NOW)
        for _ in range(6)
    ))

    assert sum(1 for grant in grants if grant.granted) <= 2
    assert await usage.count_events(
        operator_id=OPERATOR,
        event_type=ACCOUNT_RUNTIME_EVENT_CHAT_IMAGE_OVERAGE,
        since=NOW - timedelta(hours=24),
    ) <= 2


# -- the price moving under a stored consent --------------------------


@pytest.mark.asyncio
async def test_a_price_rise_revokes_the_switch_instead_of_spending_more() -> None:
    billing = _RecordingBilling(handle=_handle())
    preferences = InMemoryPreferencesRepository()
    usage = InMemoryAccountRuntimeUsageRepository()
    await _arm(preferences, CHAT_IMAGE_OVERAGE)
    service = _service(
        preferences=preferences, usage=usage, billing=billing,
        pricing=_pricing(chat_image_cr=9.0),
    )

    grant = await service.authorise(
        CHAT_IMAGE_OVERAGE, operator_id=OPERATOR, now=NOW,
    )

    assert grant.denied_reason == OVERAGE_DENIED_PRICE_CHANGED
    assert billing.begun == []
    # The switch is off, so the next background tick does not even ask.
    assert (await service.read_settings(OPERATOR)).chat_image_enabled is False
    assert service.counters.price_changed == 1
    assert service.counters.consent_revoked == 1
    # ...and the revocation is in the same trail as the consent it undoes.
    trail = await usage.list_events(
        event_type=ACCOUNT_RUNTIME_EVENT_OVERAGE_CONSENT,
    )
    assert trail[-1].resource_id == "chat_image_overage:off:9.00"


@pytest.mark.asyncio
async def test_the_agreed_price_holding_still_authorises() -> None:
    preferences = InMemoryPreferencesRepository()
    await _arm(preferences, FEED_POST_OVERAGE)
    service = _service(preferences=preferences)

    grant = await service.authorise(
        FEED_POST_OVERAGE, operator_id=OPERATOR, now=NOW,
    )

    assert grant.granted is True
    assert service.counters.price_changed == 0


@pytest.mark.asyncio
async def test_a_price_cut_needs_no_reconfirmation() -> None:
    """Consent is a ceiling, not an exact match — cheaper is still agreed."""
    preferences = InMemoryPreferencesRepository()
    await _arm(preferences, CHAT_IMAGE_OVERAGE)
    service = _service(
        preferences=preferences, pricing=_pricing(chat_image_cr=4.0),
    )

    grant = await service.authorise(
        CHAT_IMAGE_OVERAGE, operator_id=OPERATOR, now=NOW,
    )

    assert grant.granted is True


@pytest.mark.asyncio
async def test_re_enabling_at_the_new_price_authorises_again() -> None:
    preferences = InMemoryPreferencesRepository()
    await _arm(preferences, CHAT_IMAGE_OVERAGE)
    service = _service(
        preferences=preferences, pricing=_pricing(chat_image_cr=9.0),
    )
    await service.authorise(CHAT_IMAGE_OVERAGE, operator_id=OPERATOR, now=NOW)

    await service.write_settings(OPERATOR, chat_image_enabled=True)
    grant = await service.authorise(
        CHAT_IMAGE_OVERAGE, operator_id=OPERATOR, now=NOW,
    )

    assert grant.granted is True


@pytest.mark.asyncio
async def test_an_unreadable_price_denies_without_throwing_consent_away() -> None:
    """A price-list outage is not a reason to lose consent given yesterday."""
    preferences = InMemoryPreferencesRepository()
    await _arm(preferences, CHAT_IMAGE_OVERAGE)
    service = _service(preferences=preferences, pricing=_StaticPricing(None))

    grant = await service.authorise(
        CHAT_IMAGE_OVERAGE, operator_id=OPERATOR, now=NOW,
    )

    assert grant.denied_reason == OVERAGE_DENIED_UNAVAILABLE
    assert service.counters.consent_revoked == 0
    settings = await _service(preferences=preferences).read_settings(OPERATOR)
    assert settings.chat_image_enabled is True


@pytest.mark.asyncio
async def test_a_switch_stored_without_a_price_is_re_confirmed_once() -> None:
    """A ``true`` of unknown provenance is not an open tab."""
    preferences = InMemoryPreferencesRepository()
    await set_user_preference(
        preferences, CHAT_IMAGE_OVERAGE.preference_key, True, user_id=OPERATOR,
    )
    service = _service(preferences=preferences)

    grant = await service.authorise(
        CHAT_IMAGE_OVERAGE, operator_id=OPERATOR, now=NOW,
    )

    assert grant.denied_reason == OVERAGE_DENIED_PRICE_CHANGED


@pytest.mark.asyncio
async def test_releasing_an_unserved_purchase_returns_its_slot() -> None:
    billing = _RecordingBilling(handle=_handle())
    usage = InMemoryAccountRuntimeUsageRepository()
    service = _service(
        profile=_profile(daily_overage_limit=1), billing=billing, usage=usage,
    )
    await service.write_settings(OPERATOR, chat_image_enabled=True)

    grant = await service.authorise(
        CHAT_IMAGE_OVERAGE, operator_id=OPERATOR, now=NOW,
    )
    await service.release(grant)
    again = await service.authorise(
        CHAT_IMAGE_OVERAGE, operator_id=OPERATOR, now=NOW,
    )

    assert grant.granted is True
    assert again.granted is True
