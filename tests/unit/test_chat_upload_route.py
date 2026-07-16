from __future__ import annotations

from io import BytesIO

import pytest
from fastapi import UploadFile

from kokoro_link.api.routes.chat import upload_chat_attachments
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage


@pytest.mark.asyncio
async def test_chat_upload_writes_object_storage() -> None:
    storage = InMemoryObjectStorage(public_base_url="/uploads")

    class _Container:
        object_storage = storage

    response = await upload_chat_attachments(
        files=[
            UploadFile(
                filename="avatar.png",
                file=BytesIO(b"\x89PNG\r\n\x1a\nfake"),
            ),
        ],
        container=_Container(),  # type: ignore[arg-type]
        current_user_id="user-1",
    )

    assert len(response.urls) == 1
    object_key = storage.object_key_from_url(response.urls[0])
    assert object_key is not None
    assert object_key.startswith("users/user-1/chat-uploads/")
    assert await storage.get_bytes(object_key=object_key) == b"\x89PNG\r\n\x1a\nfake"
