"""The official-card half of the character-card catalogue.

Official cards live in the Yuralume Cloud control plane now (plan D1), so
this service is what turns three anonymous catalog documents into the same
three things the local pack directory has always provided: a browse list, a
preview, and an install.

Three properties are worth stating because each one is a decision:

* **Nothing here ever calls a model — browse, preview or install.** The
  catalog already carries an approved translation per locale, so reading an
  official card costs nothing, and installing one costs nothing either. The
  player-facing consequence is that the gallery's translate toggle is hidden
  on official cards: there is no charge to opt into and no second version to
  see.
* **Install downloads the real ``.lumecard``** and feeds it to the untouched
  import pipeline, then applies the approved translation through
  :class:`PreTranslatedCardTranslator`. What lands is a fully local
  character — the same one a bundled pack produced before this moved to
  Cloud, in the language the gallery showed.
* **A catalog outage is empty, never broken.** :meth:`list_summaries`
  answers ``None`` for "we do not know", which the caller renders as an
  empty official shelf plus a flag — local packs and installed characters
  carry on untouched.
"""

from __future__ import annotations

import logging

from kokoro_link.application.dto.character import InitialRelationshipPayload
from kokoro_link.application.dto.character_card import CharacterCardPreview
from kokoro_link.application.services.character_card_import_service import (
    CharacterCardImportService,
    ImportedCard,
)
from kokoro_link.application.services.exclusive_official_card_install import (
    ExclusiveOfficialCardInstaller,
)
from kokoro_link.application.services.official_card_profile import (
    preview_companions,
    profile_personality_type,
    profile_text,
    profile_text_list,
)
from kokoro_link.contracts.official_card_catalog import (
    DISTRIBUTION_CLOUD_EXCLUSIVE,
    DISTRIBUTION_PUBLIC,
    OfficialCardCatalogPort,
    OfficialCardDetail,
    OfficialCardNotFound,
    OfficialCardSummary,
    cloud_pack_ref,
    is_installable_distribution,
    to_cloud_locale,
)
from kokoro_link.contracts.official_card_exclusive import (
    OfficialCardExclusiveNotFound,
    OfficialCardExclusiveUnavailable,
)
from kokoro_link.infrastructure.character_card.pretranslated_translator import (
    PreTranslatedCardTranslator,
)

_LOGGER = logging.getLogger(__name__)

CLOUD_SOURCE = "cloud"


class OfficialCardUnavailableError(RuntimeError):
    """The Cloud catalog could not answer for a specific card right now.

    Only raised on the preview / install paths, where there is a player
    waiting on one named card and an empty answer would be a lie. Listing
    never raises — an unreachable catalog is an empty shelf there.
    """


