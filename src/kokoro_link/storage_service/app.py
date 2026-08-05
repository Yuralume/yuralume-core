from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
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

from kokoro_link.contracts.object_storage import (
    EPHEMERAL_OBJECT_KEY_PREFIXES,
    ObjectStorageError,
)
from kokoro_link.infrastructure.storage.keys import validate_object_key

_LOGGER = logging.getLogger(__name__)

# Object keys under these prefixes are staging scratch space, not durable
# storage: a create-character draft's reference image lands as
# ``draft-uploads/{user_id}/{uuid}`` and the caller deletes it best-effort once
# the draft is finalised. A crashed process or a failed delete leaves an
# orphan behind, and nothing under these prefixes should ever outlive a few
# minutes — so the service sweeps them on a TTL as a backstop. The set itself
# is the port's, shared with the writer that builds these keys and the variant
# decorator that skips them; a sweeper with its own copy of the list is a
# sweeper that can end up deleting a prefix nothing writes (or worse, not
# sweeping one that does).
EPHEMERAL_PREFIXES: tuple[str, ...] = EPHEMERAL_OBJECT_KEY_PREFIXES
EPHEMERAL_TTL_ENV = "YURALUME_STORAGE_EPHEMERAL_TTL_SECONDS"
DEFAULT_EPHEMERAL_TTL_SECONDS = 3600
# A staged object must outlive any in-flight call that is still relying on it:
# the cloud gateway's own read timeout is 300s, so an upstream provider can
# legitimately still be fetching the URL that long after the write. Two times
# that is the floor — a TTL under it turns the backstop into a sweeper that
# deletes reference images out from under live requests.
MIN_EPHEMERAL_TTL_SECONDS = 600
MIN_SWEEP_INTERVAL_SECONDS = 300


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
    ephemeral_ttl_seconds: int = DEFAULT_EPHEMERAL_TTL_SECONDS

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
            ephemeral_ttl_seconds=resolve_ephemeral_ttl_seconds(
                os.getenv(EPHEMERAL_TTL_ENV),
            ),
        )


def create_app() -> FastAPI:
    settings = LocalStorageSettings.from_env()
    store = _LocalVolumeStore(settings)

    async def sweep_once() -> None:
        # ``sweep_ephemeral`` already swallows per-file failures and never
        # raises, so this guard only covers the genuinely unexpected. It has to
        # exist anyway: an exception escaping into the loop below would kill the
        # task and silently disable the backstop for the process' lifetime.
        try:
            await asyncio.to_thread(store.sweep_ephemeral)
        except Exception:  # noqa: BLE001 - backstop must outlive any one round
            _LOGGER.warning("ephemeral sweep round failed", exc_info=True)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await sweep_once()
        interval = sweep_interval_seconds(settings.ephemeral_ttl_seconds)

        async def sweep_loop() -> None:
            while True:
                await asyncio.sleep(interval)
                await sweep_once()

        task = asyncio.create_task(sweep_loop())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(
        title="Yuralume Local Object Storage",
        version="0.1.0",
        lifespan=lifespan,
    )

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

    def sweep_ephemeral(self) -> int:
        """Drop expired objects under the ephemeral prefixes. Never raises."""
        return sweep_ephemeral_objects(
            root=self._settings.root,
            ttl_seconds=self._settings.ephemeral_ttl_seconds,
        )

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


def resolve_ephemeral_ttl_seconds(raw: str | None) -> int:
    """Parse the ephemeral TTL env value, falling back to the default.

    An unparseable or non-positive value is a misconfiguration, not a request
    to disable the sweep: the prefixes hold user-uploaded images that nothing
    else is guaranteed to clean up, so we warn and keep the default TTL.

    A positive but *too small* value is the more dangerous misconfiguration,
    because it looks like it works: the sweep runs, and every so often it
    deletes a reference image an upstream provider is still fetching, turning a
    charged action into an inexplicable intermittent failure. Anything under
    :data:`MIN_EPHEMERAL_TTL_SECONDS` is clamped up to it.
    """
    text = (raw or "").strip()
    if not text:
        return DEFAULT_EPHEMERAL_TTL_SECONDS
    try:
        value = int(text)
    except ValueError:
        _LOGGER.warning(
            "%s=%r is not an integer; using default %ds",
            EPHEMERAL_TTL_ENV,
            raw,
            DEFAULT_EPHEMERAL_TTL_SECONDS,
        )
        return DEFAULT_EPHEMERAL_TTL_SECONDS
    if value <= 0:
        _LOGGER.warning(
            "%s=%r must be positive; using default %ds",
            EPHEMERAL_TTL_ENV,
            raw,
            DEFAULT_EPHEMERAL_TTL_SECONDS,
        )
        return DEFAULT_EPHEMERAL_TTL_SECONDS
    if value < MIN_EPHEMERAL_TTL_SECONDS:
        _LOGGER.warning(
            "%s=%r is below the %ds floor (a staged object must outlive any "
            "in-flight upstream fetch of its URL); using %ds",
            EPHEMERAL_TTL_ENV,
            raw,
            MIN_EPHEMERAL_TTL_SECONDS,
            MIN_EPHEMERAL_TTL_SECONDS,
        )
        return MIN_EPHEMERAL_TTL_SECONDS
    return value


