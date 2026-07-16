"""Download inbound Telegram photos into Object Storage.

Telegram's upload flow is two-step: ``getFile`` returns a ``file_path``
(valid for 1 hour), then we GET
``https://api.telegram.org/file/bot<token>/<file_path>`` to pull the
bytes. The first call is authed with the bot token in the URL; the
second call just needs that URL built correctly.

Objects end up under ``users/{user_id}/messaging-inbound/<uuid>.<ext>``
so the chat pipeline can reference them exactly like web-uploaded
attachments.
"""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import uuid4

import httpx

from kokoro_link.contracts.object_storage import ObjectStoragePort

_LOGGER = logging.getLogger(__name__)
_TELEGRAM_API_BASE = "https://api.telegram.org"
_DEFAULT_TIMEOUT_SECONDS = 20.0
_ALLOWED_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


async def download_telegram_photo(
    *,
    bot_token: str,
    file_id: str,
    uploads_dir: Path,
    url_prefix: str = "/uploads",
    subdir: str = "messaging-inbound",
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    object_storage: ObjectStoragePort | None = None,
    user_id: str = "default",
) -> str | None:
    """Fetch a single Telegram photo into Object Storage and return its URL.

    Returns ``None`` on any failure — a broken image must not abort
    the whole inbound flow. The caller logs / swallows.
    """
    _ = uploads_dir, url_prefix
    if not bot_token or not file_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            meta_resp = await client.get(
                f"{_TELEGRAM_API_BASE}/bot{bot_token}/getFile",
                params={"file_id": file_id},
            )
            meta_resp.raise_for_status()
            meta = meta_resp.json()
            if not meta.get("ok"):
                _LOGGER.warning(
                    "telegram getFile not ok: %s", meta.get("description"),
                )
                return None
            result = meta.get("result") or {}
            file_path = result.get("file_path")
            if not isinstance(file_path, str) or not file_path:
                return None
            bytes_resp = await client.get(
                f"{_TELEGRAM_API_BASE}/file/bot{bot_token}/{file_path}",
            )
            bytes_resp.raise_for_status()
            data = bytes_resp.content
    except Exception:
        _LOGGER.exception("telegram photo download failed (file_id=%s)", file_id)
        return None

    suffix = Path(file_path).suffix.lower() or ".jpg"
    if suffix not in _ALLOWED_SUFFIXES:
        suffix = ".jpg"
    filename = f"{uuid4().hex}{suffix}"
    if object_storage is None:
        _LOGGER.error("telegram photo dropped: object storage is not configured")
        return None
    content_type = (
        bytes_resp.headers.get("content-type") or "image/jpeg"
    ).split(";", 1)[0].strip().lower()
    if not content_type.startswith("image/"):
        content_type = "image/jpeg"
    try:
        stored = await object_storage.put_bytes(
            object_key=f"users/{user_id}/{subdir}/{filename}",
            content=data,
            content_type=content_type,
            metadata={
                "user_id": user_id,
                "platform": "telegram",
                "file_id": file_id,
            },
        )
        return stored.url
    except Exception:
        _LOGGER.exception(
            "telegram photo storage write failed (file_id=%s)", file_id,
        )
        return None