class OfficialCardPackSource:
    def __init__(
        self,
        *,
        catalog: OfficialCardCatalogPort,
        import_service: CharacterCardImportService,
        exclusive_installer: ExclusiveOfficialCardInstaller | None = None,
    ) -> None:
        self._catalog = catalog
        self._import_service = import_service
        # ``None`` on every self-hosted deployment and on any hosted one
        # whose service credential does not carry the exclusive-read scope
        # (EC4). It is the single fact behind both halves of the red line:
        # cloud-exclusive rows report themselves un-installable to the
        # gallery, and an install attempted anyway is refused here rather
        # than failing somewhere upstream.
        self._exclusive_installer = exclusive_installer

    @property
    def installs_cloud_exclusive(self) -> bool:
        """Whether this deployment can complete a cloud-exclusive install."""
        return self._exclusive_installer is not None

    async def list_summaries(
        self, *, primary_language: str,
    ) -> list[CharacterCardPreview] | None:
        """The published catalog as browse rows, or ``None`` when unreachable.

        The rows are thin by nature — the catalog document carries a title, a
        summary, tags and one image, and that is what a grid needs. The prose
        a card face renders arrives with :meth:`preview`.
        """
        cards = await self._catalog.list_cards(
            locale=self._locale(primary_language),
        )
        if cards is None:
            return None
        return [
            _summary_preview(
                card, installs_exclusive=self.installs_cloud_exclusive,
            )
            for card in cards
        ]

    async def preview(
        self, card_id: str, *, primary_language: str,
    ) -> CharacterCardPreview:
        """One official card in the best language the catalog can answer with.

        No model call, ever: what comes back is either an approved
        translation or the card's own text.

        A ``stale`` translation is still shown here, and that is not an
        oversight. At browse time the catalog's text is the only text there
        is — Core holds no artifact to compare it against — and those words
        were approved by a human; hiding them would empty the card face for
        no gain. The install path is where staleness starts to matter,
        because that is where the two versions would be merged together.
        """
        detail = await self._catalog.get_card(
            card_id, locale=self._locale(primary_language),
        )
        if detail is None:
            raise OfficialCardUnavailableError(card_id)
        return _detail_preview(
            detail, installs_exclusive=self.installs_cloud_exclusive,
        )

    async def install(
        self,
        card_id: str,
        *,
        user_id: str,
        primary_language: str,
        initial_relationship: InitialRelationshipPayload | None = None,
    ) -> ImportedCard:
        """Download the card and import it as a brand-new local character.

        There is no ``translate`` parameter, and that is the point: an
        official card **never calls a model**, so a flag that could turn one
        on would be a charge the player was never shown. The gallery hides
        the translate toggle on cloud cards for the same reason, and the two
        have to agree — a toggle that does nothing on screen but bills on
        click is worse than either behaviour on its own.

        What lands is what was browsed: the catalog's own text for this
        locale, or the artifact's text when that translation cannot be
        trusted (see :func:`_profile_translator`).

        **The detail is read fresh, past the cache.** The two halves of an
        install have to describe the same version of the card, and only one
        of them was ever cached: the artifact is downloaded live every time.
        A cached detail is therefore a window — as long as the TTL — in which
        a card re-uploaded on Cloud hands out its new bytes alongside a
        document that still reports ``stale=false``, and
        :func:`_profile_translator` merges the old prose into the new
        character on that word. The browse paths keep their cache; this is
        the one caller that would be wrong to.
        """
        locale = self._locale(primary_language)
        detail = await self._catalog.get_card(card_id, locale=locale, fresh=True)
        if detail is None:
            raise OfficialCardUnavailableError(card_id)
        if detail.cloud_exclusive:
            return await self._install_cloud_exclusive(
                detail,
                user_id=user_id,
                locale=locale,
                initial_relationship=initial_relationship,
            )
        blob = await self._catalog.download_artifact(card_id)
        if blob is None:
            raise OfficialCardUnavailableError(card_id)

        return await self._import_service.import_card(
            blob,
            user_id=user_id,
            # ``translate=False`` is load-bearing beyond the profile: it is
            # also what keeps the bundled arc templates away from the arc
            # translator, which is a second model call on the same click.
            translate=False,
            target_language="",
            initial_relationship=initial_relationship,
            profile_translator=_profile_translator(detail),
        )

    async def _install_cloud_exclusive(
        self,
        detail: OfficialCardDetail,
        *,
        user_id: str,
        locale: str,
        initial_relationship: InitialRelationshipPayload | None,
    ) -> ImportedCard:
        """Install an IP-partner card through the authenticated payload.

        A deployment with no installer is answered as if the card were not
        in the catalogue at all. That is the same answer Cloud's own
        anonymous artifact route gives these cards, and for the same reason:
        a distinct status here would tell a caller which of the published
        cards are the licensed ones, which is a commercial fact about
        somebody else's contract. The reason is in the log, where the
        operator can see it and the visitor cannot.
        """
        if self._exclusive_installer is None:
            _LOGGER.info(
                "official card %s is cloud-exclusive and this deployment "
                "holds no exclusive-read credential — install refused",
                detail.id,
            )
            raise OfficialCardNotFound(detail.id)
        try:
            return await self._exclusive_installer.install(
                detail,
                user_id=user_id,
                locale=locale,
                initial_relationship=initial_relationship,
            )
        except OfficialCardExclusiveNotFound as exc:
            # Cloud says this is not an installable exclusive card after
            # all — most plausibly withdrawn between the catalog read and
            # the install. Same answer a missing public card gets.
            raise OfficialCardNotFound(detail.id) from exc
        except OfficialCardExclusiveUnavailable as exc:
            # Translated into the vocabulary the rest of the install path
            # already speaks, so the exclusive endpoint's failures reach the
            # player as the same "try again" a catalog outage does rather
            # than as a new error class every caller has to learn.
            raise OfficialCardUnavailableError(detail.id) from exc

    @staticmethod
    def _locale(primary_language: str) -> str:
        """The locale to ask the catalog for.

        A language the catalog publishes is sent as its own tag. Anything
        else is forwarded **as the player's own tag**, unmapped: Cloud
        resolves it down its fallback chain and answers with English while
        reporting ``localized=false``. Substituting ``en`` here would come
        back ``localized=true`` and tell the UI to hide the translate toggle
        from precisely the player who still needs one.
        """
        return to_cloud_locale(primary_language) or (primary_language or "").strip()


