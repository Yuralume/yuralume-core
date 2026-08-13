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
#:
#: ``character-backups/`` (CB2) holds encrypted ``.lumebackup`` export
#: artifacts awaiting download. Ephemeral on purpose (plan §8: 產物走
#: ephemeral TTL，不常駐佔 hosted 儲存), and the variant decorator skipping
#: it is load-bearing twice over: the artifact is not an image, and WebP
#: renditions of an encrypted blob would be pure dead weight. The storage
#: service sweeps it on a longer, download-window TTL than the draft
#: staging prefix — see ``storage_service.app.EPHEMERAL_PREFIX_TTL_OVERRIDES``.
BACKUP_EXPORT_OBJECT_KEY_PREFIX = "character-backups/"
EPHEMERAL_OBJECT_KEY_PREFIXES: tuple[str, ...] = (
    "draft-uploads/",
    BACKUP_EXPORT_OBJECT_KEY_PREFIX,
)


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


class SupportsPrefixPurge(Protocol):
    """Optional capability: delete every object under a key prefix.

    Deliberately kept **out** of :class:`ObjectStoragePort`, for the same
    reason as :class:`SupportsObjectStream`: adapters written before this
    existed, test doubles, and any decorator that wraps a port without
    forwarding unknown attributes all remain valid ports. Callers detect
    this capability with ``getattr(port, "purge_prefix", None)`` +
    ``callable(...)`` before calling it — never with ``isinstance``.

    Why this needs its own primitive instead of enumerate-then-``delete``:
    content-addressed keys such as ``tts/{character_id}/{digest}.{ext}``
    (built once, in ``application/services/tts_service.py``) are never
    persisted to a DB row — the digest *is* the key, derived at read time
    from generation parameters, not looked up. There is therefore no set
    of keys any caller can enumerate to delete one at a time; the only way
    to reclaim them is to remove everything under the character-scoped
    prefix they all share.

    Fallback semantics for a caller that finds no such capability (CD2):
    skip the prefix cleanup and log it — never fail the surrounding
    operation. A character delete's DB-tracked object keys (avatar,
    gallery images, ...) are still removed individually through
    :meth:`ObjectStoragePort.delete` regardless; only the untracked,
    content-addressed remainder is left behind when this capability is
    absent, which is strictly the pre-CD1 behaviour, not a regression.

    Contrast with :meth:`ObjectStoragePort.delete` plus the variant
    decorator's per-key fan-out: deleting one key there means deriving
    each of its fixed variant suffixes and deleting those too, because a
    variant lives at ``{object_key}.{variant_name}.webp`` — a key the
    decorator must construct, not discover. A prefix purge needs no such
    derivation: that variant key already starts with ``object_key``, so
    it already starts with any prefix ``object_key`` starts with, and a
    purge removes it for free as part of the same subtree.
    """

    async def purge_prefix(self, *, prefix: str) -> int:
        """Delete every object whose key starts with ``prefix``.

        Returns the number of objects removed — one per distinct object
        *key* (a WebP variant such as ``{object_key}.{name}.webp`` is its
        own key under the same subtree, so it counts too), never inflated
        by adapter-internal bookkeeping: a backend that keeps a metadata
        sidecar per object (one JSON file alongside each blob) purges that
        sidecar as part of the same call but does not count it a second
        time. A ``prefix`` that matches nothing is not an error — it
        returns ``0``.

        Implementations validate ``prefix`` the same way
        :func:`~kokoro_link.infrastructure.storage.keys.validate_object_key`
        validates a full key, plus the additional shape constraints in
        :func:`~kokoro_link.infrastructure.storage.keys.validate_purge_prefix`
        (trailing ``/``, at least two segments): a bulk, irreversible
        delete must never be reachable with an empty or single-segment
        prefix, which would purge far more than one character's objects.
        """
