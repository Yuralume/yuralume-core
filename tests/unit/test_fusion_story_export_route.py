"""Route coverage for fusion-story exports (C0-3)."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.dependencies import get_container, get_current_user_id
from kokoro_link.api.routes.fusion_story import router
from kokoro_link.domain.entities.fusion_story import FusionStory


def _ready_story() -> FusionStory:
    return FusionStory.create_pending(
        id="fusion-1",
        character_ids=["c-a", "c-b"],
        prompt="A finished story.",
    ).with_full_text("Finished prose.")


def _busy_story() -> FusionStory:
    return FusionStory.create_pending(
        id="fusion-1",
        character_ids=["c-a", "c-b"],
        prompt="Still writing.",
    )


@dataclass
class _ServiceStub:
    story: FusionStory | None = None

    async def get(self, story_id: str) -> FusionStory | None:
        assert story_id == "fusion-1"
        return self.story


@dataclass
class _ContainerStub:
    fusion_story_service: _ServiceStub | None = None


def _client(container: _ContainerStub) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_container] = lambda: container
    app.dependency_overrides[get_current_user_id] = lambda: "alice"
    return TestClient(app)


def test_export_markdown_downloads_with_disposition() -> None:
    client = _client(_ContainerStub(_ServiceStub(story=_ready_story())))
    res = client.get("/api/v1/fusion-stories/fusion-1/export?format=markdown")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/markdown")
    assert "attachment" in res.headers["content-disposition"]
    assert "Finished prose." in res.text


def test_export_epub_returns_zip_bytes() -> None:
    client = _client(_ContainerStub(_ServiceStub(story=_ready_story())))
    res = client.get("/api/v1/fusion-stories/fusion-1/export?format=epub")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/epub+zip"
    assert res.content[:2] == b"PK"


def test_export_rejects_busy_story() -> None:
    client = _client(_ContainerStub(_ServiceStub(story=_busy_story())))
    res = client.get("/api/v1/fusion-stories/fusion-1/export?format=txt")
    assert res.status_code == 409


def test_export_rejects_unknown_format() -> None:
    client = _client(_ContainerStub(_ServiceStub(story=_ready_story())))
    res = client.get("/api/v1/fusion-stories/fusion-1/export?format=pdf")
    assert res.status_code == 400


def test_export_missing_story_is_404() -> None:
    client = _client(_ContainerStub(_ServiceStub(story=None)))
    res = client.get("/api/v1/fusion-stories/fusion-1/export?format=txt")
    assert res.status_code == 404
