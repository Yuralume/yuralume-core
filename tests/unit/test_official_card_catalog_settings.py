"""``KOKORO_OFFICIAL_CARD_CATALOG_URL`` — the opt-out that has to work.

This is the one variable a privacy-minded or air-gapped self-hoster is told
to set, and the promise attached to it ("nothing is contacted at all") is
only as good as this parsing. Three states, and the difference between the
last two is the whole point: unset means "I never configured this", empty
means "I do not want this".
"""

from __future__ import annotations

import pytest

from kokoro_link.bootstrap.settings import (
    DEFAULT_OFFICIAL_CARD_CATALOG_URL,
    OfficialCardCatalogSettings,
)


def test_unset_means_the_official_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KOKORO_OFFICIAL_CARD_CATALOG_URL", raising=False)

    settings = OfficialCardCatalogSettings.from_env()

    assert settings.catalog_url == DEFAULT_OFFICIAL_CARD_CATALOG_URL
    assert settings.enabled is True


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_turns_the_whole_thing_off(
    monkeypatch: pytest.MonkeyPatch, value: str,
) -> None:
    monkeypatch.setenv("KOKORO_OFFICIAL_CARD_CATALOG_URL", value)

    settings = OfficialCardCatalogSettings.from_env()

    assert settings.catalog_url == ""
    assert settings.enabled is False


def test_a_custom_endpoint_is_taken_verbatim_minus_a_trailing_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "KOKORO_OFFICIAL_CARD_CATALOG_URL",
        " https://cards.example.test/api/user/v1/public/official-cards/ ",
    )

    settings = OfficialCardCatalogSettings.from_env()

    # Paths are joined onto this, so exactly one slash between segments.
    assert settings.catalog_url == (
        "https://cards.example.test/api/user/v1/public/official-cards"
    )


def test_the_default_points_at_the_portal_edge_path() -> None:
    """The default carries the edge's ``/api/user`` prefix on purpose.

    That prefix is where the portal's nginx forwards the public catalog, and
    the relative asset paths inside the documents are resolved against it. A
    default without it would list cards whose every image 404s.
    """
    assert DEFAULT_OFFICIAL_CARD_CATALOG_URL.endswith(
        "/api/user/v1/public/official-cards",
    )
