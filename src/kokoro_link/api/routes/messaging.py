"""Messaging account + binding CRUD and platform webhooks.

Each webhook path ends in the account's ``webhook_slug`` so we can look
up the credentials without trusting anything in the payload. Platform
auth (Telegram secret header / LINE HMAC signature) runs *on top of*
the slug, using the per-account credentials.

The old global-env webhook paths (``/telegram/webhook`` and
``/line/webhook``) are intentionally gone — one instance no longer
assumes one bot per platform.
"""

import json
import logging
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel

from kokoro_link.api.dependencies import (
    get_container,
    get_current_user_id,
    require_admin,
)
from kokoro_link.application.dto.messaging import (
    ChannelBindingResponse,
    CreateChannelBindingRequest,
    CreateMessagingAccountRequest,
    MessagingAccountResponse,
    PollingStatusResponse,
    UpdateChannelBindingRequest,
    UpdateMessagingAccountRequest,
)
from kokoro_link.application.services.channel_binding_service import (
    ChannelBindingConflictError,
)
from kokoro_link.application.services.messaging_account_service import (
    MessagingAccountConflictError,
)
from kokoro_link.application.services.messaging_public_url import (
    MESSAGING_PUBLIC_BASE_URL_KEY as _MESSAGING_PUBLIC_BASE_URL_KEY,
    normalize_public_base_url as _normalize_public_base_url,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.contracts.messaging import InboundMessage
from kokoro_link.domain.entities.messaging_account import MessagingAccount
from kokoro_link.domain.value_objects.delivery_mode import DeliveryMode
from kokoro_link.domain.value_objects.platform import CANONICAL_PLATFORMS, Platform
from kokoro_link.infrastructure.messaging.line.adapter import LineAdapter
from kokoro_link.infrastructure.messaging.line.media_fetcher import download_line_image
from kokoro_link.infrastructure.messaging.line.parser import parse_webhook as parse_line_webhook
from kokoro_link.infrastructure.messaging.line.signature import verify_signature as verify_line_signature
from kokoro_link.infrastructure.messaging.telegram.adapter import TelegramAdapter
from kokoro_link.infrastructure.messaging.telegram.media_fetcher import (
    download_telegram_photo,
)
from kokoro_link.infrastructure.messaging.telegram.parser import parse_update as parse_telegram_update
router = APIRouter(tags=["messaging"], prefix="/messaging")

_LOGGER = logging.getLogger(__name__)
_TELEGRAM_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
_LINE_SIGNATURE_HEADER = "X-Line-Signature"
_MESSAGING_TELEGRAM_DELIVERY_MODE_KEY = "messaging.telegram_delivery_mode"


# ---------------------------------------------------------------------------
# Account CRUD
# ---------------------------------------------------------------------------

def _require_account_service(container: ServiceContainer):
    service = container.messaging_account_service
    if service is None:  # pragma: no cover — always wired in production
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Messaging account service not available",
        )
    return service


def _require_binding_service(container: ServiceContainer):
    service = container.channel_binding_service
    if service is None:  # pragma: no cover — always wired in production
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Channel binding service not available",
        )
    return service


async def _ensure_character_owned(
    container: ServiceContainer, character_id: str, current_user_id: str,
) -> None:
    """Collapse cross-user access to 404 — mirrors the dependency
    helper used by ``/characters/{character_id}`` routes. Skipped when
    the character service isn't wired (unit-test stub containers)."""
    service = getattr(container, "character_service", None)
    if service is None:
        return
    try:
        character = await service.get_character_entity(
            character_id, user_id=current_user_id,
        )
    except TypeError:
        character = await service.get_character_entity(character_id)
        if (
            character is not None
            and getattr(character, "user_id", current_user_id)
            != current_user_id
        ):
            character = None
    if character is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Character not found",
        )


