"""HTTP reader for the authenticated cloud-exclusive card payload (EC1).

The mirror image of :mod:`official_card_catalog_client`, and deliberately so
in both directions:

* that one is **anonymous** and reads a public catalog every deployment may
  see; this one carries the ``core_to_user`` service credential and reads a
  route that exists only for hosted Core;
* that one **caches**, because thousands of deployments browse the same
  documents; this one does not, because one install is one read and the
  document it returns is a partner's licensed text, not a shelf.

There is no ``schema_version`` handshake here either. The public catalog
needs one because its readers upgrade on their own schedule; this route has
exactly one consumer holding exactly one credential, so a breaking change is
a coordinated two-repo release rather than a compatibility problem.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

import httpx

from kokoro_link.contracts.official_card_exclusive import (
    ExclusiveCardPayload,
    ExclusiveCardTranslation,
    OfficialCardExclusiveNotFound,
    OfficialCardExclusivePayloadPort,
    OfficialCardExclusiveUnavailable,
    TRANSLATION_STATUS_MISSING,
)
from kokoro_link.infrastructure.cloud.internal_service_auth import (
    InternalServiceCredential,
)

_LOGGER = logging.getLogger(__name__)

EXCLUSIVE_READ_SCOPE = "official-cards:exclusive-read"
"""The scope Cloud's ``official-card-exclusive-payload-read`` policy demands.

