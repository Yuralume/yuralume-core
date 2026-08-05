"""Object storage boundary for user and generated media.

Application services use this port instead of writing directly to the
repository-local ``uploads/`` tree. Container deployments use the HTTP
adapter against the storage-local service or a compatible object store.
New DB rows should persist the returned app-relative media URL
(``/v1/public/{object_key}``) instead of an origin-bound absolute URL.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Protocol

DEFAULT_STREAM_CHUNK_BYTES = 64 * 1024

#: Key prefixes whose objects are short-lived staging material, not durable
#: media. Three independent components have to agree on this set, which is why
#: it lives on the port rather than in any one of them:
#:
#: * the **writer** (``CharacterDraftService``) builds keys under it and deletes
#:   them as soon as the call that needed them is over;
#: * the **storage service** TTL-sweeps it as the backstop for whatever a crash
#:   or a failed delete left behind;
#: * the **variant decorator** skips its WebP fan-out for it — nothing under
#:   these prefixes is ever rendered by a UI, so the renditions would be dead
#:   weight paid for in the foreground latency of a charged action.
#:
#: A prefix restated in any of those three places instead of imported here is a
#: second definition that can drift: a writer whose keys nobody sweeps, or a
#: sweeper deleting a prefix nothing writes.
EPHEMERAL_OBJECT_KEY_PREFIXES: tuple[str, ...] = ("draft-uploads/",)


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


class ObjectStorageUnavailableError(ObjectStorageError):
    """Raised when the storage backend itself cannot be reached.

    Distinct from :class:`ObjectStorageError` (which also covers
    validation and backend HTTP-level failures) so callers can map
    "the storage service is down / DNS does not resolve" to a 503-style
    response instead of blaming the request.
    """


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


class SupportsObjectStream(Protocol):
    """Optional capability: read an object without materialising it.

    Deliberately kept **out** of :class:`ObjectStoragePort` so it stays an
    additive capability: adapters written before this existed, test
    doubles, and any decorator that wraps a port without forwarding
    unknown attributes all remain valid ports. Callers detect it with
    ``getattr`` and fall back to :meth:`ObjectStoragePort.get_bytes`.
    """

    def iter_bytes(
        self,
        *,
        object_key: str,
        chunk_size: int = DEFAULT_STREAM_CHUNK_BYTES,
    ) -> AsyncIterator[bytes]:
        """Yield the object's bytes in chunks.

        Raises :class:`ObjectNotFoundError` when the key is absent — but
        *lazily*, on first iteration. Callers that owe the client a real
        404 must ``stat()`` first: once a streaming response has begun,
        its headers are already on the wire.
        """
