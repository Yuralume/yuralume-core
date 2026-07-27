"""Store an external-chat attachment through Core's service boundary (LH2).

``POST /api/internal/v1/external-chat/attachments`` — the hosted LINE
official-channel Cloud service hands Core the raw bytes of an inbound image
and Core, not Cloud, decides whether to accept it: paired-identity +
subscription authorization, a bounded size, a real (magic-byte-verified)
supported raster type, and a pixel-bomb ceiling. Only then does it land in
Object Storage under the same ``users/{owner}/messaging-inbound/`` convention
the self-host LINE ingest (`media_fetcher.download_line_image`) already uses,
and Core returns an opaque ``object_ref`` (the bare object key) — never a
fetch URL.

Authorization is delegated wholesale to the roster projection so there is a
single source of truth for "may this pair chat with this character": the
character must appear in the pair's authoritative roster. A locked tenant
(empty roster), a subscription-lapse freeze (excluded), or a character that
is not this operator's are therefore all indistinguishable ``UnknownCharacter``
outcomes — opaque by construction. Idle/manual freezes stay chattable, so an
attachment to a dormant-but-listed character is accepted.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from uuid import uuid4

from kokoro_link.application.services.external_chat.attachment_media import (
    image_dimensions,
    sniff_image,
)
from kokoro_link.application.services.external_chat_roster_service import (
    ExternalChatRosterService,
    resolve_cloud_operator,
)
from kokoro_link.contracts.object_storage import ObjectStoragePort
from kokoro_link.contracts.operator_profile import OperatorProfileRepositoryPort

_MAX_BYTES = 8 * 1024 * 1024  # 8 MB — matches the web chat-upload cap.
_MAX_EDGE = 8192
_MAX_PIXELS = 33_554_432  # 8192 * 4096; guards against pixel-flood decodes.
_INBOUND_SUBDIR = "messaging-inbound"
_OWNER_SANITISE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class AttachmentRefView:
    """Resolved attachment, aligned field-for-field with the OpenAPI
    ``AttachmentRef`` schema (snake_case is the wire shape)."""

    object_ref: str
    media_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class Stored:
    """Terminal success: the attachment was validated and persisted."""

    attachment: AttachmentRefView


@dataclass(frozen=True, slots=True)
class UnknownAccount:
    """The ``(tenant_id, account_id)`` pair does not resolve (route → 404)."""


@dataclass(frozen=True, slots=True)
class UnknownCharacter:
    """The character is not a chattable member of the pair's roster.

    Opaque: covers "not this operator's character", "subscription denied",
    and "tenant locked" alike (route → 404)."""


@dataclass(frozen=True, slots=True)
class TooLarge:
    """The payload exceeds the bounded size limit (route → 413)."""


@dataclass(frozen=True, slots=True)
class UnsupportedType:
    """Declared type unsupported, magic-byte mismatch, or pixel bomb
    (route → 415)."""


IngestOutcome = (
    Stored | UnknownAccount | UnknownCharacter | TooLarge | UnsupportedType
)


class ExternalChatAttachmentService:
    """Validate and persist one external-chat attachment.

    Depends on the roster service for authorization (single source) and the
    operator repository only to recover the owner id used in the object key;
    it writes nothing else and mutates no entity."""

    def __init__(
        self,
        *,
        roster_service: ExternalChatRosterService,
        operator_profile_repository: OperatorProfileRepositoryPort,
        object_storage: ObjectStoragePort,
    ) -> None:
        self._roster = roster_service
        self._operators = operator_profile_repository
        self._object_storage = object_storage

    async def ingest(
        self,
        *,
        tenant_id: str,
        account_id: str,
        character_id: str,
        data: bytes,
        declared_content_type: str,
    ) -> IngestOutcome:
        operator = await resolve_cloud_operator(
            self._operators, tenant_id=tenant_id, account_id=account_id,
        )
        if operator is None:
            return UnknownAccount()

        # Authorization is exactly roster membership: whatever the roster
        # excludes (foreign character, subscription-denied, locked tenant) is
        # an opaque UnknownCharacter here.
        roster = await self._roster.resolve_roster(
            tenant_id=tenant_id, account_id=account_id,
        )
        chattable = (
            {item.character_id for item in roster.characters}
            if roster is not None
            else set()
        )
        if character_id not in chattable:
            return UnknownCharacter()

        if len(data) > _MAX_BYTES:
            return TooLarge()

        sniffed = sniff_image(
            declared_content_type=declared_content_type, data=data,
        )
        if sniffed is None:
            return UnsupportedType()

        dimensions = image_dimensions(media_type=sniffed.media_type, data=data)
        if dimensions is None:
            # Fail closed (H6): a supported, well-formed raster always exposes
            # its width/height in the header we already sniffed. If we cannot
            # recover them the payload is malformed / truncated / crafted and we
            # cannot bound its decode cost, so it must be rejected rather than
            # accepted on trust.
            return UnsupportedType()
        if _is_pixel_bomb(*dimensions):
            return UnsupportedType()

        object_key = (
            f"{inbound_object_key_prefix(operator.id)}"
            f"{uuid4().hex}.{sniffed.extension}"
        )
        stored = await self._object_storage.put_bytes(
            object_key=object_key,
            content=data,
            content_type=sniffed.media_type,
            metadata={
                "platform": "line",
                "tenant_id": tenant_id,
                "account_id": account_id,
            },
        )
        return Stored(
            attachment=AttachmentRefView(
                object_ref=stored.object_key,
                media_type=stored.content_type,
                size_bytes=stored.size_bytes,
                # Authoritative digest over the accepted bytes; never trust a
                # possibly-absent adapter-side hash for a required contract
                # field.
                sha256=hashlib.sha256(data).hexdigest(),
            ),
        )


def inbound_object_key_prefix(operator_id: str) -> str:
    """The object-key prefix every external-chat inbound attachment for
    ``operator_id`` is stored under.

    The single source of truth for the ``users/{owner}/messaging-inbound/``
    convention: the ingest writes here and the turn service verifies an inbound
    ``object_ref`` is owned by the resolved operator (H5) by prefix-matching
    against this, so a cross-tenant key is rejected before it is ever fetched.
    """
    return f"users/{_safe_owner(operator_id)}/{_INBOUND_SUBDIR}/"


def _is_pixel_bomb(width: int, height: int) -> bool:
    return (
        width > _MAX_EDGE
        or height > _MAX_EDGE
        or width * height > _MAX_PIXELS
    )


def _safe_owner(operator_id: str) -> str:
    """Reduce an operator id to a single object-key-safe path segment.

    Cloud operator ids look like ``cloud:acct-1``; the object-key validator
    rejects the colon, so collapse any run of disallowed characters to a dash
    and fall back to a fixed bucket if nothing survives."""
    cleaned = _OWNER_SANITISE.sub("-", operator_id or "").strip("-")
    return cleaned or "unknown"