async def _ensure_account_owned(
    container: ServiceContainer, account_id: str, current_user_id: str,
) -> MessagingAccount:
    """Resolve the account and verify its character is owned by the
    current user. Collapses missing-account / cross-user access to 404.
    """
    service = _require_account_service(container)
    account = await service.get(account_id)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Account not found",
        )
    await _ensure_character_owned(
        container, account.character_id, current_user_id,
    )
    return account


def _parse_platform(raw: str) -> Platform:
    try:
        platform = Platform.from_string(raw)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(error),
        )
    if platform not in CANONICAL_PLATFORMS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported platform {raw!r}",
        )
    return platform


def _parse_delivery_mode(raw: str | None) -> DeliveryMode | None:
    if raw is None:
        return None
    try:
        return DeliveryMode.from_string(raw)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(error),
        )


# ---------------------------------------------------------------------------
# Admin settings
# ---------------------------------------------------------------------------

class MessagingSettingsResponse(BaseModel):
    public_base_url: str = ""
    effective_public_base_url: str = ""
    source: str = "empty"
    telegram_delivery_mode: str = DeliveryMode.POLLING.value


class UpdateMessagingSettingsRequest(BaseModel):
    public_base_url: str | None = None
    telegram_delivery_mode: str | None = None


def _ensure_public_base_url_shape(value: str) -> None:
    if not value:
        return
    if not (value.startswith("https://") or value.startswith("http://")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Public Base URL must start with http:// or https://",
        )


async def _stored_public_base_url(container: ServiceContainer) -> str:
    raw = await container.preferences_repository.get(_MESSAGING_PUBLIC_BASE_URL_KEY)
    return _normalize_public_base_url(raw)


def _app_public_base_url(container: ServiceContainer) -> str:
    settings = getattr(container, "app_settings", None)
    if settings is None:
        return ""
    return _normalize_public_base_url(
        getattr(settings, "public_base_url", ""),
    )


async def _resolve_public_base_url(
    container: ServiceContainer,
) -> tuple[str, str]:
    stored = await _stored_public_base_url(container)
    if stored:
        return stored, "preference"
    app_base_url = _app_public_base_url(container)
    if app_base_url:
        return app_base_url, "env"
    return "", "empty"


def _resolve_public_base_url_from_candidate(
    container: ServiceContainer, stored: str,
) -> tuple[str, str]:
    if stored:
        return stored, "preference"
    app_base_url = _app_public_base_url(container)
    if app_base_url:
        return app_base_url, "env"
    return "", "empty"


def _parse_telegram_delivery_mode(raw: str | None) -> DeliveryMode:
    if raw is None:
        return DeliveryMode.POLLING
    mode = _parse_delivery_mode(raw)
    if mode is None:
        return DeliveryMode.POLLING
    if mode not in (DeliveryMode.POLLING, DeliveryMode.WEBHOOK):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Telegram delivery mode must be polling or webhook",
        )
    return mode


async def _stored_telegram_delivery_mode(
    container: ServiceContainer,
) -> DeliveryMode:
    raw = await container.preferences_repository.get(
        _MESSAGING_TELEGRAM_DELIVERY_MODE_KEY,
    )
    if not isinstance(raw, str) or not raw.strip():
        return DeliveryMode.POLLING
    try:
        mode = DeliveryMode.from_string(raw)
    except ValueError:
        _LOGGER.warning(
            "invalid stored Telegram delivery mode %r; falling back to polling",
            raw,
        )
        return DeliveryMode.POLLING
    if mode not in (DeliveryMode.POLLING, DeliveryMode.WEBHOOK):
        _LOGGER.warning(
            "unsupported stored Telegram delivery mode %r; falling back to polling",
            raw,
        )
        return DeliveryMode.POLLING
    return mode


def _delivery_mode_for_platform(
    platform: Platform, telegram_delivery_mode: DeliveryMode,
) -> DeliveryMode:
    if platform == Platform.TELEGRAM:
        return telegram_delivery_mode
    if platform in (Platform.DISCORD, Platform.WHATSAPP):
        return DeliveryMode.GATEWAY
    return DeliveryMode.WEBHOOK


