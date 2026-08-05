from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.routes.public_objects import router
from kokoro_link.contracts.object_storage import DEFAULT_STREAM_CHUNK_BYTES
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage

_IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"


def _client(storage: InMemoryObjectStorage) -> TestClient:
    app = FastAPI()
    app.state.container = SimpleNamespace(object_storage=storage)
    app.include_router(router)
    return TestClient(app)


def _seed(
    storage: InMemoryObjectStorage,
    *,
    object_key: str,
    content: bytes,
    content_type: str = "image/png",
) -> None:
    asyncio.run(
        storage.put_bytes(
            object_key=object_key,
            content=content,
            content_type=content_type,
        ),
    )


def test_public_object_route_serves_object_storage_bytes() -> None:
    storage = InMemoryObjectStorage(public_base_url="https://kokoro.example.com")
    asyncio.run(
        storage.put_bytes(
            object_key="characters/char-1/tools/a.png",
            content=b"PNG",
            content_type="image/png",
        ),
    )
    client = _client(storage)

    response = client.get("/v1/public/characters/char-1/tools/a.png")

    assert response.status_code == 200
    assert response.content == b"PNG"
    assert response.headers["content-type"].startswith("image/png")
    assert response.headers["x-object-key"] == "characters/char-1/tools/a.png"


def test_public_object_route_supports_head() -> None:
    storage = InMemoryObjectStorage(public_base_url="https://kokoro.example.com")
    asyncio.run(
        storage.put_bytes(
            object_key="tts/char-1/a.wav",
            content=b"WAV",
            content_type="audio/wav",
        ),
    )
    client = _client(storage)

    response = client.head("/v1/public/tts/char-1/a.wav")

    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-type"].startswith("audio/wav")
    assert response.headers["content-length"] == "3"


def test_public_object_route_returns_404_for_missing_object() -> None:
    client = _client(InMemoryObjectStorage())

    response = client.get("/v1/public/missing/a.png")

    assert response.status_code == 404


def test_public_object_route_rejects_unsafe_key() -> None:
    client = _client(InMemoryObjectStorage())

    response = client.get("/v1/public/%2e%2e/secret.png")

    assert response.status_code == 400


def test_public_object_route_hides_storage_outage_details() -> None:
    """Storage down → 503 with a generic detail on this UNAUTHENTICATED
    surface; the adapter message names internal topology (STORAGE_URL)
    and must never be echoed."""
    from kokoro_link.contracts.object_storage import (
        ObjectStorageUnavailableError,
    )

    class _DownStorage(InMemoryObjectStorage):
        async def stat(self, *, object_key):  # noqa: ANN001, ANN202
            raise ObjectStorageUnavailableError(
                "object storage unreachable at http://storage-local:9000: "
                "[Errno -2] Name or service not known",
            )

    client = _client(_DownStorage())

    response = client.get("/v1/public/characters/char-1/a.png")

    assert response.status_code == 503
    assert response.json()["detail"] == "Object storage is unavailable"
    assert "storage-local" not in response.text


# ---------------------------------------------------------------------------
# Characterization — the header contract this route已經在生產環境發出去了。
# IV2 只加變體解析、304 與 streaming，以下四個 header 一字不得動。
# ---------------------------------------------------------------------------


def test_characterize_get_emits_exact_header_contract() -> None:
    """釘住 GET 的四個 header：Cache-Control / ETag / X-Object-Key /
    Content-Length。ETag 是 sha256 的 strong tag（含雙引號、無 ``W/``）。"""
    storage = InMemoryObjectStorage(public_base_url="https://kokoro.example.com")
    payload = b"PNG-BYTES-0123456789"
    _seed(storage, object_key="characters/char-1/tools/a.png", content=payload)
    client = _client(storage)

    response = client.get("/v1/public/characters/char-1/tools/a.png")

    assert response.status_code == 200
    assert response.content == payload
    assert response.headers["cache-control"] == _IMMUTABLE_CACHE_CONTROL
    assert response.headers["etag"] == f'"{hashlib.sha256(payload).hexdigest()}"'
    assert response.headers["x-object-key"] == "characters/char-1/tools/a.png"
    assert response.headers["content-length"] == str(len(payload))
    assert response.headers["content-type"].startswith("image/png")


