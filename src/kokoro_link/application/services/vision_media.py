"""Storage 圖片 → 模型可食用 URL 的共用轉換路徑。

這幾個 helper 原本是 ``chat_service`` 的私有函式（``_to_vision_url``
等），只有聊天回合會用到。VP4 之後 character draft 成為第二個
caller，兩邊都需要把「存在 Object Storage / 本機 uploads 的圖片」
轉成模型可以吃的形式（data URL 或 provider 抓得到的公開 URL），
於是抽成本模組，讓 chat 與 draft 共用同一條轉換路徑，而不是各自
複製一份會逐漸分歧的邏輯。
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path

from kokoro_link.contracts.object_storage import ObjectStoragePort

_LOGGER = logging.getLogger(__name__)


MAX_INLINE_IMAGE_BYTES = 10 * 1024 * 1024


def to_vision_url(
    url: str, *, uploads_dir: Path | None, public_base_url: str,
) -> str | None:
    """Convert a chat attachment URL into something the LLM can ingest.

    Priority order:

    1. ``data:image/...;base64,...`` already inlined → pass through.
    2. URL points at our own ``/uploads/`` mount (relative OR absolute
       under ``public_base_url``) → return the absolute public URL.
       Container deployments inline those through Object Storage in
       ``to_vision_url_with_storage`` before this helper is reached.
    3. External ``http(s)://`` URL we don't own (e.g. Telegram CDN) →
       pass through. Models that accept HTTP (Anthropic, OpenAI cloud)
       handle it; models that don't (LM Studio) will error — there's
       no way around that without downloading first.
    4. Otherwise → ``None`` so the caller downgrades to the text
       placeholder.

    The Object Storage data-URL path is ``MAX_INLINE_IMAGE_BYTES``-
    capped (default 10 MB) before this helper is reached.
    """
    _ = uploads_dir
    if not url:
        return None
    # Already a data: URL (caller pre-encoded). Keep as-is.
    if url.startswith("data:"):
        return url
    relative_url = url
    if (
        public_base_url
        and url.startswith(public_base_url)
        and url[len(public_base_url):].startswith("/uploads/")
    ):
        relative_url = url[len(public_base_url):]
    if relative_url.startswith("/uploads/"):
        if public_base_url:
            return f"{public_base_url}{relative_url}"
        return None
    # External URL (CDN, third party). Nothing we can do but
    # pass-through; the model adapter has to deal with it.
    if url.startswith(("http://", "https://")):
        return url
    return None


def absolute_public_vision_url(
    url: str,
    *,
    public_base_url: str,
) -> str | None:
    """Promote a trusted storage media ref to a provider-fetchable URL."""
    if url.startswith(("http://", "https://")):
        return url
    if (
        public_base_url
        and url.startswith(("/v1/public/", "/uploads/"))
    ):
        return f"{public_base_url.rstrip('/')}{url}"
    return None


async def to_vision_url_with_storage(
    url: str,
    *,
    uploads_dir: Path | None,
    public_base_url: str,
    object_storage: ObjectStoragePort | None,
    prefer_public_image_urls: bool = False,
) -> str | None:
    if object_storage is not None and url and not url.startswith("data:"):
        object_key = object_storage.object_key_from_url(url)
        if object_key is not None:
            try:
                metadata = await object_storage.stat(object_key=object_key)
                if metadata is not None and metadata.size_bytes > MAX_INLINE_IMAGE_BYTES:
                    _LOGGER.warning(
                        "skipping inline image object %s (%d bytes > cap)",
                        object_key, metadata.size_bytes,
                    )
                    return None
                if prefer_public_image_urls and metadata is not None:
                    public_url = absolute_public_vision_url(
                        metadata.url,
                        public_base_url=public_base_url,
                    )
                    if public_url is not None:
                        return public_url
                    _LOGGER.warning(
                        "storage image object %s has no provider-fetchable "
                        "public URL; falling back to bounded inline handling",
                        object_key,
                    )
                data = await object_storage.get_bytes(object_key=object_key)
                if len(data) > MAX_INLINE_IMAGE_BYTES:
                    _LOGGER.warning(
                        "skipping inline image object %s (%d bytes > cap)",
                        object_key, len(data),
                    )
                    return None
                mime = (
                    metadata.content_type if metadata is not None
                    else mimetypes.guess_type(object_key)[0]
                ) or "image/png"
                b64 = base64.b64encode(data).decode("ascii")
                return f"data:{mime};base64,{b64}"
            except Exception:
                _LOGGER.exception(
                    "failed to read image object for inline encode key=%s",
                    object_key,
                )
                return None
    return to_vision_url(
        url, uploads_dir=uploads_dir, public_base_url=public_base_url,
    )
