"""Port for the Cloud-hosted official character-card catalog.

Official cards no longer ship inside this repo. They live in the Yuralume
Cloud control plane, and every deployment — hosted or self-hosted — reads
them from one anonymous public endpoint
(``OFFICIAL_CARD_CLOUD_CATALOG_PLAN`` §3.5 / D1). This module is the seam:
the DTOs Core actually renders, the port the pack service depends on, and
the **one** definition of how the two repos' locale vocabularies line up.

Three properties are contractual and are the reason this is a port rather
than a bare HTTP call:

* **Fail-soft, never a wall.** Every read returns ``None`` when the catalog
  cannot be reached at all. The official-card shelf goes empty; local packs,
  installed characters and everything else are untouched. An adapter that
  raised here would turn "Cloud had a bad minute" into a broken gallery.
* **Nothing identifying goes out.** These are anonymous ``GET``s. No
  credential, no deployment id, no telemetry — a self-hoster reading the
  official catalog must not thereby announce that they exist (plan §3.2
  匿名紅線), and the env switch that turns the whole thing off lives one
  layer up in settings.
* **Relative asset paths are absolutised by the adapter.** Cloud does not
  know what hostname it is reachable at, so it answers with
  ``/v1/public/official-cards/…`` paths; the adapter that knows the base URL
  it fetched from is the only place that can join them correctly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

# --------------------------------------------------------------------------- #
# locale vocabulary
# --------------------------------------------------------------------------- #

CLOUD_LOCALE_BY_LANGUAGE_TAG: Mapping[str, str] = MappingProxyType({
    "zh-TW": "zh-Hant",
    "en-US": "en",
    "ja-JP": "ja",
})
"""Core's stored ``primary_language`` → the Cloud catalog's locale tag.

The two repos grew their own vocabularies: Core stores BCP 47 tags with a
region (``zh-TW``), Cloud keys its translation rows by script/language
(``zh-Hant``). This table is where they meet, and it exists exactly once —
a second copy at a call site is how one of the three pairs eventually gets
written the other way round and a reader silently receives the wrong
language. Every call site imports :func:`to_cloud_locale`.
"""

LANGUAGE_TAG_BY_CLOUD_LOCALE: Mapping[str, str] = MappingProxyType({
    cloud: core for core, cloud in CLOUD_LOCALE_BY_LANGUAGE_TAG.items()
})
"""The same table read backwards, for projecting what Cloud says it has
(``available_locales``) back into the tags Core's own UI speaks."""


def to_cloud_locale(language_tag: str | None) -> str | None:
    """Map a Core ``primary_language`` onto a Cloud catalog locale.

    ``None`` means "this language is not one the catalog publishes". The
    caller must **not** substitute ``en`` and carry on as if it matched:
    Cloud's own fallback chain already answers such a request with English
    *and reports ``localized=false``*, which is the truthful answer for a
    player whose language the catalog does not speak. Silently asking for
    ``en`` instead would come back ``localized=true`` and hide the translate
    toggle from the one player who needs it.
    """
    return CLOUD_LOCALE_BY_LANGUAGE_TAG.get((language_tag or "").strip())


def to_language_tag(cloud_locale: str | None) -> str | None:
    """Map a Cloud catalog locale back onto a Core ``primary_language``.

    ``None`` for anything outside the three published locales — including a
    future fourth one this build has never heard of, which a caller should
    render as "another language" rather than guess a tag for.
    """
    return LANGUAGE_TAG_BY_CLOUD_LOCALE.get((cloud_locale or "").strip())


# --------------------------------------------------------------------------- #
# pack references
# --------------------------------------------------------------------------- #

CLOUD_PACK_REF_PREFIX = "cloud:"
"""Marks a catalogue entry as coming from the Cloud catalog.

The player-facing API addresses every card by one opaque ``pack_id``, and
the frontend hands it straight back on preview / install. Prefixing the
Cloud ids keeps that single namespace honest: a local ``.lumecard`` file
stem is used verbatim (unchanged behaviour), and the prefix — a character a
filename stem cannot contain on Windows and does not contain anywhere in
practice — is what routes a request at the Cloud source instead.
"""


def cloud_pack_ref(card_id: str) -> str:
    return f"{CLOUD_PACK_REF_PREFIX}{card_id}"


def parse_cloud_pack_ref(pack_ref: str) -> str | None:
    """Return the Cloud card id inside ``pack_ref``, or ``None`` if it names
    a local pack."""
    if not pack_ref.startswith(CLOUD_PACK_REF_PREFIX):
        return None
    return pack_ref[len(CLOUD_PACK_REF_PREFIX):] or None


