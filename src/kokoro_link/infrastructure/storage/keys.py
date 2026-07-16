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