def test_characterize_head_matches_get_headers_with_empty_body() -> None:
    """HEAD 與 GET 的 header 一致，body 為空。"""
    storage = InMemoryObjectStorage(public_base_url="https://kokoro.example.com")
    payload = b"PNG-BYTES-0123456789"
    _seed(storage, object_key="characters/char-1/tools/a.png", content=payload)
    client = _client(storage)

    get_response = client.get("/v1/public/characters/char-1/tools/a.png")
    head_response = client.head("/v1/public/characters/char-1/tools/a.png")

    assert head_response.status_code == get_response.status_code == 200
    assert head_response.content == b""
    for header in ("cache-control", "etag", "x-object-key", "content-length"):
        assert head_response.headers[header] == get_response.headers[header]
    assert (
        head_response.headers["content-type"]
        == get_response.headers["content-type"]
    )


def test_characterize_etag_is_stable_across_requests() -> None:
    storage = InMemoryObjectStorage()
    _seed(storage, object_key="characters/char-1/a.png", content=b"PNG")
    client = _client(storage)

    first = client.get("/v1/public/characters/char-1/a.png")
    second = client.get("/v1/public/characters/char-1/a.png")

    assert first.headers["etag"] == second.headers["etag"]


def test_characterize_unknown_query_params_are_ignored_not_rejected() -> None:
    """未宣告 / 未知的 query 一律靜默忽略並送原檔 —— 絕不回 400。

    IV2 在同一條 route 上引入 ``?v=`` 白名單；白名單外的值必須維持
    這個既有行為，否則舊連結會在部署當下集體變成 400。
    """
    storage = InMemoryObjectStorage()
    _seed(storage, object_key="characters/char-1/a.png", content=b"PNG")
    client = _client(storage)

    response = client.get(
        "/v1/public/characters/char-1/a.png?v=not-a-variant&foo=bar",
    )

    assert response.status_code == 200
    assert response.content == b"PNG"
    assert response.headers["x-object-key"] == "characters/char-1/a.png"


def test_characterize_missing_sha256_omits_etag() -> None:
    """儲存後端沒給 sha256 時不發 ETag（其餘 header 照舊）。"""
    from kokoro_link.contracts.object_storage import ObjectMetadata

    class _NoDigestStorage(InMemoryObjectStorage):
        async def stat(self, *, object_key):  # noqa: ANN001, ANN202
            return ObjectMetadata(
                object_key=object_key,
                url=f"/v1/public/{object_key}",
                content_type="image/png",
                size_bytes=3,
                sha256=None,
            )

    storage = _NoDigestStorage()
    _seed(storage, object_key="characters/char-1/a.png", content=b"PNG")
    client = _client(storage)

    response = client.get("/v1/public/characters/char-1/a.png")

    assert response.status_code == 200
    assert "etag" not in response.headers
    assert response.headers["cache-control"] == _IMMUTABLE_CACHE_CONTROL


# ---------------------------------------------------------------------------
# IV2 ① 變體解析（?v=w320|w768|full）
# ---------------------------------------------------------------------------


class _RecordingStorage(InMemoryObjectStorage):
    """In-memory storage that records which keys each port method saw."""

    def __init__(self, **kwargs) -> None:  # noqa: ANN003
        super().__init__(**kwargs)
        self.stat_keys: list[str] = []
        self.get_bytes_keys: list[str] = []
        self.iter_bytes_keys: list[str] = []

    async def stat(self, *, object_key):  # noqa: ANN001, ANN202
        self.stat_keys.append(object_key)
        return await super().stat(object_key=object_key)

    async def get_bytes(self, *, object_key):  # noqa: ANN001, ANN202
        self.get_bytes_keys.append(object_key)
        return await super().get_bytes(object_key=object_key)

    async def iter_bytes(  # noqa: ANN202
        self,
        *,
        object_key,  # noqa: ANN001
        chunk_size: int = DEFAULT_STREAM_CHUNK_BYTES,
    ):
        self.iter_bytes_keys.append(object_key)
        # Read through the *base* implementation so the recorder only sees
        # ``get_bytes`` calls the route itself made. The in-memory adapter
        # happens to back its stream with ``get_bytes``; the HTTP adapter
        # does not, and that detail must not leak into the assertion.
        data = await InMemoryObjectStorage.get_bytes(self, object_key=object_key)
        step = max(1, int(chunk_size))
        for start in range(0, len(data), step):
            yield data[start:start + step]


