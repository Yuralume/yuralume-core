"""The tier-gated shelf on the card gallery — TG3.

A tier-locked official card is one Cloud has fenced to a single tenant tier
while it is being tested in the real hosted environment. To the anonymous
catalog it does not exist at all — it is not in the list and its detail is
a 404, the same answer a card that was never published gets — so everything
here is about the *second* shelf: who is asked for it, what a player of the
right tenant sees, and the two deployments that must not change at all.

The reverse assertions are the load-bearing ones. A self-hosted build has no
gated client, and a hosted account with no projected tenant has nobody to
ask on behalf of; both must reach the exact same catalogue they did before
this existed, and must not spend a request finding that out.
"""

from __future__ import annotations

import pytest

from kokoro_link.application.services.character_card_import_service import (
    ImportedCard,
)
from kokoro_link.application.services.official_card_pack_source import (
    OfficialCardPackSource,
    OfficialCardUnavailableError,
)
from kokoro_link.contracts.official_card_catalog import (
    DISTRIBUTION_CLOUD_EXCLUSIVE,
    DISTRIBUTION_PUBLIC,
    OfficialCardCatalogPort,
    OfficialCardDetail,
    OfficialCardImage,
    OfficialCardNotFound,
    OfficialCardSummary,
)
from kokoro_link.contracts.official_card_gated_catalog import (
    OfficialCardGatedCatalogPort,
)

_PUBLIC_ID = "mio-cafe"
_LOCKED_ID = "mio-wip"
_TENANT = "tenant-42"
_IMAGE_URL = (
    "https://app.yuralume.com/api/user"
    "/v1/public/official-cards/mio-wip/images/0"
)


# --------------------------------------------------------------------------- #
# doubles
# --------------------------------------------------------------------------- #


class _Catalog(OfficialCardCatalogPort):
    """The anonymous shelf, which has never heard of the locked card."""

    def __init__(self, *, reachable: bool = True) -> None:
        self._reachable = reachable
        self.detail_reads: list[str] = []
        self.artifact_downloads: list[str] = []

    async def list_cards(self, *, locale: str) -> list[OfficialCardSummary] | None:
        if not self._reachable:
            return None
        return [OfficialCardSummary(
            id=_PUBLIC_ID, title="美緒", summary="咖啡廳打工女大生",
            distribution=DISTRIBUTION_PUBLIC,
        )]

    async def get_card(
        self, card_id: str, *, locale: str, fresh: bool = False,
    ) -> OfficialCardDetail | None:
        self.detail_reads.append(card_id)
        if not self._reachable:
            return None
        if card_id != _PUBLIC_ID:
            # Exactly what a tier-locked card looks like from out here, and
            # exactly what a card that does not exist looks like.
            raise OfficialCardNotFound(card_id)
        return OfficialCardDetail(
            id=card_id, title="美緒", profile={"name": "美緒"},
        )

    async def download_artifact(self, card_id: str) -> bytes | None:
        self.artifact_downloads.append(card_id)
        return None

    async def download_image(self, *, url: str) -> bytes | None:
        return None


class _GatedCatalog(OfficialCardGatedCatalogPort):
    def __init__(
        self,
        *,
        unavailable: bool = False,
        empty: bool = False,
        card_id: str = _LOCKED_ID,
    ) -> None:
        self._unavailable = unavailable
        self._empty = empty
        self._card_id = card_id
        self.calls: list[tuple[str, str]] = []

    async def list_gated_cards(
        self, *, tenant_id: str, locale: str,
    ) -> list[OfficialCardDetail] | None:
        self.calls.append((tenant_id, locale))
        if self._unavailable:
            return None
        if self._empty:
            # A tenant whose tier simply has nothing fenced to it. An
            # answer, not a failure.
            return []
        return [OfficialCardDetail(
            id=self._card_id,
            title="ミオ（調整中）",
            description="社内テスト中の公式カード",
            localized=True,
            locale=locale,
            available_locales=("zh-Hant", "en", "ja"),
            # Masked like every cloud-exclusive document: no prose, no
            # artifact.
            profile={},
            artifact_published=False,
            images=(OfficialCardImage(index=0, url=_IMAGE_URL),),
        )]


