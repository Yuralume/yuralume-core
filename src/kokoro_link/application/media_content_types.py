"""The one allow-list of image content types this app will serve from its
own origin.

Two independent surfaces attach a content type to bytes that the public
media route later echoes back **from the app's own origin**:

* player uploads (``POST /chat/uploads`` / ``POST /characters/draft``) —
  the client's declared ``Content-Type`` header, guarded by
  :func:`~kokoro_link.api.routes._uploads.ensure_allowed_upload_content_type`;
* ``.lumebackup`` restore media landing — the ``content_type`` an
  attacker put in the backup manifest, applied when the original is
  re-``put`` under the new character's keys.

Both are attacker-chosen strings, and a value like ``text/html`` (or
``image/svg+xml`` — an image by any naive test, and a scripting
container) becomes a same-origin document served under a URL the app
itself minted: stored XSS. The allow-list therefore lives in one place,
neutral to both the API layer and the application services, so the two
surfaces can never drift apart.
"""

from __future__ import annotations

#: The only content types an image may declare to be stored and served
#: back from our origin. Deliberately narrower than ``image/*``: SVG is an
#: image by that test and also carries script, so it is excluded on
#: purpose (the reason a filename allow-list alone never closes this).
ALLOWED_UPLOAD_IMAGE_CONTENT_TYPES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif"},
)


def normalise_content_type(content_type: str | None) -> str:
    """Strip parameters and normalise casing/whitespace of a declared type.

    ``"IMAGE/PNG; charset=binary"`` → ``"image/png"``; ``None`` → ``""``.
    """
    return (content_type or "").split(";", 1)[0].strip().lower()


def coerce_served_image_content_type(
    content_type: str | None, *, fallback: str,
) -> str:
    """Return ``content_type`` when it is an allowed image type, else
    ``fallback``.

    The non-raising sibling of ``ensure_allowed_upload_content_type``:
    used where a rejected type must degrade to an inert one
    (``application/octet-stream``) rather than fail the whole operation —
    a hostile ``text/html`` on a restored media original must never reach
    object storage as an executable same-origin document.
    """
    declared = normalise_content_type(content_type)
    if declared in ALLOWED_UPLOAD_IMAGE_CONTENT_TYPES:
        return declared
    return fallback
