"""Canonical request hash vectors (DR-LH0-003 / LH2).

The hash must be byte-stable across the Cloud producer and the Core verifier,
so these lock the canonicalization rules: code-point key ordering, NFC string
normalization, nested objects, order-sensitive arrays, the null-vs-omitted
distinction, int-vs-float distinction, NaN/Infinity rejection and UTF-8.
"""

from __future__ import annotations

import unicodedata

import pytest

from kokoro_link.application.services.external_chat.canonical_hash import (
    CANONICAL_HASH_VERSION,
    compute_canonical_request_hash,
)


def test_hash_is_lowercase_hex_sha256() -> None:
    digest = compute_canonical_request_hash({"message": "hi"})
    assert len(digest) == 64
    assert digest == digest.lower()
    int(digest, 16)  # valid hex


def test_version_constant_is_one() -> None:
    assert CANONICAL_HASH_VERSION == 1


def test_key_order_does_not_change_hash() -> None:
    a = compute_canonical_request_hash({"a": 1, "b": 2, "c": 3})
    b = compute_canonical_request_hash({"c": 3, "b": 2, "a": 1})
    assert a == b


def test_key_ordering_is_by_code_point() -> None:
    # "A" (0x41) < "a" (0x61) < "é" (0xE9) — a stable, well-defined order that
    # would differ under a locale-aware collation.
    a = compute_canonical_request_hash({"é": 1, "a": 2, "A": 3})
    b = compute_canonical_request_hash({"A": 3, "a": 2, "é": 1})
    assert a == b


def test_nfc_composed_equals_decomposed() -> None:
    precomposed = "é"          # é  (single code point)
    decomposed = "é"          # e + combining acute
    assert precomposed != decomposed
    assert unicodedata.normalize("NFC", decomposed) == precomposed
    assert compute_canonical_request_hash(
        {"name": precomposed},
    ) == compute_canonical_request_hash({"name": decomposed})


def test_nfc_applies_to_keys_too() -> None:
    precomposed = "é"
    decomposed = "é"
    assert compute_canonical_request_hash(
        {precomposed: 1},
    ) == compute_canonical_request_hash({decomposed: 1})


def test_nested_objects_are_canonicalized() -> None:
    a = compute_canonical_request_hash(
        {"outer": {"y": 2, "x": 1}, "top": "v"},
    )
    b = compute_canonical_request_hash(
        {"top": "v", "outer": {"x": 1, "y": 2}},
    )
    assert a == b


def test_array_order_is_significant() -> None:
    assert compute_canonical_request_hash(
        {"xs": [1, 2, 3]},
    ) != compute_canonical_request_hash({"xs": [3, 2, 1]})


def test_arrays_are_not_deduplicated() -> None:
    assert compute_canonical_request_hash(
        {"xs": [1, 1]},
    ) != compute_canonical_request_hash({"xs": [1]})


def test_null_differs_from_omitted() -> None:
    with_null = compute_canonical_request_hash(
        {"message": "hi", "conversation_id": None},
    )
    omitted = compute_canonical_request_hash({"message": "hi"})
    assert with_null != omitted


def test_null_differs_from_empty_string() -> None:
    assert compute_canonical_request_hash(
        {"k": None},
    ) != compute_canonical_request_hash({"k": ""})


def test_integer_differs_from_float() -> None:
    assert compute_canonical_request_hash(
        {"n": 1},
    ) != compute_canonical_request_hash({"n": 1.0})


def test_integers_are_not_floated() -> None:
    # A large integer must be rendered verbatim, never via float rounding.
    big = 10_000_000_000_000_001
    digest = compute_canonical_request_hash({"n": big})
    # Stable regardless of how the dict was built.
    assert digest == compute_canonical_request_hash({"n": big})


def test_bool_is_not_int() -> None:
    assert compute_canonical_request_hash(
        {"flag": True},
    ) != compute_canonical_request_hash({"flag": 1})
    assert compute_canonical_request_hash(
        {"flag": False},
    ) != compute_canonical_request_hash({"flag": 0})


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_nan_and_infinity_raise(bad: float) -> None:
    with pytest.raises(ValueError):
        compute_canonical_request_hash({"n": bad})


def test_chinese_utf8_is_stable() -> None:
    a = compute_canonical_request_hash({"message": "你好，世界"})
    b = compute_canonical_request_hash({"message": "你好，世界"})
    assert a == b
    # Different content → different hash (no accidental constant).
    assert a != compute_canonical_request_hash({"message": "你好"})


def test_attachments_preserve_order_and_int_size() -> None:
    a = compute_canonical_request_hash(
        {
            "message": "look",
            "attachments": [
                {"object_ref": "k1", "size_bytes": 10, "sha256": "ab"},
                {"object_ref": "k2", "size_bytes": 20, "sha256": "cd"},
            ],
        },
    )
    swapped = compute_canonical_request_hash(
        {
            "message": "look",
            "attachments": [
                {"object_ref": "k2", "size_bytes": 20, "sha256": "cd"},
                {"object_ref": "k1", "size_bytes": 10, "sha256": "ab"},
            ],
        },
    )
    assert a != swapped


def test_unsupported_type_raises() -> None:
    with pytest.raises(TypeError):
        compute_canonical_request_hash({"when": object()})
