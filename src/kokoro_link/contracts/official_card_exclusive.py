"""Port for the authenticated cloud-exclusive official-card payload (EC1).

A ``cloud_exclusive`` card is an IP-partner character: the catalog lists it
like any other — name, one-line summary, portrait — but its actual text and
its ``.lumecard`` never leave the hosted side. There is **no anonymous
equivalent of this endpoint**, on purpose: a self-hosted deployment holds no
service credential and therefore has no path to the prose at all, which is
the structural half of the plan's D1 red line rather than a policy check
somebody could forget to write.

Three properties are contractual:

* **Authenticated, and only from hosted Core.** The wire credential is the
  existing ``core_to_user`` service credential shape (caller ``core``,
  audience ``yuralume-user``) carrying the ``official-cards:exclusive-read``
  scope. A deployment without it does not get a degraded answer — it gets no
  client at all, and the catalogue renders the card as "cloud only".
* **Fail-soft on unreachable, refuse on unknown.** ``None`` means "Cloud
  could not answer", which the install path turns into a "try again later";
  :class:`OfficialCardExclusiveNotFound` means Cloud answered and this is not
  an installable exclusive card. Cloud deliberately answers the same 404 for
  "no such card", "not published" and "not exclusive", so this side must not
  invent a distinction it was not given.
* **Prose and structure arrive separately.** The ``profile`` map is the
  *translatable* field set (see Cloud's ``OfficialCardTranslatableFields``):
  persona prose, companions and personality-type explanation, and it is the
  only half that has translations. Everything structural — disposition bands,
  cadence numbers, world frame, tool ids, visual subject type, date of birth
  — is not translatable and therefore is not in that map at all; it rides in
  ``card_document``, the card's own ``manifest.json`` (EC4-C). A payload
  without one installs on this build's defaults for every structural field,
  which was the whole behaviour before EC4-C and is still what a fail-soft
  read falls back to.
* **A frozen card is withheld.** Cloud answers the same 404 for a card whose
  licence has been frozen as for one that does not exist, so a terminated
  contract stops minting new characters as well as freezing the existing
  ones. Nothing on this side has to know that is why.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field

SOURCE_LOCALE = "zh-Hant"
"""The language official cards are authored in.

It is the one locale that never appears in ``translations``: the source text
*is* the payload's own ``title`` / ``profile`` / … fields, so listing it a
second time would be two copies of one thing. Stated here because the
install path has to recognise it — asking for a "translation into the
source language" is the one request that is already answered.
"""

TRANSLATION_STATUS_APPROVED = "approved"
TRANSLATION_STATUS_MISSING = "missing"
"""The only two statuses this endpoint emits.