class _CorePortOnlyStorage:
    """A storage port *without* the optional streaming capability.

    Stands in for adapters and decorators that only implement
    ``ObjectStoragePort`` — the route must still serve them.
    """

    def __init__(self, inner: InMemoryObjectStorage) -> None:
        self._inner = inner
        self.get_bytes_keys: list[str] = []

    async def stat(self, *, object_key):  # noqa: ANN001, ANN202
        return await self._inner.stat(object_key=object_key)

    async def get_bytes(self, *, object_key):  # noqa: ANN001, ANN202
        self.get_bytes_keys.append(object_key)
        return await self._inner.get_bytes(object_key=object_key)


def test_variant_query_serves_derived_object_when_present() -> None:
    storage = InMemoryObjectStorage()
    _seed(storage, object_key="characters/char-1/a.png", content=b"ORIGINAL-PNG")
    _seed(
        storage,
        object_key="characters/char-1/a.png.w320.webp",
        content=b"TINY",
        content_type="image/webp",
    )
    client = _client(storage)

    response = client.get("/v1/public/characters/char-1/a.png?v=w320")

    assert response.status_code == 200
    assert response.content == b"TINY"
    assert response.headers["content-type"].startswith("image/webp")
    assert (
        response.headers["x-object-key"] == "characters/char-1/a.png.w320.webp"
    )
    assert response.headers["content-length"] == "4"
    assert response.headers["etag"] == f'"{hashlib.sha256(b"TINY").hexdigest()}"'
    assert response.headers["cache-control"] == _IMMUTABLE_CACHE_CONTROL


def test_variant_key_is_the_original_key_plus_a_fixed_suffix() -> None:
    """D1: variant key 由原 key 純字串衍生（append，不換副檔名）。"""
    storage = _RecordingStorage()
    _seed(storage, object_key="album/char-1/img.png", content=b"ORIGINAL")
    client = _client(storage)

    for label, suffix in (
        ("w320", ".w320.webp"),
        ("w768", ".w768.webp"),
        ("full", ".full.webp"),
    ):
        storage.stat_keys.clear()
        response = client.get(f"/v1/public/album/char-1/img.png?v={label}")
        assert response.status_code == 200
        assert storage.stat_keys[0] == f"album/char-1/img.png{suffix}"


def test_every_encoder_tier_is_reachable_from_the_route() -> None:
    """白名單與編碼器產出的 tier 必須是同一組。

    若某個 tier 只存在於寫入端，回填出來的變體就永遠沒人拿得到。
    """
    from kokoro_link.infrastructure.storage.image_variants import (
        VARIANT_SPECS,
        derive_variant_key,
    )

    storage = InMemoryObjectStorage()
    _seed(storage, object_key="album/char-1/img.png", content=b"ORIGINAL")
    for spec in VARIANT_SPECS:
        _seed(
            storage,
            object_key=derive_variant_key("album/char-1/img.png", spec.name),
            content=spec.name.encode(),
            content_type="image/webp",
        )
    client = _client(storage)

    for spec in VARIANT_SPECS:
        response = client.get(f"/v1/public/album/char-1/img.png?v={spec.name}")
        assert response.status_code == 200, spec.name
        assert response.content == spec.name.encode(), spec.name


def test_empty_object_key_with_variant_still_returns_400() -> None:
    """空 key 走既有的 400，變體探測不得把它變成 500。"""
    client = _client(InMemoryObjectStorage())

    assert client.get("/v1/public/").status_code == 400
    assert client.get("/v1/public/?v=w320").status_code == 400


def test_variant_missing_falls_back_to_original() -> None:
    """存量圖還沒回填變體 —— 必須送原檔，不是 404。"""
    storage = InMemoryObjectStorage()
    _seed(storage, object_key="characters/char-1/a.png", content=b"ORIGINAL-PNG")
    client = _client(storage)

    response = client.get("/v1/public/characters/char-1/a.png?v=w768")

    assert response.status_code == 200
    assert response.content == b"ORIGINAL-PNG"
    assert response.headers["x-object-key"] == "characters/char-1/a.png"
    assert response.headers["content-type"].startswith("image/png")


