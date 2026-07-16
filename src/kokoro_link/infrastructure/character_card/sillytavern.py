"""SillyTavern Character Card V2/V3 sniffing + parsing (stdlib only).

This is the *format front layer* for the SillyTavern import feature (see
``docs/SILLYTAVERN_CARD_IMPORT_PLAN.md``). It knows how to:

1. Sniff the carrier of an uploaded blob by magic bytes
   (:func:`sniff_card_kind`) — never by file extension (D2). A ``PK``
   zip signature routes to the existing ``.lumecard`` path; a PNG
   signature routes to the tEXt-chunk walker; a leading ``{`` (after
   BOM / whitespace) routes to raw JSON.
2. Walk a PNG's ``tEXt`` chunks with the stdlib only — no Pillow (D3) —
   to pull the base64-encoded ``chara`` (V2) / ``ccv3`` (V3) payload
   (:func:`extract_png_chara_chunk`).
3. Parse the JSON into a validated :class:`SillyTavernCard` model,
   discriminating V2 vs V3 by the ``spec`` field (:func:`parse_sillytavern_json`).

It performs **no** field conversion into Core shapes and calls **no**
LLM — that is the convert service's job (Phase 2). Keeping the parser
pure makes it trivially unit-testable with synthetic byte fixtures.
"""

from __future__ import annotations

import base64
import binascii
import json
import struct
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_ZIP_SIGNATURE = b"PK\x03\x04"
# Empty-archive / spanned-archive zip signatures a real .lumecard could
# never legitimately start with, but a byte-sniff should still route to
# the zip path so the packager can produce its own clear error.
_ZIP_SIGNATURE_ALT = (b"PK\x05\x06", b"PK\x07\x08")

# Defence in depth for the PNG chunk walker: cap how many chunks we will
# walk and how large a single tEXt payload may be before we decode it, so
# a hand-crafted PNG can't spin us in a loop or blow up memory on decode.
# The overall 64 MB upload cap already bounds the blob; these bound the
# per-chunk work.
_MAX_PNG_CHUNKS = 4096
_MAX_TEXT_CHUNK_BYTES = 8 * 1024 * 1024

# tEXt keyword → card spec. SillyTavern V2 writes the card JSON under the
# ``chara`` keyword; V3 writes it under ``ccv3`` (falling back to
# ``chara`` for compatibility). We prefer ``ccv3`` when both are present.
_TEXT_KEYWORD_CCV3 = b"ccv3"
_TEXT_KEYWORD_CHARA = b"chara"

_SPEC_V2 = "chara_card_v2"
_SPEC_V3 = "chara_card_v3"
_SUPPORTED_SPECS = frozenset({_SPEC_V2, _SPEC_V3})


class SillyTavernCardError(Exception):
    """Base error for SillyTavern card parsing problems."""


class InvalidSillyTavernCardError(SillyTavernCardError):
    """The blob is not a readable SillyTavern card (bad PNG chunk, bad
    base64, malformed JSON, or a missing/unknown ``spec``). Maps to
    HTTP 400 at the route layer, mirroring ``InvalidCharacterCardError``."""


class UnsupportedSillyTavernCardError(SillyTavernCardError):
    """The card declares a ``spec`` this build does not support (e.g. a V1
    flat card, or a future spec). Maps to HTTP 422 at the route layer,
    mirroring ``UnsupportedCardSchemaError``."""


class CardKind(str, Enum):
    """What kind of upload a sniffed blob is."""

    LUMECARD = "lumecard"
    SILLYTAVERN_PNG = "sillytavern_png"
    SILLYTAVERN_JSON = "sillytavern_json"
    UNKNOWN = "unknown"


def sniff_card_kind(blob: bytes) -> CardKind:
    """Classify an uploaded blob by magic bytes (D2 — never by extension).

    - ``PK`` zip signature → :attr:`CardKind.LUMECARD` (existing path;
      the packager will still validate it's a real ``.lumecard``).
    - PNG signature → :attr:`CardKind.SILLYTAVERN_PNG`.
    - leading ``{`` after an optional UTF-8 BOM / leading whitespace →
      :attr:`CardKind.SILLYTAVERN_JSON`.
    - anything else → :attr:`CardKind.UNKNOWN` (the route treats this as a
      bad upload; a real ``.lumecard`` always starts with ``PK``).
    """
    if not blob:
        return CardKind.UNKNOWN
    if blob.startswith(_ZIP_SIGNATURE) or blob.startswith(_ZIP_SIGNATURE_ALT):
        return CardKind.LUMECARD
    if blob.startswith(_PNG_SIGNATURE):
        return CardKind.SILLYTAVERN_PNG
    if _looks_like_json_object(blob):
        return CardKind.SILLYTAVERN_JSON
    return CardKind.UNKNOWN


