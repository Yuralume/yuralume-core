"""Player-authorised quota overage purchases (AP4).

Some hosted tier allowances are hard walls: reach ``daily_chat_image_limit``
and the character simply stops sending pictures; reach
``daily_feed_post_limit`` and LumeGram goes quiet for the day. AP4 lets a
player *choose*, per item, to buy past that wall with 螢火 instead.

The design point is that this is the one place where credits may be spent on
work the player did not press a button for (``feed_post_overage`` funds an
autonomous background post), so the authorisation is deliberately narrow —
four independent gates, all of which must be open:

1. **The tier opened it.** ``tier_profile.overage_enabled`` arrives through the
   control-plane runtime profile; a tier can keep overage shut as an upgrade
   incentive, and nothing here can override that.
2. **The player turned this item on.** Two independent switches, both default
   off, stored per player. Enabling ``feed_post_overage`` is the strongest
   disclosure in the product precisely because it authorises background spend.
3. **The price is the one the player agreed to.** Consent is to a *number*, not
   to an open tab: the switch may only be turned on while the current price is
   readable, that price is stored alongside the switch, and a later rise above
   it revokes the switch and asks again (:meth:`authorise`).
4. **The daily ceiling has room.** ``daily_overage_limit`` purchases per item
   per rolling 24h, counted in the same ``account_runtime_events`` ledger the
   base quotas use, so deleting a character cannot reset it.

Gate 3 is why enabling a switch is *not* a plain preference write. Pre-arming
("turn it on now, whatever it ends up costing") is refused outright — a tier
that never opened overage, or a price nobody can read right now, both answer
:class:`OverageConsentRejected` instead of storing a ``true`` the player could
not have priced. Turning a switch **off** is always allowed, whatever the tier
and price say: revoking consent must never depend on a service being up.

Every switch write also lands an append-only ``overage_consent`` row in
``account_runtime_events`` — the same ledger the ceilings are counted in —
carrying the action key, the direction, and the price the player saw. That row
is the record of what was agreed to and when; without it an enable is refused,
because an unauditable consent to spend is not consent.

Everything after that is fail-*closed*, the mirror image of the rest of the
billing stack: a wallet outage, an unconfigured price or an unreadable ledger
all deny the overage. Fail-open elsewhere means "let the player keep playing
uncharged"; fail-open here would mean "hand out free extra generations beyond
the tier allowance", which is the opposite of a safe default.

The two switches live in ``app_preferences`` under user-scoped keys
(``scoped_preferences``), the same no-schema-change home as the G2 locale
lifecycle state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Protocol

from kokoro_link.application.services.cloud_action_billing_service import (
    ActionChargeHandle,
)
from kokoro_link.application.services.scoped_preferences import (
    delete_user_preference,
    set_user_preference,
    user_preference_key,
)
from kokoro_link.contracts.account_runtime_profile import (
    AccountRuntimeProfileResolverPort,
)
from kokoro_link.contracts.account_runtime_usage import (
    ACCOUNT_RUNTIME_EVENT_CHAT_IMAGE_OVERAGE,
    ACCOUNT_RUNTIME_EVENT_FEED_POST_OVERAGE,
    ACCOUNT_RUNTIME_EVENT_OVERAGE_CONSENT,
    AccountRuntimeUsageRepositoryPort,
)
from kokoro_link.contracts.cloud_action_billing import (
    ACTION_CHAT_IMAGE_OVERAGE,
    ACTION_FEED_POST_OVERAGE,
    ACTION_KIND_QUOTA_OVERAGE,
)
from kokoro_link.contracts.cloud_action_pricing import PublicPricing
from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.contracts.repositories import PreferencesRepositoryPort
from kokoro_link.domain.value_objects.account_runtime_profile import (
    BILLING_SHAPE_ACTION_FIXED,
    DEFAULT_DAILY_OVERAGE_LIMIT,
)
from kokoro_link.infrastructure.llm.cloud_refusal import (
    INSUFFICIENT_CREDITS_CODE,
    expected_refusal_code,
)

_LOGGER = logging.getLogger(__name__)

OVERAGE_WINDOW = timedelta(hours=24)
"""Rolling window for the purchase ceiling.

