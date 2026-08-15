"""The cloud-exclusive gate on the card shelf — EC4.

One fact decides everything here: whether this deployment holds a
credential that can read a partner's card. A hosted one does; a self-hosted
one structurally cannot. These pin both sides of what that changes — what
the shelf says about such a card, and what happens to an install attempted
anyway.
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
from kokoro_link.contracts.official_card_exclusive import (
    OfficialCardExclusiveNotFound,
    OfficialCardExclusiveUnavailable,
)

_CARD_ID = "mio-cafe"
_IMAGE_URL = (
    "https://app.yuralume.com/api/user"
    "/v1/public/official-cards/mio-cafe/images/0"
)


class _Catalog(OfficialCardCatalogPort):
    def __init__(self, *, distribution: str, exclusive: bool) -> None:
        self._distribution = distribution
        self._exclusive = exclusive
        self.artifact_downloads: list[str] = []

    async def list_cards(self, *, locale: str) -> list[OfficialCardSummary] | None:
        return [OfficialCardSummary(
            id=_CARD_ID,
            title="美緒",
            summary="咖啡廳打工女大生",
            image_url=_IMAGE_URL,
            image_count=1,
            distribution=self._distribution,
        )]

    async def get_card(
        self, card_id: str, *, locale: str, fresh: bool = False,
    ) -> OfficialCardDetail | None:
        return OfficialCardDetail(
            id=card_id,
            title="美緒",
            profile={} if self._exclusive else {"name": "美緒"},
            artifact_published=not self._exclusive,
            images=(OfficialCardImage(index=0, url=_IMAGE_URL),),
        )

    async def download_artifact(self, card_id: str) -> bytes | None:
        self.artifact_downloads.append(card_id)
        return None

    async def download_image(self, *, url: str) -> bytes | None:
        return None


class _Installer:
    """Stands in for the real exclusive installer."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._raises = raises

    async def install(
        self, detail, *, user_id, locale, initial_relationship, tenant_id="",
    ):
        if self._raises is not None:
            raise self._raises
        self.calls.append((detail.id, locale))
        return ImportedCard(
            character=None,  # type: ignore[arg-type] — identity is not the point
            landed_arc_template_ids=[],
            landed_arc_series_ids=[],
        )


def _source(
    *, distribution: str, exclusive: bool, installer: _Installer | None,
) -> tuple[OfficialCardPackSource, _Catalog]:
    catalog = _Catalog(distribution=distribution, exclusive=exclusive)
    return (
        OfficialCardPackSource(
            catalog=catalog,
            import_service=None,  # type: ignore[arg-type] — never reached here
            exclusive_installer=installer,
        ),
        catalog,
    )


# --------------------------------------------------------------------------- #
# what the shelf says
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_self_host_lists_an_exclusive_card_but_cannot_install_it() -> None:
    # Listed like any other card — the name, the summary and the portrait
    # are the shop window, and withholding them would defeat the point of
    # hosting an IP character at all.
    source, _catalog = _source(
        distribution=DISTRIBUTION_CLOUD_EXCLUSIVE,
        exclusive=True,
        installer=None,
    )

    rows = await source.list_summaries(primary_language="zh-TW")

    assert rows is not None
    assert rows[0].title == "美緒"
    assert rows[0].distribution == DISTRIBUTION_CLOUD_EXCLUSIVE
    assert rows[0].installable is False


@pytest.mark.asyncio
async def test_a_hosted_deployment_with_the_credential_may_install_it() -> None:
    source, _catalog = _source(
        distribution=DISTRIBUTION_CLOUD_EXCLUSIVE,
        exclusive=True,
        installer=_Installer(),
    )

    rows = await source.list_summaries(primary_language="zh-TW")

    assert rows is not None
    assert rows[0].distribution == DISTRIBUTION_CLOUD_EXCLUSIVE
    assert rows[0].installable is True


@pytest.mark.asyncio
async def test_an_ordinary_public_card_is_installable_everywhere() -> None:
    source, _catalog = _source(
        distribution=DISTRIBUTION_PUBLIC, exclusive=False, installer=None,
    )

    rows = await source.list_summaries(primary_language="zh-TW")

    assert rows is not None
    assert rows[0].distribution == DISTRIBUTION_PUBLIC
    assert rows[0].installable is True


@pytest.mark.asyncio
async def test_a_detail_read_reports_exclusivity_from_the_withheld_artifact(
) -> None:
    # The detail document carries no ``distribution`` field; the missing
    # artifact URL is how it says the same thing.
    source, _catalog = _source(
        distribution=DISTRIBUTION_CLOUD_EXCLUSIVE,
        exclusive=True,
        installer=None,
    )

    preview = await source.preview(_CARD_ID, primary_language="zh-TW")

    assert preview.distribution == DISTRIBUTION_CLOUD_EXCLUSIVE
    assert preview.installable is False


# --------------------------------------------------------------------------- #
# install
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_install_without_the_credential_answers_like_a_missing_card(
) -> None:
    # The same answer Cloud's own anonymous artifact route gives these
    # cards: a distinct status would tell a caller which published cards are
    # the licensed ones.
    source, catalog = _source(
        distribution=DISTRIBUTION_CLOUD_EXCLUSIVE,
        exclusive=True,
        installer=None,
    )

    with pytest.raises(OfficialCardNotFound):
        await source.install(_CARD_ID, user_id="p1", primary_language="zh-TW")

    # And it never asks Cloud for an artifact that route would refuse.
    assert catalog.artifact_downloads == []


@pytest.mark.asyncio
async def test_install_with_the_credential_goes_through_the_exclusive_path(
) -> None:
    installer = _Installer()
    source, catalog = _source(
        distribution=DISTRIBUTION_CLOUD_EXCLUSIVE,
        exclusive=True,
        installer=installer,
    )

    await source.install(_CARD_ID, user_id="p1", primary_language="ja-JP")

    assert installer.calls == [(_CARD_ID, "ja")]
    assert catalog.artifact_downloads == []


@pytest.mark.asyncio
async def test_a_card_withdrawn_mid_install_reads_as_not_found() -> None:
    source, _catalog = _source(
        distribution=DISTRIBUTION_CLOUD_EXCLUSIVE,
        exclusive=True,
        installer=_Installer(raises=OfficialCardExclusiveNotFound(_CARD_ID)),
    )

    with pytest.raises(OfficialCardNotFound):
        await source.install(_CARD_ID, user_id="p1", primary_language="zh-TW")


@pytest.mark.asyncio
async def test_an_exclusive_endpoint_outage_reads_as_try_again() -> None:
    # Translated into the vocabulary the install path already speaks, so it
    # reaches the player as the 503 a catalog outage gets, not a 500.
    source, _catalog = _source(
        distribution=DISTRIBUTION_CLOUD_EXCLUSIVE,
        exclusive=True,
        installer=_Installer(raises=OfficialCardExclusiveUnavailable(_CARD_ID)),
    )

    with pytest.raises(OfficialCardUnavailableError):
        await source.install(_CARD_ID, user_id="p1", primary_language="zh-TW")
