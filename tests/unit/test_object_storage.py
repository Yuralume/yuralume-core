from __future__ import annotations

import json

import httpx
import pytest

from kokoro_link.contracts.object_storage import (
    ObjectNotFoundError,
    ObjectStorageError,
    ObjectStorageUnavailableError,
)
from kokoro_link.infrastructure.storage.http import HttpObjectStorage, _stored_from_json
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage
from kokoro_link.infrastructure.storage.keys import (
    safe_key_segment,
    validate_object_key,
    validate_purge_prefix,
)


@pytest.mark.parametrize(
    "key",
    [
        "../x.png",
        "/abs.png",
        "a\\b.png",
        "safe/../x.png",
        "space bad/x.png",
        "%2e%2e/x.png",
    ],
)
def test_validate_object_key_rejects_unsafe_paths(key: str) -> None:
    with pytest.raises(ObjectStorageError):
        validate_object_key(key)


def test_validate_object_key_accepts_nested_safe_path() -> None:
    assert (
        validate_object_key("users/default/chat-uploads/abc-123.png")
        == "users/default/chat-uploads/abc-123.png"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("abc-123.png", "abc-123.png"),
        ("../evil", "..evil"),
        ("a/b/c", "abc"),
        ("op 1;rm -rf /", "op1rm-rf"),
    ],
)
def test_safe_key_segment_collapses_input_to_one_segment(
    raw: str, expected: str,
) -> None:
    """Separators are dropped, not escaped, so nothing can grow a second
    segment — a traversal payload collapses into one harmless name."""
    assert safe_key_segment(raw, fallback="anonymous") == expected


@pytest.mark.parametrize("raw", [None, "", "   ", ".", "..", "....", "使用者"])
def test_safe_key_segment_falls_back_on_the_two_traps(raw: str | None) -> None:
    """The two cases a naive ``str.isalnum`` sanitiser gets wrong.

    ``"使用者".isalnum()`` is ``True`` yet ``validate_object_key`` refuses the
    key, so an ASCII-blind filter yields a "clean" segment whose only effect is
    a failed write; and an id that sanitises down to nothing but dots produces
    exactly the ``.`` / ``..`` segments the validator also refuses.
    """
    assert safe_key_segment(raw, fallback="anonymous") == "anonymous"


def test_safe_key_segment_output_always_survives_the_key_validator() -> None:
    """The two functions share one allow-list; this is what pins them
    together. A segment this helper emits that the validator then rejects
    turns a routine id into a silent write failure."""
    for raw in ("../evil", "使用者x", "a/b/c", "op 1;rm -rf /", "..", None):
        segment = safe_key_segment(raw, fallback="unknown")
        key = f"draft-uploads/{segment}/x.png"
        assert validate_object_key(key) == key


@pytest.mark.asyncio
async def test_in_memory_object_storage_round_trip() -> None:
    storage = InMemoryObjectStorage()
    stored = await storage.put_bytes(
        object_key="feed/char-1/a.png",
        content=b"PNG",
        content_type="image/png",
        metadata={"character_id": "char-1"},
    )

    assert stored.url == "/uploads/feed/char-1/a.png"
    assert await storage.get_bytes(object_key=stored.object_key) == b"PNG"
    meta = await storage.stat(object_key=stored.object_key)
    assert meta is not None
    assert meta.content_type == "image/png"
    assert meta.size_bytes == 3
    assert meta.metadata == {"character_id": "char-1"}
    assert storage.object_key_from_url(stored.url) == stored.object_key


@pytest.mark.asyncio
async def test_in_memory_object_storage_copy_and_delete() -> None:
    storage = InMemoryObjectStorage()
    await storage.put_bytes(
        object_key="characters/char-1/candidates/a.png",
        content=b"PNG",
        content_type="image/png",
    )
    copied = await storage.copy(
        source_key="characters/char-1/candidates/a.png",
        destination_key="characters/char-1/stage/a.png",
    )

    assert copied.object_key == "characters/char-1/stage/a.png"
    assert await storage.get_bytes(object_key=copied.object_key) == b"PNG"
    await storage.delete(object_key=copied.object_key)
    assert await storage.stat(object_key=copied.object_key) is None
    with pytest.raises(ObjectNotFoundError):
        await storage.get_bytes(object_key=copied.object_key)