class _Installer:
    """Stands in for the real exclusive installer."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def install(
        self, detail, *, user_id, locale, initial_relationship, tenant_id="",
    ):
        self.calls.append((detail.id, locale, tenant_id))
        return ImportedCard(
            character=None,  # type: ignore[arg-type] — identity is not the point
            landed_arc_template_ids=[],
            landed_arc_series_ids=[],
        )


def _source(
    *,
    gated: _GatedCatalog | None,
    installer: _Installer | None = None,
    reachable: bool = True,
) -> tuple[OfficialCardPackSource, _Catalog]:
    catalog = _Catalog(reachable=reachable)
    return (
        OfficialCardPackSource(
            catalog=catalog,
            import_service=None,  # type: ignore[arg-type] — never reached here
            exclusive_installer=installer,
            gated_catalog=gated,
        ),
        catalog,
    )


# --------------------------------------------------------------------------- #
# browse
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_tenant_in_the_circle_sees_the_locked_card_on_the_same_shelf(
) -> None:
    gated = _GatedCatalog()
    source, _catalog = _source(gated=gated, installer=_Installer())

    rows = await source.list_summaries(
        primary_language="ja-JP", cloud_tenant_id=_TENANT,
    )

    assert rows is not None
    # One shelf, one id space: the locked card is appended to the anonymous
    # rows rather than mixed into them, so the order every other player sees
    # is untouched.
    assert [row.pack_id for row in rows] == [
        f"cloud:{_PUBLIC_ID}", f"cloud:{_LOCKED_ID}",
    ]
    # It renders as an ordinary cloud-exclusive card — no new UI concept for
    # a tester to learn, and installable because the deployment holding the
    # gated client is by construction the one able to install from it.
    locked = rows[1]
    assert locked.distribution == DISTRIBUTION_CLOUD_EXCLUSIVE
    assert locked.installable is True
    assert locked.title == "ミオ（調整中）"
    assert locked.image_urls == [_IMAGE_URL]
    # Asked on behalf of this tenant, in the language the anonymous shelf
    # was asked for.
    assert gated.calls == [(_TENANT, "ja")]


@pytest.mark.asyncio
async def test_an_account_with_no_cloud_tenant_sees_the_shelf_it_always_did(
) -> None:
    gated = _GatedCatalog()
    source, _catalog = _source(gated=gated, installer=_Installer())

    rows = await source.list_summaries(primary_language="ja-JP")

    assert rows is not None
    assert [row.pack_id for row in rows] == [f"cloud:{_PUBLIC_ID}"]
    # And not one request spent working that out.
    assert gated.calls == []


@pytest.mark.asyncio
async def test_a_deployment_with_no_gated_client_is_unchanged_and_unaware(
) -> None:
    """The self-host red line, from the other direction.

    No credential means no gated client at all, so a tier-locked card is
    not merely un-installable here — it does not exist. Passing a tenant id
    in (which a self-host never would) must still change nothing.
    """
    source, _catalog = _source(gated=None, installer=None)

    rows = await source.list_summaries(
        primary_language="ja-JP", cloud_tenant_id=_TENANT,
    )

    assert rows is not None
    assert [row.pack_id for row in rows] == [f"cloud:{_PUBLIC_ID}"]


@pytest.mark.asyncio
async def test_a_gated_outage_takes_one_row_off_the_shelf_not_the_shelf(
) -> None:
    # Fail-soft, and asymmetric on purpose: the tier-locked rows are the
    # part that can go missing, because a handful of testers losing sight of
    # a WIP card for a minute is nothing beside a gallery that will not load.
    source, _catalog = _source(
        gated=_GatedCatalog(unavailable=True), installer=_Installer(),
    )

    rows = await source.list_summaries(
        primary_language="ja-JP", cloud_tenant_id=_TENANT,
    )

    assert rows is not None
    assert [row.pack_id for row in rows] == [f"cloud:{_PUBLIC_ID}"]


@pytest.mark.asyncio
async def test_an_anonymous_catalog_outage_is_still_the_whole_shelf_unknown(
) -> None:
    """Half a shelf flagged unavailable would be a stranger answer than none.

    "The official cards" means the public catalog to every caller, so not
    knowing it is not knowing the shelf — and the gated route is not asked
    at all in that state.
    """
    gated = _GatedCatalog()
    source, _catalog = _source(
        gated=gated, installer=_Installer(), reachable=False,
    )

    rows = await source.list_summaries(
        primary_language="ja-JP", cloud_tenant_id=_TENANT,
    )

    assert rows is None
    assert gated.calls == []


@pytest.mark.asyncio
async def test_a_card_locked_after_publication_shows_once_as_the_locked_row(
) -> None:
    """The two shelves are disjoint on Cloud, not in Core's two clocks.

    Locking an already-published card takes effect on Cloud at once, but the
    anonymous rows Core holds came out of a TTL cache measured in minutes —
    so for that window the same card is on both lists. Concatenating them
    gives the gallery two tiles and two identical keys; the gated row wins
    because it is the newer fact about the card.
    """
    gated = _GatedCatalog(card_id=_PUBLIC_ID)
    source, _catalog = _source(gated=gated, installer=_Installer())

    rows = await source.list_summaries(
        primary_language="ja-JP", cloud_tenant_id=_TENANT,
    )

    assert rows is not None
    assert [row.pack_id for row in rows] == [f"cloud:{_PUBLIC_ID}"]
    assert rows[0].title == "ミオ（調整中）"
    assert rows[0].distribution == DISTRIBUTION_CLOUD_EXCLUSIVE


# --------------------------------------------------------------------------- #
# preview
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_locked_cards_face_is_served_from_the_gated_document() -> None:
    # There is no anonymous detail to fall back on — that read is a 404 —
    # so the row this shelf returned is the only document the card has.
    gated = _GatedCatalog()
    source, catalog = _source(gated=gated, installer=_Installer())

    preview = await source.preview(
        _LOCKED_ID, primary_language="ja-JP", cloud_tenant_id=_TENANT,
    )

    assert preview.pack_id == f"cloud:{_LOCKED_ID}"
    assert preview.distribution == DISTRIBUTION_CLOUD_EXCLUSIVE
    assert preview.installable is True
    assert preview.localized is True
    assert catalog.detail_reads == [_LOCKED_ID]
    assert gated.calls == [(_TENANT, "ja")]


@pytest.mark.asyncio
async def test_an_ordinary_card_never_pays_for_the_second_lookup() -> None:
    # The gated shelf is reached for only on the anonymous miss, so the
    # per-request internal call the plan accepts (R-TG-4) is not one every
    # card face makes.
    gated = _GatedCatalog()
    source, _catalog = _source(gated=gated, installer=_Installer())

    await source.preview(
        _PUBLIC_ID, primary_language="ja-JP", cloud_tenant_id=_TENANT,
    )

    assert gated.calls == []


@pytest.mark.asyncio
async def test_a_card_on_neither_shelf_is_still_simply_missing() -> None:
    source, _catalog = _source(gated=_GatedCatalog(), installer=_Installer())

    with pytest.raises(OfficialCardNotFound):
        await source.preview(
            "never-existed", primary_language="ja-JP", cloud_tenant_id=_TENANT,
        )


@pytest.mark.asyncio
async def test_a_tenant_outside_the_circle_is_told_nothing_at_all() -> None:
    """The oracle red line on the Core side (plan D4).

    Cloud simply does not list the card for this tenant, and what the
    player gets is the answer a card that never existed gets — not a
    different status, not a message with the word "tier" in it.
    """
    source, _catalog = _source(gated=_GatedCatalog(), installer=_Installer())

    with pytest.raises(OfficialCardNotFound):
        await source.preview(
            _LOCKED_ID, primary_language="ja-JP", cloud_tenant_id="",
        )


@pytest.mark.asyncio
async def test_a_tenant_whose_tier_holds_no_such_card_still_gets_the_404(
) -> None:
    """The anti-oracle direction of the fix below.

    The gated shelf answered, and this card is not on it. That is the fence
    working, and it has to keep looking exactly like a card that never
    existed — a 503 here would confirm the card's existence to everybody
    outside the circle.
    """
    source, _catalog = _source(
        gated=_GatedCatalog(empty=True), installer=_Installer(),
    )

    with pytest.raises(OfficialCardNotFound):
        await source.preview(
            _LOCKED_ID, primary_language="ja-JP", cloud_tenant_id=_TENANT,
        )


@pytest.mark.asyncio
async def test_a_self_hosted_build_still_answers_404_for_a_locked_card() -> None:
    # No client to ask with: the card does not exist here, and that is a
    # fact about the wiring rather than a Cloud that went quiet.
    source, _catalog = _source(gated=None, installer=None)

    with pytest.raises(OfficialCardNotFound):
        await source.preview(
            _LOCKED_ID, primary_language="ja-JP", cloud_tenant_id=_TENANT,
        )


@pytest.mark.asyncio
async def test_a_gated_outage_on_a_card_face_is_try_again_not_no_such_card(
) -> None:
    """A blip must not read as "stop looking".

    The anonymous 404 is the *normal* state for a tier-locked card, so it is
    no evidence at all about one; only the gated shelf can say whether this
    card is there. When that shelf could not be read, the honest answer is
    the retryable one a public-card outage already gets (503), not the
    permanent one.
    """
    source, _catalog = _source(
        gated=_GatedCatalog(unavailable=True), installer=_Installer(),
    )

    with pytest.raises(OfficialCardUnavailableError):
        await source.preview(
            _LOCKED_ID, primary_language="ja-JP", cloud_tenant_id=_TENANT,
        )


# --------------------------------------------------------------------------- #
# install
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_installing_a_locked_card_goes_through_the_authenticated_path(
) -> None:
    installer = _Installer()
    source, catalog = _source(gated=_GatedCatalog(), installer=installer)

    await source.install(
        _LOCKED_ID,
        user_id="p1",
        primary_language="ja-JP",
        cloud_tenant_id=_TENANT,
    )

    # The tenant travels all the way to the payload read, where Cloud
    # re-checks the fence: the row this install started from was read a
    # moment ago, and a tier can change in between.
    assert installer.calls == [(_LOCKED_ID, "ja", _TENANT)]
    # And it never asks for an artifact the anonymous route would refuse.
    assert catalog.artifact_downloads == []


@pytest.mark.asyncio
async def test_installing_a_locked_card_without_a_tenant_is_a_missing_card(
) -> None:
    installer = _Installer()
    source, _catalog = _source(gated=_GatedCatalog(), installer=installer)

    with pytest.raises(OfficialCardNotFound):
        await source.install(
            _LOCKED_ID, user_id="p1", primary_language="ja-JP",
        )

    assert installer.calls == []


@pytest.mark.asyncio
async def test_installing_while_the_gated_shelf_is_down_is_retryable() -> None:
    # Same distinction as on the card face, and it matters more here: a
    # player who pressed Install on a card they can see must be told to try
    # again, not that the card was never there.
    installer = _Installer()
    source, _catalog = _source(
        gated=_GatedCatalog(unavailable=True), installer=installer,
    )

    with pytest.raises(OfficialCardUnavailableError):
        await source.install(
            _LOCKED_ID,
            user_id="p1",
            primary_language="ja-JP",
            cloud_tenant_id=_TENANT,
        )

    assert installer.calls == []