def _looks_like_json_object(blob: bytes) -> bool:
    """Return ``True`` when the first non-whitespace byte (after an
    optional UTF-8 BOM) is ``{``. We only peek a small prefix; decoding
    the whole blob happens later in :func:`parse_sillytavern_json`."""
    prefix = blob[:64]
    if prefix.startswith(b"\xef\xbb\xbf"):
        prefix = prefix[3:]
    for byte in prefix:
        if byte in (0x20, 0x09, 0x0A, 0x0D):  # space / tab / LF / CR
            continue
        return byte == 0x7B  # '{'
    return False


def extract_png_chara_chunk(blob: bytes) -> str:
    """Return the decoded ``chara`` / ``ccv3`` JSON text from a PNG's
    ``tEXt`` chunks, using the stdlib chunk walker only (D3).

    Walks ``8-byte signature`` then repeated ``length(4) + type(4) +
    data(length) + CRC(4)`` records, collecting every ``tEXt`` keyword.
    Prefers ``ccv3`` (V3) over ``chara`` (V2) when both are present.

    Raises :class:`InvalidSillyTavernCardError` for a truncated / malformed
    PNG, a missing card keyword, or an undecodable base64 payload.
    """
    if not blob.startswith(_PNG_SIGNATURE):
        raise InvalidSillyTavernCardError("not a PNG file")

    text_chunks = _collect_png_text_chunks(blob)
    encoded = text_chunks.get(_TEXT_KEYWORD_CCV3) or text_chunks.get(
        _TEXT_KEYWORD_CHARA,
    )
    if encoded is None:
        raise InvalidSillyTavernCardError(
            "PNG has no SillyTavern character chunk "
            "('chara' / 'ccv3' tEXt keyword)",
        )
    return _decode_base64_text(encoded)