def _reject_account_delivery_mode_payload(raw: str | None) -> None:
    if raw is None:
        return
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Delivery mode is managed by site settings",
    )


async def _save_public_base_url(
    container: ServiceContainer, public_base_url: str,
) -> None:
    if public_base_url:
        await container.preferences_repository.set(
            _MESSAGING_PUBLIC_BASE_URL_KEY,
            public_base_url,
        )
    else:
        await container.preferences_repository.delete(
            _MESSAGING_PUBLIC_BASE_URL_KEY,
        )


async def _save_telegram_delivery_mode(
    container: ServiceContainer, mode: DeliveryMode,
) -> None:
    await container.preferences_repository.set(
        _MESSAGING_TELEGRAM_DELIVERY_MODE_KEY,
        mode.value,
    )


def _telegram_sync_error(result: dict[str, Any]) -> str:
    raw = result.get("description") or result.get("error") or "unknown"
    return str(raw)[:500]


async def _delete_telegram_webhook_for_polling(bot_token: str) -> None:
    if not bot_token:
        return
    result = await TelegramAdapter().delete_webhook(
        bot_token=bot_token, drop_pending_updates=False,
    )
    if bool(result.get("ok")):
        return
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=(
            "Failed to sync Telegram polling mode: "
            + _telegram_sync_error(result)
        ),
    )


async def _sync_telegram_accounts_delivery_mode(
    container: ServiceContainer,
    *,
    mode: DeliveryMode,
    public_base_url: str,
) -> None:
    service = _require_account_service(container)
    accounts = [
        account
        for account in await service.list_all()
        if account.platform == Platform.TELEGRAM
    ]
    if not accounts:
        return

    errors: list[str] = []
    synced_accounts: list[MessagingAccount] = []
    adapter = TelegramAdapter()
    for account in accounts:
        token = account.credentials.get("bot_token", "")
        if not token:
            errors.append(f"{account.id}: missing bot_token")
            continue
        if mode == DeliveryMode.POLLING:
            result = await adapter.delete_webhook(
                bot_token=token, drop_pending_updates=False,
            )
        else:
            result = await adapter.set_webhook(
                bot_token=token,
                webhook_url=_build_webhook_url(public_base_url, account),
                secret_token=account.credentials.get("webhook_secret", ""),
            )
        if not bool(result.get("ok")):
            reason = _telegram_sync_error(result)
            errors.append(f"{account.id}: {reason}")
            continue
        synced_accounts.append(account)

    if errors:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "Failed to sync Telegram delivery mode: "
                + "; ".join(errors)
            ),
        )

    for account in synced_accounts:
        if account.delivery_mode != mode:
            await service.update(account.id, delivery_mode=mode)


async def _messaging_settings_response(
    container: ServiceContainer,
) -> MessagingSettingsResponse:
    stored = await _stored_public_base_url(container)
    effective, source = _resolve_public_base_url_from_candidate(container, stored)
    telegram_delivery_mode = await _stored_telegram_delivery_mode(container)
    return MessagingSettingsResponse(
        public_base_url=stored,
        effective_public_base_url=effective,
        source=source,
        telegram_delivery_mode=telegram_delivery_mode.value,
    )


@router.get(
    "/settings",
    response_model=MessagingSettingsResponse,
)
async def get_messaging_settings(
    container: ServiceContainer = Depends(get_container),
    _current_user_id: str = Depends(get_current_user_id),
) -> MessagingSettingsResponse:
    return await _messaging_settings_response(container)


