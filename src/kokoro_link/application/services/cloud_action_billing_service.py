"""Action-level credit charging for hosted ``billing_shape=action_fixed``
tiers (AP2).

One player action = one charge = one ledger row. The service owns the whole
lifecycle around an action's execution:

1. resolve the operator's tier profile; anything that is not
   ``action_fixed`` returns ``None`` and the caller runs exactly as it does
   today (self-host and every un-migrated tier are byte-identical);
2. charge the configured fixed price *before* the work starts, so the player
   is refused up-front rather than after burning a generation;
3. bind the resulting :class:`InteractionContext` so every Gateway call the
   action makes carries the covering charge id;
4. settle on success, release on failure — and release on a "success" the
   Gateway never covered, because it billed those calls per call and a second
   fixed charge would be a double bill (see :meth:`settle`).

**Fail-soft is the rule, with two exceptions.** A wallet-service outage must
never brick a player mid-sentence, so an unavailable charge is logged, counted
and swallowed — the action runs uncharged, matching the User service's own
fail-open on a missing price. The exceptions are ``insufficient_credits`` and
``price_changed``: both are decisions, not outages, and both propagate as the
U4-shaped structured refusals the foreground routes already render.

Closing a charge is the one place fail-soft is *not* enough: a settle/release
that never lands leaves the player's money reserved. Those retry with a bounded
backoff off the request path, and a handle whose close never succeeded stays
**open** so the User-side sweeper is the one that decides its fate.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from kokoro_link.contracts.account_runtime_profile import (
    AccountRuntimeProfileResolverPort,
)
from kokoro_link.contracts.cloud_action_billing import (
    ACTION_KIND_QUOTA_OVERAGE,
    ACTION_KIND_USER_ACTION,
    ActionChargePort,
    ActionChargeUnavailable,
    ActionPriceChanged,
    QuotedPricePort,
)
from kokoro_link.contracts.generation_trigger import (
    GenerationTrigger,
    current_generation_trigger,
)
from kokoro_link.contracts.interaction_context import (
    InteractionContext,
    interaction_scope,
    new_interaction_id,
)
from kokoro_link.contracts.operator_profile import OperatorProfileRepositoryPort

_LOGGER = logging.getLogger(__name__)

#: Backoff between the retries of a failed settle/release. Bounded on purpose:
#: the reservation is durable on the User side and has its own sweeper, so
#: Core's job is to cover a blip, not to own recovery.
_CLOSE_RETRY_DELAYS: tuple[float, ...] = (0.5, 2.0, 5.0)


@dataclass(slots=True)
class ActionBillingCounters:
    """In-process telemetry for the AP5 reconciliation story.

    Deliberately plain counters rather than a new observability port: the
    authoritative numbers are the User ledger and the Gateway usage rows, and
    these only answer "did *this* process fail to charge, and how often".
    """

    charged: int = 0
    no_price: int = 0
    charge_failed: int = 0
    settle_failed: int = 0
    release_failed: int = 0
    refused: int = 0
    price_changed: int = 0
    """Charges refused because the quoted price no longer matched. A rising
    number means the back office is being edited under live sessions."""
    close_retried: int = 0
    """Settle/release attempts that failed and were queued for retry."""
    close_recovered: int = 0
    """Retries that eventually landed — the reservation is closed after all."""
    close_abandoned: int = 0
    """Reservations Core could not close at all. Each one is money held on the
    player's wallet until the User-side sweeper resolves it, so any value above
    zero is an alert, not a statistic."""
    settled_after_use: int = 0
    """Actions that failed *after* the Gateway had already waived billing for
    at least one of their calls. Refunding those would be a free generation, so
    they settle instead; a rising number is the signal to look at why actions
    are failing late."""
    released_uncovered: int = 0
    """Actions that *succeeded* without a single covered Gateway call. The
    Gateway billed those calls per call (verifier down, unknown charge id), so
    keeping the fixed charge as well would bill the same work twice; the
    reservation is refunded instead. A rising number means Core is stamping the
    interaction header for work the Gateway is not honouring — the fixed-price
    tier is silently running on per-call billing."""


@dataclass(slots=True)
class ActionChargeHandle:
    """A live action charge. Closed exactly once, by settle or release."""

    action_key: str
    charge_id: str
    interaction_id: str
    price_cr: float = 0.0
    closed: bool = field(default=False)
    """True only once the User service has *confirmed* the close. A failed
    settle leaves this ``False``: pretending a reservation is closed when it is
    still open upstream is exactly how money goes missing."""
    close_pending: bool = field(default=False)
    """A close is in flight (or queued for retry). Blocks a second attempt
    without lying about the outcome the way ``closed`` would."""
    # Built once and kept: the context carries the mutable usage record that
    # decides refund vs settle, so handing out a fresh copy per access would
    # throw that record away.
    context: InteractionContext = field(init=False)

    def __post_init__(self) -> None:
        self.context = InteractionContext(
            interaction_id=self.interaction_id, charge_id=self.charge_id,
        )

    @property
    def consumed(self) -> bool:
        """True once the Gateway has served a *covered* call under this charge.

        Covered is the operative word: a call the Gateway billed on its own
        (verifier failure, exhausted per-action budget) is already paid for
        per-call, so counting it here would keep the fixed charge as well and
        bill the same work twice.
        """
        return self.context.usage.consumed


def _finite_price(value: float | None) -> float | None:
    """A usable quote, or ``None``. Rejects bools, NaN and infinities so a
    malformed client field falls back to the cache instead of binding the
    charge to a number no price list could ever match."""
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    price = float(value)
    if price != price or price in (float("inf"), float("-inf")) or price < 0:
        return None
    return price


@dataclass(frozen=True, slots=True)
class _BillingTarget:
    """Who pays for this action, and under which tier's published prices."""

    tenant_id: str
    tier_name: str