def _profile_translator(
    detail: OfficialCardDetail,
) -> PreTranslatedCardTranslator | None:
    """Decide whether the catalog's text is what should land.

    Two answers, and both of them install *what the player just read*:

    * **Apply it** — whatever language the catalog resolved to. If it
      answered in the player's language, that is the whole point. If it fell
      back to another one, that fallback is still what the gallery showed
      them, and a card that installs in a different language than the one on
      screen is the surprise worth avoiding.
    * **Refuse it** when there is nothing to apply, or when the catalog
      marked the translation ``stale``. Stale means it was approved against
      an *older* artifact: the merge policy matches lists by position, so an
      equal-length drift would land a character built half from one version
      and half from another, with no length check able to see it. The
      artifact's own prose is then the only internally consistent text
      there is, and ``None`` here is what lets it through untouched.

    Neither branch reaches a model. "This translation cannot be trusted" is
    not a licence to buy a replacement the player never asked for.
    """
    payload = detail.profile
    if not payload or detail.stale:
        return None
    return PreTranslatedCardTranslator(payload)


def _summary_preview(
    card: OfficialCardSummary, *, installs_exclusive: bool,
) -> CharacterCardPreview:
    return CharacterCardPreview(
        pack_id=cloud_pack_ref(card.id),
        title=card.title,
        author=card.author,
        # The catalog's one line of prose is the character's own summary, so
        # it fills both the description slot (what a grid tile shows) and
        # ``summary`` (what a card face shows) rather than being invented
        # twice.
        description=card.summary,
        tags=list(card.tags),
        name=card.title,
        summary=card.summary,
        image_urls=[card.image_url] if card.image_url else [],
        stage_image_count=card.image_count,
        source=CLOUD_SOURCE,
        localized=card.localized,
        distribution=card.distribution,
        installable=_installable(
            card.distribution, installs_exclusive=installs_exclusive,
        ),
    )


def _installable(distribution: str, *, installs_exclusive: bool) -> bool:
    """Whether *this* deployment can complete an install of such a card.

    Two different questions folded into one answer on purpose. "What licence
    is this card under" is a fact about the card (``distribution``, which
    travels alongside); "can I install it here" is a fact about the
    deployment, and it is the only one a button can act on. Leaving the
    frontend to combine them would put a second copy of the rule in front of
    every caller — and the frontend cannot see the credential at all.
    """
    return (
        is_installable_distribution(distribution) or installs_exclusive
    )


def _detail_preview(
    detail: OfficialCardDetail, *, installs_exclusive: bool,
) -> CharacterCardPreview:
    """Project one catalog detail document into the shared preview DTO.

    Only prose crosses: the catalog publishes what a reader reads, not the
    character's structural settings (disposition, cadence, world frame,
    bundled arcs). Those stay at the DTO's defaults and are marked as
    "cloud" for the renderer — they become real when the ``.lumecard``
    itself is imported.
    """
    profile = detail.profile
    distribution = (
        DISTRIBUTION_CLOUD_EXCLUSIVE
        if detail.cloud_exclusive
        else DISTRIBUTION_PUBLIC
    )
    return CharacterCardPreview(
        pack_id=cloud_pack_ref(detail.id),
        title=detail.title,
        author=detail.author,
        description=detail.description,
        tags=list(detail.tags),
        note=detail.note,
        name=profile_text(profile, "name") or detail.title,
        summary=profile_text(profile, "summary"),
        personality=profile_text_list(profile, "personality"),
        interests=profile_text_list(profile, "interests"),
        speaking_style=profile_text(profile, "speaking_style") or "natural",
        boundaries=profile_text_list(profile, "boundaries"),
        aspirations=profile_text_list(profile, "aspirations"),
        appearance=profile_text(profile, "appearance"),
        gender_identity=profile_text(profile, "gender_identity"),
        third_person_pronoun=profile_text(profile, "third_person_pronoun"),
        visual_gender_presentation=profile_text(
            profile, "visual_gender_presentation",
        ),
        world_topics=profile_text_list(profile, "world_topics"),
        excluded_topics=profile_text_list(profile, "excluded_topics"),
        personality_type=profile_personality_type(
            profile.get("personality_type"),
        ),
        companions=preview_companions(profile.get("companions")),
        image_urls=[image.url for image in detail.images],
        stage_image_count=len(detail.images),
        source=CLOUD_SOURCE,
        localized=detail.localized,
        # A detail document has no ``distribution`` field of its own — the
        # withheld artifact URL is how it says the same thing (see
        # ``OfficialCardDetail.artifact_published``). Projecting it back to
        # the vocabulary the catalog rows use keeps one word in front of the
        # renderer instead of two shapes of the same fact.
        distribution=distribution,
        installable=_installable(
            distribution, installs_exclusive=installs_exclusive,
        ),
    )


__all__ = [
    "CLOUD_SOURCE",
    "OfficialCardPackSource",
    "OfficialCardUnavailableError",
]
