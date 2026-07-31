"""Core-side view of hosted **action-level** credit charging (AP2).

Under ``billing_shape=token_floating`` every Gateway call reserves and settles
its own credits, so one chat turn produces as many ledger rows as it made HTTP
calls. Under ``billing_shape=action_fixed`` the *player action* is the billing
unit: Core charges the configured fixed price once at the action's entry
point, stamps every call the action makes with the resulting interaction scope
(see :mod:`kokoro_link.contracts.interaction_context`), and settles at the end
— one action, one ledger row, a price the player knew before pressing the
button.

Two failure policies, deliberately different:

* **Cloud unreachable / malformed answer** → :class:`ActionChargeUnavailable`.
  The billing service logs, counts, and lets the action run uncharged. Never
  blocking play on the wallet service is the same fail-open the User service
  applies to a missing price; a player mid-sentence must not be bricked by a
  billing outage.
* **Out of credits** → the upstream 402 becomes an
  :class:`~kokoro_link.infrastructure.llm.cloud_refusal.ExpectedCloudRefusal`
  with code ``insufficient_credits`` and travels the U4 structured-error path
  already wired through every foreground route.
* **The price moved** → the upstream 409 becomes :class:`ActionPriceChanged`.
  Every charge is bound to the price Core displayed (``expected_price_cr``), so
  a back-office edit mid-session refuses the charge instead of billing a number
  the player never agreed to.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol

from kokoro_link.contracts.interaction_context import InteractionContext

#: ``action_kind`` values accepted by the User ledger CHECK constraint.
ACTION_KIND_USER_ACTION = "user_action"
ACTION_KIND_QUOTA_OVERAGE = "quota_overage"

#: First-wave ``charging_mode=action`` keys from ``contracts/action-catalog.json``.
ACTION_CHAT = "chat"
ACTION_IMAGE_CHAT_TOOL = "image_chat_tool"
ACTION_IMAGE_PORTRAIT = "image_portrait"
ACTION_CHARACTER_DRAFT = "character_draft"
ACTION_CHAT_IMAGE_OVERAGE = "chat_image_overage"
ACTION_FEED_POST_OVERAGE = "feed_post_overage"

#: Second-wave Creator Studio keys (AP §8.3 R-3). A fusion story is one
#: player action no matter how many stages the pipeline runs internally, so
#: ``fusion_story`` covers the plan, every beat, the critic and the polish
#: rounds under a single fixed price. Re-running one stage afterwards is a
#: separate, cheaper action — the player pressed a different button — hence
#: ``fusion_story_iterate`` for outline / beat / polish iteration, priced per
#: iteration and sharing the same underlying features.
ACTION_FUSION_STORY = "fusion_story"
ACTION_FUSION_STORY_ITERATE = "fusion_story_iterate"


#: Actions already paid for by an *enclosing* charge, keyed by the action key
#: their entry point would otherwise raise. AP4's quota overage is the only
#: producer today: the over-quota picture is bought once, at the overage price
#: (which the plan requires to be >= the base price), so the image tool must
#: not also raise its own ``image_chat_tool`` charge for the same picture.
_PREPAID_ACTIONS: ContextVar[Mapping[str, InteractionContext]] = ContextVar(
    "yuralume_prepaid_actions", default={},
)


@contextmanager
def prepaid_action_scope(
    action_key: str, context: InteractionContext | None,
) -> Iterator[None]:
    """Declare (or explicitly withdraw) prepayment of ``action_key``.

    ``None`` is meaningful and is the reason this is a scope rather than a set:
    a second, unpaid invocation nested inside the first must not inherit the
    prepayment and run for free.
    """
    current = dict(_PREPAID_ACTIONS.get())
    if context is None:
        current.pop(action_key, None)
    else:
        current[action_key] = context
    token = _PREPAID_ACTIONS.set(current)
    try:
        yield
    finally:
        _PREPAID_ACTIONS.reset(token)


def prepaid_action(action_key: str) -> InteractionContext | None:
    """The enclosing charge that already paid for ``action_key``, if any."""
    return _PREPAID_ACTIONS.get().get(action_key)


#: The prices the *player's own client* had on screen when it sent this
#: request, keyed by action key. Core's process-local price cache is a
#: reasonable stand-in, but under several replicas (or right after a cache
#: refresh) it can hold a number this particular player was never shown, and
#: binding the charge to that defeats the point of binding it at all. The
#: client's own quote is therefore preferred wherever it sends one. Under-
#: reporting buys nothing: the User service answers ``409 price_changed``, it
#: never charges less than the published price.
_CLIENT_QUOTED_PRICES: ContextVar[Mapping[str, float]] = ContextVar(
    "yuralume_client_quoted_prices", default={},
)


@contextmanager
def client_quoted_price_scope(
    prices: Mapping[str, float | None],
) -> Iterator[None]:
    """Bind the quotes one client request carried, for this task tree.

    A scope rather than an argument because the actions a request spawns are
    not all raised by the request handler: a chat turn's ``image_chat_tool``
    charge is raised deep inside the tool cycle, and threading the number
    through the whole tool contract to reach it would put billing state in
    every tool's signature. ``None`` entries are dropped, so "the client had
    no quotable price" and "the client is an old build" stay one state.
    """
    quoted = {
        key: float(value)
        for key, value in prices.items()
        if key and isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    token = _CLIENT_QUOTED_PRICES.set(quoted)
    try:
        yield
    finally:
        _CLIENT_QUOTED_PRICES.reset(token)


def client_quoted_price(action_key: str) -> float | None:
    """The price the player's client displayed for ``action_key``, if any."""
    return _CLIENT_QUOTED_PRICES.get().get(action_key)


