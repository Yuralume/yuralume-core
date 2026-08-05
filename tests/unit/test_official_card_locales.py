"""The one place Core's language tags and Cloud's locale tags line up.

Core stores ``zh-TW`` / ``en-US`` / ``ja-JP``; the Cloud catalog keys its
approved translations by ``zh-Hant`` / ``en`` / ``ja``. This is the first
seam between the two vocabularies, and the failure it guards against is
silent: a mis-written pair does not raise, it serves a reader the wrong
language.
"""

from __future__ import annotations

import pytest

from kokoro_link.contracts.official_card_catalog import (
    CLOUD_LOCALE_BY_LANGUAGE_TAG,
    cloud_pack_ref,
    parse_cloud_pack_ref,
    to_cloud_locale,
    to_language_tag,
)


@pytest.mark.parametrize(
    ("language_tag", "cloud_locale"),
    [("zh-TW", "zh-Hant"), ("en-US", "en"), ("ja-JP", "ja")],
)
def test_the_three_published_languages_map_both_ways(
    language_tag: str, cloud_locale: str,
) -> None:
    assert to_cloud_locale(language_tag) == cloud_locale
    assert to_language_tag(cloud_locale) == language_tag


def test_the_table_is_exactly_the_three_published_languages() -> None:
    # Adding a fourth is a cross-repo change: Cloud's locale CHECK
    # constraint has to accept it first. Pinning the set here makes a
    # one-sided addition fail loudly instead of resolving to English.
    assert set(CLOUD_LOCALE_BY_LANGUAGE_TAG) == {"zh-TW", "en-US", "ja-JP"}


@pytest.mark.parametrize("unknown", ["", "  ", "ko-KR", "zh", "en", "zh-Hant"])
def test_an_unmapped_tag_returns_none_rather_than_english(unknown: str) -> None:
    """``None`` is the point.

    Substituting ``en`` here would let the caller ask the catalog for a
    language it does publish, get ``localized=true`` back, and hide the
    translate toggle from the one player whose language is missing.
    """
    assert to_cloud_locale(unknown) is None


@pytest.mark.parametrize("unknown", ["", "ko", "zh-TW", "en-US"])
def test_an_unknown_cloud_locale_has_no_language_tag(unknown: str) -> None:
    assert to_language_tag(unknown) is None


def test_cloud_pack_refs_round_trip_and_leave_local_ids_alone() -> None:
    assert cloud_pack_ref("mio-cafe") == "cloud:mio-cafe"
    assert parse_cloud_pack_ref("cloud:mio-cafe") == "mio-cafe"
    # A local pack id is a filename stem and stays exactly itself.
    assert parse_cloud_pack_ref("demo_mio") is None
    assert parse_cloud_pack_ref("美緒") is None
    # A bare prefix names no card at all.
    assert parse_cloud_pack_ref("cloud:") is None
