from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from kokoro_link.infrastructure.messaging.line import media_fetcher as line_media
from kokoro_link.infrastructure.messaging.telegram import media_fetcher as tg_media
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage


class _FakeAsyncClient:
    def __init__(self, responses: list[httpx.Response], **_: object) -> None:
        self._responses = responses

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def get(self, *_: object, **__: object) -> httpx.Response:
        return self._responses.pop(0)


def _response(
    *,
    json_body: object | None = None,
    content: bytes = b"",
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    request = httpx.Request("GET", "https://example.test")
    if json_body is not None:
        return httpx.Response(
            200,
            json=json_body,
            headers=headers,
            request=request,
        )
    return httpx.Response(
        200,
        content=content,
        headers=headers,
        request=request,
    )


@pytest.mark.asyncio
async def test_telegram_photo_uses_object_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = InMemoryObjectStorage(public_base_url="/media")
    responses = [
        _response(
            json_body={
                "ok": True,
                "result": {"file_path": "photos/file_1.png"},
            },
        ),
        _response(content=b"PNGDATA", headers={"content-type": "image/png"}),
    ]
    monkeypatch.setattr(
        tg_media.httpx,
        "AsyncClient",
        lambda **kwargs: _FakeAsyncClient(responses, **kwargs),
    )

    url = await tg_media.download_telegram_photo(
        bot_token="token",
        file_id="file-id",
        uploads_dir=tmp_path,
        object_storage=storage,
        user_id="user-1",
    )

    assert url is not None
    assert url.startswith("/media/users/user-1/messaging-inbound/")
    key = storage.object_key_from_url(url)
    assert key is not None
    assert await storage.get_bytes(object_key=key) == b"PNGDATA"
    assert not (tmp_path / "messaging-inbound").exists()


@pytest.mark.asyncio
async def test_line_image_uses_object_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = InMemoryObjectStorage(public_base_url="/media")
    responses = [
        _response(content=b"JPGDATA", headers={"content-type": "image/jpeg"}),
    ]
    monkeypatch.setattr(
        line_media.httpx,
        "AsyncClient",
        lambda **kwargs: _FakeAsyncClient(responses, **kwargs),
    )

    url = await line_media.download_line_image(
        channel_access_token="token",
        message_id="msg-1",
        uploads_dir=tmp_path,
        object_storage=storage,
        user_id="user-1",
    )

    assert url is not None
    assert url.startswith("/media/users/user-1/messaging-inbound/")
    key = storage.object_key_from_url(url)
    assert key is not None
    assert await storage.get_bytes(object_key=key) == b"JPGDATA"
    assert not (tmp_path / "messaging-inbound").exists()