@pytest.mark.asyncio
async def test_in_memory_object_storage_url_reverse_lookup() -> None:
    storage = InMemoryObjectStorage(public_base_url="http://storage.test/v1/public")
    stored = await storage.put_bytes(
        object_key="tts/char-1/hash.wav",
        content=b"WAV",
        content_type="audio/wav",
    )

    assert stored.url == "http://storage.test/v1/public/tts/char-1/hash.wav"
    assert storage.object_key_from_url(stored.url) == "tts/char-1/hash.wav"


@pytest.mark.asyncio
async def test_http_object_storage_public_url_uses_canonical_public_base() -> None:
    storage = HttpObjectStorage(
        base_url="http://storage-local:9000",
        api_key="secret",
        public_base_url="https://kokoro.example.com",
    )

    url = await storage.public_url(object_key="characters/char-1/tools/a.png")

    assert url == "/v1/public/characters/char-1/tools/a.png"
    assert (
        storage.object_key_from_url(url)
        == "characters/char-1/tools/a.png"
    )
    assert (
        storage.object_key_from_url(
            "https://kokoro.example.com/v1/public/characters/char-1/tools/a.png",
        )
        == "characters/char-1/tools/a.png"
    )
    assert (
        storage.object_key_from_url(
            "http://storage-local:9000/v1/public/characters/char-1/tools/a.png",
        )
        == "characters/char-1/tools/a.png"
    )


def _patch_httpx_connect_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route every HTTP request to a transport whose connect always fails.

    Simulates the storage host being down / unresolvable (e.g. the
    docker ``storage-local`` service not running).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno -2] Name or service not known")

    transport = httpx.MockTransport(handler)
    original_init = httpx.AsyncClient.__init__

    def patched(self: httpx.AsyncClient, **kwargs) -> None:
        kwargs["transport"] = transport
        original_init(self, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    ["put_bytes", "get_bytes", "stat", "delete", "copy", "purge_prefix"],
)
async def test_http_object_storage_network_failure_names_storage_host(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """A dead storage host must not leak a bare OS error string.

    Every network method wraps httpx transport failures in
    ``ObjectStorageUnavailableError`` naming the storage base URL so
    upstream layers can classify (503, not 400) and operators can see
    *which* service is down.
    """
    _patch_httpx_connect_error(monkeypatch)
    storage = HttpObjectStorage(
        base_url="http://storage-local:9000",
        api_key="secret",
    )

    calls = {
        "put_bytes": lambda: storage.put_bytes(
            object_key="characters/char-1/a.png",
            content=b"PNG",
            content_type="image/png",
        ),
        "get_bytes": lambda: storage.get_bytes(
            object_key="characters/char-1/a.png",
        ),
        "stat": lambda: storage.stat(object_key="characters/char-1/a.png"),
        "delete": lambda: storage.delete(object_key="characters/char-1/a.png"),
        "copy": lambda: storage.copy(
            source_key="characters/char-1/a.png",
            destination_key="characters/char-1/b.png",
        ),
        "purge_prefix": lambda: storage.purge_prefix(
            prefix="characters/char-1/",
        ),
    }

    with pytest.raises(ObjectStorageUnavailableError) as exc_info:
        await calls[operation]()

    message = str(exc_info.value)
    assert "object storage unreachable at http://storage-local:9000" in message
    assert "Name or service not known" in message


def _patch_httpx_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler,  # noqa: ANN001
) -> None:
    transport = httpx.MockTransport(handler)
    original_init = httpx.AsyncClient.__init__

    def patched(self: httpx.AsyncClient, **kwargs) -> None:
        kwargs["transport"] = transport
        original_init(self, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)


