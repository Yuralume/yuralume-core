"""``POST /characters/draft`` — what the reference image is allowed to be.

The draft route stages the uploaded bytes to Object Storage and hands the
provider a URL served back from the app's own origin, carrying the
``Content-Type`` the uploader declared. That makes the declared type a security
boundary rather than a label, and it is why the route validates it instead of
defaulting it: the old ``image.content_type or "image/png"`` fell back for
exactly the request that declined to say what it was sending.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.dependencies import get_container, get_current_user_id
from kokoro_link.application.dto.character_draft import CharacterDraftResponse

_USER_ID = "alice"
_DRAFT_PATH = "/api/v1/characters/draft"


class _RecordingDraftService:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def generate(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        return CharacterDraftResponse(name="Yui")


def _client(drafts: _RecordingDraftService) -> TestClient:
    from kokoro_link.api.routes.characters import router

    app = FastAPI()
    app.dependency_overrides[get_container] = lambda: SimpleNamespace(
        character_draft_service=drafts,
    )
    app.dependency_overrides[get_current_user_id] = lambda: _USER_ID
    app.include_router(router, prefix="/api/v1")
    return TestClient(app, raise_server_exceptions=False)


def test_draft_route_rejects_a_html_content_type() -> None:
    drafts = _RecordingDraftService()
    client = _client(drafts)

    response = client.post(
        _DRAFT_PATH,
        data={"prompt": "一個溫柔的圖書館員"},
        files={
            "image": ("avatar.png", b"<script>alert(1)</script>", "text/html"),
        },
    )

    assert response.status_code == 415, response.text
    assert "unsupported file type" in response.text
    # Refused at the edge: no service call, so nothing was charged and nothing
    # was staged.
    assert drafts.calls == []


def test_draft_route_accepts_a_whitelisted_image() -> None:
    drafts = _RecordingDraftService()
    client = _client(drafts)

    response = client.post(
        _DRAFT_PATH,
        data={"prompt": "一個溫柔的圖書館員"},
        files={"image": ("avatar.webp", b"RIFFfake", "image/webp")},
    )

    assert response.status_code == 200, response.text
    image = drafts.calls[0]["image"]
    # The validated type travels on — it is what the staged object is stored
    # and later served with.
    assert image.mime_type == "image/webp"
    assert image.data == b"RIFFfake"


def test_draft_route_rejects_an_upload_with_no_declared_type() -> None:
    """The retired fallback. ``or "image/png"`` used to put the app's name on
    a claim only the uploader could make."""
    drafts = _RecordingDraftService()
    client = _client(drafts)

    response = client.post(
        _DRAFT_PATH,
        data={"prompt": "x"},
        files={"image": ("avatar.png", b"bytes", "")},
    )

    assert response.status_code == 415, response.text
    assert drafts.calls == []


@pytest.mark.parametrize(
    "content_type", ["image/png", "image/jpeg", "image/webp", "image/gif"],
)
def test_draft_route_accepts_every_whitelisted_type(content_type: str) -> None:
    drafts = _RecordingDraftService()
    client = _client(drafts)

    response = client.post(
        _DRAFT_PATH,
        data={"prompt": "x"},
        files={"image": ("a.bin", b"bytes", content_type)},
    )

    assert response.status_code == 200, response.text
    assert drafts.calls[0]["image"].mime_type == content_type


def test_a_prompt_only_draft_needs_no_content_type() -> None:
    """The guard is on the image branch, not the route: text-only drafts are
    the common case and must stay untouched."""
    drafts = _RecordingDraftService()
    client = _client(drafts)

    response = client.post(_DRAFT_PATH, data={"prompt": "一個溫柔的圖書館員"})

    assert response.status_code == 200, response.text
    assert drafts.calls[0]["image"] is None
