"""HTTP object storage adapter."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping

import httpx

from kokoro_link.contracts.object_storage import (
    DEFAULT_STREAM_CHUNK_BYTES,
    ObjectMetadata,
    ObjectNotFoundError,
    ObjectStorageError,
    ObjectStorageUnavailableError,
    StoredObject,
)
from kokoro_link.infrastructure.storage.keys import (
    validate_object_key,
    validate_purge_prefix,
)


class HttpObjectStorage:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        public_base_url: str = "",
        timeout_seconds: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._public_base_url = public_base_url.rstrip("/")
        self._timeout = timeout_seconds

    async def put_bytes(
        self,
        *,
        object_key: str,
        content: bytes,
        content_type: str,
        metadata: Mapping[str, str] | None = None,
    ) -> StoredObject:
        key = validate_object_key(object_key)
        response = await self._send(
            "POST",
            f"{self._base_url}/v1/objects",
            data={
                "object_key": key,
                "content_type": content_type,
                "metadata": json.dumps(dict(metadata or {}), ensure_ascii=False),
            },
            files={"file": (key.rsplit("/", 1)[-1], content, content_type)},
        )
        data = self._parse_response(response)
        return _stored_from_json(data, public_base_url=self._public_base_url)

    async def get_bytes(self, *, object_key: str) -> bytes:
        key = validate_object_key(object_key)
        response = await self._send(
            "GET",
            f"{self._base_url}/v1/objects/content/{key}",
        )
        if response.status_code == 404:
            raise ObjectNotFoundError(key)
        if response.status_code >= 400:
            raise ObjectStorageError(_error_text(response))
        return response.content

    async def iter_bytes(
        self,
        *,
        object_key: str,
        chunk_size: int = DEFAULT_STREAM_CHUNK_BYTES,
    ) -> AsyncIterator[bytes]:
        """Stream object bytes straight through, never buffering the whole file.

        The public media proxy fronts multi-MB images; ``get_bytes`` would
        pull each one fully into this process before a single byte reaches
        the client. Here the upstream response body is relayed chunk by
        chunk, so peak memory is one chunk regardless of object size.
        """
        key = validate_object_key(object_key)
        url = f"{self._base_url}/v1/objects/content/{key}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream(
                    "GET", url, headers=self._headers(),
                ) as response:
                    if response.status_code == 404:
                        raise ObjectNotFoundError(key)
                    if response.status_code >= 400:
                        # Error bodies are small and must be read before
                        # they can be described.
                        await response.aread()
                        raise ObjectStorageError(_error_text(response))
                    async for chunk in response.aiter_bytes(chunk_size):
                        yield chunk
        except httpx.HTTPError as exc:
            raise ObjectStorageUnavailableError(
                f"object storage unreachable at {self._base_url}: {exc}",
            ) from exc

    async def stat(self, *, object_key: str) -> ObjectMetadata | None:
        key = validate_object_key(object_key)
        response = await self._send(
            "GET",
            f"{self._base_url}/v1/objects/metadata/{key}",
        )
        if response.status_code == 404:
            return None
        data = self._parse_response(response)
        return _metadata_from_json(data, public_base_url=self._public_base_url)

    async def delete(self, *, object_key: str) -> None:
        key = validate_object_key(object_key)
        response = await self._send(
            "DELETE",
            f"{self._base_url}/v1/objects/{key}",
        )
        if response.status_code >= 400:
            raise ObjectStorageError(_error_text(response))

    async def copy(
        self,
        *,
        source_key: str,
        destination_key: str,
        metadata: Mapping[str, str] | None = None,
    ) -> StoredObject:
        source = validate_object_key(source_key)
        dest = validate_object_key(destination_key)
        response = await self._send(
            "POST",
            f"{self._base_url}/v1/objects/copy",
            json={
                "source_key": source,
                "destination_key": dest,
                "metadata": dict(metadata or {}),
            },
        )
        if response.status_code == 404:
            raise ObjectNotFoundError(source)
        data = self._parse_response(response)
        return _stored_from_json(data, public_base_url=self._public_base_url)

    async def purge_prefix(self, *, prefix: str) -> int:
        """CD1 ``SupportsPrefixPurge``: delete every object under ``prefix``.

        Validated client-side with :func:`validate_purge_prefix` before it
        ever leaves this process — the server re-validates independently,
        but a caller building a bad prefix deserves a local error, not a
        round trip that comes back 400.
        """
        safe_prefix = validate_purge_prefix(prefix)
        response = await self._send(
            "POST",
            f"{self._base_url}/v1/objects/purge-prefix",
            json={"prefix": safe_prefix},
        )
        data = self._parse_response(response)
        return int(data.get("deleted") or 0)

    async def _send(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Issue one storage request; translate transport failures.

        A dead storage host (docker service down, DNS miss, timeout)
        surfaces from httpx as a raw OS-level message ("[Errno -2] Name
        or service not known") that says nothing about *which* service
        failed. Wrap it in :class:`ObjectStorageUnavailableError` naming
        the storage base URL so callers/logs can point at the right box.
        """
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                return await client.request(
                    method, url, headers=self._headers(), **kwargs,
                )
        except httpx.HTTPError as exc:
            raise ObjectStorageUnavailableError(
                f"object storage unreachable at {self._base_url}: {exc}",
            ) from exc

    async def public_url(self, *, object_key: str) -> str:
        key = validate_object_key(object_key)
        return f"/v1/public/{key}"

    def object_key_from_url(self, url: str) -> str | None:
        prefixes = ["/v1/public/"]
        if self._public_base_url:
            prefixes.append(f"{self._public_base_url}/v1/public/")
            prefixes.append(f"{self._public_base_url}/uploads/")
        prefixes.append(f"{self._base_url}/v1/public/")
        prefixes.append("/uploads/")
        for prefix in prefixes:
            if url.startswith(prefix):
                try:
                    return validate_object_key(url[len(prefix):])
                except ObjectStorageError:
                    return None
        return None

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    @staticmethod
    def _parse_response(response: httpx.Response) -> dict:
        if response.status_code >= 400:
            raise ObjectStorageError(_error_text(response))
        return response.json()


def _stored_from_json(data: Mapping, *, public_base_url: str) -> StoredObject:
    _ = public_base_url
    object_key = validate_object_key(str(data["object_key"]))
    url = f"/v1/public/{object_key}"
    return StoredObject(
        object_key=object_key,
        url=url,
        content_type=str(data.get("content_type") or "application/octet-stream"),
        size_bytes=int(data.get("size_bytes") or 0),
        sha256=data.get("sha256"),
        metadata=dict(data.get("metadata") or {}),
    )


def _metadata_from_json(data: Mapping, *, public_base_url: str) -> ObjectMetadata:
    stored = _stored_from_json(data, public_base_url=public_base_url)
    return ObjectMetadata(
        object_key=stored.object_key,
        url=stored.url,
        content_type=stored.content_type,
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
        metadata=stored.metadata,
    )


def _error_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"storage HTTP {response.status_code}: {response.text[:200]}"
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or payload)
    return f"storage HTTP {response.status_code}: {payload}"