Deliberately identical to the base quotas' window rather than a civil-day
boundary: the two counters are read side by side at the same decision point,
and a player who understood "N per day" for one would be ambushed by a
different meaning for the other."""


@dataclass(frozen=True, slots=True)
class OverageItem:
    """One buyable allowance, and the ids that follow it around."""

    key: str
    """Stable id used by the settings API and the SPA."""
    preference_key: str
    """``app_preferences`` key (user-scoped) holding this player's switch."""
    consented_price_key: str
    """``app_preferences`` key (user-scoped) holding the price the player was
    shown when they turned the switch on. Stored next to the switch rather than
    derived, because it is the *historical* number that was agreed to — the
    published price may have moved since. Kept short: user-scoped keys carry a
    27-character prefix into a 64-character column."""
    action_key: str
    """``contracts/action-catalog.json`` key charged for one purchase."""
    usage_event_type: str
    """``account_runtime_events`` type counting purchases for the ceiling."""


CHAT_IMAGE_OVERAGE = OverageItem(
    key="chat_image",
    preference_key="chat_image_overage_enabled",
    consented_price_key="chat_image_overage_price_cr",
    action_key=ACTION_CHAT_IMAGE_OVERAGE,
    usage_event_type=ACCOUNT_RUNTIME_EVENT_CHAT_IMAGE_OVERAGE,
)
FEED_POST_OVERAGE = OverageItem(
    key="feed_post",
    preference_key="feed_post_overage_enabled",
    consented_price_key="feed_post_overage_price_cr",
    action_key=ACTION_FEED_POST_OVERAGE,
    usage_event_type=ACCOUNT_RUNTIME_EVENT_FEED_POST_OVERAGE,
)

OVERAGE_ITEMS: tuple[OverageItem, ...] = (CHAT_IMAGE_OVERAGE, FEED_POST_OVERAGE)

#: Why an overage was not granted. Callers use these to pick their wording;
#: none of them changes the outcome, which is always "carry on as before".
OVERAGE_DENIED_TIER_OFF = "tier_off"
OVERAGE_DENIED_PLAYER_OFF = "player_off"
OVERAGE_DENIED_DAILY_LIMIT = "daily_limit"
OVERAGE_DENIED_INSUFFICIENT_CREDITS = "insufficient_credits"
OVERAGE_DENIED_UNAVAILABLE = "unavailable"
OVERAGE_DENIED_PRICE_CHANGED = "price_changed"
"""The published price is above what this player agreed to. The switch has been
turned back off; the player re-opens it at the new number or not at all."""

#: Why a switch could not be turned *on*. Returned to the settings surface so
#: it can say which gate is shut rather than showing a control that does not
#: move. Never used for turning a switch off — that always succeeds.
OVERAGE_CONSENT_TIER_CLOSED = "tier_closed"
OVERAGE_CONSENT_PRICE_UNAVAILABLE = "price_unavailable"


class OverageConsentRejected(Exception):
    """The player may not turn this switch on right now.

    Carries the item and the shut gate so the API can answer a structured
    400 the SPA can explain, instead of a silent no-op write.
    """

    def __init__(self, *, item_key: str, reason: str, message: str) -> None:
        super().__init__(message)
        self.item_key = item_key
        self.reason = reason
        self.message = message


class OverageConsentUnrecordable(RuntimeError):
    """The consent ledger could not be written, so the enable did not happen.

    Distinct from :class:`OverageConsentRejected`: nothing is wrong with the
    request, the audit trail is simply unavailable — a retry is the answer.
    """


@dataclass(slots=True)
class OverageCounters:
    """In-process telemetry for the consent gates.

    The background half of AP4 fails *silently* by design (a skipped LumeGram
    post is not an error the player should be interrupted with), so these
    counters are the only signal that a price rise is quietly turning consent
    off across a population.
    """

    price_changed: int = 0
    """Authorisations refused because the price outgrew the player's consent."""
    consent_revoked: int = 0
    """Switches turned back off by that refusal."""