def sweep_interval_seconds(ttl_seconds: int) -> float:
    """How often the background sweep runs, floored so a tiny TTL can't spin."""
    return float(max(ttl_seconds / 2, MIN_SWEEP_INTERVAL_SECONDS))


def sweep_ephemeral_objects(
    *,
    root: Path,
    ttl_seconds: int,
    prefixes: tuple[str, ...] = EPHEMERAL_PREFIXES,
    now: float | None = None,
) -> int:
    """Delete files older than ``ttl_seconds`` under the ephemeral prefixes.

    Walks both the object and the metadata tree, and touches nothing outside
    ``prefixes``. Best-effort throughout and never raises — a sweep that
    propagates an error takes the whole backstop down with it, which is a worse
    failure than one file surviving until the next round.

    Returns the number of files removed.
    """
    cutoff = (time.time() if now is None else now) - ttl_seconds
    removed = 0
    for base_name in ("objects", "metadata"):
        for prefix in prefixes:
            try:
                target = _resolve_prefix_dir(root / base_name, prefix)
                if target is None:
                    continue
                removed += _sweep_expired_files(target, cutoff)
            except Exception:  # noqa: BLE001 - see docstring
                _LOGGER.warning(
                    "ephemeral sweep failed for prefix %r under %s",
                    prefix,
                    base_name,
                    exc_info=True,
                )
    if removed:
        _LOGGER.info(
            "ephemeral sweep removed %d expired file(s) under %s (ttl=%ds)",
            removed,
            ", ".join(prefixes),
            ttl_seconds,
        )
    return removed


def _resolve_prefix_dir(base: Path, prefix: str) -> Path | None:
    """Resolve ``base/prefix``, or ``None`` when it is absent or escapes base.

    Same containment discipline as ``_LocalVolumeStore._safe_path``: the sweep
    deletes files, so it must never be able to walk out of the prefix even if
    someone adds a malformed entry to ``EPHEMERAL_PREFIXES``.
    """
    relative = prefix.strip("/")
    if not relative:
        return None
    try:
        base_resolved = base.resolve()
        target = (base_resolved / relative).resolve()
        target.relative_to(base_resolved)
    except (OSError, ValueError):
        _LOGGER.warning("ephemeral prefix %r is not inside %s", prefix, base)
        return None
    if target == base_resolved or not target.is_dir():
        return None
    return target


def _sweep_expired_files(target: Path, cutoff: float) -> int:
    removed = 0
    try:
        walked = list(os.walk(target, topdown=False, followlinks=False))
    except OSError as exc:
        _LOGGER.warning("ephemeral sweep could not walk %s: %r", target, exc)
        return 0
    for dirpath, _dirnames, filenames in walked:
        for name in filenames:
            path = Path(dirpath) / name
            try:
                # lstat, not stat: a dangling symlink still deserves removal and
                # we must not follow one out of the prefix to read its mtime.
                mtime = path.lstat().st_mtime
            except OSError:
                continue
            if mtime > cutoff:
                continue
            try:
                path.unlink()
            except OSError as exc:
                _LOGGER.warning(
                    "ephemeral sweep could not delete %s: %r", path, exc,
                )
                continue
            removed += 1
        if Path(dirpath) != target:
            # Best-effort tidy-up. rmdir only succeeds on an already-empty
            # directory, so a still-populated or racing one is simply left.
            with suppress(OSError):
                os.rmdir(dirpath)
    return removed


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
