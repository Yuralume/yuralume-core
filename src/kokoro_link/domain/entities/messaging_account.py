"""MessagingAccount entity — one bot per (character, platform).

A ``MessagingAccount`` represents the actual credentials a character
uses to speak on a platform. Because a Telegram bot (or a LINE channel)
is a single identity that can only hold one private chat with any given
user, binding the credentials one-per-character is the right shape:
if you want character A and B to both DM you on Telegram, you need two
bots, each with its own ``MessagingAccount``.

``webhook_slug`` is an unguessable URL path component; the webhook
route looks up the account by slug, which doubles as a weak auth layer
(secrets still verify per platform, but the slug alone blocks drive-by
probes against ``/webhook``).

``delivery_mode`` controls how inbound messages arrive. Telegram can use
webhook or Bot API long polling; LINE is webhook-only; Discord uses the
Gateway WebSocket; WhatsApp uses a local Baileys-compatible sidecar
gateway.

``allowed_sender_refs`` is an optional allowlist keyed on the platform's
sender id (Telegram user id, LINE userId). Empty set means accept from
anyone — convenient while first-binding, but you should lock it down
after confirming the binding works. Anything outside the list is
dropped silently by the dispatcher.

``polling_lock_owner`` / ``polling_lock_until`` form a DB-backed lease
lock for Telegram polling workers. They are not surfaced to players.
"""

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from secrets import token_urlsafe
from uuid import uuid4

from kokoro_link.domain.value_objects.delivery_mode import DeliveryMode
from kokoro_link.domain.value_objects.platform import Platform


@dataclass(frozen=True, slots=True)
class MessagingAccount:
    id: str
    character_id: str
    platform: Platform
    display_name: str
    webhook_slug: str
    credentials: dict[str, str]
    """Platform-specific secrets. Kept as a flat string dict so new
    platforms don't need schema changes. Known keys:

    * Telegram: ``bot_token``, ``webhook_secret`` (optional)
    * LINE: ``channel_secret``, ``channel_access_token``
    * Discord: ``bot_token``
    * WhatsApp: ``sidecar_url``, ``session_id``, ``api_token`` (optional)
    """

    allowed_sender_refs: tuple[str, ...] = field(default_factory=tuple)
    enabled: bool = True
    delivery_mode: DeliveryMode = field(
        default_factory=lambda: DeliveryMode.WEBHOOK,
    )
    polling_offset: int | None = None
    polling_last_update_at: datetime | None = None
    polling_last_error: str | None = None
    polling_lock_owner: str | None = None
    polling_lock_until: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def create(
        cls,
        *,
        character_id: str,
        platform: Platform,
        credentials: dict[str, str],
        display_name: str = "",
        allowed_sender_refs: tuple[str, ...] = (),
        enabled: bool = True,
        delivery_mode: DeliveryMode | None = None,
        now: datetime | None = None,
    ) -> "MessagingAccount":
        if not character_id or not character_id.strip():
            raise ValueError("character_id must be non-empty")
        required = _required_credential_keys(platform)
        missing = [k for k in required if not credentials.get(k)]
        if missing:
            raise ValueError(
                f"credentials missing required key(s) for {platform.value}: "
                f"{', '.join(missing)}",
            )
        resolved_delivery_mode = delivery_mode or _default_delivery_mode(platform)
        _validate_delivery_mode(platform, resolved_delivery_mode)
        current = now or datetime.now(timezone.utc)
        return cls(
            id=str(uuid4()),
            character_id=character_id,
            platform=platform,
            display_name=display_name.strip(),
            webhook_slug=_generate_slug(),
            credentials=dict(credentials),
            allowed_sender_refs=tuple(allowed_sender_refs),
            enabled=enabled,
            delivery_mode=resolved_delivery_mode,
            created_at=current,
            updated_at=current,
        )

    def with_credentials(
        self, credentials: dict[str, str], *, now: datetime | None = None,
    ) -> "MessagingAccount":
        required = _required_credential_keys(self.platform)
        missing = [k for k in required if not credentials.get(k)]
        if missing:
            raise ValueError(
                f"credentials missing required key(s) for {self.platform.value}: "
                f"{', '.join(missing)}",
            )
        return replace(
            self,
            credentials=dict(credentials),
            updated_at=now or datetime.now(timezone.utc),
        )

    def with_allowed_senders(
        self,
        allowed_sender_refs: tuple[str, ...],
        *,
        now: datetime | None = None,
    ) -> "MessagingAccount":
        return replace(
            self,
            allowed_sender_refs=tuple(allowed_sender_refs),
            updated_at=now or datetime.now(timezone.utc),
        )

    def with_enabled(
        self, enabled: bool, *, now: datetime | None = None,
    ) -> "MessagingAccount":
        return replace(
            self,
            enabled=enabled,
            updated_at=now or datetime.now(timezone.utc),
        )

    def with_display_name(
        self, display_name: str, *, now: datetime | None = None,
    ) -> "MessagingAccount":
        return replace(
            self,
            display_name=display_name.strip(),
            updated_at=now or datetime.now(timezone.utc),
        )

    def with_delivery_mode(
        self, delivery_mode: DeliveryMode, *, now: datetime | None = None,
    ) -> "MessagingAccount":
        _validate_delivery_mode(self.platform, delivery_mode)
        return replace(
            self,
            delivery_mode=delivery_mode,
            updated_at=now or datetime.now(timezone.utc),
        )

    def with_polling_lock(
        self,
        *,
        owner: str | None,
        until: datetime | None,
        now: datetime | None = None,
    ) -> "MessagingAccount":
        return replace(
            self,
            polling_lock_owner=owner,
            polling_lock_until=until,
            updated_at=now or datetime.now(timezone.utc),
        )

    def with_polling_progress(
        self,
        *,
        offset: int | None = None,
        checked_at: datetime | None = None,
        error: str | None = None,
        now: datetime | None = None,
    ) -> "MessagingAccount":
        current = now or datetime.now(timezone.utc)
        return replace(
            self,
            polling_offset=offset if offset is not None else self.polling_offset,
            polling_last_update_at=checked_at or self.polling_last_update_at,
            polling_last_error=error,
            updated_at=current,
        )

    def is_sender_allowed(self, sender_ref: str) -> bool:
        if not self.allowed_sender_refs:
            return True
        return sender_ref in self.allowed_sender_refs


