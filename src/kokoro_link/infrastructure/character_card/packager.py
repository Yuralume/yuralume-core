"""Zip container read/write for character cards (``.lumecard``).

The container layout is:

```
manifest.json                # card metadata + A-layer character settings
assets/stage/0.png 1.png ... # stage carousel images (image_urls)
arc_templates/<id>.yaml      # bundled arc-template blueprints (0..N)
```

This module knows nothing about characters or domain entities — it only
moves bytes in and out of the zip. The export/import services own the
projection between domain and ``manifest.json``.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

MANIFEST_NAME = "manifest.json"
STAGE_DIR = "assets/stage/"
ARC_TEMPLATE_DIR = "arc_templates/"

# Defence in depth — a hand-crafted card shouldn't be able to balloon
# memory on unpack. Generous enough for a dozen stage images + a handful
# of templates; small enough that a malicious 2GB zip bomb is rejected.
_MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024

CARD_IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


class CharacterCardError(Exception):
    """Base error for malformed / unreadable character cards."""


class InvalidCharacterCardError(CharacterCardError):
    """The blob is not a readable ``.lumecard`` (bad zip, missing
    manifest, unsafe member path, or oversized payload)."""


@dataclass(frozen=True, slots=True)
class UnpackedCard:
    """In-memory view of an unpacked card.

    ``manifest`` is the parsed ``manifest.json`` mapping (not yet
    validated against the DTO — the import service does that).
    ``stage_images`` maps each member path (e.g. ``assets/stage/0.png``)
    to its raw bytes. ``arc_templates`` maps each YAML member's basename
    (e.g. ``cafe_idol.yaml``) to its text contents.
    """

    manifest: dict
    stage_images: dict[str, bytes]
    arc_templates: dict[str, str]


def pack_character_card(
    *,
    manifest_json: str,
    stage_images: list[tuple[str, bytes]],
    arc_templates: list[tuple[str, str]],
) -> bytes:
    """Build a ``.lumecard`` zip from the given parts.

    ``stage_images`` entries are ``(member_path, data)`` where
    ``member_path`` is already the full in-zip path (e.g.
    ``assets/stage/0.png``). ``arc_templates`` entries are
    ``(filename, yaml_text)`` where ``filename`` is just the basename
    (e.g. ``cafe_idol.yaml``); the ``arc_templates/`` prefix is added
    here.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MANIFEST_NAME, manifest_json)
        for member_path, data in stage_images:
            zf.writestr(member_path, data)
        for filename, yaml_text in arc_templates:
            zf.writestr(f"{ARC_TEMPLATE_DIR}{filename}", yaml_text)
    return buffer.getvalue()


def unpack_character_card(blob: bytes) -> UnpackedCard:
    """Read a ``.lumecard`` blob into memory.

    Raises :class:`InvalidCharacterCardError` for a corrupt zip, a
    missing / unparseable ``manifest.json``, an unsafe member path
    (zip-slip / absolute path), or a payload over the size cap.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise InvalidCharacterCardError("not a valid zip archive") from exc

    with zf:
        total = sum(max(info.file_size, 0) for info in zf.infolist())
        if total > _MAX_UNCOMPRESSED_BYTES:
            raise InvalidCharacterCardError("card payload too large")

        manifest: dict | None = None
        stage_images: dict[str, bytes] = {}
        arc_templates: dict[str, str] = {}

        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename
            if not _is_safe_member(name):
                raise InvalidCharacterCardError(f"unsafe member path: {name!r}")
            if name == MANIFEST_NAME:
                raw = zf.read(info)
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise InvalidCharacterCardError(
                        "manifest.json is not valid JSON",
                    ) from exc
                if not isinstance(parsed, dict):
                    raise InvalidCharacterCardError(
                        "manifest.json top-level must be an object",
                    )
                manifest = parsed
            elif name.startswith(STAGE_DIR):
                stage_images[name] = zf.read(info)
            elif name.startswith(ARC_TEMPLATE_DIR) and name.endswith(".yaml"):
                basename = name[len(ARC_TEMPLATE_DIR):]
                arc_templates[basename] = zf.read(info).decode("utf-8")
            # Unknown members are ignored — forward-compatible with cards
            # that carry extra files a newer exporter added.

    if manifest is None:
        raise InvalidCharacterCardError("missing manifest.json")

    return UnpackedCard(
        manifest=manifest,
        stage_images=stage_images,
        arc_templates=arc_templates,
    )


def card_member_image_mime_type(member_path: str) -> str | None:
    """Return the image MIME type implied by an in-card member path."""
    return CARD_IMAGE_MIME_TYPES.get(PurePosixPath(member_path).suffix.lower())


def _is_safe_member(name: str) -> bool:
    """Reject absolute paths and ``..`` traversal in a zip member name.

    We never write members to disk (everything goes into in-memory
    dicts), so the practical risk is low, but a member path also becomes
    a storage key prefix on import — guarding here keeps a hostile card
    from smuggling ``../`` into a later ``put_bytes`` key."""
    if not name or name.startswith(("/", "\\")):
        return False
    # Normalise separators so a Windows-authored zip can't sneak ``..``
    # past us with backslashes.
    parts = name.replace("\\", "/").split("/")
    return ".." not in parts
