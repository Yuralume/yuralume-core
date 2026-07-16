"""Messaging account CRUD with per-platform uniqueness guard."""

import re

from kokoro_link.contracts.messaging import (
    ChannelBindingRepositoryPort,
    MessagingAccountRepositoryPort,
)
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.entities.messaging_account import MessagingAccount
from kokoro_link.domain.value_objects.delivery_mode import DeliveryMode
from kokoro_link.domain.value_objects.platform import Platform


class MessagingAccountConflictError(Exception):
    """Raised when a character already has an account on this platform."""


DEFAULT_WHATSAPP_SIDECAR_URL = "http://whatsapp-sidecar:32190"
_UNSAFE_WHATSAPP_SESSION_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")


class MessagingAccountService:
    def __init__(
        self,
        *,
        account_repository: MessagingAccountRepositoryPort,
        binding_repository: ChannelBindingRepositoryPort,
        character_repository: CharacterRepositoryPort,
        default_whatsapp_sidecar_url: str = DEFAULT_WHATSAPP_SIDECAR_URL,
        default_whatsapp_api_token: str = "",
    ) -> None:
        self._accounts = account_repository
        self._bindings = binding_repository
        self._characters = character_repository
        self._default_whatsapp_sidecar_url = (
            default_whatsapp_sidecar_url.strip().rstrip("/")
            or DEFAULT_WHATSAPP_SIDECAR_URL
        )
        self._default_whatsapp_api_token = default_whatsapp_api_token.strip()

    async def create(
        self,
        *,
        character_id: str,
        platform: Platform,
        credentials: dict[str, str],
        display_name: str = "",
        allowed_sender_refs: tuple[str, ...] = (),
        enabled: bool = True,
        delivery_mode: DeliveryMode | None = None,
    ) -> MessagingAccount:
        character = await self._characters.get(character_id)
        if character is None:
            raise ValueError("Character not found")
        existing = await self._accounts.find_for_character(platform, character_id)
        if existing is not None:
            raise MessagingAccountConflictError(
                f"Character {character_id} already has a {platform.value} account",
            )
        credentials = self._prepare_credentials(
            platform=platform,
            credentials=credentials,
            character_id=character_id,
        )
        await self.validate_credentials_available(
            platform=platform,
            credentials=credentials,
        )
        account = MessagingAccount.create(
            character_id=character_id,
            platform=platform,
            credentials=credentials,
            display_name=display_name,
            allowed_sender_refs=allowed_sender_refs,
            enabled=enabled,
            delivery_mode=delivery_mode,
        )
        await self._accounts.save(account)
        return account

    async def update(
        self,
        account_id: str,
        *,
        display_name: str | None = None,
        credentials: dict[str, str] | None = None,
        allowed_sender_refs: tuple[str, ...] | None = None,
        enabled: bool | None = None,
        delivery_mode: DeliveryMode | None = None,
    ) -> MessagingAccount:
        account = await self._accounts.get(account_id)
        if account is None:
            raise ValueError("Account not found")
        updated = account
        if display_name is not None:
            updated = updated.with_display_name(display_name)
        if credentials is not None:
            credentials = self._prepare_credentials(
                platform=account.platform,
                credentials=credentials,
                character_id=account.character_id,
            )
            await self.validate_credentials_available(
                platform=account.platform,
                credentials=credentials,
                current_account_id=account.id,
            )
            updated = updated.with_credentials(credentials)
        if allowed_sender_refs is not None:
            updated = updated.with_allowed_senders(tuple(allowed_sender_refs))
        if enabled is not None:
            updated = updated.with_enabled(enabled)
        if delivery_mode is not None:
            updated = updated.with_delivery_mode(delivery_mode)
        if updated is not account:
            await self._accounts.save(updated)
        return updated

    async def delete(self, account_id: str) -> bool:
        # Bindings follow via FK CASCADE at the DB layer; the in-memory
        # repos don't enforce that so we clean them explicitly too.
        await self._bindings.delete_for_account(account_id)
        return await self._accounts.delete(account_id)

    async def list_for_character(
        self, character_id: str,
    ) -> list[MessagingAccount]:
        return await self._accounts.list_for_character(character_id)

    async def list_all(self) -> list[MessagingAccount]:
        return await self._accounts.list_all()

    async def get(self, account_id: str) -> MessagingAccount | None:
        return await self._accounts.get(account_id)

    async def find_by_slug(self, webhook_slug: str) -> MessagingAccount | None:
        return await self._accounts.find_by_slug(webhook_slug)

    async def validate_credentials_available(
        self,
        *,
        platform: Platform,
        credentials: dict[str, str],
        current_account_id: str | None = None,
    ) -> None:
        await self._ensure_telegram_bot_token_available(
            platform=platform,
            credentials=credentials,
            current_account_id=current_account_id,
        )
        await self._ensure_discord_bot_token_available(
            platform=platform,
            credentials=credentials,
            current_account_id=current_account_id,
        )
        await self._ensure_whatsapp_session_available(
            platform=platform,
            credentials=credentials,
            current_account_id=current_account_id,
        )

    def _prepare_credentials(
        self,
        *,
        platform: Platform,
        credentials: dict[str, str],
        character_id: str,
    ) -> dict[str, str]:
        normalized = {
            key: value.strip()
            for key, value in credentials.items()
            if isinstance(value, str) and value.strip()
        }
        if platform != Platform.WHATSAPP:
            return normalized
        if not normalized.get("sidecar_url"):
            normalized["sidecar_url"] = self._default_whatsapp_sidecar_url
        else:
            normalized["sidecar_url"] = normalized["sidecar_url"].rstrip("/")
        if not normalized.get("session_id"):
            normalized["session_id"] = _default_whatsapp_session_id(character_id)
        if self._default_whatsapp_api_token and not normalized.get("api_token"):
            normalized["api_token"] = self._default_whatsapp_api_token
        return normalized

    async def _ensure_telegram_bot_token_available(
        self,
        *,
        platform: Platform,
        credentials: dict[str, str],
        current_account_id: str | None = None,
    ) -> None:
        if platform != Platform.TELEGRAM:
            return
        bot_token = credentials.get("bot_token")
        if not bot_token:
            return
        for account in await self._accounts.list_all():
            if (
                account.id != current_account_id
                and account.platform == Platform.TELEGRAM
                and account.credentials.get("bot_token") == bot_token
            ):
                raise MessagingAccountConflictError(
                    "Telegram bot token is already bound to another account",
                )

    async def _ensure_discord_bot_token_available(
        self,
        *,
        platform: Platform,
        credentials: dict[str, str],
        current_account_id: str | None = None,
    ) -> None:
        if platform != Platform.DISCORD:
            return
        bot_token = credentials.get("bot_token")
        if not bot_token:
            return
        for account in await self._accounts.list_all():
            if (
                account.id != current_account_id
                and account.platform == Platform.DISCORD
                and account.credentials.get("bot_token") == bot_token
            ):
                raise MessagingAccountConflictError(
                    "Discord bot token is already bound to another account",
                )

    async def _ensure_whatsapp_session_available(
        self,
        *,
        platform: Platform,
        credentials: dict[str, str],
        current_account_id: str | None = None,
    ) -> None:
        if platform != Platform.WHATSAPP:
            return
        session_key = _whatsapp_session_key(credentials)
        if session_key is None:
            return
        for account in await self._accounts.list_all():
            if (
                account.id == current_account_id
                or account.platform != Platform.WHATSAPP
            ):
                continue
            if _whatsapp_session_key(account.credentials) == session_key:
                raise MessagingAccountConflictError(
                    "WhatsApp sidecar session is already bound to another account",
                )


def _whatsapp_session_key(credentials: dict[str, str]) -> tuple[str, str] | None:
    sidecar_url = credentials.get("sidecar_url", "").strip().rstrip("/")
    session_id = credentials.get("session_id", "").strip()
    if not sidecar_url or not session_id:
        return None
    return (sidecar_url, session_id)


def _default_whatsapp_session_id(character_id: str) -> str:
    sanitized = _UNSAFE_WHATSAPP_SESSION_CHARS.sub(
        "-", character_id.strip(),
    ).strip("-._")
    if not sanitized:
        return "character-default"
    return f"character-{sanitized}"
