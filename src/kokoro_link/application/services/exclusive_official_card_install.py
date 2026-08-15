"""Installing a cloud-exclusive official card (EC4).

A public official card installs by downloading its ``.lumecard`` and running
the ordinary import pipeline. A cloud-exclusive one has no downloadable
artifact by design, so this is the second install path — and the shape of
the difference is worth stating, because it is not "the same thing with
authentication":

* **What arrives is a document, not a card file.** The authenticated payload
  carries the translatable field set (persona prose, companions,
  personality-type explanation) *and* the card's own ``manifest.json``
  (``card_document``, EC4-C) — the two halves that a ``.lumecard`` keeps in
  one file. The prose half is the one with translations and wins for every
  word; the manifest is the authority on everything that must never be
  translated, which is exactly the settings that make the character behave
  like the licensor's: disposition bands, cadence numbers, world frame, tool
  ids, visual subject type, date of birth. A payload with no document
  installs on this build's defaults — the pre-EC4-C behaviour, kept as the
  fail-soft floor rather than as the plan.
* **Bundled arcs still do not travel.** The document is the manifest alone;
  a ``.lumecard``'s arc templates are separate members of the zip, which
  never leaves Cloud. So an install lands no arc templates and no arc series
  and reports empty lists, and the manifest's own arc refs are dropped
  rather than carried forward to name something that is not here.
* **What lands is bound to the card.** ``origin_official_card_id`` is
  written at creation (insert-only — nothing can add or remove it later),
  the licensed voice is written in the same breath, and the official art is
  landed into this deployment's own object storage under keys that mark it
  as the partner's (see :mod:`official_character_art`), which is what makes
  it undeletable afterwards and what lets the image path find the likeness
  lock without another round trip to Cloud.
* **The player's own slot rules still apply.** The install goes through
  :meth:`CharacterService.create_character`, so an account at its character
  cap or its daily-create limit is refused here exactly as it is when they
  press "new character" — an IP character is still a character.

Failure policy is the importer's, deliberately: an image that will not
download or will not store is skipped with a warning rather than sinking an
install that is otherwise complete, because a character missing one of five
portraits is recoverable and a half-created character is not.
"""

from __future__ import annotations

import logging
import mimetypes