Checked *here*, before a client is ever built (see
:func:`build_exclusive_payload_client`), rather than discovered as a 403 at
install time. A deployment whose credential does not carry it cannot install
these cards, and the honest way to say so is to have no client at all — the
catalogue then renders them "cloud only", which is exactly what they are for
that deployment.
"""

_DEFAULT_TIMEOUT_SECONDS = 5.0


class OfficialCardExclusiveClient(OfficialCardExclusivePayloadPort):
    """One authenticated ``GET`` against the hosted User service."""

    def __init__(
        self,
        *,
        base_url: str,
        credential: InternalServiceCredential,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = (base_url or "").strip().rstrip("/")
        self._credential = credential
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def fetch_payload(
        self, card_id: str, *, tenant_id: str | None = None,
    ) -> ExclusiveCardPayload | None:
        if not self._base_url:
            raise OfficialCardExclusiveUnavailable(
                "cloud user service url is not configured",
            )
        url = (
            f"{self._base_url}/internal/v1/official-cards/"
            f"{quote(card_id, safe='')}/exclusive-payload"
        )
        # Omitted entirely when there is no tenant to name, rather than sent
        # blank: an absent parameter is the request every pre-TG deployment
        # made, and it is the one Cloud still answers unchanged for a card
        # with no tier fence on it.
        tenant = (tenant_id or "").strip()
        params = {"tenant_id": tenant} if tenant else {}
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.get(
                    url,
                    params=params,
                    headers={
                        **self._credential.headers(),
                        "Accept": "application/json",
                    },
                )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            _LOGGER.warning(
                "official card exclusive payload unreachable for %s", card_id,
                exc_info=True,
            )
            raise OfficialCardExclusiveUnavailable(str(exc)) from exc

        if response.status_code == 404:
            # Absent, unpublished, not exclusive, frozen, or fenced to a
            # tier this tenant is not in — Cloud answers all of them the
            # same way on purpose, so this side must not pretend to know
            # which. The tier case in particular must never become
            # distinguishable: that would make this endpoint an oracle for
            # what is being tested and for who is in the test.
            raise OfficialCardExclusiveNotFound(card_id)
        if response.status_code in {401, 403}:
            # A credential problem is an operator problem, not a player one:
            # say so loudly in the log and behave like an outage upstream so
            # nothing installs half a character.
            _LOGGER.error(
                "official card exclusive payload rejected this deployment's "
                "service credential (%s) — check that it carries the %s scope",
                response.status_code, EXCLUSIVE_READ_SCOPE,
            )
            raise OfficialCardExclusiveUnavailable(
                f"exclusive payload refused the service credential "
                f"({response.status_code})",
            )
        if response.status_code >= 400:
            raise OfficialCardExclusiveUnavailable(
                f"exclusive payload returned {response.status_code}",
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise OfficialCardExclusiveUnavailable(
                "exclusive payload response is not valid JSON",
            ) from exc
        if not isinstance(body, Mapping):
            raise OfficialCardExclusiveUnavailable(
                "exclusive payload response is not a JSON object",
            )
        return _as_payload(card_id, body)


def build_exclusive_payload_client(
    *,
    base_url: str,
    credential_descriptor: str,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> OfficialCardExclusiveClient | None:
    """The client, or ``None`` when this deployment cannot hold one.

    ``None`` is the self-host answer and it is a *structural* one: no base
    URL, no credential, or a credential without
    :data:`EXCLUSIVE_READ_SCOPE` all mean there is no path to a partner's
    text from here. The caller renders cloud-exclusive cards as "cloud
    only" and greys out their install — which is the truth, rather than a
    button that fails at the far end.
    """
    if not (base_url or "").strip():
        return None
    descriptor = (credential_descriptor or "").strip()
    if not descriptor:
        return None
    try:
        credential = InternalServiceCredential.parse(descriptor)
    except ValueError:
        _LOGGER.warning(
            "cloud user service credential is malformed — cloud-exclusive "
            "card installs stay disabled", exc_info=True,
        )
        return None
    if EXCLUSIVE_READ_SCOPE not in credential.scopes:
        _LOGGER.info(
            "cloud user service credential does not carry the %s scope — "
            "cloud-exclusive cards will list as cloud-only", EXCLUSIVE_READ_SCOPE,
        )
        return None
    return OfficialCardExclusiveClient(
        base_url=base_url,
        credential=credential,
        timeout_seconds=timeout_seconds,
    )


def _as_payload(card_id: str, body: Mapping[str, Any]) -> ExclusiveCardPayload:
    profile = body.get("profile")
    document = body.get("card_document")
    return ExclusiveCardPayload(
        card_id=_text(body.get("card_id")) or card_id,
        title=_text(body.get("title")),
        description=_text(body.get("description")),
        tags=_text_tuple(body.get("tags")),
        author=_text(body.get("author")),
        note=_text(body.get("note")),
        # Passed through as the map it is: the field set belongs to the card
        # translator on both sides of the wire, not to this adapter.
        profile=dict(profile) if isinstance(profile, Mapping) else {},
        # Absent when Cloud could not read the stored artifact, and that is a
        # normal answer rather than a malformed one — an install without the
        # structural document is the pre-EC4-C install, not a failed read.
        # Anything that is not an object is treated the same way for the same
        # reason: this adapter does not validate the manifest schema (the card
        # DTO does, at the point of use), it only refuses to invent one.
        card_document=dict(document) if isinstance(document, Mapping) else {},
        translations=_translations(body.get("translations")),
        reference_image_indexes=_reference_indexes(body.get("reference_images")),
        required_product=_text(body.get("required_product")),
    )


def _translations(raw: object) -> tuple[ExclusiveCardTranslation, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    translations: list[ExclusiveCardTranslation] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        locale = _text(entry.get("locale"))
        if not locale:
            continue
        payload = entry.get("payload")
        translations.append(ExclusiveCardTranslation(
            locale=locale,
            status=_text(entry.get("status")) or TRANSLATION_STATUS_MISSING,
            stale=entry.get("stale") is True,
            payload=dict(payload) if isinstance(payload, Mapping) else {},
        ))
    return tuple(translations)


def _reference_indexes(raw: object) -> tuple[int, ...]:
    """Which stage images are the likeness lock, in the order Cloud listed.

    Only the indexes are kept. The URLs beside them address the same public
    image route the detail document already published, and one list of image
    addresses per install is enough — a second would be a second thing that
    can point somewhere else.
    """
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    indexes: list[int] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        index = entry.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            continue
        if index not in indexes:
            indexes.append(index)
    return tuple(indexes)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _text_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str))


__all__ = [
    "EXCLUSIVE_READ_SCOPE",
    "OfficialCardExclusiveClient",
    "build_exclusive_payload_client",
]
