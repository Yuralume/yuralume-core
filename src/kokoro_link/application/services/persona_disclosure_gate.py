"""Who is allowed to hear text that is *about the account owner*.

Two different bodies of owner-attached text ride outbound messages: the
persona this character inferred from talking to the player, and the
persona note the player declared for the relationship. Both answer the
question "what do we know about the human on the other end", so both are
disclosure — and disclosure needs one rule, in one place, or the two
surfaces drift apart the first time either is touched.

The rule was already being applied on the inbound side by the messaging
dispatcher (which is where persona *learning* had to be suppressed); it
lives here now so the outbound composers can apply the same test without
importing a private helper out of a sibling service.
"""

from __future__ import annotations

from kokoro_link.domain.entities.messaging_account import MessagingAccount


def persona_safe_for_account(account: MessagingAccount) -> bool:
    """True only when exactly one external human is identified.

    An empty allowlist means "accept anyone" and a multi-entry allowlist
    can represent a group / shared account. Inbound, both cases would
    write several humans into the same ``DEFAULT_OPERATOR_ID``. Outbound,
    both cases would read the owner's declared setting out loud to
    whoever happens to be in that chat — the same defect pointed the
    other way, so it takes the same answer.
    """
    senders = tuple(ref for ref in account.allowed_sender_refs if ref)
    return len(senders) == 1


__all__ = ["persona_safe_for_account"]
