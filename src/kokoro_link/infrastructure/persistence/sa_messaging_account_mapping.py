"""Domain <-> ORM mapping for messaging accounts."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from kokoro_link.domain.entities.messaging_account import MessagingAccount
from kokoro_link.domain.value_objects.delivery_mode import DeliveryMode
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.persistence.models import MessagingAccountRow


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def row_to_domain(row: MessagingAccountRow) -> MessagingAccount:
    credentials = json.loads(row.credentials_json or "{}")
    allowed = json.loads(row.allowed_sender_refs_json or "[]")
    return MessagingAccount(
        id=row.id,
        character_id=row.character_id,
        platform=Platform.from_string(row.platform),
        display_name=row.display_name or "",
        webhook_slug=row.webhook_slug,
        credentials={str(k): str(v) for k, v in credentials.items()},
        allowed_sender_refs=tuple(str(x) for x in allowed),
        enabled=row.enabled,
        delivery_mode=DeliveryMode.from_string(row.delivery_mode or "webhook"),
        polling_offset=row.polling_offset,
        polling_last_update_at=(
            _ensure_utc(row.polling_last_update_at)
            if row.polling_last_update_at is not None
            else None
        ),
        polling_last_error=row.polling_last_error,
        polling_lock_owner=row.polling_lock_owner,
        polling_lock_until=(
            _ensure_utc(row.polling_lock_until)
            if row.polling_lock_until is not None
            else None
        ),
        created_at=_ensure_utc(row.created_at),
        updated_at=_ensure_utc(row.updated_at),
    )


def apply_domain_to_row(
    account: MessagingAccount, row: MessagingAccountRow,
) -> None:
    row.character_id = account.character_id
    row.platform = account.platform.value
    row.display_name = account.display_name
    row.webhook_slug = account.webhook_slug
    row.credentials_json = json.dumps(account.credentials, ensure_ascii=False)
    row.allowed_sender_refs_json = json.dumps(
        list(account.allowed_sender_refs), ensure_ascii=False,
    )
    row.enabled = account.enabled
    row.delivery_mode = account.delivery_mode.value
    row.polling_offset = account.polling_offset
    row.polling_last_update_at = account.polling_last_update_at
    row.polling_last_error = account.polling_last_error
    row.polling_lock_owner = account.polling_lock_owner
    row.polling_lock_until = account.polling_lock_until
    row.created_at = account.created_at
    row.updated_at = account.updated_at
