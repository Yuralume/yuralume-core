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

Since the TG series the shelf has a *second* source, and the split is by
who may see a card rather than by where it lives: cards fenced to a tenant
tier are absent from the anonymous catalog entirely (they 404 there, like a
card that does not exist) and arrive instead through
:class:`~kokoro_link.contracts.official_card_gated_catalog.OfficialCardGatedCatalogPort`,
which names the player's Cloud tenant. The two shelves are disjoint *on the
Cloud side, at any one instant* — but Core reads them through two layers
with different clocks (the anonymous one behind a TTL cache, the gated one
per request), so a card locked down after publication is on both shelves
until the cache turns over. :meth:`OfficialCardPackSource.list_summaries`
therefore merges by id rather than concatenating. A deployment with no gated
client, which is every self-hosted one, takes every path below exactly as it
did before.
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
from kokoro_link.contracts.official_card_gated_catalog import (
    OfficialCardGatedCatalogPort,
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
        gated_catalog: OfficialCardGatedCatalogPort | None = None,
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
        # ``None`` under exactly the same conditions and for the same
        # reason (TG3): the gated shelf is read with the very credential
        # that installs from it, so a deployment that could list a
        # tier-locked card could always also install it. Absent here means
        # the tier-locked cards do not exist as far as this build is
        # concerned — no row, no card face, no install.
        self._gated_catalog = gated_catalog

    @property
    def installs_cloud_exclusive(self) -> bool:
        """Whether this deployment can complete a cloud-exclusive install."""
        return self._exclusive_installer is not None

    async def list_summaries(
        self, *, primary_language: str, cloud_tenant_id: str = "",
    ) -> list[CharacterCardPreview] | None:
        """The published catalog as browse rows, or ``None`` when unreachable.

        The rows are thin by nature — the catalog document carries a title, a
        summary, tags and one image, and that is what a grid needs. The prose
        a card face renders arrives with :meth:`preview`.

        ``cloud_tenant_id`` is what lets a hosted player also see the cards
        fenced to their tenant's tier. They are appended to the anonymous
        shelf rather than mixed into it, so the order every other player
        sees is untouched, and they render as ordinary cloud-exclusive rows
        — a tester is not shown a new kind of card, they are shown a card.
        An id that arrives on both shelves at once is one card, not two:
        see the merge below.
        """
        locale = self._locale(primary_language)
        cards = await self._catalog.list_cards(locale=locale)
        if cards is None:
            # The public shelf is what "the official cards" means to the
            # caller, so not knowing it is not knowing the shelf. The gated
            # rows are deliberately not asked for either: half a shelf
            # flagged as unavailable would be a stranger answer than none.
            return None
        rows = [
            _summary_preview(
                card, installs_exclusive=self.installs_cloud_exclusive,
            )
            for card in cards
        ]
        # ``None`` here is the fail-soft case and reads as "no tier-locked
        # rows this time" (already logged one level down): a shelf minus the
        # WIP cards, not a shelf nobody can load.
        gated = await self._gated_rows(
            locale=locale, cloud_tenant_id=cloud_tenant_id,
        )
        locked = [
            _detail_preview(
                row, installs_exclusive=self.installs_cloud_exclusive,
            )
            for row in gated or ()
        ]
        if locked:
            # The two shelves are disjoint by construction — but only in the
            # sense that Cloud never *serves* one card on both at the same
            # instant. These two lists were not read at the same instant: the
            # anonymous one comes out of a TTL cache measured in minutes and
            # the gated one is live, so a card locked down after it was
            # published sits on both until that cache turns over, and a
            # concatenation would give the gallery two tiles and two
            # identical keys. The gated row wins because it is the newer
            # fact: this card is fenced now.
            locked_ids = {row.pack_id for row in locked}
            rows = [row for row in rows if row.pack_id not in locked_ids]
            rows.extend(locked)
        return rows

    async def preview(
        self, card_id: str, *, primary_language: str, cloud_tenant_id: str = "",
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

        A tier-fenced card has no anonymous document at all, so
        ``cloud_tenant_id`` is what decides whether this can answer for one
        — and a player outside the circle gets the answer a card that does
        not exist gets, which is the whole point of the fence.
        """
        detail = await self._resolve_detail(
            card_id,
            locale=self._locale(primary_language),
            cloud_tenant_id=cloud_tenant_id,
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
        cloud_tenant_id: str = "",
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

        ``cloud_tenant_id`` travels the whole way down to the authenticated
        payload read, where Cloud re-checks the tier fence: the row this
        install started from was read a moment ago, and a card's fence — or
        a tenant's tier — can change in between. Losing that race is the
        ordinary "no such card" answer, never a message about tiers.
        """
        locale = self._locale(primary_language)
        detail = await self._resolve_detail(
            card_id,
            locale=locale,
            cloud_tenant_id=cloud_tenant_id,
            fresh=True,
        )
        if detail is None:
            raise OfficialCardUnavailableError(card_id)
        if detail.cloud_exclusive:
            return await self._install_cloud_exclusive(
                detail,
                user_id=user_id,
                locale=locale,
                initial_relationship=initial_relationship,
                cloud_tenant_id=cloud_tenant_id,
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
        cloud_tenant_id: str = "",
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
                tenant_id=(cloud_tenant_id or "").strip(),
            )
        except OfficialCardExclusiveNotFound as exc:
            # Cloud says this is not an installable exclusive card after
            # all — most plausibly withdrawn between the catalog read and
            # the install, or fenced to a tier this tenant left in between.
            # Same answer a missing public card gets, and deliberately the
            # same for both: the player is never told that a card they can
            # see is one they are not in the circle for.
            raise OfficialCardNotFound(detail.id) from exc
        except OfficialCardExclusiveUnavailable as exc:
            # Translated into the vocabulary the rest of the install path
            # already speaks, so the exclusive endpoint's failures reach the
            # player as the same "try again" a catalog outage does rather
            # than as a new error class every caller has to learn.
            raise OfficialCardUnavailableError(detail.id) from exc

    async def _resolve_detail(
        self,
        card_id: str,
        *,
        locale: str,
        cloud_tenant_id: str,
        fresh: bool = False,
    ) -> OfficialCardDetail | None:
        """One card's document, from whichever shelf actually holds it.

        The anonymous catalog is asked first and answers for every card that
        is not tier-fenced, which is all of them on a self-hosted deployment
        and nearly all of them on a hosted one. A tier-fenced card is a 404
        there — indistinguishable from a card that does not exist, which is
        the whole point — and *that* is the only branch which reaches for
        the gated shelf. The cost of the second call is therefore paid on
        the miss, never on the ordinary read.

        An unreachable catalog (``None``) is left alone rather than
        retried against the gated route: "we could not find out" is already
        an honest answer with a caller-side meaning, and turning an outage
        into a second lookup would let a browse succeed with half a shelf
        while an install thinks it confirmed something it did not.

        The gated shelf failing is the mirror image of that and behaves the
        same way: :meth:`_gated_detail` raises
        :class:`OfficialCardUnavailableError` (a 503, "try again") rather
        than letting the anonymous 404 stand, because the anonymous 404 is
        not evidence about a card only the other shelf can answer for. A
        gated shelf that *did* answer and does not list the card keeps the
        404 — that is a tenant outside the circle, and it must look exactly
        like a card that never existed.

        ``fresh`` reaches the anonymous read only. A gated row is fetched
        per request with no cache anywhere behind it (plan D6), so it is
        unconditionally what ``fresh`` exists to guarantee.
        """
        try:
            detail = await self._catalog.get_card(
                card_id, locale=locale, fresh=fresh,
            )
        except OfficialCardNotFound:
            gated = await self._gated_detail(
                card_id, locale=locale, cloud_tenant_id=cloud_tenant_id,
            )
            if gated is None:
                raise
            return gated
        return detail

    async def _gated_detail(
        self, card_id: str, *, locale: str, cloud_tenant_id: str,
    ) -> OfficialCardDetail | None:
        """The tier-locked card by that id, if this tenant may see it.

        Read out of the same list the shelf is built from rather than
        through a per-card route, because there is no per-card route: the
        gated endpoint answers "everything your tier may see", and one card
        is a row of that. At internal-test volumes — a handful of cards —
        that is the cheaper shape as well as the only one.

        ``None`` means the shelf was read and this card is not on it, which
        the caller turns into the ordinary "no such card". A shelf that
        could not be read at all is a different thing and raises
        :class:`OfficialCardUnavailableError` instead: telling a player 404
        about a card that was in front of them a second ago is telling them
        to stop trying, and only Cloud actually answering may say that.
        """
        rows = await self._gated_rows(
            locale=locale, cloud_tenant_id=cloud_tenant_id,
        )
        if rows is None:
            raise OfficialCardUnavailableError(card_id)
        for row in rows:
            if row.id == card_id:
                return row
        return None

    async def _gated_rows(
        self, *, locale: str, cloud_tenant_id: str,
    ) -> list[OfficialCardDetail] | None:
        """Every tier-locked card this player may see; ``None`` if unasked.

        Two absences are *answers* and collapse to ``[]``: a deployment with
        no gated client (every self-hosted one) and a player with no Cloud
        tenant projected onto their profile. Both of those genuinely have no
        tier-locked cards, and a caller is right to say "no such card" about
        one — that is the fence working, and it must stay indistinguishable
        from a card that never existed.

        A Cloud that could not answer is the third case and is deliberately
        **not** folded in with them, because one branch down the two mean
        opposite things. Browsing renders both the same way (fail-soft: the
        shelf minus its tier-locked rows), but a card face cannot — "you are
        not in the circle" is a 404 and "we could not find out" is a 503, and
        collapsing them here is what turns a one-minute outage into a
        permanent "that card does not exist" for the tester watching it.
        """
        tenant = (cloud_tenant_id or "").strip()
        if self._gated_catalog is None or not tenant:
            return []
        rows = await self._gated_catalog.list_gated_cards(
            tenant_id=tenant, locale=locale,
        )
        if rows is None:
            _LOGGER.warning(
                "tier-gated official cards could not be read for tenant %s — "
                "they are absent from this shelf", tenant,
            )
        return rows

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