@router.put(
    "/settings",
    response_model=MessagingSettingsResponse,
)
async def set_messaging_settings(
    payload: UpdateMessagingSettingsRequest,
    container: ServiceContainer = Depends(get_container),
    _admin: object = Depends(require_admin),
) -> MessagingSettingsResponse:
    fields = payload.model_fields_set
    current_public_base_url = await _stored_public_base_url(container)
    current_mode = await _stored_telegram_delivery_mode(container)

    public_base_url = current_public_base_url
    if "public_base_url" in fields:
        public_base_url = _normalize_public_base_url(payload.public_base_url)
        _ensure_public_base_url_shape(public_base_url)

    telegram_delivery_mode = current_mode
    if "telegram_delivery_mode" in fields:
        telegram_delivery_mode = _parse_telegram_delivery_mode(
            payload.telegram_delivery_mode,
        )

    effective_public_base_url, _source = _resolve_public_base_url_from_candidate(
        container, public_base_url,
    )
    if (
        telegram_delivery_mode == DeliveryMode.WEBHOOK
        and not effective_public_base_url
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Public Base URL is required for Telegram webhook mode",
        )

    needs_sync = (
        telegram_delivery_mode != current_mode
        or (
            "public_base_url" in fields
            and telegram_delivery_mode == DeliveryMode.WEBHOOK
        )
    )
    if needs_sync:
        await _sync_telegram_accounts_delivery_mode(
            container,
            mode=telegram_delivery_mode,
            public_base_url=effective_public_base_url,
        )

    if "public_base_url" in fields:
        await _save_public_base_url(container, public_base_url)
    if "telegram_delivery_mode" in fields:
        await _save_telegram_delivery_mode(container, telegram_delivery_mode)
    return await _messaging_settings_response(container)


@router.get("/accounts", response_model=list[MessagingAccountResponse])
async def list_accounts(
    character_id: str = Query(..., min_length=1),
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> list[MessagingAccountResponse]:
    service = _require_account_service(container)
    await _ensure_character_owned(
        container, character_id, current_user_id,
    )
    accounts = await service.list_for_character(character_id)
    return [MessagingAccountResponse.from_domain(a) for a in accounts]


@router.post(
    "/accounts",
    response_model=MessagingAccountResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_account(
    payload: CreateMessagingAccountRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> MessagingAccountResponse:
    service = _require_account_service(container)
    platform = _parse_platform(payload.platform)
    _reject_account_delivery_mode_payload(payload.delivery_mode)
    site_telegram_delivery_mode = await _stored_telegram_delivery_mode(container)
    delivery_mode = _delivery_mode_for_platform(
        platform, site_telegram_delivery_mode,
    )
    await _ensure_character_owned(
        container, payload.character_id, current_user_id,
    )
    try:
        await service.validate_credentials_available(
            platform=platform,
            credentials=payload.credentials,
        )
    except MessagingAccountConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error),
        )
    if platform == Platform.TELEGRAM and delivery_mode == DeliveryMode.POLLING:
        await _delete_telegram_webhook_for_polling(
            payload.credentials.get("bot_token", ""),
        )
    try:
        account = await service.create(
            character_id=payload.character_id,
            platform=platform,
            credentials=payload.credentials,
            display_name=payload.display_name,
            allowed_sender_refs=tuple(payload.allowed_sender_refs),
            enabled=payload.enabled,
            delivery_mode=delivery_mode,
        )
    except MessagingAccountConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error),
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(error),
        )
    return MessagingAccountResponse.from_domain(account)


