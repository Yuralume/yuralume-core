"""The authenticated cloud-exclusive payload reader — EC4 against EC1.

Driven by ``httpx.MockTransport``, so the wire shape (snake_case body, the
service-credential headers, the deliberate single 404) is pinned against the
document Cloud actually serves and no test touches the network.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from kokoro_link.contracts.official_card_exclusive import (
    OfficialCardExclusiveNotFound,
    OfficialCardExclusiveUnavailable,
    TRANSLATION_STATUS_APPROVED,
    TRANSLATION_STATUS_MISSING,
)
from kokoro_link.infrastructure.cloud.internal_service_auth import (
    InternalServiceCredential,
)
from kokoro_link.infrastructure.cloud.official_card_exclusive_client import (
    EXCLUSIVE_READ_SCOPE,
    OfficialCardExclusiveClient,
    build_exclusive_payload_client,
)

_BASE = "https://app.yuralume.com/api/user"
_CARD_ID = "mio-cafe"
_DESCRIPTOR = (
    f"key-1|core|yuralume-user|{EXCLUSIVE_READ_SCOPE},announcements:read|s3cr3t"
)

_BODY: dict[str, Any] = {
    "card_id": _CARD_ID,
    "title": "美緒",
    "description": "官方卡",
    "tags": ["現代"],
    "author": "Yuralume",
    "note": "建議動漫風",
    "profile": {"name": "美緒", "summary": "咖啡廳打工女大生"},
    "translations": [
        {
            "locale": "en",
            "status": "approved",
            "stale": False,
            "payload": {"name": "Mio"},
        },
        {"locale": "ja", "status": "missing", "stale": False, "payload": {}},
    ],
    "reference_images": [
        {
            "index": 1,
            "url": "/v1/public/official-cards/mio-cafe/images/1",
            "content_type": "image/png",
            "width": 1024,
            "height": 1536,
        },
    ],
}


def _client(handler) -> OfficialCardExclusiveClient:
    return OfficialCardExclusiveClient(
        base_url=_BASE,
        credential=InternalServiceCredential.parse(_DESCRIPTOR),
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_the_payload_is_read_with_the_service_credential() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        return httpx.Response(200, json=_BODY)

    payload = await _client(handler).fetch_payload(_CARD_ID)

    assert seen["url"] == (
        f"{_BASE}/internal/v1/official-cards/{_CARD_ID}/exclusive-payload"
    )
    assert seen["headers"]["X-Yuralume-Service-Caller"] == "core"
    assert seen["headers"]["X-Yuralume-Service-Audience"] == "yuralume-user"
    assert EXCLUSIVE_READ_SCOPE in seen["headers"]["X-Yuralume-Service-Scope"]
    assert seen["headers"]["X-Yuralume-Service-Token"] == "s3cr3t"

    assert payload is not None
    assert payload.card_id == _CARD_ID
    assert payload.profile["name"] == "美緒"
    assert payload.reference_image_indexes == (1,)
    assert payload.required_product == ""


@pytest.mark.asyncio
async def test_the_installing_tenant_is_named_when_there_is_one() -> None:
    """TG3: the tier fence is checked on Cloud, against the tenant id.

    What travels is the *id*, never the tier Core has cached on the
    operator profile — that copy drifts, and the side that owns the answer
    is the side that must resolve it (plan D4).
    """
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_BODY)

    await _client(handler).fetch_payload(_CARD_ID, tenant_id="tenant-42")

    assert seen["url"] == (
        f"{_BASE}/internal/v1/official-cards/{_CARD_ID}"
        f"/exclusive-payload?tenant_id=tenant-42"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant_id", [None, "", "   "])
async def test_without_a_tenant_the_request_is_byte_for_byte_the_old_one(
    tenant_id: str | None,
) -> None:
    """The parameter is omitted, not sent blank.

    A self-hosted deployment and a hosted account with no projected tenant
    both make the pre-TG request, which is the one Cloud still answers
    unchanged for every card with no tier fence on it. Sending
    ``?tenant_id=`` instead would be a new request shape for the case that
    is supposed to be untouched.
    """
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_BODY)

    await _client(handler).fetch_payload(_CARD_ID, tenant_id=tenant_id)

    assert seen["url"] == (
        f"{_BASE}/internal/v1/official-cards/{_CARD_ID}/exclusive-payload"
    )


@pytest.mark.asyncio
async def test_the_cards_own_manifest_is_read_through_as_the_map_it_is() -> None:
    # EC4-C: the structural half. Passed through untouched for the same
    # reason ``profile`` is — the manifest schema belongs to the card
    # exporter, and this adapter is not a second copy of it.
    document = {
        "schema_version": 1,
        "card": {"title": "", "author": "Yuralume"},
        "character": {
            "name": "美緒",
            "world_frame": "school",
            "allowed_tools": ["generate_image"],
        },
        "stage_images": ["assets/stage/0.png"],
    }

    payload = await _client(
        lambda _request: httpx.Response(
            200, json={**_BODY, "card_document": document},
        ),
    ).fetch_payload(_CARD_ID)

    assert payload is not None
    assert payload.card_document == document


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [_BODY, {**_BODY, "card_document": "nonsense"}])
async def test_an_absent_or_unusable_card_document_reads_as_empty(
    body: dict[str, Any],
) -> None:
    # Cloud omits the key rather than fail an install when the stored
    # artifact is unreadable, so "absent" is an ordinary answer and has to
    # arrive as "nothing to apply" — never as a parse failure that would
    # turn a fidelity shortfall into a refused install.
    payload = await _client(
        lambda _request: httpx.Response(200, json=body),
    ).fetch_payload(_CARD_ID)

    assert payload is not None
    assert payload.card_document == {}
    assert payload.profile["name"] == "美緒"


@pytest.mark.asyncio
async def test_a_missing_locale_is_present_and_unusable_rather_than_absent(
) -> None:
    # "Is this language available" must be a field to read, not a set
    # difference to compute.
    payload = await _client(
        lambda _request: httpx.Response(200, json=_BODY),
    ).fetch_payload(_CARD_ID)

    assert payload is not None
    japanese = payload.translation("ja")
    assert japanese is not None
    assert japanese.status == TRANSLATION_STATUS_MISSING
    assert japanese.usable is False
    english = payload.translation("en")
    assert english is not None
    assert english.status == TRANSLATION_STATUS_APPROVED
    assert english.usable is True


@pytest.mark.asyncio
async def test_a_stale_approved_translation_is_reported_but_not_usable() -> None:
    body = {
        **_BODY,
        "translations": [
            {
                "locale": "en",
                "status": "approved",
                "stale": True,
                "payload": {"name": "Mio"},
            },
        ],
    }

    payload = await _client(
        lambda _request: httpx.Response(200, json=body),
    ).fetch_payload(_CARD_ID)

    assert payload is not None
    english = payload.translation("en")
    assert english is not None
    assert english.stale is True
    assert english.usable is False


@pytest.mark.asyncio
async def test_a_404_is_not_found_whatever_it_actually_meant() -> None:
    # Cloud answers "no such card", "not published" and "not exclusive" with
    # the same 404 on purpose; this side must not invent a distinction.
    with pytest.raises(OfficialCardExclusiveNotFound):
        await _client(
            lambda _request: httpx.Response(404, json={"error": "nope"}),
        ).fetch_payload(_CARD_ID)


@pytest.mark.asyncio
async def test_a_rejected_credential_is_an_outage_not_a_missing_card() -> None:
    # An operator misconfiguration must not read as "this card was
    # withdrawn" — that would send the wrong person looking.
    with pytest.raises(OfficialCardExclusiveUnavailable):
        await _client(
            lambda _request: httpx.Response(403),
        ).fetch_payload(_CARD_ID)


@pytest.mark.asyncio
async def test_an_unreachable_service_is_an_outage() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(OfficialCardExclusiveUnavailable):
        await _client(handler).fetch_payload(_CARD_ID)


# --------------------------------------------------------------------------- #
# who gets a client at all
# --------------------------------------------------------------------------- #


def test_a_credential_carrying_the_scope_builds_a_client() -> None:
    assert build_exclusive_payload_client(
        base_url=_BASE, credential_descriptor=_DESCRIPTOR,
    ) is not None


@pytest.mark.parametrize(
    ("base_url", "descriptor"),
    [
        # Self-host: no cloud user service at all.
        ("", _DESCRIPTOR),
        # Cloud mode with no internal credential configured.
        (_BASE, ""),
        # A credential that exists but was never granted the scope — the
        # honest answer is no client, not a button that 403s at the far end.
        (_BASE, "key-1|core|yuralume-user|announcements:read|s3cr3t"),
        # Malformed descriptor: a misconfiguration must not crash boot.
        (_BASE, "not-a-descriptor"),
    ],
)
def test_no_client_without_a_usable_exclusive_read_credential(
    base_url: str, descriptor: str,
) -> None:
    assert build_exclusive_payload_client(
        base_url=base_url, credential_descriptor=descriptor,
    ) is None