@pytest.mark.asyncio
async def test_http_object_storage_iter_bytes_streams_in_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public media proxy relays multi-MB images.

    ``iter_bytes`` must hand back the upstream body chunk by chunk so peak
    memory is one chunk, not the whole object.
    """
    payload = bytes(range(256)) * 8
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=payload)

    _patch_httpx_transport(monkeypatch, handler)
    storage = HttpObjectStorage(
        base_url="http://storage-local:9000",
        api_key="secret",
    )

    chunks = [
        chunk
        async for chunk in storage.iter_bytes(
            object_key="characters/char-1/a.png",
            chunk_size=64,
        )
    ]

    assert b"".join(chunks) == payload
    assert len(chunks) > 1
    assert seen[0].url.path == "/v1/objects/content/characters/char-1/a.png"
    assert seen[0].headers["authorization"] == "Bearer secret"


@pytest.mark.asyncio
async def test_http_object_storage_iter_bytes_maps_404_to_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "not found"}})

    _patch_httpx_transport(monkeypatch, handler)
    storage = HttpObjectStorage(
        base_url="http://storage-local:9000",
        api_key="secret",
    )

    with pytest.raises(ObjectNotFoundError):
        async for _ in storage.iter_bytes(object_key="characters/char-1/a.png"):
            pass


@pytest.mark.asyncio
async def test_http_object_storage_iter_bytes_describes_backend_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "boom"}})

    _patch_httpx_transport(monkeypatch, handler)
    storage = HttpObjectStorage(
        base_url="http://storage-local:9000",
        api_key="secret",
    )

    with pytest.raises(ObjectStorageError) as exc_info:
        async for _ in storage.iter_bytes(object_key="characters/char-1/a.png"):
            pass

    assert not isinstance(exc_info.value, ObjectNotFoundError)
    assert "boom" in str(exc_info.value)


@pytest.mark.asyncio
async def test_http_object_storage_iter_bytes_names_storage_host_when_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_httpx_connect_error(monkeypatch)
    storage = HttpObjectStorage(
        base_url="http://storage-local:9000",
        api_key="secret",
    )

    with pytest.raises(ObjectStorageUnavailableError) as exc_info:
        async for _ in storage.iter_bytes(object_key="characters/char-1/a.png"):
            pass

    assert (
        "object storage unreachable at http://storage-local:9000"
        in str(exc_info.value)
    )


@pytest.mark.asyncio
async def test_in_memory_object_storage_iter_bytes_round_trip() -> None:
    storage = InMemoryObjectStorage()
    payload = bytes(range(256))
    await storage.put_bytes(
        object_key="feed/char-1/a.png",
        content=payload,
        content_type="image/png",
    )

    chunks = [
        chunk
        async for chunk in storage.iter_bytes(
            object_key="feed/char-1/a.png",
            chunk_size=100,
        )
    ]

    assert b"".join(chunks) == payload
    assert [len(chunk) for chunk in chunks] == [100, 100, 56]

    with pytest.raises(ObjectNotFoundError):
        async for _ in storage.iter_bytes(object_key="feed/char-1/missing.png"):
            pass


@pytest.mark.parametrize(
    "prefix",
    [
        "characters/char-1",  # no trailing slash
        "characters/",  # single segment
        "/",  # empty after stripping the slash
        "/characters/char-1/",  # leading slash
        "characters//",  # empty segment
        "characters/../char-1/",  # traversal segment
        "characters/./char-1/",  # "." segment
        "characters/.../",  # F5: all-dots segment beyond ".."
        "characters/..../",  # F5: all-dots segment, longer run
        "a\\b/",
        "characters/space bad/",  # unsafe char
    ],
)
def test_validate_purge_prefix_rejects_unsafe_or_shallow_prefixes(
    prefix: str,
) -> None:
    with pytest.raises(ObjectStorageError):
        validate_purge_prefix(prefix)


def test_validate_purge_prefix_accepts_a_two_segment_prefix() -> None:
    assert (
        validate_purge_prefix("characters/char-1/")
        == "characters/char-1/"
    )


def test_validate_purge_prefix_accepts_deeper_prefixes() -> None:
    assert (
        validate_purge_prefix("tts/char-1/")
        == "tts/char-1/"
    )


@pytest.mark.asyncio
async def test_in_memory_purge_prefix_deletes_only_matching_keys() -> None:
    storage = InMemoryObjectStorage()
    await storage.put_bytes(
        object_key="tts/char-1/aaa.wav",
        content=b"WAV1",
        content_type="audio/wav",
    )
    await storage.put_bytes(
        object_key="tts/char-1/bbb.wav",
        content=b"WAV2",
        content_type="audio/wav",
    )
    await storage.put_bytes(
        object_key="tts/char-2/ccc.wav",
        content=b"WAV3",
        content_type="audio/wav",
    )

    deleted = await storage.purge_prefix(prefix="tts/char-1/")

    assert deleted == 2
    assert await storage.stat(object_key="tts/char-1/aaa.wav") is None
    assert await storage.stat(object_key="tts/char-1/bbb.wav") is None
    with pytest.raises(ObjectNotFoundError):
        await storage.get_bytes(object_key="tts/char-1/aaa.wav")
    # sibling character's objects under a similarly-named prefix survive.
    assert await storage.stat(object_key="tts/char-2/ccc.wav") is not None


@pytest.mark.asyncio
async def test_in_memory_purge_prefix_sweeps_variant_siblings_for_free() -> None:
    """A derived variant key (``{object_key}.{name}.webp``) always starts
    with the same prefix as its original, so a prefix purge removes it
    without any variant-specific logic — unlike ``delete()``, which the
    variant decorator has to fan out per suffix."""
    storage = InMemoryObjectStorage()
    await storage.put_bytes(
        object_key="characters/char-1/stage/a.png",
        content=b"PNG",
        content_type="image/png",
    )
    await storage.put_bytes(
        object_key="characters/char-1/stage/a.png.w320.webp",
        content=b"WEBP",
        content_type="image/webp",
    )

    deleted = await storage.purge_prefix(prefix="characters/char-1/")

    assert deleted == 2
    assert await storage.stat(object_key="characters/char-1/stage/a.png") is None
    assert (
        await storage.stat(object_key="characters/char-1/stage/a.png.w320.webp")
        is None
    )


@pytest.mark.asyncio
async def test_in_memory_purge_prefix_of_nothing_returns_zero() -> None:
    storage = InMemoryObjectStorage()

    assert await storage.purge_prefix(prefix="characters/never-written/") == 0


@pytest.mark.asyncio
async def test_in_memory_purge_prefix_rejects_unsafe_prefix() -> None:
    storage = InMemoryObjectStorage()

    with pytest.raises(ObjectStorageError):
        await storage.purge_prefix(prefix="characters/")


@pytest.mark.asyncio
async def test_http_object_storage_purge_prefix_posts_validated_prefix_and_parses_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"deleted": 3})

    _patch_httpx_transport(monkeypatch, handler)
    storage = HttpObjectStorage(
        base_url="http://storage-local:9000",
        api_key="secret",
    )

    deleted = await storage.purge_prefix(prefix="tts/char-1/")

    assert deleted == 3
    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/v1/objects/purge-prefix"
    assert json.loads(seen[0].content) == {"prefix": "tts/char-1/"}


@pytest.mark.asyncio
async def test_http_object_storage_purge_prefix_rejects_unsafe_prefix_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"deleted": 0})

    _patch_httpx_transport(monkeypatch, handler)
    storage = HttpObjectStorage(
        base_url="http://storage-local:9000",
        api_key="secret",
    )

    with pytest.raises(ObjectStorageError):
        await storage.purge_prefix(prefix="characters/")

    assert seen == []


@pytest.mark.asyncio
async def test_http_object_storage_purge_prefix_surfaces_backend_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "disk full"}})

    _patch_httpx_transport(monkeypatch, handler)
    storage = HttpObjectStorage(
        base_url="http://storage-local:9000",
        api_key="secret",
    )

    with pytest.raises(ObjectStorageError) as exc_info:
        await storage.purge_prefix(prefix="tts/char-1/")

    assert "disk full" in str(exc_info.value)


def test_http_object_storage_stores_app_relative_url_from_upload_response() -> None:
    stored = _stored_from_json(
        {
            "object_key": "characters/char-1/tools/a.png",
            "url": "http://127.0.0.1:9012/v1/public/characters/char-1/tools/a.png",
            "content_type": "image/png",
            "size_bytes": 3,
        },
        public_base_url="https://kokoro.example.com",
    )

    assert stored.object_key == "characters/char-1/tools/a.png"
    assert stored.url == "/v1/public/characters/char-1/tools/a.png"