@router.patch("/accounts/{account_id}", response_model=MessagingAccountResponse)
async def update_account(
    account_id: str,
    payload: UpdateMessagingAccountRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> MessagingAccountResponse:
    service = _require_account_service(container)
    _reject_account_delivery_mode_payload(payload.delivery_mode)
    existing = await _ensure_account_owned(container, account_id, current_user_id)
    if (
        payload.credentials is not None
        and existing.platform == Platform.TELEGRAM
        and existing.delivery_mode == DeliveryMode.POLLING
    ):
        try:
            await service.validate_credentials_available(
                platform=existing.platform,
                credentials=payload.credentials,
                current_account_id=existing.id,
            )
        except MessagingAccountConflictError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(error),
            )
        await _delete_telegram_webhook_for_polling(
            payload.credentials.get("bot_token", ""),
        )
    try:
        account = await service.update(
            account_id,
            display_name=payload.display_name,
            credentials=payload.credentials,
            allowed_sender_refs=(
                tuple(payload.allowed_sender_refs)
                if payload.allowed_sender_refs is not None
                else None
            ),
            enabled=payload.enabled,
        )
    except MessagingAccountConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error),
        )
    except ValueError as error:
        code = status.HTTP_400_BAD_REQUEST
        if str(error) == "Account not found":
            code = status.HTTP_404_NOT_FOUND
        raise HTTPException(
            status_code=code, detail=str(error),
        )
    return MessagingAccountResponse.from_domain(account)


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    account_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> Response:
    service = _require_account_service(container)
    await _ensure_account_owned(container, account_id, current_user_id)
    removed = await service.delete(account_id)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Account not found",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Webhook management (register / inspect via platform APIs)
#
# Front-end calls these to point the platform at our webhook URL without
# making the operator run curl. We keep credentials server-side (they're
# never returned via the read API) so these endpoints are the only
# sanctioned way to use them.
# ---------------------------------------------------------------------------

class RegisterWebhookRequest(BaseModel):
    public_base_url: str | None = None


class WebhookRegisterResponse(BaseModel):
    ok: bool
    webhook_url: str
    message: str | None = None
    platform_response: dict[str, Any] | None = None


class WebhookStatusResponse(BaseModel):
    ok: bool
    info: dict[str, Any] | None = None
    message: str | None = None


class PollingControlResponse(BaseModel):
    ok: bool
    status: PollingStatusResponse
    message: str | None = None
    platform_response: dict[str, Any] | None = None


def _build_webhook_url(base: str, account: MessagingAccount) -> str:
    trimmed = base.rstrip("/")
    return (
        f"{trimmed}/api/v1/messaging/{account.platform.value}"
        f"/webhook/{account.webhook_slug}"
    )


async def _load_account_or_404(
    container: ServiceContainer, account_id: str,
) -> MessagingAccount:
    service = _require_account_service(container)
    account = await service.get(account_id)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Account not found",
        )
    return account


@router.post(
    "/accounts/{account_id}/webhook/register",
    response_model=WebhookRegisterResponse,
)
async def register_webhook(
    account_id: str,
    payload: RegisterWebhookRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> WebhookRegisterResponse:
    account = await _ensure_account_owned(
        container, account_id, current_user_id,
    )
    site_telegram_delivery_mode = await _stored_telegram_delivery_mode(container)
    if (
        account.platform == Platform.TELEGRAM
        and site_telegram_delivery_mode != DeliveryMode.WEBHOOK
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Telegram webhook is disabled by site delivery mode",
        )
    public_base_url = _normalize_public_base_url(payload.public_base_url)
    _ensure_public_base_url_shape(public_base_url)
    if not public_base_url:
        public_base_url, _source = await _resolve_public_base_url(container)
    if not public_base_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Public Base URL is not configured",
        )
    webhook_url = _build_webhook_url(public_base_url, account)

    if account.platform == Platform.TELEGRAM:
        token = account.credentials.get("bot_token", "")
        if not token:
            return WebhookRegisterResponse(
                ok=False, webhook_url=webhook_url, message="Missing bot_token",
            )
        result = await TelegramAdapter().set_webhook(
            bot_token=token,
            webhook_url=webhook_url,
            secret_token=account.credentials.get("webhook_secret", ""),
        )
        ok = bool(result.get("ok"))
        message = result.get("description") or result.get("error")
        if ok and account.delivery_mode != DeliveryMode.WEBHOOK:
            await _require_account_service(container).update(
                account.id, delivery_mode=DeliveryMode.WEBHOOK,
            )
        return WebhookRegisterResponse(
            ok=ok, webhook_url=webhook_url, message=message,
            platform_response=result,
        )

    if account.platform == Platform.LINE:
        token = account.credentials.get("channel_access_token", "")
        if not token:
            return WebhookRegisterResponse(
                ok=False, webhook_url=webhook_url,
                message="Missing channel_access_token",
            )
        result = await LineAdapter().set_webhook_endpoint(
            channel_access_token=token, webhook_url=webhook_url,
        )
        ok = bool(result.get("ok"))
        if ok and account.delivery_mode != DeliveryMode.WEBHOOK:
            await _require_account_service(container).update(
                account.id, delivery_mode=DeliveryMode.WEBHOOK,
            )
        return WebhookRegisterResponse(
            ok=ok, webhook_url=webhook_url,
            message=None if ok else result.get("error"),
            platform_response=result,
        )

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unsupported platform {account.platform.value!r}",
    )