A ``draft`` is unreviewed machine output and never leaves the Cloud console,
authenticated caller or not — so a status this build does not recognise is
treated as "not approved" rather than as a third case to handle.
"""


@dataclass(frozen=True, slots=True)
class ExclusiveCardTranslation:
    """One non-source locale's signed-off rendering, or the absence of one.

    A locale nobody has approved is *present* with ``status="missing"`` and
    an empty payload rather than absent from the list, so "is this language
    available" is a field to read instead of a set difference to compute.
    """

    locale: str
    status: str = TRANSLATION_STATUS_MISSING
    stale: bool = False
    payload: Mapping[str, object] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        """Whether this rendering may be laid over the source profile.

        ``stale`` disqualifies it for the same reason it disqualifies an
        approved translation on the public install path: the merge policy
        pairs a translated list with the source list *by position*, so text
        approved against an older version of the card can land a character
        built half from one version and half from another, with no length
        check able to notice.
        """
        return (
            self.status == TRANSLATION_STATUS_APPROVED
            and not self.stale
            and bool(self.payload)
        )


@dataclass(frozen=True, slots=True)
class ExclusiveCardPayload:
    """Everything hosted Core needs to install one cloud-exclusive card.

    ``profile`` is the **source** (zh-Hant) prose; ``translations`` carries
    every other locale. ``reference_image_indexes`` names the stage images
    Cloud marked as the likeness lock (D5) — indexes into the same stage
    image list the public detail document publishes, which is where their
    bytes come from. Cloud guarantees the set is non-empty for a card with
    at least one stage image (unmarked defaults to the first portrait).
    """

    card_id: str
    title: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()
    author: str = ""
    note: str = ""
    profile: Mapping[str, object] = field(default_factory=dict)
    translations: tuple[ExclusiveCardTranslation, ...] = ()
    reference_image_indexes: tuple[int, ...] = ()
    card_document: Mapping[str, object] = field(default_factory=dict)
    """The card's own ``manifest.json``, or empty when Cloud could not read it.

    This is the *structural* half of the card and the only place it exists:
    ``profile`` above is the translatable field set, and a field that must
    never be translated (a world frame, a tool id, a disposition band, a
    cadence number) is by construction absent from it. Installing without
    this document is installing the licensor's words over the installing
    build's own settings — a different character, quietly.

    Empty is a real and expected value, not a bug to defend against: Cloud
    omits it rather than fail an install when the stored artifact is
    unreadable, and every install before EC4-C behaved exactly that way. The
    install path therefore treats it as "nothing to apply", never as a
    reason to refuse.

    Carried as the raw map for the same reason ``profile`` is — the manifest
    schema belongs to the card exporter, and a second definition of it on
    the wire would go stale the day somebody adds a field.
    """

    required_product: str = ""
    """Entitlement this card is sold behind, or ``""`` for "included".

    Read defensively rather than assumed absent: v1 publishes no such field,
    and the install refuses any card that starts carrying one until an
    entitlement port exists to check it (plan D2). Refusing is the only
    fail-safe direction — installing a card whose licence this build cannot
    verify is the mistake that cannot be taken back.
    """

    def translation(self, locale: str) -> ExclusiveCardTranslation | None:
        wanted = (locale or "").strip()
        if not wanted:
            return None
        for translation in self.translations:
            if translation.locale == wanted:
                return translation
        return None


class OfficialCardExclusiveUnavailable(RuntimeError):
    """Cloud could not be reached for the exclusive payload this time."""


class OfficialCardExclusiveNotFound(RuntimeError):
    """Cloud answered: there is no installable exclusive card by that id.

    One error for all three of Cloud's cases — absent, unpublished, not
    exclusive — because Cloud deliberately answers all three with the same
    404 rather than becoming an oracle for which published cards are the
    licensed ones.
    """


class OfficialCardExclusivePayloadPort(ABC):
    """Read-only access to one cloud-exclusive card's full text."""

    @abstractmethod
    async def fetch_payload(
        self, card_id: str, *, tenant_id: str | None = None,
    ) -> ExclusiveCardPayload | None:
        """The card's authored text.

        ``tenant_id`` names the Cloud tenant the install is being made for,
        and it is optional because for almost every card it changes nothing:
        Cloud only consults it when the card is fenced to a tier (TG series
        D4), and a request that omits it is byte-for-byte the pre-TG one. It
        is the *tenant id*, never the tier — Core caches a tenant's tier on
        the operator profile and that cache drifts, so the side that owns
        the answer resolves it on every call.

        Three outcomes, and the caller has to tell them apart because they
        are three different things to say to a player:

        * a payload — install it;
        * :class:`OfficialCardExclusiveNotFound` — Cloud answered, and this
          is not an installable exclusive card (withdrawn, unpublished, or
          never exclusive; Cloud does not say which);
        * :class:`OfficialCardExclusiveUnavailable`, or ``None`` from an
          implementation that prefers to stay quiet — nothing was learned,
          so the honest answer downstream is "try again", never a partial
          install.

        A tenant whose tier may not see a fenced card is answered with the
        *same* 404 as a card that does not exist, so nothing here has to
        know that tiers are the reason — and nothing downstream can turn
        this endpoint into an oracle for which cards are being tested.
        """


__all__ = [
    "SOURCE_LOCALE",
    "TRANSLATION_STATUS_APPROVED",
    "TRANSLATION_STATUS_MISSING",
    "ExclusiveCardPayload",
    "ExclusiveCardTranslation",
    "OfficialCardExclusiveNotFound",
    "OfficialCardExclusivePayloadPort",
    "OfficialCardExclusiveUnavailable",
]
