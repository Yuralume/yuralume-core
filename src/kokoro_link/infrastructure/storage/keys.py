"""Object-key validation shared by storage adapters and storage service."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from urllib.parse import unquote

from kokoro_link.contracts.object_storage import ObjectStorageError

#: Any segment made up of nothing but dots -- ``"."``, ``".."``, ``"..."``,
#: ``"...."``... -- not just the two POSIX-special cases. Win32 path
#: normalisation silently *strips* a trailing-dot path component entirely
#: (``"tts\\...\\"`` resolves to ``"tts\\"``), so on Windows a segment this
#: pattern doesn't catch can make a purge prefix that looks two-segments-deep
#: (``"tts/.../"``, passes the length-2 check below) collapse, once resolved
#: on disk, to its one-segment parent -- turning a scoped purge into one that
#: empties the whole parent directory. POSIX doesn't strip these, but the
#: validator is shared code and must be safe regardless of the OS it runs on.
_ALL_DOTS_SEGMENT_RE = re.compile(r"^\.+$")

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


def validate_purge_prefix(raw: str) -> str:
    """Validate a prefix for :meth:`SupportsPrefixPurge.purge_prefix`.

    A prefix purge is bulk, irreversible deletion driven by a caller-built
    string — CD1's threat model (contracts/object_storage.py) is that
    string being wrong, not malicious, but "wrong" is exactly as
    destructive as "malicious" when the operation is delete-everything-
    under. The shape checks below exist to make a purge call structurally
    incapable of reaching past the one character's subtree it is meant
    for:

    * must end with ``/`` — a purge always targets a directory-shaped
      subtree, never a single object key or a bare stem that a later typo
      could turn into a much wider match (``"characters/1"`` would also
      match ``"characters/10/..."``; ``"characters/1/"`` cannot);
    * at least two non-empty segments — ``characters/{id}/`` is the
      shallowest legitimate target. A single segment such as
      ``"characters/"`` would purge *every* character's objects in one
      call, which is never what a per-character delete wants;
    * every segment passes the same character allow-list
      :func:`validate_object_key` enforces, and any all-dots segment
      (``.``, ``..``, ``...``, ...) is rejected the same way — a prefix
      this function returns is a prefix ``validate_object_key`` would
      also accept as key parts. All-dots segments beyond ``.``/``..`` are
      rejected here specifically (not just those two): see
      :data:`_ALL_DOTS_SEGMENT_RE`.

    Raises :class:`ObjectStorageError` on any violation. Returns the
    normalised prefix (always ``/``-joined, always trailing-slashed) on
    success.
    """
    text = unquote((raw or "").strip())
    if not text.endswith("/"):
        raise ObjectStorageError(f"purge prefix must end with '/': {raw!r}")
    if text.startswith("/") or "\\" in text:
        raise ObjectStorageError(f"unsafe purge prefix: {raw!r}")
    segments = text[:-1].split("/")
    if len(segments) < 2 or any(
        seg == "" or _ALL_DOTS_SEGMENT_RE.match(seg) for seg in segments
    ):
        raise ObjectStorageError(
            f"purge prefix needs at least two non-empty, non-dots-only "
            f"segments: {raw!r}",
        )
    for segment in segments:
        if any(ch not in _ALLOWED_CHARS for ch in segment):
            raise ObjectStorageError(f"unsafe purge prefix segment: {segment!r}")
    return "/".join(segments) + "/"

