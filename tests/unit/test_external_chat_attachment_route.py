"""Service-to-service external-chat attachment write route (LH2).

``POST /api/internal/v1/external-chat/attachments`` — credential-gated
(``KOKORO_CLOUD_INTERNAL_CREDENTIALS``, fail-closed), multipart. Covers the
gate states (unconfigured 503, bad secret / missing scope 401), the multipart
happy path with a field-by-field ``AttachmentRef`` assertion, the 413/415
error bodies, and the opaque-404 body consistency between an unknown account
and an unknown character.
"""

from __future__ import annotations

import asyncio

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.character_state import CharacterState

_PATH = "/api/internal/v1/external-chat/attachments"
_TENANT = "tenant-A"
_ACCOUNT = "acct-1"
_CHARACTER_ID = "c1"
_OPERATOR_ID = "cloud:acct-1"
_CREDENTIAL = (
    "chan-kid|cloud-channel|yuralume-core|"
    "external-chat:attachment-write|chan-secret"
)


def png_bytes(width: int, height: int) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr_payload = (
        width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
    )
    ihdr = (
        len(ihdr_payload).to_bytes(4, "big") + b"IHDR" + ihdr_payload
        + b"\x00\x00\x00\x00"
    )
    return signature + ihdr


def _configure_env(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")
    monkeypatch.setenv("CONFIG_ENCRYPTION_KEY", "unit-test-external-chat-key")
    monkeypatch.delenv("KOKORO_CLOUD_INTERNAL_TOKENS", raising=False)


def _client(monkeypatch, *, credentials: str | None = _CREDENTIAL):
    from fastapi.testclient import TestClient

    from kokoro_link.api.app import create_app

    _configure_env(monkeypatch)
    if credentials is None:
        monkeypatch.delenv("KOKORO_CLOUD_INTERNAL_CREDENTIALS", raising=False)
    else:
        monkeypatch.setenv("KOKORO_CLOUD_INTERNAL_CREDENTIALS", credentials)
    return TestClient(create_app())


def _headers(
    *,
    token: str = "chan-secret",
    key_id: str = "chan-kid",
    caller: str = "cloud-channel",
    audience: str = "yuralume-core",
    scope: str = "external-chat:attachment-write",
) -> dict[str, str]:
    return {
        "X-Yuralume-Service-Token": token,
        "X-Yuralume-Service-Key-Id": key_id,
        "X-Yuralume-Service-Caller": caller,
        "X-Yuralume-Service-Audience": audience,
        "X-Yuralume-Service-Scope": scope,
    }


def _character(character_id: str = _CHARACTER_ID) -> Character:
    state = CharacterState(
        emotion="calm", affection=50, fatigue=20, trust=50, energy=60,
    )
    return Character(
        id=character_id,
        name="Yui",
        summary="",
        user_id=_OPERATOR_ID,
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=state,
        image_urls=(),
    )


def _seed(client) -> None:
    container = client.app.state.container
    asyncio.run(
        container.operator_profile_repository.save(
            OperatorProfile(
                id=_OPERATOR_ID,
                display_name="Player",
                cloud_account_id=_ACCOUNT,
                cloud_tenant_id=_TENANT,
                auth_provider="cloud",
            ),
        ),
    )
    asyncio.run(container.character_repository.save(_character()))


def _post(
    client,
    headers,
    *,
    tenant_id: str = _TENANT,
    account_id: str = _ACCOUNT,
    character_id: str = _CHARACTER_ID,
    data: bytes | None = None,
    content_type: str = "image/png",
):
    payload = data if data is not None else png_bytes(4, 4)
    return client.post(
        _PATH,
        data={
            "tenant_id": tenant_id,
            "account_id": account_id,
            "character_id": character_id,
        },
        files={"file": ("upload.bin", payload, content_type)},
        headers=headers,
    )


# --- credential gate -------------------------------------------------------


def test_missing_credential_config_returns_503(monkeypatch) -> None:
    client = _client(monkeypatch, credentials=None)
    _seed(client)
    assert _post(client, _headers()).status_code == 503


def test_bad_secret_returns_401(monkeypatch) -> None:
    client = _client(monkeypatch)
    _seed(client)
    assert _post(client, _headers(token="wrong")).status_code == 401


def test_missing_scope_returns_401(monkeypatch) -> None:
    client = _client(monkeypatch)
    _seed(client)
    resp = _post(client, _headers(scope="external-chat:roster-read"))
    assert resp.status_code == 401


# --- happy path ------------------------------------------------------------


def test_valid_request_returns_attachment_ref(monkeypatch) -> None:
    import hashlib

    client = _client(monkeypatch)
    _seed(client)
    data = png_bytes(4, 4)

    resp = _post(client, _headers(), data=data)

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {
        "object_ref", "media_type", "size_bytes", "sha256",
    }
    assert body["media_type"] == "image/png"
    assert body["size_bytes"] == len(data)
    assert body["sha256"] == hashlib.sha256(data).hexdigest()
    assert body["object_ref"].startswith(
        "users/cloud-acct-1/messaging-inbound/",
    )
    # object_ref is an opaque object key, never a URL.
    assert "://" not in body["object_ref"]
    assert not body["object_ref"].startswith("/")


# --- error bodies ----------------------------------------------------------


def test_oversize_returns_413_error_body(monkeypatch) -> None:
    client = _client(monkeypatch)
    _seed(client)
    data = png_bytes(4, 4) + b"\x00" * (8 * 1024 * 1024)
    resp = _post(client, _headers(), data=data)
    assert resp.status_code == 413
    # M11: aligned OpenAPI ErrorEnvelope, not the legacy ``{"error": str}``.
    body = resp.json()
    assert body["error"]["code"] == "payload_too_large"
    assert body["error"]["retryable"] is False
    assert isinstance(body["error"]["message"], str)


def test_content_mismatch_returns_415_error_body(monkeypatch) -> None:
    client = _client(monkeypatch)
    _seed(client)
    # Declares PNG but sends GIF bytes.
    gif = b"GIF89a" + (4).to_bytes(2, "little") + (4).to_bytes(2, "little")
    resp = _post(client, _headers(), data=gif, content_type="image/png")
    assert resp.status_code == 415
    # M11: aligned OpenAPI ErrorEnvelope, not the legacy ``{"error": str}``.
    body = resp.json()
    assert body["error"]["code"] == "unsupported_media_type"
    assert body["error"]["retryable"] is False


def test_unknown_account_and_character_share_opaque_404_body(monkeypatch) -> None:
    client = _client(monkeypatch)
    _seed(client)

    unknown_account = _post(client, _headers(), tenant_id="tenant-OTHER")
    unknown_character = _post(client, _headers(), character_id="ghost")

    assert unknown_account.status_code == 404
    assert unknown_character.status_code == 404
    # Opaque: byte-identical body so the caller cannot tell which half failed.
    assert unknown_account.json() == unknown_character.json()
    # M11: the opaque 404 is an aligned ErrorEnvelope, not FastAPI's {"detail"}.
    body = unknown_account.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["retryable"] is False
