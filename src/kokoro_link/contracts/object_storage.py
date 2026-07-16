"""Object storage boundary for user and generated media.

Application services use this port instead of writing directly to the
repository-local ``uploads/`` tree. Container deployments use the HTTP
adapter against the storage-local service or a compatible object store.
New DB rows should persist the returned app-relative media URL
(``/v1/public/{object_key}``) instead of an origin-bound absolute URL.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class StoredObject:
    object_key: str
    url: str
    content_type: str
    size_bytes: int
    sha256: str | None = None
    metadata: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class ObjectMetadata:
    object_key: str
    url: str
    content_type: str
    size_bytes: int
    sha256: str | None = None
    metadata: Mapping[str, str] | None = None


class ObjectStorageError(Exception):
    """Base error for storage adapter failures."""


class ObjectNotFoundError(ObjectStorageError):
    """Raised when an object key does not exist."""


class ObjectStoragePort(Protocol):
    async def put_bytes(
        self,
        *,
        object_key: str,
        content: bytes,
        content_type: str,
        metadata: Mapping[str, str] | None = None,
    ) -> StoredObject:
        """Persist bytes at ``object_key`` and return its portable media ref."""

    async def get_bytes(self, *, object_key: str) -> bytes:
        """Return object bytes or raise :class:`ObjectNotFoundError`."""

    async def stat(self, *, object_key: str) -> ObjectMetadata | None:
        """Return metadata, or ``None`` when the object does not exist."""

    async def delete(self, *, object_key: str) -> None:
        """Delete object if present. Missing objects are a no-op."""

    async def copy(
        self,
        *,
        source_key: str,
        destination_key: str,
        metadata: Mapping[str, str] | None = None,
    ) -> StoredObject:
        """Copy one object key to another and return destination metadata."""

    async def public_url(self, *, object_key: str) -> str:
        """Return the stable browser-readable URL/ref for ``object_key``.

        HTTP storage adapters should prefer an app-relative URL so DB
        values survive app-domain, storage-backend, or CDN changes.
        """

    def object_key_from_url(self, url: str) -> str | None:
        """Best-effort reverse lookup for URLs produced by this adapter."""
