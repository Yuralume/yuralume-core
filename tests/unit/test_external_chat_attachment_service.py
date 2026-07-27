"""Unit coverage for :class:`ExternalChatAttachmentService` (LH2).

Covers the ingest sealed outcomes: paired-identity fail-closed
(``UnknownAccount``), roster-membership authorization collapsing foreign /
subscription-denied / locked-tenant into an opaque ``UnknownCharacter``, the
size ceiling (``TooLarge``), magic-byte sniffing and pixel-bomb header
rejection (``UnsupportedType``), and a happy path whose ``sha256`` / size /
object key are exactly right. Images are generated as raw header bytes so no
Pillow dependency is required.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone

from kokoro_link.application.services.external_chat_attachment_service import (
    ExternalChatAttachmentService,
    Stored,
    TooLarge,
    UnknownAccount,
    UnknownCharacter,
    UnsupportedType,
)
from kokoro_link.application.services.external_chat_roster_service import (
    ExternalChatRosterService,
)
from kokoro_link.application.services.subscription_access_guard import (
    SubscriptionAccessGuard,
)
from kokoro_link.domain.entities.character import (
    FREEZE_REASON_SUBSCRIPTION_LAPSE,
    Character,
)
from kokoro_link.domain.entities.cloud_subscription import CloudSubscriptionState
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage

_TENANT = "tenant-A"
_ACCOUNT = "acct-1"
_OPERATOR_ID = "cloud:acct-1"
_CHARACTER_ID = "c1"


# --- image byte generators (header-only, no Pillow) ------------------------


def png_bytes(width: int, height: int) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr_payload = (
        width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
    )
    ihdr = (
        len(ihdr_payload).to_bytes(4, "big")
        + b"IHDR"
        + ihdr_payload
        + b"\x00\x00\x00\x00"  # placeholder CRC — never validated
    )
    return signature + ihdr


def jpeg_bytes(width: int, height: int) -> bytes:
    soi = b"\xff\xd8"
    sof0 = (
        b"\xff\xc0"
        + (17).to_bytes(2, "big")
        + b"\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    )
    return soi + sof0 + b"\xff\xd9"


def gif_bytes(width: int, height: int) -> bytes:
    return (
        b"GIF89a"
        + width.to_bytes(2, "little")
        + height.to_bytes(2, "little")
        + b"\x80\x00\x00"
    )


def webp_vp8x_bytes(width: int, height: int) -> bytes:
    body = (
        b"VP8X"
        + (10).to_bytes(4, "little")
        + b"\x00"
        + b"\x00\x00\x00"
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little")
    )
    return b"RIFF" + (len(body) + 4).to_bytes(4, "little") + b"WEBP" + body


# --- fakes -----------------------------------------------------------------


class _FakeOperatorRepo:
    def __init__(self, operators: list[OperatorProfile]) -> None:
        self._by_id = {op.id: op for op in operators}

    async def get(self, operator_id: str) -> OperatorProfile | None:
        return self._by_id.get(operator_id)

    async def get_by_cloud_account_id(
        self, cloud_account_id: str,
    ) -> OperatorProfile | None:
        target = cloud_account_id.strip()
        for op in self._by_id.values():
            if (op.cloud_account_id or "") == target:
                return op
        return None


class _FakeSubscriptionRepo:
    def __init__(self, locked_tenants: tuple[str, ...] = ()) -> None:
        self._locked = set(locked_tenants)

    async def get(self, tenant_id: str) -> CloudSubscriptionState | None:
        if tenant_id in self._locked:
            return CloudSubscriptionState(
                tenant_id=tenant_id,
                locked=True,
                updated_at=datetime.now(timezone.utc),
            )
        return None


class _FakeCharacterRepo:
    def __init__(self, by_user: dict[str, list[Character]]) -> None:
        self._by_user = by_user

    async def list_for_user(self, user_id: str) -> list[Character]:
        return list(self._by_user.get(user_id, []))


def _operator(auth_provider: str = "cloud") -> OperatorProfile:
    return OperatorProfile(
        id=_OPERATOR_ID,
        display_name="Player",
        cloud_account_id=_ACCOUNT,
        cloud_tenant_id=_TENANT,
        auth_provider=auth_provider,
    )


def _character(
    *,
    character_id: str = _CHARACTER_ID,
    frozen: bool = False,
    frozen_reason: str | None = None,
) -> Character:
    state = CharacterState(
        emotion="calm", affection=50, fatigue=20, trust=50, energy=60,
    )
    return Character(
        id=character_id,
        name="Yui",
        summary="",
        user_id=_OPERATOR_ID,
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=state,
        image_urls=(),
        frozen=frozen,
        frozen_reason=frozen_reason,
    )


def _service(
    *,
    characters: list[Character] | None = None,
    operators: list[OperatorProfile] | None = None,
    locked_tenants: tuple[str, ...] = (),
    storage: InMemoryObjectStorage | None = None,
) -> ExternalChatAttachmentService:
    ops = operators if operators is not None else [_operator()]
    operator_repo = _FakeOperatorRepo(ops)
    guard = SubscriptionAccessGuard(
        subscription_repository=_FakeSubscriptionRepo(locked_tenants),
        operator_profile_repository=operator_repo,
    )
    by_user: dict[str, list[Character]] = {}
    for ch in characters or []:
        by_user.setdefault(ch.user_id, []).append(ch)
    object_storage = storage if storage is not None else InMemoryObjectStorage()
    roster = ExternalChatRosterService(
        character_repository=_FakeCharacterRepo(by_user),
        operator_profile_repository=operator_repo,
        subscription_access_guard=guard,
        object_storage=object_storage,
    )
    return ExternalChatAttachmentService(
        roster_service=roster,
        operator_profile_repository=operator_repo,
        object_storage=object_storage,
    )


def _ingest(
    service: ExternalChatAttachmentService,
    *,
    tenant_id: str = _TENANT,
    account_id: str = _ACCOUNT,
    character_id: str = _CHARACTER_ID,
    data: bytes,
    declared_content_type: str = "image/png",
):
    return asyncio.run(
        service.ingest(
            tenant_id=tenant_id,
            account_id=account_id,
            character_id=character_id,
            data=data,
            declared_content_type=declared_content_type,
        ),
    )


# --- pairing / authorization -----------------------------------------------


def test_unknown_account_returns_unknown_account() -> None:
    service = _service(characters=[_character()])
    outcome = _ingest(service, account_id="ghost", data=png_bytes(4, 4))
    assert isinstance(outcome, UnknownAccount)


def test_tenant_mismatch_returns_unknown_account() -> None:
    service = _service(characters=[_character()])
    outcome = _ingest(service, tenant_id="tenant-OTHER", data=png_bytes(4, 4))
    assert isinstance(outcome, UnknownAccount)


def test_non_cloud_operator_returns_unknown_account() -> None:
    service = _service(
        operators=[_operator(auth_provider="local")],
        characters=[_character()],
    )
    outcome = _ingest(service, data=png_bytes(4, 4))
    assert isinstance(outcome, UnknownAccount)


def test_character_not_belonging_returns_unknown_character() -> None:
    service = _service(characters=[_character()])
    outcome = _ingest(service, character_id="nope", data=png_bytes(4, 4))
    assert isinstance(outcome, UnknownCharacter)


def test_subscription_denied_character_is_opaque_unknown_character() -> None:
    lapsed = _character(
        frozen=True, frozen_reason=FREEZE_REASON_SUBSCRIPTION_LAPSE,
    )
    service = _service(characters=[lapsed])
    outcome = _ingest(service, data=png_bytes(4, 4))
    assert isinstance(outcome, UnknownCharacter)


def test_locked_tenant_is_opaque_unknown_character() -> None:
    service = _service(characters=[_character()], locked_tenants=(_TENANT,))
    outcome = _ingest(service, data=png_bytes(4, 4))
    assert isinstance(outcome, UnknownCharacter)


# --- size ------------------------------------------------------------------


def test_oversize_returns_too_large() -> None:
    service = _service(characters=[_character()])
    data = png_bytes(4, 4) + b"\x00" * (8 * 1024 * 1024)
    outcome = _ingest(service, data=data)
    assert isinstance(outcome, TooLarge)


# --- sniffing --------------------------------------------------------------


def test_declared_type_content_mismatch_returns_unsupported() -> None:
    service = _service(characters=[_character()])
    # Declares PNG but the bytes are a JPEG.
    outcome = _ingest(
        service, data=jpeg_bytes(4, 4), declared_content_type="image/png",
    )
    assert isinstance(outcome, UnsupportedType)


def test_unsupported_declared_type_returns_unsupported() -> None:
    service = _service(characters=[_character()])
    outcome = _ingest(
        service, data=png_bytes(4, 4), declared_content_type="image/bmp",
    )
    assert isinstance(outcome, UnsupportedType)


def test_image_jpg_alias_is_not_accepted() -> None:
    service = _service(characters=[_character()])
    outcome = _ingest(
        service, data=jpeg_bytes(4, 4), declared_content_type="image/jpg",
    )
    assert isinstance(outcome, UnsupportedType)


# --- pixel bomb ------------------------------------------------------------


def test_oversized_edge_header_is_pixel_bomb() -> None:
    service = _service(characters=[_character()])
    outcome = _ingest(service, data=png_bytes(9000, 10))
    assert isinstance(outcome, UnsupportedType)


def test_oversized_area_header_is_pixel_bomb() -> None:
    service = _service(characters=[_character()])
    # 8000 * 8000 = 64,000,000 > 33,554,432 while each edge stays <= 8192.
    outcome = _ingest(service, data=png_bytes(8000, 8000))
    assert isinstance(outcome, UnsupportedType)


# --- happy path (each supported type) --------------------------------------


def test_png_success_reports_exact_sha256_size_and_key() -> None:
    storage = InMemoryObjectStorage()
    service = _service(characters=[_character()], storage=storage)
    data = png_bytes(4, 4)
    outcome = _ingest(service, data=data)
    assert isinstance(outcome, Stored)
    ref = outcome.attachment
    assert ref.media_type == "image/png"
    assert ref.size_bytes == len(data)
    assert ref.sha256 == hashlib.sha256(data).hexdigest()
    assert ref.object_ref.startswith("users/cloud-acct-1/messaging-inbound/")
    assert ref.object_ref.endswith(".png")
    # The stored bytes are exactly what we validated.
    assert asyncio.run(storage.get_bytes(object_key=ref.object_ref)) == data


def test_supported_types_land_with_matching_extension() -> None:
    cases = [
        ("image/jpeg", jpeg_bytes(8, 8), ".jpg"),
        ("image/gif", gif_bytes(8, 8), ".gif"),
        ("image/webp", webp_vp8x_bytes(8, 8), ".webp"),
    ]
    for content_type, data, suffix in cases:
        service = _service(characters=[_character()])
        outcome = _ingest(
            service, data=data, declared_content_type=content_type,
        )
        assert isinstance(outcome, Stored), content_type
        assert outcome.attachment.media_type == content_type
        assert outcome.attachment.object_ref.endswith(suffix)
        assert outcome.attachment.sha256 == hashlib.sha256(data).hexdigest()
