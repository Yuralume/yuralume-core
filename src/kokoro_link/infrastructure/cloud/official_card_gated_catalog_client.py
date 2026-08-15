"""HTTP reader for the tier-gated official-card shelf (TG3).

Structurally the twin of :mod:`official_card_exclusive_client` — same
``core_to_user`` credential, same ``official-cards:exclusive-read`` scope,
same "no credential, no client" rule — and it reads the second route that
scope opens: ``GET /internal/v1/official-cards/gated-catalog``.

Two differences from that module are decisions rather than accidents:

* **It never raises.** The exclusive payload is read with a player waiting
  on one named card, so "Cloud could not answer" has to reach them as a
  distinguishable outcome. This is a *shelf* read: the honest fail-soft
  answer is ``None``, which the pack source renders as "no tier-locked rows
  this time" while the rest of the gallery carries on (plan D6).
* **It does not cache, and no layer above it does either.** These rows are
  per-tenant-tier; the shared catalog cache one layer up is per deployment,
  and dropping per-tier rows into it would serve one tester's WIP card to
  the next player who opened the gallery.

The response is the anonymous detail document's ``card`` object, one per
row, which is why the rows are parsed into
:class:`~kokoro_link.contracts.official_card_catalog.OfficialCardDetail` —
Cloud is not publishing a new shape here, it is publishing the same shape
through a door that checks who is asking.

**Two URLs, two jobs.** The document is read from the User service
(``base_url``), which is an internal address; the *art* those rows point at
is served by the public catalog and is fetched anonymously by the browser
and by the installer alike (plan D3 / R-TG-1). So the image paths are
absolutised by the anonymous
:class:`~kokoro_link.infrastructure.cloud.official_card_catalog_client.OfficialCardCatalogClient`'s
own ``absolutise``, handed in at construction — not by joining them to
``base_url``. Joining them there would produce a URL on the internal host,
which the anonymous downloader's asset-origin guard rejects: the install
then lands a character with no stage images at all, silently, because a
failed image download is fail-soft by design. Sharing the function rather
than re-deriving the origin is what keeps the two shelves' image URLs the
same URL.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence

import httpx

from kokoro_link.contracts.official_card_catalog import (
    OfficialCardDetail,
    OfficialCardImage,
)
from kokoro_link.contracts.official_card_gated_catalog import (
    OfficialCardGatedCatalogPort,
)
from kokoro_link.infrastructure.cloud.internal_service_auth import (
    InternalServiceCredential,
)
from kokoro_link.infrastructure.cloud.official_card_catalog_client import (
    CATALOG_SCHEMA_VERSION,
)
from kokoro_link.infrastructure.cloud.official_card_exclusive_client import (
    EXCLUSIVE_READ_SCOPE,
)

_LOGGER = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 5.0

_GATED_CATALOG_PATH = "/internal/v1/official-cards/gated-catalog"


class OfficialCardGatedCatalogClient(OfficialCardGatedCatalogPort):
    """One authenticated ``GET`` per browse, and no cache behind it."""

    def __init__(
        self,
        *,
        base_url: str,
        credential: InternalServiceCredential,
        absolutise_asset: Callable[[str], str],
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = (base_url or "").strip().rstrip("/")
        self._credential = credential
        # The anonymous catalog client's own absolutiser, injected rather
        # than reimplemented. See the module docstring: these rows' art
        # lives on the public catalog origin, and the anonymous downloader
        # refuses any image URL that is not on it.
        self._absolutise_asset = absolutise_asset
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def list_gated_cards(
        self, *, tenant_id: str, locale: str,
    ) -> list[OfficialCardDetail] | None:
        tenant = (tenant_id or "").strip()
        if not tenant:
            # An account with no projected Cloud tenant has no tier, and a
            # request that named nobody would come back empty anyway. Saying
            # so here keeps every self-host-shaped account off the wire
            # instead of paying a round trip to be told what is already
            # known — and it is ``[]``, not ``None``: nothing is wrong.
            return []
        if not self._base_url:
            return None
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.get(
                    f"{self._base_url}{_GATED_CATALOG_PATH}",
                    params={"tenant_id": tenant, "locale": locale},
                    headers={
                        **self._credential.headers(),
                        "Accept": "application/json",
                    },
                )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            _LOGGER.warning(
                "official card gated catalog unreachable: %s", exc, exc_info=True,
            )
            return None

        if response.status_code in {401, 403}:
            # An operator problem, not a player one — and loud, because the
            # symptom on the player side is silence: the tier-locked cards
            # simply never appear and nobody would think to ask why.
            _LOGGER.error(
                "official card gated catalog rejected this deployment's "
                "service credential (%s) — check that it carries the %s scope",
                response.status_code, EXCLUSIVE_READ_SCOPE,
            )
            return None
        if response.status_code >= 400:
            _LOGGER.warning(
                "official card gated catalog returned %s", response.status_code,
            )
            return None
        try:
            body = response.json()
        except ValueError:
            _LOGGER.warning(
                "official card gated catalog response is not valid JSON",
                exc_info=True,
            )
            return None
        if not isinstance(body, Mapping):
            _LOGGER.warning(
                "official card gated catalog response is not a JSON object",
            )
            return None
        declared = body.get("schema_version")
        if isinstance(declared, int) and declared > CATALOG_SCHEMA_VERSION:
            # Same refusal the public catalog makes, for the same reason:
            # half-understanding a newer document renders a mangled card,
            # and an absent shelf is the recoverable version of that.
            _LOGGER.warning(
                "official card gated catalog schema_version %s is newer than "
                "this build supports (%s)", declared, CATALOG_SCHEMA_VERSION,
            )
            return None
        raw_rows = body.get("cards")
        if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes)):
            _LOGGER.warning("official card gated catalog response has no card list")
            return None
        rows: list[OfficialCardDetail] = []
        for entry in raw_rows:
            row = self._as_detail(entry)
            if row is not None:
                rows.append(row)
        return rows

    # -- internals ---------------------------------------------------------- #

    def _as_detail(self, entry: object) -> OfficialCardDetail | None:
        """One row, or ``None`` when it is not a readable one.

        A malformed row is skipped rather than allowed to blank the shelf,
        exactly as the public catalog skips an entry without an id: one bad
        document upstream must not cost the tester every other card.
        """
        if not isinstance(entry, Mapping):
            return None
        card = entry.get("card")
        if not isinstance(card, Mapping):
            return None
        card_id = _text(card.get("id")) or _text(entry.get("id"))
        if not card_id:
            _LOGGER.warning(
                "official card gated catalog: row without an id — skipping",
            )
            return None
        profile = card.get("profile")
        return OfficialCardDetail(
            id=card_id,
            title=_text(card.get("title")),
            description=_text(card.get("description")),
            tags=_text_tuple(card.get("tags")),
            author=_text(card.get("author")),
            note=_text(card.get("note")),
            # Per row, not per document: this route answers one locale
            # question per card, because a card translated into Japanese and
            # one that is not sit side by side on the same shelf.
            localized=entry.get("localized") is True,
            stale=entry.get("stale") is True,
            locale=_text(entry.get("locale")),
            available_locales=_text_tuple(entry.get("available_locales")),
            # Read through defensively rather than forced empty: Cloud masks
            # the prose today, and if it ever stops the card face should show
            # what arrived instead of this adapter re-deciding the mask.
            profile=dict(profile) if isinstance(profile, Mapping) else {},
            images=self._as_images(card.get("images")),
            # Not derived from ``artifact_url``, which is null on every row
            # here. Every card on this route is fenced to a tier, which means
            # its artifact is a 404 on the anonymous route by construction —
            # so "there is a downloadable ``.lumecard``" is false whatever
            # the field says, and this is the fail-safe direction: the row
            # installs through the authenticated payload, the only path it
            # has.
            artifact_published=False,
        )

    def _as_images(self, raw: object) -> tuple[OfficialCardImage, ...]:
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            return ()
        images: list[OfficialCardImage] = []
        for entry in raw:
            if not isinstance(entry, Mapping):
                continue
            url = self._absolutise_asset(_text(entry.get("url")))
            if not url:
                continue
            images.append(OfficialCardImage(
                index=_count(entry.get("index")),
                url=url,
                content_type=_text(entry.get("content_type")),
                width=_count(entry.get("width")),
                height=_count(entry.get("height")),
                variants=_text_tuple(entry.get("variants")),
            ))
        return tuple(images)


def build_gated_catalog_client(
    *,
    base_url: str,
    credential_descriptor: str,
    absolutise_asset: Callable[[str], str],
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> OfficialCardGatedCatalogClient | None:
    """The client, or ``None`` when this deployment cannot hold one.

    The same structural answer :func:`build_exclusive_payload_client` gives,
    and deliberately reached by the same three checks: a deployment with no
    User service URL, no credential, or a credential without
    :data:`EXCLUSIVE_READ_SCOPE` has no path to a tier-locked card's
    existence, never mind its text. A self-hosted build therefore does not
    merely see an empty gated shelf — it has no client to ask with, which is
    what makes "these cards are invisible off the hosted side" a fact about
    the wiring rather than a rule somebody could forget to write.

    ``absolutise_asset`` is the anonymous catalog client's method of the
    same name. It is required rather than defaulted precisely because a
    default would have to guess an origin, and the wrong guess is invisible:
    the images simply never land.
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
            "cloud user service credential is malformed — tier-locked "
            "official cards stay invisible", exc_info=True,
        )
        return None
    if EXCLUSIVE_READ_SCOPE not in credential.scopes:
        _LOGGER.info(
            "cloud user service credential does not carry the %s scope — "
            "tier-locked official cards will not be listed", EXCLUSIVE_READ_SCOPE,
        )
        return None
    return OfficialCardGatedCatalogClient(
        base_url=base_url,
        credential=credential,
        absolutise_asset=absolutise_asset,
        timeout_seconds=timeout_seconds,
    )


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _text_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str))


def _count(value: object) -> int:
    # bool is an int subclass; a boolean here means the shape changed.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


__all__ = [
    "OfficialCardGatedCatalogClient",
    "build_gated_catalog_client",
]