@dataclass(frozen=True, slots=True)
class OverageSettings:
    """What the settings surface shows one player.

    The tier fields ride along so the UI can explain *why* a switch has no
    effect without a second round trip — a player whose tier never opened
    overage should see that, not a switch that silently does nothing.

    The per-item prices ride along for the same reason and one more: they are
    resolved against *this player's* tier, which the public price list alone
    does not identify, and a ``None`` is the UI's cue that the switch cannot be
    turned on at all right now (:class:`OverageConsentRejected` would follow).
    """

    chat_image_enabled: bool = False
    feed_post_enabled: bool = False
    tier_overage_enabled: bool = False
    daily_limit: int = DEFAULT_DAILY_OVERAGE_LIMIT
    chat_image_price_cr: float | None = None
    feed_post_price_cr: float | None = None


@dataclass(frozen=True, slots=True)
class OverageGrant:
    """The answer to "may this one over-quota action proceed, and who paid?"."""

    charge: ActionChargeHandle | None = None
    denied_reason: str | None = None
    claim_id: str | None = None
    """The daily-ceiling slot this grant took. Returned on a refund so a
    purchase the player never received does not also cost them a slot."""

    @property
    def granted(self) -> bool:
        return self.charge is not None


def _denied(reason: str) -> OverageGrant:
    return OverageGrant(denied_reason=reason)


class OveragePriceSource(Protocol):
    """The published price list, as this service needs it.

    Structural on purpose: the collaborator is ``CloudPricingService``, but the
    only thing that matters here is "give me the current list, or admit you do
    not know". Depending on the shape rather than the class keeps this module
    free of the HTTP/cache stack.
    """

    async def get_pricing(self, *, refresh: bool = False) -> object:
        """A ``PricingSnapshot``, or ``None`` when nothing is known."""


#: Float prices come back through JSON and a preference store, so comparing
#: the raw doubles would eventually re-ask for consent over a rounding
#: artefact. Both sides are quantized to :data:`PRICE_QUANTUM` before they are
#: compared (see :func:`price_to_2dp`), which removes that class of false
#: positive exactly rather than approximately.


def _resolve_price(
    pricing: PublicPricing, *, tier_name: str, action_key: str,
) -> float | None:
    """This player's price for one action, or ``None`` when it cannot be
    stated without guessing.

    The player's own tier is the answer whenever the list publishes it. When it
    does not (a tier renamed on one side of the wire), the fallback is the
    unanimous fixed-price answer — the same rule the SPA uses, and the same red
    line: quoting one tier's number to another tier's player would be a
    fabricated price, and consent to a fabricated price is not consent.
    """
    wanted = (tier_name or "").strip().casefold()
    for tier in pricing.tiers:
        if tier.tier_name.strip().casefold() != wanted:
            continue
        for action in tier.actions:
            if action.action_key == action_key:
                return action.price_cr
        # The player's own tier is published and does not sell this.
        return None
    prices: set[float] = set()
    for tier in pricing.tiers:
        if tier.billing_shape != BILLING_SHAPE_ACTION_FIXED:
            continue
        entry = next(
            (a for a in tier.actions if a.action_key == action_key), None,
        )
        if entry is None:
            return None
        prices.add(entry.price_cr)
    return prices.pop() if len(prices) == 1 else None


def _price_of(
    pricing: PublicPricing | None, item: OverageItem, profile: object,
) -> float | None:
    """``_resolve_price`` for one item, tolerating an unknown list."""
    if pricing is None:
        return None
    return _resolve_price(
        pricing,
        tier_name=str(getattr(profile, "name", "") or ""),
        action_key=item.action_key,
    )


#: Credits are quoted, stored and charged to two decimals, so that is the
#: precision the whole consent chain speaks. The audit row used ``%.6g``
#: before, which silently rewrote a six-figure price (``123456.78`` →
#: ``123457``): a consent record that does not name the agreed number is not
#: evidence of consent.
PRICE_QUANTUM = Decimal("0.01")


def price_to_2dp(price_cr: float | int) -> Decimal:
    """One price at the single precision the consent chain agrees on."""
    return Decimal(str(price_cr)).quantize(PRICE_QUANTUM, rounding=ROUND_HALF_UP)


def _format_price(price_cr: float | None) -> str:
    """The agreed number, exactly as it will be compared later."""
    if price_cr is None:
        return "-"
    return format(price_to_2dp(price_cr), "f")


