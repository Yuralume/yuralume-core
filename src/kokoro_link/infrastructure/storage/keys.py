"""Object-key validation shared by storage adapters and storage service."""

from __future__ import annotations

from pathlib import PurePosixPath
from urllib.parse import unquote

from kokoro_link.contracts.object_storage import ObjectStorageError

_ALLOWED_CHARS = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "._-",
)


def safe_key_segment(raw: str | None, *, fallback: str) -> str:
    """Reduce ``raw`` to a single object-key path segment, or ``fallback``.

    Callers use this to fold a caller-supplied identifier (an operator id, a
    user id) into a key without letting it shape the key's structure. The
    allow-list is exactly :data:`_ALLOWED_CHARS` — the same set
    :func:`validate_object_key` enforces — so anything this function emits
    survives validation downstream. Characters outside it are dropped rather
    than replaced, and ``/`` is not in the set, so a traversal payload
    collapses instead of escaping: ``"../evil"`` becomes ``"..evil"``, one
    harmless segment.

    Two traps this closes that a naive ``str.isalnum`` filter does not:

    * **CJK.** ``"使用者".isalnum()`` is ``True``, but ``validate_object_key``
      refuses the resulting key — so an ASCII-blind sanitiser hands back a
      "clean" segment whose only effect is to make the write fail, once per
      request, for every non-ASCII id.
    * **All dots.** ``".."`` survives any per-character allow-list that keeps
      ``.``, and it is precisely the segment the validator rejects. An id
      that sanitises down to nothing but dots therefore falls back too.

    ``fallback`` is the caller's, not this module's: "no id" means different
    things at different call sites (an anonymous draft vs an unidentified
    upload), and picking one here would flatten that distinction.
    """
    cleaned = "".join(ch for ch in (raw or "") if ch in _ALLOWED_CHARS)
    if not cleaned.strip("."):
        return fallback
    return cleaned


def validate_object_key(raw: str) -> str:
    key = unquote((raw or "").strip())
    if not key:
        raise ObjectStorageError("object_key must be non-empty")
    if key.startswith("/") or "\\" in key:
        raise ObjectStorageError(f"unsafe object_key: {raw!r}")
    path = PurePosixPath(key)
    parts = path.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ObjectStorageError(f"unsafe object_key: {raw!r}")
    for part in parts:
        if any(ch not in _ALLOWED_CHARS for ch in part):
            raise ObjectStorageError(f"unsafe object_key segment: {part!r}")
    return "/".join(parts)

