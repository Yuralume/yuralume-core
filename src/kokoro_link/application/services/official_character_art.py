"""Where a managed character's official art lives, and what it means.

An IP-partner card installs its stage images into this deployment's own
object storage (EC4) — the generation hot path must not reach out to Cloud
every time it draws — and two different consumers then have to recognise
them again later:

* the **delete guard** (:mod:`character_image_service`): official art is the
  partner's identity, not the player's upload, and a managed character's
  carousel must not be editable down to nothing;
* the **reference-image injection** (EC6): only the images Cloud *marked*
  are the likeness lock, and an image model handed the whole gallery is a
  different instruction than one handed the marked portrait.

Both of those are per-image facts about art that lands in
``Character.image_urls`` — an ordered list of plain strings with nowhere to
hang a flag, and no schema column to add (the provenance migration was EC2-A
and is closed). So the fact is carried **by the object key**, which is
already per-image, already immutable, and already reachable from a URL
through ``ObjectStoragePort.object_key_from_url``.

One module, three consumers, one convention::

    characters/{character_id}/official/{index}{ext}            official art
    characters/{character_id}/official-reference/{index}{ext}   … and a lock

Sibling prefixes rather than nesting, so "is this the lock" is one prefix
test rather than a prefix test qualified by another one.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

OFFICIAL_ART_SEGMENT = "official"
OFFICIAL_REFERENCE_SEGMENT = "official-reference"


def official_art_prefix(character_id: str, *, reference: bool) -> str:
    segment = (
        OFFICIAL_REFERENCE_SEGMENT if reference else OFFICIAL_ART_SEGMENT
    )
    return f"characters/{character_id}/{segment}/"


def official_art_object_key(
    character_id: str, *, index: int, extension: str, reference: bool,
) -> str:
    """The permanent key for one official stage image.

    Keyed by the card's own stage ``index`` rather than a fresh UUID (which
    is what a player upload gets): official art is not appended to over
    time, it is the card's fixed set, and a deterministic key makes a
    re-install idempotent instead of accumulating orphans.
    """
    suffix = extension if extension.startswith(".") else f".{extension}"
    return f"{official_art_prefix(character_id, reference=reference)}{index}{suffix}"


def is_official_art_key(object_key: str | None) -> bool:
    """Whether this key holds partner art — reference-marked or not.

    Used by the delete guard, which does not care which of the two it is:
    both are the partner's, neither is the player's to remove.
    """
    if not object_key:
        return False
    segments = object_key.split("/")
    return (
        len(segments) >= 4
        and segments[0] == "characters"
        and segments[2] in {OFFICIAL_ART_SEGMENT, OFFICIAL_REFERENCE_SEGMENT}
    )


def is_official_reference_key(object_key: str | None) -> bool:
    """Whether this key holds an image Cloud marked as the likeness lock."""
    if not object_key:
        return False
    segments = object_key.split("/")
    return (
        len(segments) >= 4
        and segments[0] == "characters"
        and segments[2] == OFFICIAL_REFERENCE_SEGMENT
    )


def select_official_reference_urls(
    image_urls: Iterable[str],
    object_key_of: Callable[[str], str | None],
) -> list[str]:
    """The likeness-lock subset of a character's gallery, in carousel order.

    The entry point for EC6's reference injection, so that "which of these
    images did Cloud mark" is asked in one place rather than re-derived from
    the key convention at each of the four generation call sites.

    ``object_key_of`` is the storage port's ``object_key_from_url``; passing
    it keeps this module free of any dependency on storage while still
    speaking about keys, which is the only place the mark exists.
    """
    selected: list[str] = []
    for url in image_urls:
        try:
            object_key = object_key_of(url)
        except Exception:  # noqa: BLE001 — adapters differ on malformed urls
            continue
        if is_official_reference_key(object_key):
            selected.append(url)
    return selected


__all__ = [
    "OFFICIAL_ART_SEGMENT",
    "OFFICIAL_REFERENCE_SEGMENT",
    "is_official_art_key",
    "is_official_reference_key",
    "official_art_object_key",
    "official_art_prefix",
    "select_official_reference_urls",
]
