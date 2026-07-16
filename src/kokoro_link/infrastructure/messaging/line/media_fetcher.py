"""Download inbound LINE image content into Object Storage.

LINE exposes image bytes via ``GET api-data.line.me/v2/bot/message/{id}/content``
authenticated by the channel access token. The response is the raw
binary; ``Content-Type`` carries the format (LINE strips it down to
JPEG in practice, but we honour whatever header comes back).

Objects go under ``users/{user_id}/messaging-inbound/<uuid>.<ext>``
so the rest of the pipeline can treat them like web-side uploads.
"""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import uuid4

import httpx

from kokoro_link.contracts.object_storage import ObjectStoragePort

_LOGGER = logging.getLogger(__name__)
_LINE_CONTENT_BASE = "https://api-data.line.me/v2/bot/message"
_DEFAULT_TIMEOUT_SECONDS = 20.0
_MIME_TO_SUFFIX = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


async def download_line_image(
    *,
    channel_access_token: str,
    message_id: str,
    uploads_dir: Path,
    url_prefix: str = "/uploads",
    subdir: str = "messaging-inbound",
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    object_storage: ObjectStoragePort | None = None,
    user_id: str = "default",
) -> str | None:
    """Fetch a single LINE image message into Object Storage and return its URL.

    Returns ``None`` on any failure — don't break the inbound flow
    over one bad image.
    """
    _ = uploads_dir, url_prefix
    if not channel_access_token or not message_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"{_LINE_CONTENT_BASE}/{message_id}/content",
                headers={"Authorization": f"Bearer {channel_access_token}"},
            )
            resp.raise_for_status()
            data = resp.content
            content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    except Exception:
        _LOGGER.exception(
            "line image download failed (message_id=%s)", message_id,
        )
        return None

    suffix = _MIME_TO_SUFFIX.get(content_type, ".jpg")
    filename = f"{uuid4().hex}{suffix}"
    if object_storage is None:
        _LOGGER.error("line image dropped: object storage is not configured")
        return None
    stored_content_type = (
        content_type if content_type.startswith("image/")
        else "image/jpeg"
    )
    try:
        stored = await object_storage.put_bytes(
            object_key=f"users/{user_id}/{subdir}/{filename}",
            content=data,
            content_type=stored_content_type,
            metadata={
                "user_id": user_id,
                "platform": "line",
                "message_id": message_id,
            },
        )
        return stored.url
    except Exception:
        _LOGGER.exception(
            "line image storage write failed (message_id=%s)", message_id,
        )
        return None
