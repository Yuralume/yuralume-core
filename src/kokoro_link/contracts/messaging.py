"""Ports for messaging accounts, bindings, and channel adapters.

Adapters translate platform-specific payloads into ``InboundMessage`` and
accept ``OutboundMessage`` for delivery. Credentials live on the
``MessagingAccount`` and are provided by the dispatcher at call time so
one adapter class can serve many accounts.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from kokoro_link.domain.entities.channel_binding import ChannelBinding
from kokoro_link.domain.entities.messaging_account import MessagingAccount
from kokoro_link.domain.value_objects.platform import Platform


@dataclass(frozen=True, slots=True)
class ParsedInbound:
    """What a platform parser produces from a raw webhook payload.

    Account identity is resolved at the route layer (from the URL slug)
    and spliced in before the dispatcher sees the message, so parsers
    can stay platform-only and never need to know which account they
    belong to.

    ``photo_refs`` are opaque platform-specific identifiers for images
    attached to the message (Telegram ``file_id``, LINE ``message.id``).
    The route layer uses them to pull bytes via the bot API and save
    them to uploads; the resulting URLs end up on
    ``InboundMessage.attachment_urls``. Parsers don't fetch bytes
    themselves — they have no credentials.
    """

    platform: Platform
    chat_ref: str
    sender_ref: str
    text: str
    platform_message_id: str
    received_at: datetime
    photo_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class InboundMessage:
    platform: Platform
    account_id: str
    chat_ref: str
    sender_ref: str
    text: str
    platform_message_id: str
    received_at: datetime
    attachment_urls: tuple[str, ...] = ()

    @classmethod
    def from_parsed(
        cls,
        parsed: ParsedInbound,
        *,
        account_id: str,
        attachment_urls: tuple[str, ...] = (),
    ) -> "InboundMessage":
        return cls(
            platform=parsed.platform,
            account_id=account_id,
            chat_ref=parsed.chat_ref,
            sender_ref=parsed.sender_ref,
            text=parsed.text,
            platform_message_id=parsed.platform_message_id,
            received_at=parsed.received_at,
            attachment_urls=attachment_urls,
        )


@dataclass(frozen=True, slots=True)
class OutboundAttachment:
    """A file the adapter should deliver alongside the text.

    ``kind`` drives which platform endpoint we reach for — e.g.
    Telegram ``sendPhoto`` vs ``sendDocument``. ``url`` is **already
    public-facing** (served by our ``/uploads`` mount or some CDN);
    the adapter passes the URL straight to the platform so we don't
    stream binary through ourselves.
    """

    kind: str
    url: str
    mime_type: str = "application/octet-stream"
    caption: str | None = None


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    platform: Platform
    chat_ref: str
    text: str
    credentials: dict[str, str]
    attachments: tuple[OutboundAttachment, ...] = ()
    """Files to deliver with the message. Adapters that can't ship a
    given ``kind`` fall back to appending the URL to ``text`` so the
    recipient at least gets a link."""
    locale: str = "zh-TW"
    """Operator's content language (BCP 47). Adapters use it to localize
    the *deterministic* channel-wrapper text they compose themselves
    (attachment labels, fallback notes) so a non-Chinese operator's
    players don't receive zh-TW system strings. The character's own
    reply text is untouched — it was already produced in the operator's
    language upstream. Defaults to the ship-first ``zh-TW`` so callers
    that don't know the operator language keep the prior behaviour."""


class ChannelAdapterPort(Protocol):
    @property
    def platform(self) -> Platform: ...

    async def send(self, message: OutboundMessage) -> None: ...


class TelegramPollingPort(Protocol):
    async def get_updates(
        self,
        *,
        bot_token: str,
        offset: int | None = None,
        timeout_seconds: int = 25,
        limit: int = 100,
    ) -> dict[str, Any]: ...

    async def delete_webhook(
        self,
        *,
        bot_token: str,
        drop_pending_updates: bool = False,
    ) -> dict[str, Any]: ...


class MessagingAccountRepositoryPort(Protocol):
    async def get(self, account_id: str) -> MessagingAccount | None: ...

    async def find_by_slug(self, webhook_slug: str) -> MessagingAccount | None: ...

    async def find_for_character(
        self, platform: Platform, character_id: str,
    ) -> MessagingAccount | None: ...

    async def list_for_character(
        self, character_id: str,
    ) -> list[MessagingAccount]: ...

    async def list_all(self) -> list[MessagingAccount]: ...

    async def list_polling_candidates(self) -> list[MessagingAccount]: ...

    async def list_gateway_candidates(self) -> list[MessagingAccount]: ...

    async def save(self, account: MessagingAccount) -> None: ...

    async def try_acquire_polling_lock(
        self,
        account_id: str,
        *,
        owner_id: str,
        now: datetime,
        ttl: timedelta,
    ) -> MessagingAccount | None: ...

    async def try_acquire_gateway_lock(
        self,
        account_id: str,
        *,
        owner_id: str,
        now: datetime,
        ttl: timedelta,
    ) -> MessagingAccount | None: ...

    async def release_polling_lock(
        self, account_id: str, *, owner_id: str,
    ) -> bool: ...

    async def release_gateway_lock(
        self, account_id: str, *, owner_id: str,
    ) -> bool: ...

    async def advance_polling_offset(
        self,
        account_id: str,
        *,
        owner_id: str,
        offset: int,
        at: datetime,
    ) -> bool: ...

    async def mark_polling_success(
        self,
        account_id: str,
        *,
        owner_id: str,
        at: datetime,
    ) -> bool: ...

    async def mark_gateway_success(
        self,
        account_id: str,
        *,
        owner_id: str,
        at: datetime,
    ) -> bool: ...

    async def record_polling_error(
        self,
        account_id: str,
        *,
        owner_id: str,
        error: str,
        at: datetime,
    ) -> bool: ...

    async def record_gateway_error(
        self,
        account_id: str,
        *,
        owner_id: str,
        error: str,
        at: datetime,
    ) -> bool: ...

    async def delete(self, account_id: str) -> bool: ...

    async def delete_for_character(self, character_id: str) -> int: ...


class ChannelBindingRepositoryPort(Protocol):
    async def get(self, binding_id: str) -> ChannelBinding | None: ...

    async def find(
        self, account_id: str, chat_ref: str,
    ) -> ChannelBinding | None: ...

    async def list_for_account(
        self, account_id: str,
    ) -> list[ChannelBinding]: ...

    async def save(self, binding: ChannelBinding) -> None: ...

    async def delete(self, binding_id: str) -> bool: ...

    async def delete_for_account(self, account_id: str) -> int: ...