def _collect_png_text_chunks(blob: bytes) -> dict[bytes, bytes]:
    """Walk the PNG and return ``{keyword: raw_value_bytes}`` for each
    ``tEXt`` chunk carrying a card keyword. Later duplicates win (rare).

    Only the card-carrying keywords are retained to keep memory bounded;
    other tEXt chunks (e.g. ``Software``) are skipped."""
    offset = len(_PNG_SIGNATURE)
    total = len(blob)
    chunks: dict[bytes, bytes] = {}
    walked = 0

    while offset + 8 <= total:
        walked += 1
        if walked > _MAX_PNG_CHUNKS:
            raise InvalidSillyTavernCardError("PNG has too many chunks")
        (length,) = struct.unpack(">I", blob[offset : offset + 4])
        chunk_type = blob[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + length
        # +4 for the trailing CRC.
        if length < 0 or data_end + 4 > total:
            raise InvalidSillyTavernCardError("truncated PNG chunk")

        if chunk_type == b"tEXt":
            keyword, value = _split_text_chunk(blob[data_start:data_end])
            if keyword in (_TEXT_KEYWORD_CCV3, _TEXT_KEYWORD_CHARA):
                if len(value) > _MAX_TEXT_CHUNK_BYTES:
                    raise InvalidSillyTavernCardError(
                        "PNG character chunk is too large",
                    )
                chunks[keyword] = value

        offset = data_end + 4  # skip data + CRC
        if chunk_type == b"IEND":
            break

    return chunks


def _split_text_chunk(data: bytes) -> tuple[bytes, bytes]:
    """A PNG ``tEXt`` chunk is ``keyword \\x00 value`` (Latin-1 text)."""
    sep = data.find(b"\x00")
    if sep < 0:
        return data, b""
    return data[:sep], data[sep + 1 :]


def _decode_base64_text(encoded: bytes) -> str:
    """Decode a base64 tEXt payload into UTF-8 JSON text.

    SillyTavern stores the whole card JSON base64-encoded inside the
    tEXt value. Some writers wrap the base64 in surrounding whitespace;
    strip it before decoding."""
    stripped = encoded.strip()
    try:
        raw = base64.b64decode(stripped, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidSillyTavernCardError(
            "PNG character chunk is not valid base64",
        ) from exc
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidSillyTavernCardError(
            "PNG character chunk is not valid UTF-8 JSON",
        ) from exc


class SillyTavernCharacterBook(BaseModel):
    """Minimal lorebook projection — parsed for the preview drop-notice
    (D8: lorebook is dropped in the MVP), never converted into memory."""

    name: str = ""
    entry_count: int = 0

    @classmethod
    def from_raw(cls, raw: object) -> "SillyTavernCharacterBook | None":
        if not isinstance(raw, Mapping):
            return None
        entries = raw.get("entries")
        if isinstance(entries, Sequence) and not isinstance(entries, (str, bytes)):
            entry_count = len(entries)
        elif isinstance(entries, Mapping):
            entry_count = len(entries)
        else:
            entry_count = 0
        name = raw.get("name")
        return cls(
            name=name.strip() if isinstance(name, str) else "",
            entry_count=entry_count,
        )


class SillyTavernCard(BaseModel):
    """Parsed SillyTavern Character Card (V2 or V3).

    Fields mirror the ST ``data`` object. Free-text prose fields
    (``description`` / ``personality`` / ``scenario`` / ``mes_example``)
    are carried verbatim; the convert service normalises them via an LLM
    (Phase 2). ``first_mes`` / ``alternate_greetings`` are carried only as
    tone evidence (D9); ``character_book`` is carried only for the drop
    notice (D8).
    """

    spec: str
    spec_version: str = ""
    name: str = ""
    description: str = ""
    personality: str = ""
    scenario: str = ""
    first_mes: str = ""
    mes_example: str = ""
    creator_notes: str = ""
    creator: str = ""
    tags: list[str] = Field(default_factory=list)
    alternate_greetings: list[str] = Field(default_factory=list)
    character_book: SillyTavernCharacterBook | None = None
    has_assets: bool = False
    """V3 ``assets[]`` presence — MVP uses the main PNG only (P3=a); this
    flag lets the preview note say multi-asset cards were reduced."""


def parse_sillytavern_json(text: str) -> SillyTavernCard:
    """Parse SillyTavern card JSON text into a :class:`SillyTavernCard`.

    Discriminates V2 (``chara_card_v2``) vs V3 (``chara_card_v3``) by the
    top-level ``spec`` field. Card fields live under ``data`` in both
    specs. A V1 flat card (no ``spec``) is explicitly unsupported and
    fails closed rather than guessing at flat fields.

    Raises :class:`InvalidSillyTavernCardError` for malformed JSON /
    shape, and :class:`UnsupportedSillyTavernCardError` for an unknown
    or missing ``spec``.
    """
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise InvalidSillyTavernCardError(
            "SillyTavern card is not valid JSON",
        ) from exc
    if not isinstance(parsed, Mapping):
        raise InvalidSillyTavernCardError(
            "SillyTavern card JSON top-level must be an object",
        )

    spec = parsed.get("spec")
    if not isinstance(spec, str) or not spec.strip():
        raise UnsupportedSillyTavernCardError(
            "SillyTavern card has no 'spec' field — V1 flat cards and "
            "unversioned exports are not supported; re-export as V2/V3.",
        )
    spec_value = spec.strip()
    if spec_value not in _SUPPORTED_SPECS:
        raise UnsupportedSillyTavernCardError(
            f"unsupported SillyTavern card spec {spec_value!r} "
            f"(supported: {', '.join(sorted(_SUPPORTED_SPECS))})",
        )

    data = parsed.get("data")
    if not isinstance(data, Mapping):
        raise InvalidSillyTavernCardError(
            "SillyTavern card is missing its 'data' object",
        )

    spec_version = parsed.get("spec_version")
    return SillyTavernCard(
        spec=spec_value,
        spec_version=spec_version.strip() if isinstance(spec_version, str) else "",
        name=_text(data.get("name")),
        description=_text(data.get("description")),
        personality=_text(data.get("personality")),
        scenario=_text(data.get("scenario")),
        first_mes=_text(data.get("first_mes")),
        mes_example=_text(data.get("mes_example")),
        creator_notes=_text(data.get("creator_notes")),
        creator=_text(data.get("creator")),
        tags=_text_list(data.get("tags")),
        alternate_greetings=_text_list(data.get("alternate_greetings")),
        character_book=SillyTavernCharacterBook.from_raw(data.get("character_book")),
        has_assets=_has_assets(data.get("assets")),
    )


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _text_list(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            cleaned = item.strip()
            if cleaned:
                out.append(cleaned)
    return out


def _has_assets(value: object) -> bool:
    return (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes))
        and len(value) > 0
    )


__all__ = [
    "CardKind",
    "InvalidSillyTavernCardError",
    "SillyTavernCard",
    "SillyTavernCardError",
    "SillyTavernCharacterBook",
    "UnsupportedSillyTavernCardError",
    "extract_png_chara_chunk",
    "parse_sillytavern_json",
    "sniff_card_kind",
]