def test_variant_miss_is_not_logged_as_an_error(caplog) -> None:  # noqa: ANN001
    """紅線：變體不存在是正常狀態，不得因此噴 error log。

    未回填的存量圖片每一次請求都會 miss；若這裡是 warning/error，
    正常流量就會把 log 塞滿。
    """
    import logging

    storage = InMemoryObjectStorage()
    _seed(storage, object_key="characters/char-1/a.png", content=b"ORIGINAL-PNG")
    client = _client(storage)

    with caplog.at_level(logging.DEBUG):
        response = client.get("/v1/public/characters/char-1/a.png?v=w320")

    assert response.status_code == 200
    noisy = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert noisy == [], [r.getMessage() for r in noisy]


def test_variant_label_whitelist_is_exact() -> None:
    """白名單外的值（含大小寫變形與空字串）視同未帶，送原檔不回 400。"""
    storage = InMemoryObjectStorage()
    _seed(storage, object_key="characters/char-1/a.png", content=b"ORIGINAL-PNG")
    _seed(
        storage,
        object_key="characters/char-1/a.png.w320.webp",
        content=b"TINY",
        content_type="image/webp",
    )
    client = _client(storage)

    for raw in ("W320", "", "w320.webp", "thumb", "w321"):
        response = client.get(f"/v1/public/characters/char-1/a.png?v={raw}")
        assert response.status_code == 200, raw
        assert response.content == b"ORIGINAL-PNG", raw
        assert response.headers["x-object-key"] == "characters/char-1/a.png"


def test_variant_query_on_unsafe_key_still_returns_400() -> None:
    """變體探測不得吞掉 key 驗證 —— 原檔探測會以既有語意重新浮現。"""
    client = _client(InMemoryObjectStorage())

    response = client.get("/v1/public/%2e%2e/secret.png?v=w320")

    assert response.status_code == 400


def test_variant_query_on_storage_outage_still_returns_503() -> None:
    from kokoro_link.contracts.object_storage import (
        ObjectStorageUnavailableError,
    )

    class _DownStorage(InMemoryObjectStorage):
        async def stat(self, *, object_key):  # noqa: ANN001, ANN202
            raise ObjectStorageUnavailableError(
                "object storage unreachable at http://storage-local:9000",
            )

    client = _client(_DownStorage())

    response = client.get("/v1/public/characters/char-1/a.png?v=w320")

    assert response.status_code == 503
    assert response.json()["detail"] == "Object storage is unavailable"


def test_variant_head_matches_variant_get() -> None:
    storage = InMemoryObjectStorage()
    _seed(storage, object_key="characters/char-1/a.png", content=b"ORIGINAL-PNG")
    _seed(
        storage,
        object_key="characters/char-1/a.png.w768.webp",
        content=b"MEDIUM-WEBP",
        content_type="image/webp",
    )
    client = _client(storage)

    get_response = client.get("/v1/public/characters/char-1/a.png?v=w768")
    head_response = client.head("/v1/public/characters/char-1/a.png?v=w768")

    assert head_response.status_code == get_response.status_code == 200
    assert head_response.content == b""
    for header in ("cache-control", "etag", "x-object-key", "content-length"):
        assert head_response.headers[header] == get_response.headers[header]


def test_variant_missing_head_matches_original_get() -> None:
    storage = InMemoryObjectStorage()
    _seed(storage, object_key="characters/char-1/a.png", content=b"ORIGINAL-PNG")
    client = _client(storage)

    get_response = client.get("/v1/public/characters/char-1/a.png")
    head_response = client.head("/v1/public/characters/char-1/a.png?v=w320")

    assert head_response.status_code == 200
    for header in ("cache-control", "etag", "x-object-key", "content-length"):
        assert head_response.headers[header] == get_response.headers[header]


# ---------------------------------------------------------------------------
# IV2 ② If-None-Match → 304
# ---------------------------------------------------------------------------


class _BodyTripwireStorage(InMemoryObjectStorage):
    """Blows up if anything tries to read an object body."""

    async def get_bytes(self, *, object_key):  # noqa: ANN001, ANN202
        raise AssertionError(f"body must not be read: {object_key}")

    def iter_bytes(self, *, object_key, chunk_size=0):  # noqa: ANN001, ANN202
        raise AssertionError(f"body must not be read: {object_key}")


