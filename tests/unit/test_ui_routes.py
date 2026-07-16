from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.routes.ui import router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_favicon_route_serves_frontend_asset():
    client = _client()

    response = client.get("/favicon.svg")

    assert response.status_code == 200
    assert "image/svg+xml" in response.headers["content-type"]
    assert "<svg" in response.text


def test_spa_fallback_serves_admin_deep_link():
    client = _client()

    response = client.get("/admin/providers")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_spa_fallback_does_not_shadow_api_paths():
    client = _client()

    response = client.get("/api/v1/not-found")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


def test_spa_fallback_serves_dist_root_static_asset():
    """Files Vite copies from ``frontend/public/`` to ``dist/`` root
    (logo.png, LumeGramLogo.png, icons.svg, …) must be served as the
    real file. Regression: the original spa_fallback returned
    index.html for every non-``/api`` path, so the browser tried to
    decode HTML bytes as an image and painted broken-icon glyphs in
    the container build."""
    client = _client()

    response = client.get("/logo.png")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")
    # Defensive: the bug would have returned text/html with an HTML body
    assert not response.content.startswith(b"<!DOCTYPE")
    assert not response.content.startswith(b"<html")


def test_spa_fallback_serves_nested_dist_asset():
    """Nested public/ subdirectories (e.g. ``memoir/*.png``) must work
    too -- the path-traversal guard should accept paths that resolve
    inside DIST_DIR."""
    client = _client()

    response = client.get("/memoir/bg_mem.png")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")


def test_spa_fallback_rejects_path_traversal():
    """``..`` segments that would escape DIST_DIR must not leak host
    files; they fall through to the index.html branch."""
    client = _client()

    response = client.get("/../../etc/passwd")

    # FastAPI normalises ``..`` segments at the routing layer, so the
    # request never reaches our handler with literal ``..`` in the
    # path. Either way the response must not be the host's passwd.
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert b"root:" not in response.content


def test_spa_fallback_serves_vue_route_when_no_asset_matches():
    """The original behaviour -- arbitrary SPA routes resolve to
    index.html so Vue Router can take over client-side -- must
    still work after the asset-passthrough branch was added."""
    client = _client()

    response = client.get("/some/non-existent/vue/route")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
