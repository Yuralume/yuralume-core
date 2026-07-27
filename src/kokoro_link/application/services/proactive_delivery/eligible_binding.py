"""Shared eligible-binding resolution for the self-host proactive sink.

Extracted verbatim from ``ProactiveDispatcher._find_eligible_binding`` so BOTH
the dispatcher (which needs the binding for its pre-decider NO_BINDING gate, the
tool-invocation conversation id, and the SENT ``binding_id`` audit) and the
:class:`LocalMessagingProactiveDeliveryAdapter` (which delivers to it) resolve
the same binding through one code path — no drift, no duplicated policy.
"""

from __future__ import annotations

from kokoro_link.contracts.messaging import (
    ChannelBindingRepositoryPort,
    MessagingAccountRepositoryPort,
)
from kokoro_link.domain.entities.channel_binding import ChannelBinding
from kokoro_link.domain.entities.messaging_account import MessagingAccount


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

    accounts = await account_repository.list_for_character(character_id)
    candidates: list[tuple[ChannelBinding, MessagingAccount]] = []
    for account in accounts:
        if not account.enabled:
            continue
        bindings = await binding_repository.list_for_account(account.id)
        for binding in bindings:
            if binding.enabled and binding.accepts_proactive:
                candidates.append((binding, account))
    if not candidates:
        return None
    # Prefer the most recently touched binding.
    candidates.sort(key=lambda pair: pair[0].updated_at, reverse=True)
    return candidates[0]
