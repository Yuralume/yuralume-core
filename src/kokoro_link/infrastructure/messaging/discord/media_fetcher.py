"""Download inbound Discord attachment URLs into Object Storage."""

from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from kokoro_link.contracts.object_storage import ObjectStoragePort

_LOGGER = logging.getLogger(__name__)
_DEFAULT_TIMEOUT_SECONDS = 20.0
_MIME_TO_SUFFIX = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
_SUFFIX_TO_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


async def download_discord_attachment(
    *,
    attachment_url: str,
    uploads_dir: Path,
    url_prefix: str = "/uploads",
    subdir: str = "messaging-inbound",
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    object_storage: ObjectStoragePort | None = None,
    user_id: str = "default",
) -> str | None:
    """Fetch one Discord image attachment into Object Storage.

    ``uploads_dir`` and ``url_prefix`` remain in the signature for parity
    with Telegram/LINE downloaders; new writes require Object Storage.
    """
    _ = uploads_dir, url_prefix
    if not attachment_url:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(attachment_url)
            resp.raise_for_status()
            data = resp.content
            content_type = (
                resp.headers.get("content-type") or ""
            ).split(";")[0].strip().lower()
    except Exception:
        _LOGGER.exception(
            "discord attachment download failed url=%s", attachment_url,
        )
        return None

    suffix = _resolve_suffix(attachment_url, content_type)
    if object_storage is None:
        _LOGGER.error("discord attachment dropped: object storage is not configured")
        return None
    stored_content_type = (
        content_type if content_type.startswith("image/")
        else _SUFFIX_TO_MIME.get(suffix, "image/png")
    )
    filename = f"{uuid4().hex}{suffix}"
    try:
        stored = await object_storage.put_bytes(
            object_key=f"users/{user_id}/{subdir}/{filename}",
            content=data,
            content_type=stored_content_type,
            metadata={
                "user_id": user_id,
                "platform": "discord",
                "source_url": attachment_url,
            },
        )
        return stored.url
    except Exception:
        _LOGGER.exception(
            "discord attachment storage write failed url=%s", attachment_url,
        )
        return None


def _resolve_suffix(url: str, content_type: str) -> str:
    if content_type in _MIME_TO_SUFFIX:
        return _MIME_TO_SUFFIX[content_type]
    suffix = PurePosixPath(urlparse(url).path).suffix.lower()
    return suffix if suffix in _SUFFIX_TO_MIME else ".png"