class CloudActionBillingService:
    def __init__(
        self,
        *,
        client: ActionChargePort,
        profile_resolver: AccountRuntimeProfileResolverPort,
        operator_profiles: OperatorProfileRepositoryPort,
        pricing: QuotedPricePort | None = None,
        close_retry_delays: Sequence[float] = _CLOSE_RETRY_DELAYS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client
        self._profiles = profile_resolver
        self._operators = operator_profiles
        # Optional so self-host and every test that does not care about quote
        # binding keeps constructing the service with three arguments.
        self._pricing = pricing
        self._close_retry_delays = tuple(close_retry_delays)
        self._sleep = sleep
        self._pending_closes: set[asyncio.Task] = set()
        self.counters = ActionBillingCounters()

    async def begin(
        self,
        action_key: str,
        *,
        operator_id: str,
        action_kind: str = ACTION_KIND_USER_ACTION,
        quoted_price_cr: float | None = None,
        interaction_id: str | None = None,
    ) -> ActionChargeHandle | None:
        """Charge for one action, or ``None`` when this action is not billed.

        ``None`` covers every "carry on as before" case: a tier still on token
        billing, an operator with no hosted tenant, and a wallet outage.
        Callers must not branch on the reason — that is exactly the coupling
        this return shape removes.

        ``quoted_price_cr`` is the number the player's own client had on
        screen. It wins over Core's process-local cache, which is only a proxy
        for what was displayed and can be a price this player never saw (a
        second replica, a cache refreshed between the quote and the send).
        Omitted — an older client, or one with nothing quotable — falls back to
        the cache, which is the pre-R9 behaviour.

        Raises :class:`ActionPriceChanged` when the User service refuses the
        quote Core displayed. Foreground routes render it; background callers
        (quota overage) treat it as a quiet denial, because a price the player
        never saw cannot be re-confirmed by them.
        """
        target = await self._billable_target(
            operator_id, action_kind=action_kind,
        )
        if target is None:
            return None
        if interaction_id is None:
            interaction_id = new_interaction_id()
        quoted = _finite_price(quoted_price_cr)
        if quoted is None:
            quoted = self._quoted_price(
                tier_name=target.tier_name, action_key=action_key,
            )
        if quoted is None:
            # A cold cache has no number to bind, and the User service refuses
            # an unquoted charge for a priced action — which would demote this
            # whole action to a free run. Warm once and re-read; the steady
            # state never takes this branch.
            await self._warm_quotes()
            quoted = self._quoted_price(
                tier_name=target.tier_name, action_key=action_key,
            )
        try:
            charge = await self._client.charge(
                tenant_id=target.tenant_id,
                action_key=action_key,
                interaction_id=interaction_id,
                action_kind=action_kind,
                expected_price_cr=quoted,
            )
        except ActionChargeUnavailable:
            self.counters.charge_failed += 1
            _LOGGER.warning(
                "action charge unavailable (action=%s tenant=%s) — running "
                "this action uncharged",
                action_key, target.tenant_id, exc_info=True,
            )
            return None
        except ActionPriceChanged as exc:
            # The quote Core showed is stale by definition now, so drop it
            # before anything else can quote the same wrong number.
            self.counters.price_changed += 1
            self._invalidate_quotes()
            _LOGGER.info(
                "action price changed under a live session (action=%s "
                "quoted=%s current=%s) — nothing was reserved",
                action_key, exc.expected_price_cr, exc.current_price_cr,
            )
            raise
        except Exception:
            # An out-of-credits refusal is not an outage; it is the answer.
            self.counters.refused += 1
            raise
        if not charge.charge_id:
            # No active price for (tier, action_key) and nothing to name. The
            # User service has already alerted; Core must not invent one.
            self.counters.no_price += 1
            return None
        if charge.no_charge:
            # A priceless action now comes back as a real zero-amount charge.
            # Keeping the handle (and so the interaction header) is the whole
            # point: every call the action makes stays *covered* — free —
            # instead of falling back to per-call billing, which would charge
            # the player for an action the back office never priced.
            self.counters.no_price += 1
            _LOGGER.info(
                "action %s has no configured price — running it covered "
                "under zero-amount charge %s", action_key, charge.charge_id,
            )
        else:
            self.counters.charged += 1
        return ActionChargeHandle(
            action_key=action_key,
            charge_id=charge.charge_id,
            interaction_id=interaction_id,
            price_cr=charge.price_cr,
        )

    async def settle(
        self,
        handle: ActionChargeHandle | None,
        *,
        gateway_delivered: bool = True,
    ) -> None:
        """Close a successful action — by refunding it when nobody was covered.

        Success alone does not mean the fixed charge is owed. When the Gateway
        declines to waive a call (verifier outage, unknown charge id) it bills
        that call itself, per call, against the same wallet; settling the fixed
        charge on top would take the player's money twice for one action. So a
        handle that never saw a covered call is *released*, and the player
        keeps only the per-call bill they already paid.

        ``gateway_delivered=False`` is the one exception, and it belongs to the
        caller because only the caller knows what the action delivers: an
        action whose product is not a Gateway call at all (a LumeGram post
        funded by ``feed_post_overage`` — the underlying composition runs as
        background work the Gateway never covers, yet the post *is* published)
        has nothing to cross-check and settles on success as it always did.
        """
        if (
            gateway_delivered
            and handle is not None
            and not handle.closed
            and not handle.close_pending
            and not handle.consumed
        ):
            self.counters.released_uncovered += 1
            _LOGGER.warning(
                "action %s succeeded without a covered Gateway call "
                "(charge=%s) — refunding the fixed charge, the Gateway billed "
                "those calls itself", handle.action_key, handle.charge_id,
            )
            await self._close(handle, "release")
            return
        await self._close(handle, "settle")

    async def release(
        self,
        handle: ActionChargeHandle | None,
        *,
        settle_if_probed: bool = False,
    ) -> None:
        """Refund the action — unless the Gateway already served it.

        The refund test is "did this action produce any covered call", not "did
        the turn reach its end". Under ``action_fixed`` the Gateway waives
        per-call billing for everything inside the action, so once a covered
        call has been served the provider's tokens are spent and nobody else
        will ever charge for them; refunding then would make "send, then abort"
        an unlimited free-generation loop. A failure before the first covered
        call is a genuine refund and still gets one.

        ``settle_if_probed=True`` says this handle *cannot answer that test*:
        it was rebuilt from job params after a restart, so its usage record is
        empty no matter how much the dead process consumed. Reading that empty
        record as "nothing was served" would refund work the player already
        received, so the local check is skipped and the User service decides
        from the covered-call probe only it still holds.
        """
        if settle_if_probed:
            await self._close(handle, "release", settle_if_probed=True)
            return
        if (
            handle is not None
            and not handle.closed
            and not handle.close_pending
            and handle.consumed
        ):
            self.counters.settled_after_use += 1
            _LOGGER.info(
                "action failed after %d covered call(s) (action=%s charge=%s) "
                "— settling rather than refunding work already served",
                handle.context.usage.covered_calls, handle.action_key,
                handle.charge_id,
            )
            await self.settle(handle)
            return
        await self._close(handle, "release")

    async def wait_for_pending_closes(self) -> None:
        """Await the retry tasks still in flight.

        Exists for shutdown and for tests: the retries are deliberately off the
        request path, so without this there is no way to observe them.
        """
        while self._pending_closes:
            await asyncio.gather(
                *tuple(self._pending_closes), return_exceptions=True,
            )

    # -- closing a charge ----------------------------------------------

    async def _close(
        self,
        handle: ActionChargeHandle | None,
        verb: str,
        *,
        settle_if_probed: bool = False,
    ) -> None:
        if not self._claim(handle):
            return
        assert handle is not None
        try:
            await self._call_close(
                handle, verb, settle_if_probed=settle_if_probed,
            )
        except ActionChargeUnavailable:
            self._count_close_failure(verb)
            # Critically: ``closed`` stays False. The reservation is still open
            # on the User side, and a handle that claims otherwise would strand
            # it with nothing left to retry — the failure mode that made this a
            # release-blocker. ``close_pending`` (already set by ``_claim``)
            # stops a concurrent second attempt without making that claim.
            self.counters.close_retried += 1
            _LOGGER.warning(
                "action %s failed (action=%s charge=%s) — retrying off the "
                "request path", verb, handle.action_key, handle.charge_id,
                exc_info=True,
            )
            self._schedule_close_retry(
                handle, verb, settle_if_probed=settle_if_probed,
            )
            return
        handle.closed = True
        handle.close_pending = False

    async def _call_close(
        self,
        handle: ActionChargeHandle,
        verb: str,
        *,
        settle_if_probed: bool = False,
    ) -> None:
        if verb == "settle":
            await self._client.settle(handle.charge_id)
        else:
            await self._client.release(
                handle.charge_id, settle_if_probed=settle_if_probed,
            )

    def _schedule_close_retry(
        self,
        handle: ActionChargeHandle,
        verb: str,
        *,
        settle_if_probed: bool = False,
    ) -> None:
        try:
            task = asyncio.get_running_loop().create_task(
                self._retry_close(
                    handle, verb, settle_if_probed=settle_if_probed,
                ),
            )
        except RuntimeError:  # pragma: no cover — no loop (sync test harness)
            handle.close_pending = False
            self.counters.close_abandoned += 1
            return
        self._pending_closes.add(task)
        task.add_done_callback(self._pending_closes.discard)

    async def _retry_close(
        self,
        handle: ActionChargeHandle,
        verb: str,
        *,
        settle_if_probed: bool = False,
    ) -> None:
        for delay in self._close_retry_delays:
            await self._sleep(delay)
            try:
                await self._call_close(
                    handle, verb, settle_if_probed=settle_if_probed,
                )
            except ActionChargeUnavailable:
                continue
            except Exception:  # noqa: BLE001 — a retry must never escape
                _LOGGER.exception(
                    "action %s retry raised (action=%s charge=%s)",
                    verb, handle.action_key, handle.charge_id,
                )
                break
            handle.closed = True
            handle.close_pending = False
            self.counters.close_recovered += 1
            return
        handle.close_pending = False
        self.counters.close_abandoned += 1
        # Left open on purpose: the User-side sweeper owns it from here and
        # decides settle-vs-release from its own covered-call probe, which is
        # the only place that still knows whether anything was served.
        _LOGGER.error(
            "action %s abandoned after %d retries (action=%s charge=%s) — the "
            "reservation stays open for the upstream sweeper",
            verb, len(self._close_retry_delays), handle.action_key,
            handle.charge_id,
        )

    def _count_close_failure(self, verb: str) -> None:
        if verb == "settle":
            self.counters.settle_failed += 1
        else:
            self.counters.release_failed += 1

    @asynccontextmanager
    async def action(
        self,
        action_key: str,
        *,
        operator_id: str,
        action_kind: str = ACTION_KIND_USER_ACTION,
        quoted_price_cr: float | None = None,
        interaction_id: str | None = None,
    ) -> AsyncIterator[ActionChargeHandle | None]:
        """Charge, scope, and close one action around the ``async with`` body.

        The scope is entered even when the handle is ``None`` — as an explicit
        *clear*, so a nested uncharged action cannot inherit the outer action's
        charge id and be billed to it.
        """
        handle = await self.begin(
            action_key,
            operator_id=operator_id,
            action_kind=action_kind,
            quoted_price_cr=quoted_price_cr,
            interaction_id=interaction_id,
        )
        with interaction_scope(handle.context if handle else None):
            try:
                yield handle
            except BaseException:
                await self.release(handle)
                raise
        await self.settle(handle)

    # -- who is billed, and at what quoted price -----------------------

    def _quoted_price(
        self, *, tier_name: str, action_key: str,
    ) -> float | None:
        """The price Core last showed the player for this action.

        Read from the cache without refreshing: the number that binds the
        charge has to be the one the SPA was actually served, and a fresh fetch
        here would both add a wallet round-trip to every action and quote a
        price nobody ever saw.
        """
        if self._pricing is None or not tier_name:
            return None
        try:
            return self._pricing.quoted_price_cr(
                tier_name=tier_name, action_key=action_key,
            )
        except Exception:  # noqa: BLE001 — a bad cache must not block play
            _LOGGER.warning(
                "could not read the quoted price for %s/%s — charging unbound",
                tier_name, action_key, exc_info=True,
            )
            return None

    def _invalidate_quotes(self) -> None:
        if self._pricing is None:
            return
        try:
            self._pricing.invalidate()
        except Exception:  # noqa: BLE001
            _LOGGER.warning("could not invalidate the price cache", exc_info=True)

    async def _warm_quotes(self) -> None:
        if self._pricing is None:
            return
        try:
            await self._pricing.warm()
        except Exception:  # noqa: BLE001 — a pricing outage must not block play
            _LOGGER.warning(
                "could not warm the price cache — charging unbound",
                exc_info=True,
            )

    async def _billable_target(
        self, operator_id: str, *, action_kind: str = ACTION_KIND_USER_ACTION,
    ) -> _BillingTarget | None:
        """The hosted tenant to charge, or ``None`` if this action is not."""
        overage = action_kind == ACTION_KIND_QUOTA_OVERAGE
        if (
            not overage
            and current_generation_trigger() is not GenerationTrigger.USER_ACTION
        ):
            # R1–R4 red line: the player is never charged for work they did
            # not ask for. The same entry points are reachable from autonomous
            # paths (the pending-follow-up dispatcher re-runs a full turn, the
            # proactive scheduler drafts portraits), and those already scope
            # themselves as background for the Gateway's own no-charge policy.
            return None
        cleaned = (operator_id or "").strip()
        if not cleaned:
            return None
        profile = await self._profiles.resolve_for_operator(cleaned)
        if not profile.uses_action_pricing:
            # Fixed-price billing is the precondition for *every* action charge,
            # overage included. A ``token_floating`` tier already meters the
            # extra generation per Gateway call, so adding an overage price on
            # top would bill the same picture twice — and the User service
            # refuses such a charge anyway (``fixedPriceProfile``), which would
            # otherwise leave the player looking at a switch that can never
            # succeed.
            return None
        if overage and not profile.overage_enabled:
            # AP4's owner-ratified exception to the red line above: a quota
            # overage IS the player's own instruction, given in advance at the
            # settings switch rather than at the moment of spend, which is why
            # ``feed_post_overage`` may fund an autonomous background post.
            # ``QuotaOverageService`` owns the other half (the player's switch
            # and the daily ceiling); this check exists so no caller can ever
            # reach a tier that never opened overage at all.
            return None
        operator = await self._operators.get(cleaned)
        tenant_id = (getattr(operator, "cloud_tenant_id", None) or "").strip()
        if not tenant_id:
            _LOGGER.warning(
                "operator %s resolves to an action-priced tier but has no "
                "cloud tenant — running this action uncharged", cleaned,
            )
            return None
        return _BillingTarget(
            tenant_id=tenant_id, tier_name=(profile.name or "").strip(),
        )

    @staticmethod
    def _claim(handle: ActionChargeHandle | None) -> bool:
        """Take ownership of closing ``handle``; ``False`` if already claimed.

        Settle and release are both reachable from the abandoned-stream path
        and the finish path, so single-shot closing is a correctness
        requirement, not defensive coding. ``close_pending`` extends that to
        the retry window: a close that is still being attempted must not be
        started a second time, but it must not be reported as done either.
        """
        if handle is None or handle.closed or handle.close_pending:
            return False
        handle.close_pending = True
        return True


class NullActionBillingService:
    """Null object — nothing is ever charged.

    Lets every instrumented entry point depend on a billing service
    unconditionally instead of scattering ``if is None`` branches that only
    fire in self-host and tests. The real service returns the same ``None``
    handle for any tier still on token billing, so behaviour is identical.
    """

    async def begin(
        self,
        action_key: str,
        *,
        operator_id: str,
        action_kind: str = ACTION_KIND_USER_ACTION,
        quoted_price_cr: float | None = None,
        interaction_id: str | None = None,
    ) -> ActionChargeHandle | None:
        return None

    async def settle(
        self,
        handle: ActionChargeHandle | None,
        *,
        gateway_delivered: bool = True,
    ) -> None:
        return None

    async def release(
        self,
        handle: ActionChargeHandle | None,
        *,
        settle_if_probed: bool = False,
    ) -> None:
        return None

    async def wait_for_pending_closes(self) -> None:
        return None

    @asynccontextmanager
    async def action(
        self,
        action_key: str,
        *,
        operator_id: str,
        action_kind: str = ACTION_KIND_USER_ACTION,
        quoted_price_cr: float | None = None,
        interaction_id: str | None = None,
    ) -> AsyncIterator[ActionChargeHandle | None]:
        yield None
