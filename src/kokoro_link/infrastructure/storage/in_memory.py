"""In-memory object storage adapter for unit tests."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from kokoro_link.contracts.object_storage import (
    ObjectMetadata,
    ObjectNotFoundError,
    StoredObject,
)
from kokoro_link.infrastructure.storage.keys import validate_object_key


class InMemoryObjectStorage:
    def __init__(self, *, public_base_url: str = "/uploads") -> None:
        self._public_base_url = public_base_url.rstrip("/")
        self._objects: dict[str, bytes] = {}
        self._metadata: dict[str, ObjectMetadata] = {}

    async def put_bytes(
        self,
        *,
        object_key: str,
        content: bytes,
        content_type: str,
        metadata: Mapping[str, str] | None = None,
    ) -> StoredObject:
        key = validate_object_key(object_key)
        data = bytes(content)
        sha = hashlib.sha256(data).hexdigest()
        url = await self.public_url(object_key=key)
        meta = ObjectMetadata(
            object_key=key,
            url=url,
            content_type=content_type,
            size_bytes=len(data),
            sha256=sha,
            metadata=dict(metadata or {}),
        )
        self._objects[key] = data
        self._metadata[key] = meta
        return StoredObject(
            object_key=meta.object_key,
            url=meta.url,
            content_type=meta.content_type,
            size_bytes=meta.size_bytes,
            sha256=meta.sha256,
            metadata=meta.metadata,
        )

    async def get_bytes(self, *, object_key: str) -> bytes:
        key = validate_object_key(object_key)
        try:
            return self._objects[key]
        except KeyError as exc:
            raise ObjectNotFoundError(key) from exc

    async def stat(self, *, object_key: str) -> ObjectMetadata | None:
        return self._metadata.get(validate_object_key(object_key))

    async def delete(self, *, object_key: str) -> None:
        key = validate_object_key(object_key)
        self._objects.pop(key, None)
        self._metadata.pop(key, None)

    async def copy(
        self,
        *,
        source_key: str,
        destination_key: str,
        metadata: Mapping[str, str] | None = None,
    ) -> StoredObject:
        source = validate_object_key(source_key)
        dest = validate_object_key(destination_key)
        data = await self.get_bytes(object_key=source)
        source_meta = self._metadata[source]
        return await self.put_bytes(
            object_key=dest,
            content=data,
            content_type=source_meta.content_type,
            metadata=metadata if metadata is not None else source_meta.metadata,
        )

    async def public_url(self, *, object_key: str) -> str:
        key = validate_object_key(object_key)
        return f"{self._public_base_url}/{key}"

    def object_key_from_url(self, url: str) -> str | None:
        prefix = f"{self._public_base_url}/"
        if url.startswith(prefix):
            try:
                return validate_object_key(url[len(prefix):])
            except Exception:
                return None
        if url.startswith("/uploads/"):
            try:
                return validate_object_key(url[len("/uploads/"):])
            except Exception:
                return None
        return None
