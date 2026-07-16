from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response

from kokoro_link.api.dependencies import get_container
from kokoro_link.contracts.object_storage import (
    ObjectNotFoundError,
    ObjectStorageError,
    ObjectStorageUnavailableError,
)

_LOGGER = logging.getLogger(__name__)

router = APIRouter()


@router.api_route("/v1/public/{object_key:path}", methods=["GET", "HEAD"])
async def public_object(
    object_key: str,
    request: Request,
    container=Depends(get_container),  # noqa: ANN001
) -> Response:
    object_storage = getattr(container, "object_storage", None)
    if object_storage is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Object storage is not configured",
        )
    try:
        metadata = await object_storage.stat(object_key=object_key)
        if metadata is None:
            raise HTTPException(status_code=404, detail="Object not found")
        body = b""
        if request.method != "HEAD":
            body = await object_storage.get_bytes(object_key=object_key)
    except HTTPException:
        raise
    except ObjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Object not found") from exc
    except ObjectStorageUnavailableError as exc:
        # Unauthenticated surface: log the specific reason server-side but
        # never echo it — the message names internal topology (STORAGE_URL).
        _LOGGER.exception("public object fetch failed: storage unreachable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Object storage is unavailable",
        ) from exc
    except ObjectStorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    headers = {
        "Cache-Control": "public, max-age=31536000, immutable",
        "X-Object-Key": metadata.object_key,
        "Content-Length": str(metadata.size_bytes),
    }
    if metadata.sha256:
        headers["ETag"] = f'"{metadata.sha256}"'
    return Response(
        content=body,
        media_type=metadata.content_type,
        headers=headers,
    )
