from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from pathlib import Path
from uuid import uuid4

from fastapi import (
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from kokoro_link.contracts.object_storage import ObjectStorageError
from kokoro_link.infrastructure.storage.keys import validate_object_key


class CopyRequest(BaseModel):
    source_key: str
    destination_key: str
    metadata: dict[str, str] = Field(default_factory=dict)


class LocalStorageSettings(BaseModel):
    root: Path
    api_key: str
    public_base_url: str
    max_object_bytes: int
    cache_control: str

    @classmethod
    def from_env(cls) -> "LocalStorageSettings":
        return cls(
            root=Path(os.getenv("YURALUME_STORAGE_ROOT", "/data")).resolve(),
            api_key=(
                os.getenv("YURALUME_STORAGE_API_KEY")
                or os.getenv("STORAGE_KEY")
                or os.getenv("STORAGE_API_KEY")
                or "change-me"
            ),
            public_base_url=(
                os.getenv("YURALUME_STORAGE_PUBLIC_BASE_URL")
                or os.getenv("STORAGE_PUBLIC_URL")
                or os.getenv("STORAGE_PUBLIC_BASE_URL")
                or "http://127.0.0.1:9012"
            ).rstrip("/"),
            max_object_bytes=int(
                os.getenv("YURALUME_STORAGE_MAX_OBJECT_BYTES", "536870912"),
            ),
            cache_control=os.getenv(
                "YURALUME_STORAGE_CACHE_CONTROL",
                "public, max-age=31536000, immutable",
            ),
        )


def create_app() -> FastAPI:
    settings = LocalStorageSettings.from_env()
    store = _LocalVolumeStore(settings)
    app = FastAPI(title="Yuralume Local Object Storage", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, str]:
        store.ensure_dirs()
        return {"status": "ok"}

    @app.post("/v1/objects")
    async def put_object(
        object_key: str = Form(...),
        content_type: str = Form(...),
        metadata: str = Form("{}"),
        file: UploadFile = File(...),
        authorization: str | None = Header(default=None),
    ) -> dict:
        _require_auth(settings, authorization)
        meta = _parse_metadata(metadata)
        data = await file.read()
        if len(data) > settings.max_object_bytes:
            raise _error(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                "object_too_large",
                "object exceeds configured max size",
            )
        return store.put(
            object_key=object_key,
            content=data,
            content_type=content_type,
            metadata=meta,
        )

    @app.get("/v1/objects/content/{object_key:path}")
    async def get_object_content(
        object_key: str,
        authorization: str | None = Header(default=None),
    ) -> FileResponse:
        _require_auth(settings, authorization)
        key = _safe_key(object_key)
        path = store.object_path(key)
        if not path.is_file():
            raise _error(status.HTTP_404_NOT_FOUND, "not_found", "object not found")
        meta = store.metadata_for(key)
        return FileResponse(
            path,
            media_type=meta.get("content_type") or _guess_type(path),
            headers=store.public_headers(key, meta),
        )

    @app.get("/v1/objects/metadata/{object_key:path}")
    async def get_object_metadata(
        object_key: str,
        authorization: str | None = Header(default=None),
    ) -> dict:
        _require_auth(settings, authorization)
        key = _safe_key(object_key)
        if not store.object_path(key).is_file():
            raise _error(status.HTTP_404_NOT_FOUND, "not_found", "object not found")
        return store.metadata_for(key)

    @app.delete("/v1/objects/{object_key:path}", status_code=204)
    async def delete_object(
        object_key: str,
        authorization: str | None = Header(default=None),
    ) -> Response:
        _require_auth(settings, authorization)
        store.delete(_safe_key(object_key))
        return Response(status_code=204)

    @app.post("/v1/objects/copy")
    async def copy_object(
        request: CopyRequest,
        authorization: str | None = Header(default=None),
    ) -> dict:
        _require_auth(settings, authorization)
        return store.copy(
            source_key=request.source_key,
            destination_key=request.destination_key,
            metadata=request.metadata,
        )

    @app.get("/v1/public/{object_key:path}")
    @app.head("/v1/public/{object_key:path}")
    async def public_object(object_key: str) -> FileResponse:
        key = _safe_key(object_key)
        path = store.object_path(key)
        if not path.is_file():
            raise _error(status.HTTP_404_NOT_FOUND, "not_found", "object not found")
        meta = store.metadata_for(key)
        return FileResponse(
            path,
            media_type=meta.get("content_type") or _guess_type(path),
            headers=store.public_headers(key, meta),
        )

    return app


class _LocalVolumeStore:
    def __init__(self, settings: LocalStorageSettings) -> None:
        self._settings = settings
        self._objects = settings.root / "objects"
        self._metadata = settings.root / "metadata"
        self.ensure_dirs()

    def ensure_dirs(self) -> None:
        self._objects.mkdir(parents=True, exist_ok=True)
        self._metadata.mkdir(parents=True, exist_ok=True)

    def object_path(self, object_key: str) -> Path:
        return self._safe_path(self._objects, object_key)

    def metadata_path(self, object_key: str) -> Path:
        return self._safe_path(self._metadata, f"{object_key}.json")

    def put(
        self,
        *,
        object_key: str,
        content: bytes,
        content_type: str,
        metadata: dict[str, str],
    ) -> dict:
        key = _safe_key(object_key)
        target = self.object_path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / f".{target.name}.{uuid4().hex}.part"
        tmp.write_bytes(content)
        tmp.replace(target)
        payload = self._build_metadata(
            object_key=key,
            content=content,
            content_type=content_type,
            metadata=metadata,
        )
        self._write_metadata(key, payload)
        return payload

    def copy(
        self,
        *,
        source_key: str,
        destination_key: str,
        metadata: dict[str, str],
    ) -> dict:
        source = _safe_key(source_key)
        dest = _safe_key(destination_key)
        source_path = self.object_path(source)
        if not source_path.is_file():
            raise _error(status.HTTP_404_NOT_FOUND, "not_found", "object not found")
        content = source_path.read_bytes()
        source_meta = self.metadata_for(source)
        return self.put(
            object_key=dest,
            content=content,
            content_type=source_meta.get("content_type") or _guess_type(source_path),
            metadata=metadata or dict(source_meta.get("metadata") or {}),
        )

    def delete(self, object_key: str) -> None:
        for path in (self.object_path(object_key), self.metadata_path(object_key)):
            try:
                if path.is_file():
                    path.unlink()
            except OSError:
                pass

    def metadata_for(self, object_key: str) -> dict:
        key = _safe_key(object_key)
        meta_path = self.metadata_path(key)
        if meta_path.is_file():
            try:
                return json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        path = self.object_path(key)
        data = path.read_bytes()
        return self._build_metadata(
            object_key=key,
            content=data,
            content_type=_guess_type(path),
            metadata={},
        )

    def public_headers(self, object_key: str, metadata: dict) -> dict[str, str]:
        headers = {
            "Cache-Control": self._settings.cache_control,
            "X-Object-Key": object_key,
        }
        sha = metadata.get("sha256")
        if sha:
            headers["ETag"] = f'"{sha}"'
        return headers

    def _build_metadata(
        self,
        *,
        object_key: str,
        content: bytes,
        content_type: str,
        metadata: dict[str, str],
    ) -> dict:
        sha = hashlib.sha256(content).hexdigest()
        return {
            "object_key": object_key,
            "url": f"{self._settings.public_base_url}/v1/public/{object_key}",
            "content_type": content_type or "application/octet-stream",
            "size_bytes": len(content),
            "sha256": sha,
            "metadata": dict(metadata or {}),
        }

    def _write_metadata(self, object_key: str, payload: dict) -> None:
        path = self.metadata_path(object_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f".{path.name}.{uuid4().hex}.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    @staticmethod
    def _safe_path(root: Path, object_key: str) -> Path:
        key = _safe_key(object_key)
        path = (root / key).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError:
            raise _error(status.HTTP_400_BAD_REQUEST, "unsafe_key", "unsafe object key")
        return path


def _safe_key(raw: str) -> str:
    try:
        return validate_object_key(raw)
    except ObjectStorageError as exc:
        raise _error(status.HTTP_400_BAD_REQUEST, "unsafe_key", str(exc)) from exc


def _require_auth(settings: LocalStorageSettings, authorization: str | None) -> None:
    expected = f"bearer {settings.api_key}".lower()
    if (authorization or "").strip().lower() != expected:
        raise _error(status.HTTP_401_UNAUTHORIZED, "unauthorized", "invalid token")


def _parse_metadata(raw: str) -> dict[str, str]:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "invalid_metadata",
            "metadata must be JSON",
        ) from exc
    if not isinstance(data, dict):
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "invalid_metadata",
            "metadata must be an object",
        )
    return {str(k): str(v) for k, v in data.items()}


def _guess_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message, "retryable": False}},
    )


app = create_app()
