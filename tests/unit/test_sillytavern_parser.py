"""Unit tests for the SillyTavern card parser (Phase 1, no LLM).

PNG fixtures are hand-built from raw bytes — no Pillow (D3) — to prove
the stdlib chunk walker reads ``chara`` / ``ccv3`` tEXt chunks correctly.
"""

from __future__ import annotations

import base64
import json
import struct
import zlib

import pytest

from kokoro_link.infrastructure.character_card.sillytavern import (
    CardKind,
    InvalidSillyTavernCardError,
    UnsupportedSillyTavernCardError,
    extract_png_chara_chunk,
    parse_sillytavern_json,
    sniff_card_kind,
)

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(chunk_type: bytes, data: bytes) -> bytes:
    length = struct.pack(">I", len(data))
    crc = struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
    return length + chunk_type + data + crc


def _text_chunk(keyword: bytes, value: bytes) -> bytes:
    return _chunk(b"tEXt", keyword + b"\x00" + value)


def _build_png(*text_chunks: bytes) -> bytes:
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
    iend = _chunk(b"IEND", b"")
    return _PNG_SIGNATURE + ihdr + b"".join(text_chunks) + iend


def _v2_card(**overrides: object) -> dict:
    data = {
        "name": "Mio",
        "description": "A cheerful barista who loves latte art.",
        "personality": "warm, energetic",
        "scenario": "You walk into her cafe on a rainy afternoon.",
        "first_mes": "Welcome in! Rough weather, huh?",
        "mes_example": "<START>\n{{char}}: Order up! One caramel macchiato~",
        "creator_notes": "Best with an anime image profile.",
        "creator": "cafe_author",
        "tags": ["slice-of-life", "modern"],
        "alternate_greetings": ["Oh, you again!"],
    }
    data.update(overrides)
    return {"spec": "chara_card_v2", "spec_version": "2.0", "data": data}


def _v3_card(**overrides: object) -> dict:
    card = _v2_card(**overrides)
    card["spec"] = "chara_card_v3"
    card["spec_version"] = "3.0"
    return card


def _b64_json(card: dict) -> bytes:
    return base64.b64encode(json.dumps(card).encode("utf-8"))


# --- sniffing ---------------------------------------------------------


def test_sniff_zip_signature_is_lumecard() -> None:
    assert sniff_card_kind(b"PK\x03\x04rest") is CardKind.LUMECARD


def test_sniff_png_signature_is_sillytavern_png() -> None:
    assert sniff_card_kind(_build_png()) is CardKind.SILLYTAVERN_PNG


def test_sniff_leading_brace_is_sillytavern_json() -> None:
    assert sniff_card_kind(b'  \n{"spec": "chara_card_v2"}') is (
        CardKind.SILLYTAVERN_JSON
    )


def test_sniff_json_with_bom_is_sillytavern_json() -> None:
    assert sniff_card_kind(b"\xef\xbb\xbf{}") is CardKind.SILLYTAVERN_JSON


def test_sniff_unknown_blob() -> None:
    assert sniff_card_kind(b"just some text") is CardKind.UNKNOWN
    assert sniff_card_kind(b"") is CardKind.UNKNOWN


# --- PNG chunk extraction ---------------------------------------------


def test_extract_v2_chara_chunk() -> None:
    png = _build_png(_text_chunk(b"chara", _b64_json(_v2_card())))
    text = extract_png_chara_chunk(png)
    assert json.loads(text)["spec"] == "chara_card_v2"


def test_extract_v3_ccv3_chunk_preferred_over_chara() -> None:
    png = _build_png(
        _text_chunk(b"chara", _b64_json(_v2_card())),
        _text_chunk(b"ccv3", _b64_json(_v3_card())),
    )
    text = extract_png_chara_chunk(png)
    assert json.loads(text)["spec"] == "chara_card_v3"


def test_extract_ignores_unrelated_text_chunks() -> None:
    png = _build_png(
        _text_chunk(b"Software", b"NightCafe"),
        _text_chunk(b"chara", _b64_json(_v2_card())),
    )
    assert json.loads(extract_png_chara_chunk(png))["data"]["name"] == "Mio"


def test_extract_png_without_chara_chunk_raises() -> None:
    png = _build_png(_text_chunk(b"Software", b"whatever"))
    with pytest.raises(InvalidSillyTavernCardError):
        extract_png_chara_chunk(png)


def test_extract_png_with_bad_base64_raises() -> None:
    png = _build_png(_text_chunk(b"chara", b"!!!not base64!!!"))
    with pytest.raises(InvalidSillyTavernCardError):
        extract_png_chara_chunk(png)


def test_extract_non_png_raises() -> None:
    with pytest.raises(InvalidSillyTavernCardError):
        extract_png_chara_chunk(b"PK\x03\x04 not a png")


def test_extract_truncated_chunk_raises() -> None:
    # Claim a 9999-byte chunk but supply nothing after the header.
    bad = _PNG_SIGNATURE + struct.pack(">I", 9999) + b"tEXt"
    with pytest.raises(InvalidSillyTavernCardError):
        extract_png_chara_chunk(bad)


# --- JSON parsing + version discrimination ----------------------------


def test_parse_v2_card_fields() -> None:
    card = parse_sillytavern_json(json.dumps(_v2_card()))
    assert card.spec == "chara_card_v2"
    assert card.name == "Mio"
    assert card.description.startswith("A cheerful barista")
    assert card.tags == ["slice-of-life", "modern"]
    assert card.creator == "cafe_author"
    assert card.alternate_greetings == ["Oh, you again!"]


def test_parse_v3_card_discriminated() -> None:
    card = parse_sillytavern_json(json.dumps(_v3_card()))
    assert card.spec == "chara_card_v3"
    assert card.spec_version == "3.0"


def test_parse_v3_assets_flag() -> None:
    card = parse_sillytavern_json(
        json.dumps(_v3_card(assets=[{"type": "icon", "uri": "main"}])),
    )
    assert card.has_assets is True


def test_parse_character_book_entry_count() -> None:
    card = parse_sillytavern_json(
        json.dumps(
            _v2_card(
                character_book={
                    "name": "Lore",
                    "entries": [{"keys": ["cafe"]}, {"keys": ["latte"]}],
                },
            ),
        ),
    )
    assert card.character_book is not None
    assert card.character_book.entry_count == 2


def test_parse_missing_spec_is_unsupported() -> None:
    # A V1 flat card has no spec — fail closed, don't guess flat fields.
    with pytest.raises(UnsupportedSillyTavernCardError):
        parse_sillytavern_json(json.dumps({"name": "Flat", "description": "V1"}))


def test_parse_unknown_spec_is_unsupported() -> None:
    with pytest.raises(UnsupportedSillyTavernCardError):
        parse_sillytavern_json(json.dumps({"spec": "chara_card_v9", "data": {}}))


def test_parse_missing_data_object_raises() -> None:
    with pytest.raises(InvalidSillyTavernCardError):
        parse_sillytavern_json(json.dumps({"spec": "chara_card_v2"}))


def test_parse_malformed_json_raises() -> None:
    with pytest.raises(InvalidSillyTavernCardError):
        parse_sillytavern_json("{not json")


def test_parse_forward_compatible_extra_fields_ignored() -> None:
    card = parse_sillytavern_json(
        json.dumps(_v2_card(some_future_field="ignored", nested={"x": 1})),
    )
    assert card.name == "Mio"