#: Machine-readable code for "the price moved between the quote and the
#: charge". Travels the same structured-detail path as
#: ``insufficient_credits`` (see :mod:`kokoro_link.api.routes._cloud_errors`).
PRICE_CHANGED_CODE = "price_changed"

#: Player-facing default when the User service sends no message of its own.
#: The SPA renders its own localized copy off ``code``; this is the fallback.
PRICE_CHANGED_MESSAGE = "價格已更新，請重新送出"


class ActionChargeUnavailable(RuntimeError):
    """The charge could not be recorded — transport, 5xx, or malformed body.

    Always means "we do not know whether this was billable", never "the player
    cannot pay" (that is the 402 refusal path).
    """


class ActionPriceChanged(RuntimeError):
    """The back-office price moved after Core quoted it to the player.

    Core binds every charge to the price it displayed (``expected_price_cr``);
    the User service answers ``409`` rather than silently charging a number the
    player never agreed to. Nothing was reserved, so there is no charge to
    settle or release — the action simply did not start.

    A foreground action turns this into the structured ``price_changed``
    refusal the SPA renders as "the price was updated, please send again"; a
    background one (quota overage) fails quietly, because a price the player
    was never shown cannot be re-confirmed by them.
    """

    def __init__(
        self,
        *,
        action_key: str = "",
        expected_price_cr: float | None = None,
        current_price_cr: float | None = None,
        message: str = "",
    ) -> None:
        super().__init__(
            message
            or (
                f"price for {action_key or 'this action'} changed "
                f"({expected_price_cr} → {current_price_cr})"
            ),
        )
        self.code = PRICE_CHANGED_CODE
        self.action_key = action_key
        self.expected_price_cr = expected_price_cr
        self.current_price_cr = current_price_cr
        self.reason = message or PRICE_CHANGED_MESSAGE


@dataclass(frozen=True, slots=True)
class ActionCharge:
    """What the User service recorded for one action."""

    charge_id: str
    price_cr: float = 0.0
    no_charge: bool = False
    """True when no active price is configured for ``(tier, action_key)``.
    The User service alerts and writes no ledger row; Core carries on."""
    gift: float = 0.0
    purchased: float = 0.0


class ActionChargePort(Protocol):
    async def charge(
        self,
        *,
        tenant_id: str,
        action_key: str,
        interaction_id: str,
        action_kind: str = ACTION_KIND_USER_ACTION,
        expected_price_cr: float | None = None,
    ) -> ActionCharge:
        """Reserve the configured fixed price for one player action.

        ``expected_price_cr`` is the price Core last quoted to the player. The
        User service refuses (``409``) when it no longer matches, which is what
        stops a back-office edit mid-session from charging a number nobody was
        shown. ``None`` means "Core has no quote to bind" (empty price cache)
        and deliberately keeps the old unbound behaviour rather than blocking
        play.
        """

    async def settle(self, charge_id: str) -> None:
        """Close the reservation at its reserved amount (zero delta)."""

    async def release(
        self, charge_id: str, *, settle_if_probed: bool = False,
    ) -> None:
        """Return the whole reservation — the action did not happen.

        ``settle_if_probed`` hands the settle-or-release decision back to the
        User service. Core asks for it when the caller *cannot* know whether
        any covered call was served under this charge: a handle rebuilt from
        job params after a restart, a superseded row, a resumed pipeline. The
        covered facts belong to the process that died, and only the ledger's
        own ``covered_probe_at`` still remembers them — so the server settles a
        charge it can see was probed and releases one it cannot, and Core stops
        guessing. The answer names which it did (``{"closed": "settled" |
        "released"}``); the flag is otherwise a plain release.
        """


class QuotedPricePort(Protocol):
    """The price list Core showed the player, as the charge path sees it."""

    def quoted_price_cr(
        self, *, tier_name: str, action_key: str,
    ) -> float | None:
        """The cached quote for one action, or ``None`` if nothing is known."""

    def invalidate(self) -> None:
        """Drop the cache — the published prices are known to have moved."""

    async def warm(self) -> None:
        """Fetch the published list once when nothing is cached yet.

        The charge path calls this only on a cold cache: the User service
        refuses an unquoted charge for a priced action, and running the whole
        action uncharged instead would hand it out free. In the steady state
        the binding number is still whatever the SPA was last served."""
