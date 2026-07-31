"""resolve_seed_region — operator primary_language → seed region."""

import pytest

from kokoro_link.application.services.story_seed_region import (
    resolve_seed_region,
)


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("zh-TW", "tw"),
        ("zh", "tw"),
        ("zh-Hant-TW", "tw"),
        ("ja-JP", "jp"),
        ("ja", "jp"),
        ("en-US", "west"),
        ("en", "west"),
        ("EN-GB", "west"),
        ("ko-KR", None),
        ("fr-FR", None),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_resolve_seed_region(language: str | None, expected: str | None) -> None:
    assert resolve_seed_region(language) == expected
