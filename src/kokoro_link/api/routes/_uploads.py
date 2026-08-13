"""Shared upload guards for the routes that accept player-supplied images.

Two routes take multipart image uploads — ``POST /chat/uploads`` and
``POST /characters/draft`` — and both hand the bytes to Object Storage with the
client's declared ``Content-Type`` attached. That header is what the public
media route later echoes back **from the app's own origin**, so it is not a
cosmetic label: a file accepted as ``text/html`` becomes a same-origin HTML
document served under a URL the app itself minted, which is stored XSS with an
upload as the delivery mechanism.

A filename allow-list does not close this. The extension and the content type
are two independent fields of the same multipart part, both attacker-chosen and
never checked against each other — ``avatar.png`` declared as ``text/html``
passes any suffix test ever written.

Hence one allow-list, in one place, for every route that stores an upload.
"""

from __future__ import annotations

from fastapi import HTTPException, status

# Single source of truth for the served-image allow-list (shared with the
# ``.lumebackup`` restore media landing — see the module docstring there):
# duplicating the frozenset is how the two surfaces drift apart.
from kokoro_link.application.media_content_types import (
    ALLOWED_UPLOAD_IMAGE_CONTENT_TYPES,
    normalise_content_type,
)

__all__ = [
    "ALLOWED_UPLOAD_IMAGE_CONTENT_TYPES",
    "ensure_allowed_upload_content_type",
]


def ensure_allowed_upload_content_type(content_type: str | None) -> str:
    """Return ``content_type`` when it is allowed, else raise ``415``.

    A missing header is rejected rather than defaulted. Guessing a type for an
    upload that declined to declare one puts the app's name on a claim only the
    uploader was in a position to make.
    """
    declared = normalise_content_type(content_type)
    if declared not in ALLOWED_UPLOAD_IMAGE_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"unsupported file type: {declared or '(missing)'}",
        )
    return declared
