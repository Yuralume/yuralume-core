from __future__ import annotations

from io import BytesIO

import pytest
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

from kokoro_link.api.routes.chat import upload_chat_attachments
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage


def _upload(filename: str, content_type: str, data: bytes = b"x") -> UploadFile:
    return UploadFile(
        filename=filename,
        file=BytesIO(data),
        headers=Headers({"content-type": content_type}),
    )


class _Container:
    def __init__(self, storage: InMemoryObjectStorage) -> None:
        self.object_storage = storage


@pytest.mark.asyncio
async def test_chat_upload_writes_object_storage() -> None:
    storage = InMemoryObjectStorage(public_base_url="/uploads")

    response = await upload_chat_attachments(
        files=[_upload("avatar.png", "image/png", b"\x89PNG\r\n\x1a\nfake")],
        container=_Container(storage),  # type: ignore[arg-type]
        current_user_id="user-1",
    )

    assert len(response.urls) == 1
    object_key = storage.object_key_from_url(response.urls[0])
    assert object_key is not None
    assert object_key.startswith("users/user-1/chat-uploads/")
    assert await storage.get_bytes(object_key=object_key) == b"\x89PNG\r\n\x1a\nfake"


@pytest.mark.asyncio
async def test_chat_upload_rejects_a_html_content_type_behind_an_image_name(
) -> None:
    """The filename allow-list cannot see this one.

    Suffix and content type are two independent, both attacker-chosen fields of
    the same multipart part. The stored object is served back from the app's
    *own* origin carrying the declared type, so ``text/html`` accepted here is
    same-origin HTML at a URL the app itself minted — stored XSS, delivered by
    a file that passes every extension check ever written.
    """
    storage = InMemoryObjectStorage(public_base_url="/uploads")

    with pytest.raises(HTTPException) as excinfo:
        await upload_chat_attachments(
            files=[_upload("avatar.png", "text/html", b"<script>alert(1)</script>")],
            container=_Container(storage),  # type: ignore[arg-type]
            current_user_id="user-1",
        )

    assert excinfo.value.status_code == 415
    assert "unsupported file type" in str(excinfo.value.detail)


@pytest.mark.asyncio
async def test_chat_upload_rejects_a_missing_content_type() -> None:
    """No header is not a licence to guess: only the uploader was ever in a
    position to say what the bytes are."""
    storage = InMemoryObjectStorage(public_base_url="/uploads")

    with pytest.raises(HTTPException) as excinfo:
        await upload_chat_attachments(
            files=[UploadFile(filename="avatar.png", file=BytesIO(b"x"))],
            container=_Container(storage),  # type: ignore[arg-type]
            current_user_id="user-1",
        )

    assert excinfo.value.status_code == 415