def _consent_payload(
    item: OverageItem, *, enabled: bool, price_cr: float | None,
) -> str:
    """The audit row's body: what was agreed to, and at what price.

    Packed into ``resource_id`` (the ledger's only free field) rather than a
    new table — the *when* is already the row's ``occurred_at``, and the *who*
    its ``operator_id``, so three tokens carry the rest. Kept well inside the
    column's 128 characters.

    The price is written to two decimals rather than a significant-figure
    format: this row is the record of what the player agreed to, so a rounding
    that changes the number changes the evidence.
    """
    price = _format_price(price_cr)
    return f"{item.action_key}:{'on' if enabled else 'off'}:{price}"[:128]


def _tier_offers_overage(profile: object) -> bool:
    """Both halves of the tier gate, in one place.

    ``overage_enabled`` is the operator's switch, but an overage is an action
    charge and the wallet only books those on a fixed-price tier. A tier still
    on token billing therefore cannot sell one however the switch is set —
    surfacing that here keeps the settings page from offering a purchase that
    can only ever fail.
    """
    return bool(
        getattr(profile, "overage_enabled", False)
        and getattr(profile, "uses_action_pricing", False),
    )


class QuotaOverageService:
    def __init__(
        self,
        *,
        preferences: PreferencesRepositoryPort,
        profile_resolver: AccountRuntimeProfileResolverPort,
        usage_repository: AccountRuntimeUsageRepositoryPort | None,
        action_billing: object,
        pricing: OveragePriceSource | None = None,
    ) -> None:
        self._preferences = preferences
        self._profiles = profile_resolver
        self._usage = usage_repository
        self._billing = action_billing
        self._pricing = pricing
        self.counters = OverageCounters()

    # -- settings ----------------------------------------------------

    async def read_settings(self, operator_id: str) -> OverageSettings:
        profile = await self._profiles.resolve_for_operator(operator_id)
        pricing = await self._price_list()
        return OverageSettings(
            chat_image_enabled=await self._switch(
                operator_id, CHAT_IMAGE_OVERAGE,
            ),
            feed_post_enabled=await self._switch(
                operator_id, FEED_POST_OVERAGE,
            ),
            tier_overage_enabled=_tier_offers_overage(profile),
            daily_limit=profile.daily_overage_limit,
            chat_image_price_cr=_price_of(pricing, CHAT_IMAGE_OVERAGE, profile),
            feed_post_price_cr=_price_of(pricing, FEED_POST_OVERAGE, profile),
        )

    async def write_settings(
        self,
        operator_id: str,
        *,
        chat_image_enabled: bool | None = None,
        feed_post_enabled: bool | None = None,
        now: datetime | None = None,
    ) -> OverageSettings:
        """Partial update — ``None`` leaves that switch untouched.

        Turning a switch **on** is a priced consent, not a preference: it is
        refused (:class:`OverageConsentRejected`) unless the tier sells overage
        *and* the current price is readable, and the price the player agreed to
        is stored with the switch so :meth:`authorise` can tell later whether
        it still holds. Turning one **off** is unconditional.

        Both switches are validated before either is written, so a two-switch
        payload cannot half-apply.
        """
        requested = [
            (item, value)
            for item, value in (
                (CHAT_IMAGE_OVERAGE, chat_image_enabled),
                (FEED_POST_OVERAGE, feed_post_enabled),
            )
            if value is not None
        ]
        if not requested:
            return await self.read_settings(operator_id)
        profile = await self._profiles.resolve_for_operator(operator_id)
        pricing = await self._price_list()
        stamp = ensure_utc(now or datetime.now(timezone.utc))
        prices = {
            item.key: _price_of(pricing, item, profile)
            for item, value in requested
            if value
        }
        for item, value in requested:
            if value:
                self._reject_unpriced_consent(item, profile, prices[item.key])
        for item, value in requested:
            await self._apply_switch(
                operator_id, item, enabled=value,
                price_cr=prices.get(item.key), at=stamp,
            )
        return await self.read_settings(operator_id)

    def _reject_unpriced_consent(
        self, item: OverageItem, profile: object, price_cr: float | None,
    ) -> None:
        """The C6 pre-arm gate. Raises rather than storing an unpriced yes."""
        if (
            not _tier_offers_overage(profile)
            or getattr(profile, "daily_overage_limit", 0) <= 0
        ):
            raise OverageConsentRejected(
                item_key=item.key,
                reason=OVERAGE_CONSENT_TIER_CLOSED,
                message=(
                    "this plan does not sell extra "
                    f"{item.key} allowance"
                ),
            )
        if price_cr is None:
            raise OverageConsentRejected(
                item_key=item.key,
                reason=OVERAGE_CONSENT_PRICE_UNAVAILABLE,
                message=(
                    f"the current price of {item.action_key} cannot be read, "
                    "so it cannot be agreed to"
                ),
            )

    async def _apply_switch(
        self,
        operator_id: str,
        item: OverageItem,
        *,
        enabled: bool,
        price_cr: float | None,
        at: datetime,
    ) -> None:
        """Write one switch and its audit row, in the safe order.

        An enable records the consent *first*: if the ledger is unreachable the
        switch must not move, because an enable nobody can prove is exactly the
        thing this row exists to prevent. A disable writes the switch first and
        records afterwards, best-effort — revoking consent may never be blocked
        by a ledger outage.
        """
        if enabled:
            await self._record_consent(
                operator_id, item, enabled=True, price_cr=price_cr, at=at,
                required=True,
            )
        await self._set_switch(operator_id, item, enabled)
        await self._set_consented_price(
            operator_id, item, price_cr if enabled else None,
        )
        if not enabled:
            await self._record_consent(
                operator_id, item, enabled=False, price_cr=None, at=at,
                required=False,
            )

    # -- authorisation -----------------------------------------------

    async def authorise(
        self, item: OverageItem, *, operator_id: str, now: datetime,
    ) -> OverageGrant:
        """Buy one extra ``item`` for this player, or explain why not.

        A granted result carries a live charge the caller **must** close:
        :meth:`settle` when the work landed, :meth:`release` when it did not.
        """
        operator = (operator_id or "").strip()
        if not operator:
            return _denied(OVERAGE_DENIED_PLAYER_OFF)
        try:
            profile = await self._profiles.resolve_for_operator(operator)
        except Exception:  # noqa: BLE001 - an unknown tier may not buy
            _LOGGER.exception(
                "overage: tier profile unavailable (operator=%s item=%s)",
                operator, item.key,
            )
            return _denied(OVERAGE_DENIED_UNAVAILABLE)
        if not _tier_offers_overage(profile):
            return _denied(OVERAGE_DENIED_TIER_OFF)
        if not await self._switch(operator, item):
            return _denied(OVERAGE_DENIED_PLAYER_OFF)
        priced = await self._price_consent_denial(operator, item, profile, now)
        if priced is not None:
            return _denied(priced)
        if profile.daily_overage_limit <= 0:
            return _denied(OVERAGE_DENIED_DAILY_LIMIT)
        if self._usage is None:
            # Without the ledger the ceiling cannot be enforced, and an
            # unbounded purchase loop is exactly what it exists to prevent.
            _LOGGER.error(
                "overage: runtime usage ledger is not configured "
                "(operator=%s item=%s)", operator, item.key,
            )
            return _denied(OVERAGE_DENIED_UNAVAILABLE)
        # The slot is claimed *before* the money moves, and claiming is what
        # enforces the ceiling: counting first and recording afterwards lets two
        # concurrent ticks of the same operator (several characters, several
        # hosted replicas) both see the last free slot and both spend it. A
        # claim that ends up unused is discarded below.
        try:
            claim = await self._usage.claim_event_slot(
                operator_id=operator,
                event_type=item.usage_event_type,
                occurred_at=now,
                since=now - OVERAGE_WINDOW,
                limit=profile.daily_overage_limit,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "overage: purchase ledger unusable (operator=%s item=%s)",
                operator, item.key,
            )
            return _denied(OVERAGE_DENIED_UNAVAILABLE)
        if claim is None:
            return _denied(OVERAGE_DENIED_DAILY_LIMIT)
        charge = await self._charge(item, operator)
        if isinstance(charge, str):
            await self._discard(claim, item.key)
            return _denied(charge)
        return OverageGrant(charge=charge, claim_id=claim)

    async def settle(
        self, grant: OverageGrant | None, *, gateway_delivered: bool = True,
    ) -> None:
        """Close a granted purchase the player received.

        ``gateway_delivered`` is forwarded untouched to the billing service:
        the default cross-checks that the Gateway actually waived a call under
        this charge (so a per-call fallback cannot bill the same picture
        twice), and a caller whose deliverable is not a Gateway call at all —
        ``feed_post_overage``, whose post is composed by background work the
        Gateway never covers — passes ``False`` to opt out of that check.
        """
        if grant is None or grant.charge is None:
            return
        await self._billing.settle(
            grant.charge, gateway_delivered=gateway_delivered,
        )

    async def release(self, grant: OverageGrant | None) -> None:
        if grant is None or grant.charge is None:
            return
        # Read before releasing: a charge whose covered calls were already
        # served settles instead of refunding, and a purchase that was paid for
        # must keep its slot.
        served = bool(getattr(grant.charge, "consumed", False))
        await self._billing.release(grant.charge)
        if not served and grant.claim_id:
            await self._discard(grant.claim_id, "release")

    # -- price consent -----------------------------------------------

    async def _price_consent_denial(
        self,
        operator: str,
        item: OverageItem,
        profile: object,
        now: datetime,
    ) -> str | None:
        """Why this purchase must not happen at today's price, or ``None``.

        Two different answers, deliberately not merged:

        * the price cannot be read → ``unavailable``. The switch stays exactly
          as the player left it; a price-list outage is not a reason to throw
          away consent they will still want tomorrow.
        * the price is above what they agreed to → ``price_changed``, and the
          switch goes off. Consent was to a number; the number moved, so the
          agreement lapses and the player is asked again at the new one.
        """
        price = _price_of(await self._price_list(), item, profile)
        if price is None:
            _LOGGER.warning(
                "overage: no readable price, refusing to spend blind "
                "(operator=%s item=%s)", operator, item.key,
            )
            return OVERAGE_DENIED_UNAVAILABLE
        consented = await self._consented_price(operator, item)
        if consented is not None and price_to_2dp(price) <= price_to_2dp(consented):
            return None
        self.counters.price_changed += 1
        _LOGGER.info(
            "overage: %s now costs %s, above the %s agreed to — asking again "
            "(operator=%s)", item.action_key, price, consented, operator,
        )
        await self._revoke_consent(operator, item, price_cr=price, at=now)
        return OVERAGE_DENIED_PRICE_CHANGED

    async def _revoke_consent(
        self, operator: str, item: OverageItem, *, price_cr: float, at: datetime,
    ) -> None:
        """Turn the switch off on the player's behalf, and say so in the ledger.

        Best-effort throughout: this runs on a path that has already decided to
        deny, so a preference-store hiccup must not turn a skipped purchase into
        a raised exception. A revocation that fails to stick is retried by the
        next authorisation, which reaches this same branch.
        """
        try:
            await self._set_switch(operator, item, False)
            await self._set_consented_price(operator, item, None)
        except Exception:  # noqa: BLE001 - the denial stands either way
            _LOGGER.exception(
                "overage: could not revoke consent after a price rise "
                "(operator=%s item=%s)", operator, item.key,
            )
            return
        self.counters.consent_revoked += 1
        await self._record_consent(
            operator, item, enabled=False, price_cr=price_cr, at=at,
            required=False,
        )

    async def _price_list(self) -> PublicPricing | None:
        """The published list, or ``None`` when it cannot be trusted.

        A *stale* snapshot counts as unknown here even though the settings page
        would happily show it: the two readers ask different questions. "What
        does this cost, roughly?" tolerates last-known-good; "may I take this
        player's money at that number?" does not.
        """
        if self._pricing is None:
            return None
        try:
            snapshot = await self._pricing.get_pricing()
        except Exception:  # noqa: BLE001 - an unreadable list is "unknown"
            _LOGGER.exception("overage: price list unreadable")
            return None
        if snapshot is None or getattr(snapshot, "stale", False):
            return None
        return getattr(snapshot, "pricing", None)

    async def _consented_price(
        self, operator_id: str, item: OverageItem,
    ) -> float | None:
        """The price this player last agreed to, or ``None`` if there is none.

        ``None`` is treated as "never agreed" rather than "any price is fine":
        a switch stored before prices were recorded gets one re-confirmation,
        which is the safe direction for a stored yes of unknown provenance.
        """
        try:
            raw = await self._preferences.get(
                user_preference_key(operator_id, item.consented_price_key),
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "overage: consented price unreadable (operator=%s item=%s)",
                operator_id, item.key,
            )
            return None
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        return float(raw)

    async def _set_consented_price(
        self, operator_id: str, item: OverageItem, price_cr: float | None,
    ) -> None:
        """Store (or clear) the agreed price. Clearing *deletes* the row rather
        than storing a null, so "no agreement" is one state, not two.

        Stored at :data:`PRICE_QUANTUM` precision — the same number the audit
        row names and the same one :meth:`_price_consent_denial` compares
        against, so the stored yes, the ledger evidence and the later
        re-confirmation can never disagree by a rounding step."""
        if price_cr is None:
            await delete_user_preference(
                self._preferences, item.consented_price_key,
                user_id=operator_id,
            )
            return
        await set_user_preference(
            self._preferences,
            item.consented_price_key,
            float(price_to_2dp(price_cr)),
            user_id=operator_id,
        )

    async def _record_consent(
        self,
        operator_id: str,
        item: OverageItem,
        *,
        enabled: bool,
        price_cr: float | None,
        at: datetime,
        required: bool,
    ) -> None:
        """Append one immutable consent row to the runtime ledger.

        ``required`` is the difference between the two directions: an enable
        that cannot be recorded does not happen at all
        (:class:`OverageConsentUnrecordable`), while a disable is recorded on a
        best-effort basis because nothing may stand between a player and
        switching spending off.
        """
        if self._usage is None:
            if required:
                raise OverageConsentUnrecordable(
                    "the consent ledger is not configured",
                )
            return
        try:
            await self._usage.record_event(
                operator_id=operator_id,
                event_type=ACCOUNT_RUNTIME_EVENT_OVERAGE_CONSENT,
                occurred_at=at,
                resource_id=_consent_payload(
                    item, enabled=enabled, price_cr=price_cr,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception(
                "overage: consent could not be recorded "
                "(operator=%s item=%s enabled=%s)",
                operator_id, item.key, enabled,
            )
            if required:
                raise OverageConsentUnrecordable(
                    "the consent ledger is unavailable",
                ) from exc

    # -- internals ---------------------------------------------------

    async def _discard(self, claim: str, reason: str) -> None:
        """Give an unused slot back; a failure here only costs one slot."""
        if self._usage is None:
            return
        try:
            await self._usage.discard_event(event_id=claim)
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "overage: could not release an unused purchase slot (%s)",
                reason,
            )

    async def _charge(
        self, item: OverageItem, operator: str,
    ) -> ActionChargeHandle | str:
        """The charge, or the denial reason it turned into."""
        try:
            handle = await self._billing.begin(
                item.action_key,
                operator_id=operator,
                action_kind=ACTION_KIND_QUOTA_OVERAGE,
            )
        except Exception as exc:  # noqa: BLE001
            if expected_refusal_code(exc) == INSUFFICIENT_CREDITS_CODE:
                # Not an error: the player is out of 螢火, so the overage
                # simply does not happen and the base limit stands.
                return OVERAGE_DENIED_INSUFFICIENT_CREDITS
            _LOGGER.warning(
                "overage: charge failed (operator=%s item=%s)",
                operator, item.key, exc_info=True,
            )
            return OVERAGE_DENIED_UNAVAILABLE
        if handle is None:
            # No configured price, no hosted tenant, or a swallowed outage.
            # The billing service deliberately does not say which; either
            # way there is nothing to bill, so nothing extra is granted.
            return OVERAGE_DENIED_UNAVAILABLE
        return handle

    async def _switch(self, operator_id: str, item: OverageItem) -> bool:
        """This player's switch for ``item``. Unreadable or unset ⇒ off.

        Read without the installation-wide fallback that most scoped
        preferences use: consent to spend money is per player by definition,
        and a global default would be consent nobody gave.
        """
        try:
            raw = await self._preferences.get(
                user_preference_key(operator_id, item.preference_key),
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "overage: switch unreadable (operator=%s item=%s)",
                operator_id, item.key,
            )
            return False
        return raw is True

    async def _set_switch(
        self, operator_id: str, item: OverageItem, enabled: bool,
    ) -> None:
        await set_user_preference(
            self._preferences,
            item.preference_key,
            bool(enabled),
            user_id=operator_id,
        )
