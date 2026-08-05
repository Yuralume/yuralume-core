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
from collections.abc import Mapping, Sequence
from typing import Any

from kokoro_link.application.dto.character import (
    CharacterPersonalityTypePayload,
    InitialRelationshipPayload,
)
from kokoro_link.application.dto.character_card import (
    CharacterCardPreview,
    CharacterCardPreviewCompanion,
)
from kokoro_link.application.services.character_card_import_service import (
    CharacterCardImportService,
    ImportedCard,
)
from kokoro_link.contracts.official_card_catalog import (
    OfficialCardCatalogPort,
    OfficialCardDetail,
    OfficialCardSummary,
    cloud_pack_ref,
    to_cloud_locale,
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
    ) -> None:
        self._catalog = catalog
        self._import_service = import_service

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
        return [_summary_preview(card) for card in cards]

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
        return _detail_preview(detail)

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


def _summary_preview(card: OfficialCardSummary) -> CharacterCardPreview:
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
    )


def _detail_preview(detail: OfficialCardDetail) -> CharacterCardPreview:
    """Project one catalog detail document into the shared preview DTO.

    Only prose crosses: the catalog publishes what a reader reads, not the
    character's structural settings (disposition, cadence, world frame,
    bundled arcs). Those stay at the DTO's defaults and are marked as
    "cloud" for the renderer — they become real when the ``.lumecard``
    itself is imported.
    """
    profile = detail.profile
    return CharacterCardPreview(
        pack_id=cloud_pack_ref(detail.id),
        title=detail.title,
        author=detail.author,
        description=detail.description,
        tags=list(detail.tags),
        note=detail.note,
        name=_text(profile, "name") or detail.title,
        summary=_text(profile, "summary"),
        personality=_text_list(profile, "personality"),
        interests=_text_list(profile, "interests"),
        speaking_style=_text(profile, "speaking_style") or "natural",
        boundaries=_text_list(profile, "boundaries"),
        aspirations=_text_list(profile, "aspirations"),
        appearance=_text(profile, "appearance"),
        gender_identity=_text(profile, "gender_identity"),
        third_person_pronoun=_text(profile, "third_person_pronoun"),
        visual_gender_presentation=_text(profile, "visual_gender_presentation"),
        world_topics=_text_list(profile, "world_topics"),
        excluded_topics=_text_list(profile, "excluded_topics"),
        personality_type=_personality_type(profile.get("personality_type")),
        companions=_companions(profile.get("companions")),
        image_urls=[image.url for image in detail.images],
        stage_image_count=len(detail.images),
        source=CLOUD_SOURCE,
        localized=detail.localized,
    )


def _personality_type(raw: object) -> CharacterPersonalityTypePayload:
    if not isinstance(raw, Mapping):
        return CharacterPersonalityTypePayload()
    try:
        return CharacterPersonalityTypePayload(
            code=_text(raw, "code"),
            rationale=_text(raw, "rationale"),
            consistency_notes=_text_list(raw, "consistency_notes"),
        )
    except ValueError:
        # An unknown 16-type code fails the domain validator. One bad field
        # must not blank a whole card in the gallery.
        _LOGGER.warning(
            "official card catalog: unusable personality_type — dropping",
            exc_info=True,
        )
        return CharacterPersonalityTypePayload()


def _companions(raw: object) -> list[CharacterCardPreviewCompanion]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    companions: list[CharacterCardPreviewCompanion] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        name = _text(entry, "name")
        if not name:
            continue
        companions.append(CharacterCardPreviewCompanion(
            name=name, role=_text(entry, "role"),
        ))
    return companions


def _text(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    return value.strip() if isinstance(value, str) else ""


def _text_list(payload: Mapping[str, Any], field: str) -> list[str]:
    value = payload.get(field)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item.strip() for item in value if isinstance(item, str)]


__all__ = [
    "CLOUD_SOURCE",
    "OfficialCardPackSource",
    "OfficialCardUnavailableError",
]
