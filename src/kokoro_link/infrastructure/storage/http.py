"""HTTP object storage adapter."""

from __future__ import annotations

import json
from collections.abc import Mapping

import httpx

from kokoro_link.contracts.object_storage import (
    ObjectMetadata,
    ObjectNotFoundError,
    ObjectStorageError,
    StoredObject,
)
from kokoro_link.infrastructure.storage.keys import validate_object_key


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
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/v1/objects",
                headers=self._headers(),
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
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                f"{self._base_url}/v1/objects/content/{key}",
                headers=self._headers(),
            )
        if response.status_code == 404:
            raise ObjectNotFoundError(key)
        if response.status_code >= 400:
            raise ObjectStorageError(_error_text(response))
        return response.content

    async def stat(self, *, object_key: str) -> ObjectMetadata | None:
        key = validate_object_key(object_key)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                f"{self._base_url}/v1/objects/metadata/{key}",
                headers=self._headers(),
            )
        if response.status_code == 404:
            return None
        data = self._parse_response(response)
        return _metadata_from_json(data, public_base_url=self._public_base_url)

    async def delete(self, *, object_key: str) -> None:
        key = validate_object_key(object_key)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.delete(
                f"{self._base_url}/v1/objects/{key}",
                headers=self._headers(),
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
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/v1/objects/copy",
                headers=self._headers(),
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