@router.get(
    "/accounts/{account_id}/webhook/status",
    response_model=WebhookStatusResponse,
)
async def get_webhook_status(
    account_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> WebhookStatusResponse:
    account = await _ensure_account_owned(
        container, account_id, current_user_id,
    )

    if account.platform == Platform.TELEGRAM:
        token = account.credentials.get("bot_token", "")
        if not token:
            return WebhookStatusResponse(ok=False, message="Missing bot_token")
        result = await TelegramAdapter().get_webhook_info(bot_token=token)
        return WebhookStatusResponse(
            ok=bool(result.get("ok")),
            info=result.get("result") if isinstance(result.get("result"), dict) else None,
            message=result.get("description") or result.get("error"),
        )

    if account.platform == Platform.LINE:
        token = account.credentials.get("channel_access_token", "")
        if not token:
            return WebhookStatusResponse(
                ok=False, message="Missing channel_access_token",
            )
        result = await LineAdapter().get_webhook_endpoint(
            channel_access_token=token,
        )
        ok = bool(result.get("ok"))
        info = {k: v for k, v in result.items() if k != "ok"} if ok else None
        return WebhookStatusResponse(
            ok=ok, info=info, message=None if ok else result.get("error"),
        )

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unsupported platform {account.platform.value!r}",
    )


# ---------------------------------------------------------------------------
# WhatsApp sidecar management
# ---------------------------------------------------------------------------

def _require_whatsapp_account(account: MessagingAccount) -> None:
    if account.platform != Platform.WHATSAPP:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="WhatsApp QR is only supported for WhatsApp accounts",
        )


@router.get("/accounts/{account_id}/whatsapp/qr.svg")
async def get_whatsapp_qr_svg(
    account_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> Response:
    account = await _ensure_account_owned(
        container, account_id, current_user_id,
    )
    _require_whatsapp_account(account)
    sidecar_url = account.credentials.get("sidecar_url", "").strip().rstrip("/")
    session_id = account.credentials.get("session_id", "").strip()
    if not sidecar_url or not session_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="WhatsApp sidecar session is not configured",
        )
    url = f"{sidecar_url}/sessions/{quote(session_id, safe='')}/qr.svg"
    headers: dict[str, str] = {}
    api_token = account.credentials.get("api_token", "").strip()
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            upstream = await client.get(url, headers=headers)
    except httpx.HTTPError as error:
        _LOGGER.warning(
            "WhatsApp sidecar QR fetch failed account=%s error=%s",
            account.id,
            error,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="WhatsApp sidecar is not reachable",
        ) from error
    if upstream.status_code == status.HTTP_404_NOT_FOUND:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="WhatsApp QR is not available yet",
        )
    if upstream.status_code in (
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
    ):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="WhatsApp sidecar rejected the configured token",
        )
    if upstream.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="WhatsApp sidecar QR request failed",
        )
    return Response(
        content=upstream.content,
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# Telegram polling control
# ---------------------------------------------------------------------------

def _require_telegram_account(account: MessagingAccount) -> None:
    if account.platform != Platform.TELEGRAM:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Polling is only supported for Telegram accounts",
        )


