from __future__ import annotations

import pytest

from kokoro_link.contracts.object_storage import ObjectNotFoundError, ObjectStorageError
from kokoro_link.infrastructure.storage.http import HttpObjectStorage, _stored_from_json
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage
from kokoro_link.infrastructure.storage.keys import validate_object_key


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
