from __future__ import annotations

from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]


def _dockerfile(path: str) -> str:
    return (_ROOT / path).read_text(encoding="utf-8")


def test_runtime_base_images_are_immutable_and_security_hardened() -> None:
    app = _dockerfile("docker/app/Dockerfile")
    storage = _dockerfile("docker/storage-local/Dockerfile")
    postgres = _dockerfile("docker/postgres/Dockerfile")
    whatsapp = _dockerfile("docker/whatsapp-sidecar/Dockerfile")

    for dockerfile in (app, storage, postgres, whatsapp):
        for line in dockerfile.splitlines():
            if line.startswith("FROM "):
                assert "@sha256:" in line

    assert "python3.13-trixie-slim@sha256:" in app
    assert "uv cache clean" in app
    assert "uv cache clean" in storage
    assert "go install github.com/tianon/gosu@1.19" in postgres
    assert "COPY --from=gosu-builder /go/bin/gosu" in postgres
    assert "rm -rf /usr/local/lib/node_modules/npm" in whatsapp
