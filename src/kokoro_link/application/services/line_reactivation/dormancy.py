"""Is this character (still) a legitimate recall target? (LR, D1)

The candidate listing and the campaign runner ask the same question at
two different instants, and the gap between them is the whole reason this
module exists. An operator loads the list, reads it, selects, confirms —
and a serial walk over a few hundred characters then takes as long as a
few hundred model calls take. Somewhere in that window a player can come
back and start typing. Re-asserting D1 immediately before each send is
what keeps a recall message from landing in the middle of a live
conversation.

The predicate itself is the *scheduler's*, imported rather than restated:
"dormant" has to mean one thing across the deployment, or a tuning change
to the background scheduler would silently change who gets called back
and nothing would turn red.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime

from kokoro_link.application.services.account_runtime_profile_cache import (
    resolve_profile_or_none,
)
# The scheduler's own dormancy predicate (NF4). Imported, never copied.
from kokoro_link.application.services.due_job_scheduler import (
    _default_dormancy_resolver as _scheduler_dormancy_rule,
)
from kokoro_link.contracts.account_runtime_profile import (
    AccountRuntimeProfileResolverPort,
)
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.account_runtime_profile import (
    AccountRuntimeProfile,
)

SKIP_REASON_NOT_FOUND = "character_not_found"
"""Deleted between selection and send."""

SKIP_REASON_FROZEN = "frozen"
SKIP_REASON_SUBSCRIPTION_LOCKED = "subscription_locked"

SKIP_REASON_NO_POLICY = "no_dormancy_policy"
"""The control plane could not be asked, or the tier carries no dormancy
window. Fail-closed: without a policy there is no way to say the player
is still away, and a recall message is not the thing to send on a guess.
Recorded rather than retried (D6) — the operator sees the reason and can
open a new campaign once the control plane answers again."""

SKIP_REASON_NO_LONGER_DORMANT = "no_longer_dormant"
"""The player came back between the listing and this row's turn. The one
this whole module exists for."""


async def is_dormant_by_scheduler_rule(
    character: Character,
    profile: AccountRuntimeProfile,
    moment: datetime,
) -> bool:
    """Delegate to the scheduler's own NF4 predicate.

    Kept as a named seam rather than an inline call so the coupling is
    visible: this feature is only correct while it agrees with what the
    background scheduler considers dormant.
    """

    return await _scheduler_dormancy_rule(character, profile, moment)


class RecallTargetGuard:
    """Re-asserts D1 for one character, immediately before its send.

    Only the checks Core owns and that can flip inside the campaign
    window: existence, the two site-level locks, and dormancy. Channel
    eligibility is deliberately *not* re-checked here — the dispatcher
    resolves the cloud identity and the channel arbitrates the binding
    itself, so a second copy of that check would be a second place for
    it to drift while adding a network round trip per character.
    """

    __slots__ = ("_characters", "_profile_resolver", "_dormancy_rule")

    def __init__(
        self,
        *,
        character_repository: CharacterRepositoryPort,
        profile_resolver: AccountRuntimeProfileResolverPort,
        dormancy_rule: Callable[
            [Character, AccountRuntimeProfile, datetime], Awaitable[bool]
        ] = is_dormant_by_scheduler_rule,
    ) -> None:
        self._characters = character_repository
        self._profile_resolver = profile_resolver
        self._dormancy_rule = dormancy_rule

    async def check(self, character_id: str, *, now: datetime) -> str | None:
        """``None`` when the character may be messaged; else a reason code.

        The reason codes are the ``detail`` an operator reads next to a
        skipped row, so they name the *situation* ("no_longer_dormant"),
        never the code path that noticed it.
        """

        character = await self._characters.get(character_id)
        if character is None:
            return SKIP_REASON_NOT_FOUND
        if getattr(character, "frozen", False):
            return SKIP_REASON_FROZEN
        if getattr(character, "subscription_locked", False):
            return SKIP_REASON_SUBSCRIPTION_LOCKED
        profile = await self._resolve_profile(character.user_id)
        if profile is None or profile.background_dormancy_days is None:
            return SKIP_REASON_NO_POLICY
        if not await self._dormancy_rule(character, profile, now):
            return SKIP_REASON_NO_LONGER_DORMANT
        return None

    async def _resolve_profile(
        self, operator_id: str,
    ) -> AccountRuntimeProfile | None:
        # ``resolve_profile_or_none`` answers ``None`` for a resolver
        # failure, and the scheduler reads that as "no dormancy, no
        # multiplier" — the safe reading when the question is whether to
        # *skip* background work. Here the same ``None`` has to mean "do
        # not send", which is why :meth:`check` maps it to a skip instead
        # of falling through to a permissive default.
        return await resolve_profile_or_none(
            self._profile_resolver, operator_id,
        )


__all__ = [
    "SKIP_REASON_FROZEN",
    "SKIP_REASON_NOT_FOUND",
    "SKIP_REASON_NO_LONGER_DORMANT",
    "SKIP_REASON_NO_POLICY",
    "SKIP_REASON_SUBSCRIPTION_LOCKED",
    "RecallTargetGuard",
    "is_dormant_by_scheduler_rule",
]