def _etag_for(client: TestClient, url: str) -> str:
    response = client.get(url)
    assert response.status_code == 200
    return response.headers["etag"]


def test_if_none_match_exact_returns_304_without_body() -> None:
    storage = InMemoryObjectStorage()
    _seed(storage, object_key="characters/char-1/a.png", content=b"ORIGINAL-PNG")
    client = _client(storage)
    etag = _etag_for(client, "/v1/public/characters/char-1/a.png")

    response = client.get(
        "/v1/public/characters/char-1/a.png",
        headers={"If-None-Match": etag},
    )

    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["etag"] == etag
    assert response.headers["cache-control"] == _IMMUTABLE_CACHE_CONTROL
    assert response.headers["x-object-key"] == "characters/char-1/a.png"
    assert "content-length" not in response.headers


def test_if_none_match_does_not_read_the_object_body() -> None:
    """304 不得讀 body —— 這正是這條 route 之前浪費頻寬的地方。"""
    storage = _BodyTripwireStorage()
    _seed(storage, object_key="characters/char-1/a.png", content=b"ORIGINAL-PNG")
    client = _client(storage)
    etag = f'"{hashlib.sha256(b"ORIGINAL-PNG").hexdigest()}"'

    response = client.get(
        "/v1/public/characters/char-1/a.png",
        headers={"If-None-Match": etag},
    )

    assert response.status_code == 304


def test_if_none_match_wildcard_returns_304() -> None:
    storage = InMemoryObjectStorage()
    _seed(storage, object_key="characters/char-1/a.png", content=b"ORIGINAL-PNG")
    client = _client(storage)

    response = client.get(
        "/v1/public/characters/char-1/a.png",
        headers={"If-None-Match": "*"},
    )

    assert response.status_code == 304


def test_if_none_match_accepts_weak_and_listed_validators() -> None:
    """RFC 7232 §3.2：If-None-Match 用 weak comparison，且可以是清單。"""
    storage = InMemoryObjectStorage()
    _seed(storage, object_key="characters/char-1/a.png", content=b"ORIGINAL-PNG")
    client = _client(storage)
    etag = _etag_for(client, "/v1/public/characters/char-1/a.png")

    for header_value in (
        f"W/{etag}",
        f'"deadbeef", {etag}',
        f'{etag} , W/"cafebabe"',
    ):
        response = client.get(
            "/v1/public/characters/char-1/a.png",
            headers={"If-None-Match": header_value},
        )
        assert response.status_code == 304, header_value


def test_if_none_match_mismatch_serves_the_body() -> None:
    storage = InMemoryObjectStorage()
    _seed(storage, object_key="characters/char-1/a.png", content=b"ORIGINAL-PNG")
    client = _client(storage)

    response = client.get(
        "/v1/public/characters/char-1/a.png",
        headers={"If-None-Match": '"not-the-current-digest"'},
    )

    assert response.status_code == 200
    assert response.content == b"ORIGINAL-PNG"


def test_if_none_match_is_compared_against_the_served_variant() -> None:
    """帶 ?v= 時比對的是變體自己的 ETag，不是原檔的。"""
    storage = InMemoryObjectStorage()
    _seed(storage, object_key="characters/char-1/a.png", content=b"ORIGINAL-PNG")
    _seed(
        storage,
        object_key="characters/char-1/a.png.w320.webp",
        content=b"TINY",
        content_type="image/webp",
    )
    client = _client(storage)
    original_etag = _etag_for(client, "/v1/public/characters/char-1/a.png")
    variant_etag = _etag_for(client, "/v1/public/characters/char-1/a.png?v=w320")
    assert original_etag != variant_etag

    stale = client.get(
        "/v1/public/characters/char-1/a.png?v=w320",
        headers={"If-None-Match": original_etag},
    )
    fresh = client.get(
        "/v1/public/characters/char-1/a.png?v=w320",
        headers={"If-None-Match": variant_etag},
    )

    assert stale.status_code == 200
    assert stale.content == b"TINY"
    assert fresh.status_code == 304


def test_head_also_honours_if_none_match() -> None:
    storage = InMemoryObjectStorage()
    _seed(storage, object_key="characters/char-1/a.png", content=b"ORIGINAL-PNG")
    client = _client(storage)
    etag = _etag_for(client, "/v1/public/characters/char-1/a.png")

    response = client.head(
        "/v1/public/characters/char-1/a.png",
        headers={"If-None-Match": etag},
    )

    assert response.status_code == 304
    assert response.content == b""


