"""Shared eligible-binding resolution for the self-host proactive sink.

Extracted verbatim from ``ProactiveDispatcher._find_eligible_binding`` so BOTH
the dispatcher (which needs the binding for its pre-decider NO_BINDING gate, the
tool-invocation conversation id, and the SENT ``binding_id`` audit) and the
:class:`LocalMessagingProactiveDeliveryAdapter` (which delivers to it) resolve
the same binding through one code path — no drift, no duplicated policy.
"""

from __future__ import annotations

from dataclasses import dataclass

from kokoro_link.contracts.messaging import (
    ChannelBindingRepositoryPort,
    MessagingAccountRepositoryPort,
)
from kokoro_link.domain.entities.channel_binding import ChannelBinding
from kokoro_link.domain.entities.messaging_account import MessagingAccount


@dataclass(frozen=True, slots=True)
class ResolvedProactiveSink:
    """ONE resolution of "where may this character's push go", kept.

    :func:`find_eligible_proactive_binding` answers from live rows, so two
    calls minutes apart are two different answers. Anything that *decides*
    something from the first answer (may this message carry the owner's own
    declaration?) and only *sends* after a long window (an LLM compose with a
    tool loop in it) must carry this snapshot across the gap instead of
    asking again — otherwise a binding whose ``updated_at`` was bumped
    mid-compose can promote itself to the head of the ordering and receive a
    message that was cleared for somebody else entirely.

    ``binding is None`` is a resolution too, and the load-bearing one: it
    records that the gate saw NO external sink (web-only character), which is
    exactly the state that makes disclosure permissible. A delivery carrying
    that verdict must therefore not go looking for a binding either — one
    that appeared during the window was never gated.
    """

    binding: ChannelBinding | None
    account: MessagingAccount | None

    @classmethod
    def of(
        cls,
        eligible: tuple[ChannelBinding, MessagingAccount] | None,
    ) -> "ResolvedProactiveSink":
        """Wrap a :func:`find_eligible_proactive_binding` result."""
        if eligible is None:
            return cls(binding=None, account=None)
        binding, account = eligible
        return cls(binding=binding, account=account)

    @property
    def eligible(self) -> tuple[ChannelBinding, MessagingAccount] | None:
        """The pinned pair in the shape the resolution helper returns."""
        if self.binding is None or self.account is None:
            return None
        return (self.binding, self.account)

    def confirm_against(
        self,
        candidates: tuple[tuple[ChannelBinding, MessagingAccount], ...],
    ) -> tuple[ChannelBinding, MessagingAccount] | None:
        """This pin, re-confirmed against a *fresh* eligibility listing.

        Two failures are possible in the window between the gate and the
        send, and they need opposite answers:

        * A binding rose to the head of the ordering that was never gated —
          answered by looking the pin up **by id** instead of taking the
          head again.
        * The pinned binding was switched off, or its account was — the
          owner withdrew the endpoint, and a snapshot taken before that is
          not a licence to use it. Answered by requiring the pin to still be
          in ``candidates``, i.e. still eligible on the rows as they stand
          now.

        The pair returned is the **pinned snapshot**, not the fresh row: a
        binding re-pointed at another chat mid-window must not carry the
        message to the new destination either (the L12 rule the local
        delivery adapter already implements). ``None`` when the pin is empty
        or no longer eligible — nothing external to send to.
        """
        pinned = self.eligible
        if pinned is None:
            return None
        binding_id = pinned[0].id
        for binding, _account in candidates:
            if binding.id == binding_id:
                return pinned
        return None


async def list_eligible_proactive_bindings(
    *,
    account_repository: MessagingAccountRepositoryPort,
    binding_repository: ChannelBindingRepositoryPort,
    character_id: str,
) -> tuple[tuple[ChannelBinding, MessagingAccount], ...]:
    """Every enabled ``accepts_proactive`` binding, most-recently-touched first.

    The whole set rather than just the winner, because a caller holding a
    pinned sink needs to ask "is *mine* still eligible?" — a question the head
    of the ordering cannot answer.
    """

    accounts = await account_repository.list_for_character(character_id)
    candidates: list[tuple[ChannelBinding, MessagingAccount]] = []
    for account in accounts:
        if not account.enabled:
            continue
        bindings = await binding_repository.list_for_account(account.id)
        for binding in bindings:
            if binding.enabled and binding.accepts_proactive:
                candidates.append((binding, account))
    # Prefer the most recently touched binding.
    candidates.sort(key=lambda pair: pair[0].updated_at, reverse=True)
    return tuple(candidates)


async def find_eligible_proactive_binding(
    *,
    account_repository: MessagingAccountRepositoryPort,
    binding_repository: ChannelBindingRepositoryPort,
    character_id: str,
) -> tuple[ChannelBinding, MessagingAccount] | None:
    """The most-recently-touched enabled binding that ``accepts_proactive``.

    ``None`` when the character has no enabled account with a proactive-opted
    binding — i.e. there is nowhere external to push.
    """

    candidates = await list_eligible_proactive_bindings(
        account_repository=account_repository,
        binding_repository=binding_repository,
        character_id=character_id,
    )
    return candidates[0] if candidates else None