# --------------------------------------------------------------------------- #
# DTOs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class OfficialCardSummary:
    """One row of the Cloud catalog — everything the browse grid needs.

    Deliberately thin, because the catalog document is: a card's prose,
    companions and personality type only arrive with
    :class:`OfficialCardDetail`. ``image_url`` is already absolute.
    """

    id: str
    title: str
    summary: str = ""
    tags: tuple[str, ...] = ()
    author: str = ""
    localized: bool = False
    image_url: str | None = None
    image_count: int = 0


@dataclass(frozen=True, slots=True)
class OfficialCardImage:
    """One stage image, at an absolute URL the browser fetches directly."""

    index: int
    url: str
    content_type: str = ""
    width: int = 0
    height: int = 0
    variants: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OfficialCardDetail:
    """One card in full, in the best language the catalog could answer with.

    ``profile`` is passed through as the map Cloud sent. Its key set is
    defined by the extractor on the Cloud side and by Core's own card
    translator; re-projecting it into fields here would be a third
    definition, and the first field somebody adds would go missing without a
    test turning red. :func:`merge_translated_profile` consumes it directly.

    ``stale`` is the catalog saying "this translation was approved against an
    *older* version of the artifact". Cloud keeps serving it on purpose (plan
    §3.1: one re-upload must not blank three languages at once) and a reader
    loses nothing — the words are still the ones a human approved. An
    **install** is the case that cannot survive it: the merge policy pairs a
    translated list with the source list *by position*, so an equal-length
    drift lands a character built half from one version and half from
    another, with no length check able to notice. It defaults to ``False`` so
    a Cloud that predates the field stays readable — silence is not a
    warning.
    """

    id: str
    title: str
    description: str = ""
    tags: tuple[str, ...] = ()
    author: str = ""
    note: str = ""
    localized: bool = False
    stale: bool = False
    locale: str = ""
    available_locales: tuple[str, ...] = ()
    profile: Mapping[str, object] = field(default_factory=dict)
    images: tuple[OfficialCardImage, ...] = ()


class OfficialCardCatalogUnavailable(RuntimeError):
    """The catalog could not be read this time.

    Raised by the HTTP adapter and absorbed by the caching layer, which
    decides whether last-known-good is good enough. It never reaches the
    player-facing service, which sees ``None``.
    """


class OfficialCardNotFound(RuntimeError):
    """The catalog answered, and there is no such published card.

    Deliberately **not** a subclass of
    :class:`OfficialCardCatalogUnavailable`: an outage may be answered with
    stale content, but a card that was withdrawn a minute ago must stop being
    served the moment Cloud says so. Folding the two together would pin a
    withdrawn card in every worker's cache for as long as the process lives,
    because every refresh would keep "failing" and keep returning it.
    """


class OfficialCardCatalogPort(ABC):
    """Read-only access to the Cloud official-card catalog.

    Every method answers ``None`` for "we could not find out" — not an empty
    list, which would claim the catalog is genuinely empty and let a Cloud
    outage look like "the official cards were withdrawn".
    """

    @abstractmethod
    async def list_cards(self, *, locale: str) -> list[OfficialCardSummary] | None:
        """The published catalog in ``locale``, or ``None`` when unreachable."""

    @abstractmethod
    async def get_card(
        self, card_id: str, *, locale: str, fresh: bool = False,
    ) -> OfficialCardDetail | None:
        """One card's detail, or ``None`` when the catalog is unreachable.

        Raises :class:`OfficialCardNotFound` when the catalog answered and
        said there is no such published card.

        ``fresh`` is for the install path, and it changes two things at once
        because they are the same decision. The artifact is deliberately
        never cached, so a cached detail beside a freshly downloaded
        ``.lumecard`` is precisely the pairing ``stale`` exists to prevent:
        for as long as the TTL runs, a re-uploaded card hands out new bytes
        described by a document that still says ``stale=false``. So a fresh
        read skips the cache on the way in **and** refuses to fall back to it
        on failure — an install that cannot confirm what it is installing
        must stop, where a browsing reader is happily served last-known-good.
        """

    @abstractmethod
    async def download_artifact(self, card_id: str) -> bytes | None:
        """The ``.lumecard`` bytes Core feeds to its own importer.

        ``None`` when the download failed; :class:`OfficialCardNotFound` when
        the card is not published.
        """


__all__ = [
    "CLOUD_LOCALE_BY_LANGUAGE_TAG",
    "CLOUD_PACK_REF_PREFIX",
    "LANGUAGE_TAG_BY_CLOUD_LOCALE",
    "OfficialCardCatalogPort",
    "OfficialCardCatalogUnavailable",
    "OfficialCardDetail",
    "OfficialCardImage",
    "OfficialCardNotFound",
    "OfficialCardSummary",
    "cloud_pack_ref",
    "parse_cloud_pack_ref",
    "to_cloud_locale",
    "to_language_tag",
]
