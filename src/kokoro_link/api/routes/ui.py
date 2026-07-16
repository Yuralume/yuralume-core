from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter(include_in_schema=False)

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
DIST_DIR = FRONTEND_DIR / "dist"
LEGACY_STATIC_DIR = FRONTEND_DIR / "static"


def frontend_asset(name: str) -> FileResponse:
    dist_asset = DIST_DIR / name
    if dist_asset.exists():
        return FileResponse(dist_asset)
    return FileResponse(FRONTEND_DIR / "public" / name)


def frontend_index() -> FileResponse:
    dist_index = DIST_DIR / "index.html"
    if dist_index.exists():
        return FileResponse(dist_index)
    return FileResponse(LEGACY_STATIC_DIR / "index.html")


@router.get("/")
async def index() -> FileResponse:
    return frontend_index()


@router.get("/favicon.svg")
async def favicon() -> FileResponse:
    return frontend_asset("favicon.svg")


@router.get("/{path:path}")
async def spa_fallback(path: str) -> FileResponse:
    """Catch-all for Vue router paths.

    Three layers, checked in order:

    1. ``/api/*`` must miss this handler -- raising 404 lets the actual
       API routers' own 404s show through.
    2. If ``path`` resolves to a real file inside ``DIST_DIR`` (e.g.
       ``logo.png``, ``LumeGramLogo.png``, ``memoir/bg_mem.png`` -- the
       files Vite copies from ``frontend/public/`` to the dist root),
       serve it directly. Without this branch the SPA fallback below
       returns ``index.html`` for every asset, which makes the browser
       paint broken-icon glyphs everywhere static images are
       referenced.
    3. Everything else returns ``index.html`` so the Vue router can
       resolve the route client-side.

    The path-traversal guard rejects ``..`` and absolute-path tricks by
    requiring the resolved candidate to live under ``DIST_DIR``."""
    if path.startswith("api/"):
        raise HTTPException(status_code=404)

    if DIST_DIR.exists():
        candidate: Path | None
        try:
            candidate = (DIST_DIR / path).resolve()
            candidate.relative_to(DIST_DIR.resolve())
        except (ValueError, OSError):
            candidate = None
        if candidate is not None and candidate.is_file():
            return FileResponse(candidate)

    return frontend_index()
