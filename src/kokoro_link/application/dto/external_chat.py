"""Application-layer command for the external-chat turn endpoint (LH2).

The API route (pydantic model) translates one wire request into this plain
command so the turn service stays independent of the HTTP framework. The
``conversation_id_present`` flag preserves the *omitted* vs *explicit null*
distinction the canonical request hash depends on (DR-LH0-003): a missing
``conversation_id`` key must never hash the same as an explicit ``null``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExternalChatAttachmentInput:
    """One inbound attachment reference (already ingested into Object Storage).

    ``object_ref`` is a bare Core-owned object key (opaque), never a URL.
    """

    object_ref: str
    media_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ExternalChatTurnCommand:
    """The logical contract fields of one external-chat turn request.

    Mirrors the OpenAPI ``ExternalChatTurnRequest`` required set plus the
    optional ``conversation_id``. ``conversation_id_present`` records whether
    the wire request carried the key at all, so the canonical hash omits it
    when absent and hashes an explicit ``null`` when present-and-null.
    """

    request_id: str
    tenant_id: str
    account_id: str
    character_id: str
    message: str
    attachments: tuple[ExternalChatAttachmentInput, ...]
    channel: str
    contract_version: int
    conversation_id: str | None = None
    conversation_id_present: bool = False