def test_if_none_match_without_etag_serves_the_body() -> None:
    """後端沒給 sha256 就沒有 validator，任何條件請求都只能是 200。"""
    from kokoro_link.contracts.object_storage import ObjectMetadata

    class _NoDigestStorage(InMemoryObjectStorage):
        async def stat(self, *, object_key):  # noqa: ANN001, ANN202
            return ObjectMetadata(
                object_key=object_key,
                url=f"/v1/public/{object_key}",
                content_type="image/png",
                size_bytes=3,
                sha256=None,
            )

    storage = _NoDigestStorage()
    _seed(storage, object_key="characters/char-1/a.png", content=b"PNG")
    client = _client(storage)

    response = client.get(
        "/v1/public/characters/char-1/a.png",
        headers={"If-None-Match": "*"},
    )

    assert response.status_code == 200
    assert response.content == b"PNG"


# ---------------------------------------------------------------------------
# IV2 ③ streaming
# ---------------------------------------------------------------------------


def test_get_streams_and_never_calls_get_bytes() -> None:
    """不再 get_bytes() 整檔進記憶體。"""
    storage = _RecordingStorage()
    payload = b"X" * 4096
    _seed(storage, object_key="characters/char-1/a.png", content=payload)
    client = _client(storage)

    response = client.get("/v1/public/characters/char-1/a.png")

    assert response.status_code == 200
    assert response.content == payload
    assert storage.iter_bytes_keys == ["characters/char-1/a.png"]
    assert storage.get_bytes_keys == []


def test_head_reads_neither_stream_nor_bytes() -> None:
    storage = _RecordingStorage()
    _seed(storage, object_key="characters/char-1/a.png", content=b"X" * 4096)
    client = _client(storage)

    response = client.head("/v1/public/characters/char-1/a.png")

    assert response.status_code == 200
    assert storage.iter_bytes_keys == []
    assert storage.get_bytes_keys == []


def test_streamed_variant_reads_the_variant_key() -> None:
    storage = _RecordingStorage()
    _seed(storage, object_key="characters/char-1/a.png", content=b"ORIGINAL-PNG")
    _seed(
        storage,
        object_key="characters/char-1/a.png.w320.webp",
        content=b"TINY",
        content_type="image/webp",
    )
    client = _client(storage)

    response = client.get("/v1/public/characters/char-1/a.png?v=w320")

    assert response.status_code == 200
    assert storage.iter_bytes_keys == ["characters/char-1/a.png.w320.webp"]


def test_multi_chunk_stream_reassembles_exactly() -> None:
    """分塊邊界不得吃掉或重複 byte，且 Content-Length 仍是完整長度。"""

    class _TinyChunkStorage(InMemoryObjectStorage):
        async def iter_bytes(self, *, object_key, chunk_size=0):  # noqa: ANN001, ANN202
            async for chunk in super().iter_bytes(
                object_key=object_key,
                chunk_size=7,
            ):
                yield chunk

    storage = _TinyChunkStorage()
    payload = bytes(range(256)) * 5
    _seed(storage, object_key="characters/char-1/a.png", content=payload)
    client = _client(storage)

    response = client.get("/v1/public/characters/char-1/a.png")

    assert response.status_code == 200
    assert response.content == payload
    assert response.headers["content-length"] == str(len(payload))


def test_storage_without_streaming_capability_still_serves() -> None:
    """只實作核心 port 的 adapter / decorator 仍然可用（降級到 get_bytes）。"""
    inner = InMemoryObjectStorage()
    _seed(inner, object_key="characters/char-1/a.png", content=b"ORIGINAL-PNG")
    storage = _CorePortOnlyStorage(inner)
    client = _client(storage)  # type: ignore[arg-type]

    response = client.get("/v1/public/characters/char-1/a.png")

    assert response.status_code == 200
    assert response.content == b"ORIGINAL-PNG"
    assert storage.get_bytes_keys == ["characters/char-1/a.png"]
    assert response.headers["cache-control"] == _IMMUTABLE_CACHE_CONTROL
    assert response.headers["content-length"] == "12"