@router.post(
    "/accounts/{account_id}/polling/start",
    response_model=PollingControlResponse,
)
async def start_polling(
    account_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> PollingControlResponse:
    await _ensure_account_owned(container, account_id, current_user_id)
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Telegram delivery mode is managed by site settings",
    )


@router.post(
    "/accounts/{account_id}/polling/stop",
    response_model=PollingControlResponse,
)
async def stop_polling(
    account_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> PollingControlResponse:
    await _ensure_account_owned(container, account_id, current_user_id)
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Telegram delivery mode is managed by site settings",
    )


@router.get(
    "/accounts/{account_id}/polling/status",
    response_model=PollingStatusResponse,
)
async def get_polling_status(
    account_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> PollingStatusResponse:
    account = await _ensure_account_owned(
        container, account_id, current_user_id,
    )
    _require_telegram_account(account)
    return PollingStatusResponse.from_domain(account)


# ---------------------------------------------------------------------------
# Binding CRUD
# ---------------------------------------------------------------------------

@router.get("/bindings", response_model=list[ChannelBindingResponse])
async def list_bindings(
    account_id: str = Query(..., min_length=1),
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> list[ChannelBindingResponse]:
    service = _require_binding_service(container)
    await _ensure_account_owned(container, account_id, current_user_id)
    bindings = await service.list_for_account(account_id)
    return [ChannelBindingResponse.from_domain(b) for b in bindings]


@router.post(
    "/bindings",
    response_model=ChannelBindingResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_binding(
    payload: CreateChannelBindingRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> ChannelBindingResponse:
    service = _require_binding_service(container)
    await _ensure_account_owned(
        container, payload.account_id, current_user_id,
    )
    try:
        binding = await service.create(
            account_id=payload.account_id,
            chat_ref=payload.chat_ref,
            enabled=payload.enabled,
        )
    except ChannelBindingConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error),
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error),
        )
    return ChannelBindingResponse.from_domain(binding)


async def _ensure_binding_owned(
    container: ServiceContainer,
    binding_id: str,
    current_user_id: str,
) -> None:
    """Resolve the binding's account and verify the account's character
    is owned by the current user. Collapses cross-user / missing rows
    to a single 404 so callers cannot enumerate ids."""
    service = _require_binding_service(container)
    binding = await service.get(binding_id)
    if binding is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Binding not found",
        )
    await _ensure_account_owned(container, binding.account_id, current_user_id)


@router.patch("/bindings/{binding_id}", response_model=ChannelBindingResponse)
async def update_binding(
    binding_id: str,
    payload: UpdateChannelBindingRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> ChannelBindingResponse:
    service = _require_binding_service(container)
    await _ensure_binding_owned(container, binding_id, current_user_id)
    try:
        binding = await service.update(
            binding_id,
            enabled=payload.enabled,
            accepts_proactive=payload.accepts_proactive,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error),
        )
    return ChannelBindingResponse.from_domain(binding)


@router.delete("/bindings/{binding_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_binding(
    binding_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> Response:
    service = _require_binding_service(container)
    await _ensure_binding_owned(container, binding_id, current_user_id)
    removed = await service.delete(binding_id)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Binding not found",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

async def _load_account(
    container: ServiceContainer, slug: str, platform: Platform,
) -> MessagingAccount:
    service = _require_account_service(container)
    account = await service.find_by_slug(slug)
    if account is None or account.platform != platform:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown webhook slug",
        )
    return account


async def _account_owner_id(
    container: ServiceContainer, account: MessagingAccount,
) -> str:
    service = getattr(container, "character_service", None)
    if service is None:
        return "default"
    try:
        character = await service.get_character_entity(account.character_id)
    except Exception:
        _LOGGER.exception(
            "could not resolve owner for messaging account %s", account.id,
        )
        return "default"
    return str(getattr(character, "user_id", "default") or "default")