def _required_credential_keys(platform: Platform) -> tuple[str, ...]:
    if platform == Platform.TELEGRAM:
        return ("bot_token",)
    if platform == Platform.LINE:
        return ("channel_secret", "channel_access_token")
    if platform == Platform.DISCORD:
        return ("bot_token",)
    if platform == Platform.WHATSAPP:
        return ("sidecar_url", "session_id")
    return ()


def _default_delivery_mode(platform: Platform) -> DeliveryMode:
    if platform == Platform.TELEGRAM:
        return DeliveryMode.POLLING
    if platform in (Platform.DISCORD, Platform.WHATSAPP):
        return DeliveryMode.GATEWAY
    return DeliveryMode.WEBHOOK


def _validate_delivery_mode(platform: Platform, delivery_mode: DeliveryMode) -> None:
    if platform == Platform.LINE and delivery_mode != DeliveryMode.WEBHOOK:
        raise ValueError("LINE messaging accounts only support webhook delivery")
    if platform == Platform.DISCORD and delivery_mode != DeliveryMode.GATEWAY:
        raise ValueError("Discord messaging accounts only support gateway delivery")
    if platform == Platform.WHATSAPP and delivery_mode != DeliveryMode.GATEWAY:
        raise ValueError("WhatsApp messaging accounts only support gateway delivery")
    if (
        platform == Platform.TELEGRAM
        and delivery_mode not in (DeliveryMode.POLLING, DeliveryMode.WEBHOOK)
    ):
        raise ValueError(
            "Telegram messaging accounts only support polling or webhook delivery",
        )


def _generate_slug() -> str:
    # 32 urlsafe chars ~= 192 bits of entropy, fine as the primary
    # unguessable webhook path component.
    return token_urlsafe(24)
