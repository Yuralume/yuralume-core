"""Fail-closed owner resolution for connector-downloaded inbound media."""

from __future__ import annotations

import logging

from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.entities.messaging_account import MessagingAccount


class MessagingMediaOwnerUnavailable(RuntimeError):
    """The connector cannot prove which user namespace owns inbound media."""


async def resolve_messaging_media_owner(
    *,
    characters: CharacterRepositoryPort,
    account: MessagingAccount,
    logger: logging.Logger,
) -> str:
    """Resolve the account character's owner or fail before any media write."""
    try:
        character = await characters.get(account.character_id)
    except Exception as exc:
        logger.error(
            "messaging media owner lookup failed account=%s error_type=%s",
            account.id,
            type(exc).__name__,
        )
        raise MessagingMediaOwnerUnavailable(
            "messaging media owner unavailable",
        ) from exc
    owner_id = str(getattr(character, "user_id", "") or "").strip()
    if not owner_id:
        logger.error(
            "messaging media owner missing account=%s character=%s",
            account.id,
            account.character_id,
        )
        raise MessagingMediaOwnerUnavailable(
            "messaging media owner unavailable",
        )
    return owner_id