def _require_dispatcher(container: ServiceContainer):
    dispatcher = container.messaging_dispatcher
    if dispatcher is None:  # pragma: no cover — always wired in production
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Messaging dispatcher not available",
        )
    return dispatcher


@router.post(
    "/telegram/webhook/{slug}", status_code=status.HTTP_200_OK,
)
async def telegram_webhook(
    slug: str,
    request: Request,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    account = await _load_account(container, slug, Platform.TELEGRAM)
    site_telegram_delivery_mode = await _stored_telegram_delivery_mode(container)
    if (
        not account.enabled
        or site_telegram_delivery_mode != DeliveryMode.WEBHOOK
    ):
        return {"ok": True, "dispatched": False}

    expected_secret = account.credentials.get("webhook_secret", "")
    if expected_secret:
        provided = request.headers.get(_TELEGRAM_SECRET_HEADER, "")
        if provided != expected_secret:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid webhook secret",
            )

    try:
        update = await request.json()
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Body is not valid JSON",
        )

    parsed = parse_telegram_update(update)
    if parsed is None:
        return {"ok": True, "dispatched": False}

    attachment_urls: tuple[str, ...] = ()
    if parsed.photo_refs:
        settings = getattr(request.app.state, "settings", None)
        bot_token = account.credentials.get("bot_token", "")
        if settings is not None and bot_token:
            urls: list[str] = []
            owner_id = await _account_owner_id(container, account)
            object_storage = getattr(container, "object_storage", None)
            for file_id in parsed.photo_refs:
                url = await download_telegram_photo(
                    bot_token=bot_token,
                    file_id=file_id,
                    uploads_dir=settings.uploads_dir,
                    object_storage=object_storage,
                    user_id=owner_id,
                )
                if url:
                    urls.append(url)
            attachment_urls = tuple(urls)

    inbound = InboundMessage.from_parsed(
        parsed, account_id=account.id, attachment_urls=attachment_urls,
    )
    dispatcher = _require_dispatcher(container)
    try:
        await dispatcher.handle_inbound(inbound)
    except Exception:
        _LOGGER.exception("dispatcher crashed for Telegram update")
    return {"ok": True, "dispatched": True}


@router.post("/line/webhook/{slug}", status_code=status.HTTP_200_OK)
async def line_webhook(
    slug: str,
    request: Request,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    account = await _load_account(container, slug, Platform.LINE)
    if not account.enabled:
        return {"ok": True, "dispatched": 0}

    body = await request.body()
    provided = request.headers.get(_LINE_SIGNATURE_HEADER, "")
    secret = account.credentials.get("channel_secret", "")
    if not verify_line_signature(
        channel_secret=secret, body=body, signature=provided,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid LINE signature",
        )

    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Body is not valid JSON",
        )

    parsed_events = parse_line_webhook(payload)
    dispatcher = _require_dispatcher(container)
    dispatched = 0
    for parsed in parsed_events:
        attachment_urls: tuple[str, ...] = ()
        if parsed.photo_refs:
            settings = getattr(request.app.state, "settings", None)
            channel_token = account.credentials.get(
                "channel_access_token", "",
            )
            if settings is not None and channel_token:
                urls: list[str] = []
                owner_id = await _account_owner_id(container, account)
                object_storage = getattr(container, "object_storage", None)
                for msg_id in parsed.photo_refs:
                    url = await download_line_image(
                        channel_access_token=channel_token,
                        message_id=msg_id,
                        uploads_dir=settings.uploads_dir,
                        object_storage=object_storage,
                        user_id=owner_id,
                    )
                    if url:
                        urls.append(url)
                attachment_urls = tuple(urls)
        inbound = InboundMessage.from_parsed(
            parsed, account_id=account.id, attachment_urls=attachment_urls,
        )
        try:
            await dispatcher.handle_inbound(inbound)
            dispatched += 1
        except Exception:
            _LOGGER.exception("dispatcher crashed for LINE event")
    return {"ok": True, "dispatched": dispatched}
