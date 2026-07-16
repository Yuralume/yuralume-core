"""Domain <-> ORM mapping for channel bindings."""

from __future__ import annotations

from datetime import datetime, timezone

from kokoro_link.domain.entities.channel_binding import ChannelBinding
from kokoro_link.infrastructure.persistence.models import ChannelBindingRow


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def row_to_domain(row: ChannelBindingRow) -> ChannelBinding:
    return ChannelBinding(
        id=row.id,
        account_id=row.account_id,
        chat_ref=row.chat_ref,
        enabled=row.enabled,
        created_at=_ensure_utc(row.created_at),
        updated_at=_ensure_utc(row.updated_at),
        conversation_id=row.conversation_id,
        accepts_proactive=row.accepts_proactive,
    )


def apply_domain_to_row(binding: ChannelBinding, row: ChannelBindingRow) -> None:
    row.account_id = binding.account_id
    row.chat_ref = binding.chat_ref
    row.enabled = binding.enabled
    row.created_at = binding.created_at
    row.updated_at = binding.updated_at
    row.conversation_id = binding.conversation_id
    row.accepts_proactive = binding.accepts_proactive
