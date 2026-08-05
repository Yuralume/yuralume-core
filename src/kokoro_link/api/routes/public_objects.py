"""Public read proxy for object-storage media.

Serves ``GET|HEAD /v1/public/{object_key}`` for generated images, uploads,
TTS cache and feed media on the same app origin as the SPA.

Three concerns live here beyond the plain proxy:

* **Variant resolution** (`?v=`) — an opt-in, purely additive query that
  asks for a smaller derived rendition. Missing variants fall back to the
  original, which is what lets the delivery side ship before anything has
  been backfilled.
* **Conditional requests** — the route has always emitted an ``ETag`` but
  never compared ``If-None-Match``, so every "unchanged" revalidation paid
  for a full body.
* **Streaming** — a 2.5 MB PNG used to be pulled entirely into this
  process before the first byte reached the client.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response, StreamingResponse

from kokoro_link.api.dependencies import get_container
from kokoro_link.contracts.object_storage import (
    ObjectMetadata,
    ObjectNotFoundError,
    ObjectStorageError,
    ObjectStorageUnavailableError,
)
from kokoro_link.infrastructure.storage.image_variants import (
    VARIANT_SPECS,
    derive_variant_key,
)

_LOGGER = logging.getLogger(__name__)

router = APIRouter()

_CACHE_CONTROL = "public, max-age=31536000, immutable"

# The accepted ``?v=`` labels are exactly the tiers the encoder produces —
# derived from ``VARIANT_SPECS`` rather than restated, so a tier can never
# exist on the write side while being unreachable on the read side (or the
# reverse). Key derivation likewise goes through ``derive_variant_key``:
# write-side decorator, backfill script and this route must agree byte for
# byte, or a variant that was generated is never found again.
_VARIANT_NAMES = frozenset(spec.name for spec in VARIANT_SPECS)


@router.api_route("/v1/public/{object_key:path}", methods=["GET", "HEAD"])
async def public_object(
    object_key: str,
    request: Request,
    v: str | None = Query(
        default=None,
        description=(
            "Optional rendition: w320 | w768 | full. Unknown values and "
            "variants that do not exist yet fall back to the original."
        ),
    ),
    container=Depends(get_container),  # noqa: ANN001
) -> Response:
    object_storage = getattr(container, "object_storage", None)
    if object_storage is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Object storage is not configured",
        )

    with _translated_storage_errors():
        metadata = await _resolve_metadata(
            object_storage,
            object_key=object_key,
            variant=v,
        )
    if metadata is None:
        raise HTTPException(status_code=404, detail="Object not found")

    headers = {
        "Cache-Control": _CACHE_CONTROL,
        "X-Object-Key": metadata.object_key,
        "Content-Length": str(metadata.size_bytes),
    }
    etag = f'"{metadata.sha256}"' if metadata.sha256 else None
    if etag:
        headers["ETag"] = etag

    if etag and _if_none_match_matches(
        request.headers.get("if-none-match"),
        etag,
    ):
        return _not_modified(headers)

    if request.method == "HEAD":
        return Response(
            content=b"",
            media_type=metadata.content_type,
            headers=headers,
        )

    stream = _open_stream(object_storage, object_key=metadata.object_key)
    if stream is not None:
        return StreamingResponse(
            stream,
            media_type=metadata.content_type,
            headers=headers,
        )

    # Compatibility path for storage ports that predate the streaming
    # capability (and for test doubles that only implement the core port).
    with _translated_storage_errors():
        body = await object_storage.get_bytes(object_key=metadata.object_key)
    return Response(
        content=body,
        media_type=metadata.content_type,
        headers=headers,
    )


async def _resolve_metadata(
    object_storage,  # noqa: ANN001
    *,
    object_key: str,
    variant: str | None,
) -> ObjectMetadata | None:
    """Return the metadata of the object that should actually be served.

    A requested variant wins only when it exists; everything else — no
    ``?v=``, an unknown label, a variant that was never generated —
    resolves to the original.
    """
    name = (variant or "").strip()
    # An empty key is rejected downstream with the existing 400; asking the
    # deriver about it first would turn that into a 500.
    if object_key and name in _VARIANT_NAMES:
        variant_metadata = await _stat_variant(
            object_storage,
            variant_key=derive_variant_key(object_key, name),
        )
        if variant_metadata is not None:
            return variant_metadata
    return await object_storage.stat(object_key=object_key)


async def _stat_variant(
    object_storage,  # noqa: ANN001
    *,
    variant_key: str,
) -> ObjectMetadata | None:
    """Probe a derived variant. **A miss is a normal state, not a fault.**

    Every image stored before the variant pipeline existed will miss on
    every single request until it is backfilled — so this probe must never
    reach error level, and must never decide the request's outcome. Any
    storage-level failure here (backend down, malformed key) is swallowed
    and re-surfaces with its proper 503/400 semantics when the original is
    probed a moment later.
    """
    try:
        return await object_storage.stat(object_key=variant_key)
    except ObjectStorageError:
        _LOGGER.debug(
            "variant probe unavailable; serving original instead: %s",
            variant_key,
        )
        return None


def _open_stream(
    object_storage,  # noqa: ANN001
    *,
    object_key: str,
) -> AsyncIterator[bytes] | None:
    """Return a chunked reader when the adapter offers one.

    Capability detection rather than an interface requirement: a storage
    decorator that forwards only the core port keeps working, it just
    degrades to the buffered path.
    """
    iter_bytes = getattr(object_storage, "iter_bytes", None)
    if not callable(iter_bytes):
        return None
    return iter_bytes(object_key=object_key)


def _not_modified(headers: dict[str, str]) -> Response:
    """304 carrying the validators, and no body.

    ``Content-Length`` is deliberately dropped: RFC 7230 §3.3.2 permits it
    on a 304 only when it equals what a 200 would have sent, and a length
    header on a bodiless response buys nothing but a chance for an
    intermediary to disagree.
    """
    return Response(
        status_code=status.HTTP_304_NOT_MODIFIED,
        headers={
            key: value
            for key, value in headers.items()
            if key != "Content-Length"
        },
    )


def _if_none_match_matches(header_value: str | None, etag: str) -> bool:
    """Weak comparison of ``If-None-Match`` against our strong ``ETag``.

    RFC 7232 §3.2 mandates the weak comparison function for
    ``If-None-Match``, so a client echoing ``W/"abc"`` matches our
    ``"abc"``.
    """
    if not header_value:
        return False
    raw = header_value.strip()
    if raw == "*":
        return True
    for candidate in raw.split(","):
        tag = candidate.strip()
        if tag.startswith("W/"):
            tag = tag[2:].strip()
        if tag and tag == etag:
            return True
    return False


@contextmanager
def _translated_storage_errors() -> Iterator[None]:
    """Map storage-port failures onto this unauthenticated surface."""
    try:
        yield
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
