"""The tier-gated official-card shelf reader — TG3 against TG1.

Driven by ``httpx.MockTransport``, so the wire shape (the two query
parameters, the service-credential headers, the row projection) is pinned
against the document Cloud actually serves and no test touches the network.

The other half of what these pin is the fail-soft rule: every way this read
can go wrong has to end as "no tier-locked rows", never as an exception
climbing into a player's gallery.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from kokoro_link.contracts.official_card_catalog import (
    OfficialCardCatalogUnavailable,
)
from kokoro_link.infrastructure.cloud.internal_service_auth import (
    InternalServiceCredential,
)
from kokoro_link.infrastructure.cloud.official_card_catalog_client import (
    OfficialCardCatalogClient,
)
from kokoro_link.infrastructure.cloud.official_card_exclusive_client import (
    EXCLUSIVE_READ_SCOPE,
)
from kokoro_link.infrastructure.cloud.official_card_gated_catalog_client import (
    OfficialCardGatedCatalogClient,
    build_gated_catalog_client,
)

_BASE = "https://users.example"
"""The *internal* User service, where the gated document is read.

Deliberately a different host from the public catalog below: they are two
different addresses in production, and a test that used one string for both
could not see an image URL built against the wrong one. A placeholder host
rather than the real one: this file ships in the public repo, where the
hosted deployment's internal address has no business appearing.
"""

_CATALOG_URL = "https://app.yuralume.com/api/user/v1/public/official-cards"
"""The anonymous catalog — where the *art* on those rows is served from."""

_CARD_ID = "mio-wip"
_TENANT = "tenant-42"
_DESCRIPTOR = (
    f"key-1|core|yuralume-user|{EXCLUSIVE_READ_SCOPE},announcements:read|s3cr3t"
)

_BODY: dict[str, Any] = {
    "schema_version": 1,
    "cards": [
        {
            "id": _CARD_ID,
            "distribution": "cloud_exclusive",
            "locale": "ja",
            "localized": True,
            "stale": False,
            "available_locales": ["zh-Hant", "en", "ja"],
            "card": {
                "id": _CARD_ID,
                "title": "ミオ（調整中）",
                "description": "社内テスト中の公式カード",
                "tags": ["現代"],
                "author": "Yuralume",
                "note": "アニメ調",
                # Masked exactly as an anonymous cloud-exclusive detail is.
                "artifact_url": None,
                "profile": {},
                "images": [
                    {
                        "index": 0,
                        "url": "/v1/public/official-cards/mio-wip/images/0",
                        "content_type": "image/png",
                        "width": 1024,
                        "height": 1536,
                        "variants": ["w320", "w768"],
                    },
                ],
                "updated_at": "2026-08-14T00:00:00Z",
                "published_at": "2026-08-13T00:00:00Z",
            },
        },
    ],
}


def _anonymous(handler=None) -> OfficialCardCatalogClient:
    """The real public-catalog client, not a stand-in.

    Its ``absolutise`` is what the gated client is wired to in the container,
    and its download guard is what an image URL has to satisfy — so both
    halves of the chain are the production objects here.
    """
    return OfficialCardCatalogClient(
        base_url=_CATALOG_URL,
        transport=httpx.MockTransport(handler) if handler else None,
    )


def _client(handler) -> OfficialCardGatedCatalogClient:
    return OfficialCardGatedCatalogClient(
        base_url=_BASE,
        credential=InternalServiceCredential.parse(_DESCRIPTOR),
        absolutise_asset=_anonymous().absolutise,
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_the_shelf_is_read_with_the_service_credential_for_one_tenant(
) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        return httpx.Response(200, json=_BODY)

    rows = await _client(handler).list_gated_cards(
        tenant_id=_TENANT, locale="ja",
    )

    assert seen["url"] == (
        f"{_BASE}/internal/v1/official-cards/gated-catalog"
        f"?tenant_id={_TENANT}&locale=ja"
    )
    assert seen["headers"]["X-Yuralume-Service-Caller"] == "core"
    assert seen["headers"]["X-Yuralume-Service-Audience"] == "yuralume-user"
    assert EXCLUSIVE_READ_SCOPE in seen["headers"]["X-Yuralume-Service-Scope"]
    assert seen["headers"]["X-Yuralume-Service-Token"] == "s3cr3t"

    assert rows is not None
    assert len(rows) == 1
    row = rows[0]
    assert row.id == _CARD_ID
    assert row.title == "ミオ（調整中）"
    assert row.description == "社内テスト中の公式カード"
    assert row.tags == ("現代",)
    assert row.note == "アニメ調"
    # Per row, not per document: two cards on one shelf can have been
    # translated to different degrees.
    assert row.locale == "ja"
    assert row.localized is True
    assert row.stale is False
    assert row.available_locales == ("zh-Hant", "en", "ja")


@pytest.mark.asyncio
async def test_a_gated_row_reads_as_cloud_exclusive_whatever_the_field_said(
) -> None:
    """Its artifact is a 404 on every anonymous route, by construction.

    So "there is a downloadable ``.lumecard``" is false for these cards
    regardless of what the document carries, and the fail-safe direction is
    to say so here rather than derive it from a field that is always null:
    the install path this answer selects is the authenticated one, which is
    the only path such a card has.
    """
    body = {
        "schema_version": 1,
        "cards": [
            {
                **_BODY["cards"][0],
                # A Cloud that stopped nulling the field must not turn a
                # tier-locked card into one Core tries to download openly.
                "card": {
                    **_BODY["cards"][0]["card"],
                    "artifact_url": "/v1/public/official-cards/mio-wip/artifact",
                },
            },
        ],
    }

    rows = await _client(
        lambda _request: httpx.Response(200, json=body),
    ).list_gated_cards(tenant_id=_TENANT, locale="ja")

    assert rows is not None
    assert rows[0].artifact_published is False
    assert rows[0].cloud_exclusive is True


@pytest.mark.asyncio
async def test_image_paths_are_absolutised_against_the_public_catalog() -> None:
    """Not against the User service the document itself came from.

    A tier-locked card hides its text, not its art (plan D3 / R-TG-1): the
    images stay on the anonymous catalog route, and both the browser and the
    installer fetch them from there. Building them on the internal host
    instead is invisible at this layer and fatal one layer down — see the
    next test.
    """
    rows = await _client(
        lambda _request: httpx.Response(200, json=_BODY),
    ).list_gated_cards(tenant_id=_TENANT, locale="ja")

    assert rows is not None
    image = rows[0].images[0]
    # Public host, and carrying the ``/api/user`` mount prefix the edge adds
    # — which the internal service URL knows nothing about.
    assert image.url == (
        "https://app.yuralume.com/api/user"
        "/v1/public/official-cards/mio-wip/images/0"
    )
    assert image.content_type == "image/png"
    assert image.variants == ("w320", "w768")


@pytest.mark.asyncio
async def test_a_gated_rows_image_is_one_the_anonymous_downloader_accepts(
) -> None:
    """The regression that made this a HIGH rather than a cosmetic URL bug.

    Installing a tier-locked card downloads its stage images through the
    anonymous client, which refuses any URL outside its own asset origin.
    A URL built against the internal User service is refused there, the
    failure is swallowed as "no image", and the card installs with zero
    reference art and nothing in the response saying so.
    """
    rows = await _client(
        lambda _request: httpx.Response(200, json=_BODY),
    ).list_gated_cards(tenant_id=_TENANT, locale="ja")
    assert rows is not None

    downloader = _anonymous(
        lambda _request: httpx.Response(200, content=b"png-bytes"),
    )

    assert await downloader.fetch_image(rows[0].images[0].url) == b"png-bytes"
    # And the guard that would have caught it is really armed.
    with pytest.raises(OfficialCardCatalogUnavailable):
        await downloader.fetch_image(
            f"{_BASE}/v1/public/official-cards/mio-wip/images/0",
        )


@pytest.mark.asyncio
async def test_a_tenant_with_no_locked_cards_is_an_empty_shelf_not_a_failure(
) -> None:
    rows = await _client(
        lambda _request: httpx.Response(
            200, json={"schema_version": 1, "cards": []},
        ),
    ).list_gated_cards(tenant_id=_TENANT, locale="en")

    # ``[]`` and ``None`` mean different things to the caller: nothing to
    # show versus could not find out.
    assert rows == []


@pytest.mark.asyncio
async def test_an_account_with_no_cloud_tenant_never_reaches_the_wire() -> None:
    """Nothing is wrong, and nothing can be learned by asking.

    A blank tenant is what a self-host-shaped account looks like, and the
    answer is knowable here — so the round trip is skipped rather than
    spent, and the result is the empty shelf rather than the unknown one.
    """
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        calls.append(str(request.url))
        return httpx.Response(200, json=_BODY)

    rows = await _client(handler).list_gated_cards(tenant_id="  ", locale="ja")

    assert rows == []
    assert calls == []


@pytest.mark.asyncio
async def test_a_malformed_row_is_skipped_rather_than_blanking_the_shelf(
) -> None:
    body = {
        "schema_version": 1,
        "cards": [
            "not-an-object",
            {"id": "no-card-object"},
            {"card": {"id": "", "title": "nameless"}},
            _BODY["cards"][0],
        ],
    }

    rows = await _client(
        lambda _request: httpx.Response(200, json=body),
    ).list_gated_cards(tenant_id=_TENANT, locale="ja")

    assert rows is not None
    assert [row.id for row in rows] == [_CARD_ID]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        # A credential problem: an operator problem, and the player-side
        # symptom is silence, so it must not also be an exception.
        httpx.Response(401),
        httpx.Response(403),
        httpx.Response(500),
        # Not JSON, and JSON that is not a document.
        httpx.Response(200, content=b"<html>nope</html>"),
        httpx.Response(200, json=["not", "an", "object"]),
        # No card list at all.
        httpx.Response(200, json={"schema_version": 1}),
        # A document shape this build cannot read: refusing beats rendering
        # a mangled card, and an absent shelf is the recoverable version.
        httpx.Response(200, json={"schema_version": 99, "cards": []}),
    ],
)
async def test_every_bad_answer_is_none_rather_than_an_exception(
    response: httpx.Response,
) -> None:
    rows = await _client(
        lambda _request: response,
    ).list_gated_cards(tenant_id=_TENANT, locale="ja")

    assert rows is None


@pytest.mark.asyncio
async def test_an_unreachable_service_is_none_not_a_broken_gallery() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    rows = await _client(handler).list_gated_cards(
        tenant_id=_TENANT, locale="ja",
    )

    assert rows is None


# --------------------------------------------------------------------------- #
# who gets a client at all
# --------------------------------------------------------------------------- #


def test_a_credential_carrying_the_scope_builds_a_client() -> None:
    assert build_gated_catalog_client(
        base_url=_BASE,
        credential_descriptor=_DESCRIPTOR,
        absolutise_asset=_anonymous().absolutise,
    ) is not None


@pytest.mark.parametrize(
    ("base_url", "descriptor"),
    [
        # Self-host: no cloud user service at all.
        ("", _DESCRIPTOR),
        # Cloud mode with no internal credential configured.
        (_BASE, ""),
        # A credential that exists but was never granted the scope.
        (_BASE, "key-1|core|yuralume-user|announcements:read|s3cr3t"),
        # Malformed descriptor: a misconfiguration must not crash boot.
        (_BASE, "not-a-descriptor"),
    ],
)
def test_no_client_without_a_usable_exclusive_read_credential(
    base_url: str, descriptor: str,
) -> None:
    # The same three checks the exclusive payload client makes, so a
    # deployment can never be able to *see* a tier-locked card without also
    # being able to install it.
    assert build_gated_catalog_client(
        base_url=base_url,
        credential_descriptor=descriptor,
        absolutise_asset=_anonymous().absolutise,
    ) is None