from kokoro_link.application.dto.character import InitialRelationshipPayload
from kokoro_link.application.dto.character_card import CharacterCardProfile
from kokoro_link.application.services.character_card_export_service import (
    CharacterCardError,
)
from kokoro_link.application.services.character_card_import_service import (
    ImportedCard,
)
from kokoro_link.application.services.character_image_service import (
    CharacterImageError,
    CharacterImageService,
    OfficialCardArtImage,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.managed_character_origin import (
    ManagedCharacterOrigin,
)
from kokoro_link.application.services.official_card_profile import (
    card_profile_from_catalog,
    card_profile_with_document_structure,
)
from kokoro_link.contracts.official_card_catalog import (
    OfficialCardCatalogPort,
    OfficialCardDetail,
    OfficialCardImage,
)
from kokoro_link.contracts.official_card_exclusive import (
    ExclusiveCardPayload,
    OfficialCardExclusivePayloadPort,
    OfficialCardExclusiveUnavailable,
    SOURCE_LOCALE,
)
from kokoro_link.contracts.tts_catalog import TTSVoiceCatalogPort
from kokoro_link.domain.value_objects.voice_profile import VoiceProfile
from kokoro_link.infrastructure.character_card.llm_translator import (
    merge_translated_profile,
)

_LOGGER = logging.getLogger(__name__)

FALLBACK_LOCALE = "en"
"""The middle link of the catalog's own chain: requested → en → source.

Mirrored here rather than reinvented so an install lands the language a
browse would have shown. The one addition is the staleness rule the public
install path already applies — a translation approved against an older
version of the card is skipped, and the chain simply continues past it.
"""


class ExclusiveCardEntitlementRequiredError(CharacterCardError):
    """The card is sold behind an entitlement this build cannot check.

    v1 publishes no ``required_product``, so this is the seam rather than a
    live rule (plan D2). It refuses instead of installing because that is
    the only fail-safe direction: an install this deployment could not
    verify the licence for is a mistake with a partner's IP in it, and
    "refused, and here is why in the log" is recoverable where "installed
    anyway" is not.
    """


class ExclusiveOfficialCardInstaller:
    def __init__(
        self,
        *,
        catalog: OfficialCardCatalogPort,
        exclusive_payloads: OfficialCardExclusivePayloadPort,
        character_service: CharacterService,
        character_image_service: CharacterImageService,
        voice_catalog: TTSVoiceCatalogPort | None = None,
    ) -> None:
        self._catalog = catalog
        self._exclusive_payloads = exclusive_payloads
        self._character_service = character_service
        self._character_image_service = character_image_service
        # Optional: a deployment with no cloud TTS wired still installs the
        # character, it just has no exclusive voice to bind.
        self._voice_catalog = voice_catalog

    async def install(
        self,
        detail: OfficialCardDetail,
        *,
        user_id: str,
        locale: str,
        initial_relationship: InitialRelationshipPayload | None = None,
        tenant_id: str = "",
    ) -> ImportedCard:
        """Create one managed character from ``detail``'s exclusive payload.

        ``detail`` is the freshly-read public document the caller already
        holds — it is where the stage image addresses come from, and reading
        it twice would risk describing two different versions of the card.

        ``tenant_id`` is the installing player's Cloud tenant, forwarded so
        Cloud can check a tier fence on the card (TG series D4). Blank on a
        deployment or an account with no projected tenant, which is the
        pre-TG request and installs every unfenced card exactly as before.
        A fenced card the tenant may not have arrives as the ordinary
        "no such card" 404 — the player is told what they would have been
        told about a withdrawn card, and the word *tier* is never spoken on
        this side of the wire.
        """
        payload = await self._exclusive_payloads.fetch_payload(
            detail.id, tenant_id=tenant_id or None,
        )
        if payload is None:
            raise OfficialCardExclusiveUnavailable(detail.id)
        if payload.required_product:
            raise ExclusiveCardEntitlementRequiredError(
                f"official card {detail.id} requires the "
                f"'{payload.required_product}' entitlement, which this build "
                f"cannot verify",
            )

        profile = self._localized_profile(payload, locale=locale)
        create_request = profile.to_create_request(
            # Art is landed after creation: the object keys are scoped to
            # the character id, which does not exist yet.
            image_urls=[],
            arc_template_id=None,
            arc_series_id=None,
        )
        if initial_relationship is not None:
            create_request = create_request.model_copy(
                update={"initial_relationship": initial_relationship},
            )

        created = await self._character_service.create_character(
            create_request,
            user_id=user_id,
            managed_origin=ManagedCharacterOrigin(
                official_card_id=payload.card_id,
                voice_profile=await self._exclusive_voice(payload.card_id),
            ),
        )

        await self._land_official_art(
            created.id,
            images=detail.images,
            reference_indexes=payload.reference_image_indexes,
        )
        final = await self._character_service.get_character(
            created.id, user_id=user_id,
        )
        return ImportedCard(
            character=final or created,
            # An exclusive card carries no bundled arc material: the payload
            # carries the manifest, and a `.lumecard`'s arc templates are
            # separate zip members that never leave Cloud. Empty is the
            # honest answer, not a placeholder for something that failed to
            # land.
            landed_arc_template_ids=[],
            landed_arc_series_ids=[],
        )

    def _localized_profile(
        self, payload: ExclusiveCardPayload, *, locale: str,
    ) -> CharacterCardProfile:
        """The card as it should land: the licensor's settings, the player's
        language.

        Two sources, in this order, because they answer different questions:

        1. the **prose**, resolved to the best language the card was signed
           off in — the source payload is the base and a signed-off
           translation is laid over it through the same merge policy the
           public install path uses (same-length lists or nothing, never
           blank the source), which is what makes a partly-translated card
           land as a coherent character instead of a half-empty one;
        2. the **structure**, from the card's own manifest, with that
           resolved prose laid over it (EC4-C).

        The order matters and is not interchangeable: translations exist
        only for the prose half, so localizing first and then applying it to
        the manifest is the only sequence in which a Japanese player gets a
        Japanese character that still behaves like the one the partner
        authored.
        """
        profile = card_profile_from_catalog(
            payload.profile, fallback_name=payload.title or payload.card_id,
        )
        for candidate in (locale, FALLBACK_LOCALE):
            if not candidate or candidate == SOURCE_LOCALE:
                break
            translation = payload.translation(candidate)
            if translation is None or not translation.usable:
                continue
            profile = merge_translated_profile(profile, translation.payload)
            break
        return card_profile_with_document_structure(
            profile, payload.card_document,
        )

    async def _exclusive_voice(self, card_id: str) -> VoiceProfile | None:
        """The voice the catalog bound to this card, if there is one yet.

        The listing has to be asked *as this card*: a cloud-exclusive voice
        is withheld from any caller that has not declared the card it
        belongs to (EC5-A), so an undeclared listing would come back without
        the very voice this method exists to find, and every install would
        take the fallback below while looking like it had checked. The card
        is its own origin — the character that would carry it does not exist
        yet — so the slug is declared directly (EC5-D).

        Fail-soft in both directions, and that is a decision rather than
        laziness: a voice published *after* the card it belongs to is a
        legitimate order for two admin actions, and an unreachable TTS
        catalog is an outage in a different subsystem. Neither is a reason
        to refuse a player the character. What they get instead is the
        deployment's default voice — and a log line saying which card is
        still waiting for its own.
        """
        if self._voice_catalog is None:
            return None
        try:
            voices = await self._voice_catalog.list_voices(
                character_origin=card_id,
            )
        except Exception:  # noqa: BLE001 — a TTS outage must not block install
            _LOGGER.warning(
                "official card %s: voice catalog unreachable — installing on "
                "the default voice", card_id, exc_info=True,
            )
            return None
        for voice in voices:
            if getattr(voice, "exclusive_card_id", "") == card_id:
                return VoiceProfile(voice_id=voice.id)
        _LOGGER.info(
            "official card %s has no exclusive voice in the catalog yet — "
            "installing on the default voice", card_id,
        )
        return None

    async def _land_official_art(
        self,
        character_id: str,
        *,
        images: tuple[OfficialCardImage, ...],
        reference_indexes: tuple[int, ...],
    ) -> None:
        """Download the card's stage images into this deployment's storage.

        Landing them locally is the whole point of doing it at install time:
        the image-generation path injects the reference art on every draw,
        and a hot path that reaches Cloud for its inputs turns somebody
        else's bad minute into this deployment's failed generation.

        A reference index Cloud marked that is absent from the public stage
        list is skipped rather than fetched some other way — the two
        documents describe one card, and disagreeing about which images
        exist is a Cloud-side inconsistency, not something to paper over.
        """
        marked = set(reference_indexes)
        landed: list[OfficialCardArtImage] = []
        for image in images:
            data = await self._catalog.download_image(url=image.url)
            if data is None:
                _LOGGER.warning(
                    "official card art %s could not be downloaded — the "
                    "character installs without it", image.url,
                )
                continue
            landed.append(OfficialCardArtImage(
                index=image.index,
                data=data,
                content_type=image.content_type or _guess_content_type(image.url),
                reference=image.index in marked,
            ))
        missing = marked - {image.index for image in landed}
        if missing:
            _LOGGER.warning(
                "official card reference images %s did not land for character "
                "%s — generation will use whatever art did",
                sorted(missing), character_id,
            )
        if not landed:
            return
        try:
            await self._character_image_service.attach_official_card_art(
                character_id, images=landed,
            )
        except CharacterImageError:
            _LOGGER.exception(
                "official card art could not be stored for character %s — "
                "the character is installed without it", character_id,
            )


def _guess_content_type(url: str) -> str:
    guessed, _ = mimetypes.guess_type(url)
    return guessed or "application/octet-stream"


__all__ = [
    "ExclusiveCardEntitlementRequiredError",
    "ExclusiveOfficialCardInstaller",
]
