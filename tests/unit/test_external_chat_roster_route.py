"""Service-to-service external-chat roster read route (LH2).

``GET /api/internal/v1/external-chat/roster`` — credential-gated
(``KOKORO_CLOUD_INTERNAL_CREDENTIALS`` rotation set, fail-closed), never
behind the operator JWT. Covers the credential-gate states (unconfigured →
503, bad secret / wrong caller / missing scope → 401), the opaque 404 for an
unresolvable pair, and a field-by-field 200 schema assertion.
"""

from __future__ import annotations

import asyncio

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.character_state import CharacterState

_PATH = "/api/internal/v1/external-chat/roster"
_TENANT = "tenant-A"
_ACCOUNT = "acct-1"
_OPERATOR_ID = "cloud:acct-1"
_CREDENTIAL = (
    "chan-kid|cloud-channel|yuralume-core|external-chat:roster-read|chan-secret"
)


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
    scope: str = "external-chat:roster-read",
) -> dict[str, str]:
    return {
        "X-Yuralume-Service-Token": token,
        "X-Yuralume-Service-Key-Id": key_id,
        "X-Yuralume-Service-Caller": caller,
        "X-Yuralume-Service-Audience": audience,
        "X-Yuralume-Service-Scope": scope,
    }


def _character(
    *,
    character_id: str = "c1",
    name: str = "Yui",
    image_urls: tuple[str, ...] = (),
) -> Character:
    state = CharacterState(
        emotion="calm", affection=50, fatigue=20, trust=50, energy=60,
    )
    return Character(
        id=character_id,
        name=name,
        summary="",
        user_id=_OPERATOR_ID,
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=state,
        image_urls=image_urls,
    )


def _seed(client, *, characters: list[Character] | None = None) -> None:
    container = client.app.state.container
    operators = container.operator_profile_repository
    character_repo = container.character_repository
    asyncio.run(
        operators.save(
            OperatorProfile(
                id=_OPERATOR_ID,
                display_name="Player",
                cloud_account_id=_ACCOUNT,
                cloud_tenant_id=_TENANT,
                auth_provider="cloud",
            ),
        ),
    )
    for character in characters or []:
        asyncio.run(character_repo.save(character))


def _get(client, headers, *, tenant_id=_TENANT, account_id=_ACCOUNT):
    return client.get(
        _PATH,
        params={"tenant_id": tenant_id, "account_id": account_id},
        headers=headers,
    )


def _assert_error_envelope(resp, *, status_code: int) -> None:
    assert resp.status_code == status_code
    body = resp.json()
    # M11: the credential-gate + validation failures share the SAME ErrorEnvelope
    # shape as every other external-chat error, never FastAPI's {"detail": ...}.
    assert set(body.keys()) == {"error"}
    assert set(body["error"].keys()) == {"code", "message", "retryable"}


def test_missing_credential_config_returns_503(monkeypatch) -> None:
    client = _client(monkeypatch, credentials=None)
    assert _get(client, _headers()).status_code == 503


# --- M11: fail-closed + validation bodies are the ErrorEnvelope --------------


def test_m11_unconfigured_503_body_is_error_envelope(monkeypatch) -> None:
    client = _client(monkeypatch, credentials=None)
    _assert_error_envelope(_get(client, _headers()), status_code=503)


def test_m11_bad_credential_401_body_is_error_envelope(monkeypatch) -> None:
    client = _client(monkeypatch)
    resp = _get(client, _headers(token="wrong-secret"))
    _assert_error_envelope(resp, status_code=401)


def test_m11_request_validation_422_body_is_error_envelope(monkeypatch) -> None:
    client = _client(monkeypatch)
    # Valid credentials, but a required query param is missing → 422; the body
    # must be the ErrorEnvelope, not FastAPI's default {"detail": [...]}.
    resp = client.get(_PATH, params={"account_id": _ACCOUNT}, headers=_headers())
    _assert_error_envelope(resp, status_code=422)
    assert resp.json()["error"]["code"] == "invalid_request"


def test_m11_validation_handler_preserves_default_for_non_external_paths() -> None:
    # The RequestValidationError handler is registered app-wide but must be a
    # no-op for any non-external-chat path (delegates to FastAPI's default body),
    # so global behaviour is unchanged.
    import asyncio
    import json

    from fastapi.exceptions import RequestValidationError
    from starlette.requests import Request

    from kokoro_link.api.routes.external_chat import _validation_error_handler

    exc = RequestValidationError([])

    def _request(path: str) -> Request:
        return Request(
            {"type": "http", "method": "POST", "path": path, "headers": []},
        )

    external = asyncio.run(
        _validation_error_handler(
            _request("/api/internal/v1/external-chat/turns"), exc,
        ),
    )
    assert external.status_code == 422
    assert "error" in json.loads(bytes(external.body))

    other = asyncio.run(
        _validation_error_handler(_request("/api/v1/characters"), exc),
    )
    assert other.status_code == 422
    # Default FastAPI shape — NOT the envelope.
    assert "detail" in json.loads(bytes(other.body))


def test_bad_secret_returns_401(monkeypatch) -> None:
    client = _client(monkeypatch)
    assert _get(client, _headers(token="wrong-secret")).status_code == 401


def test_wrong_caller_returns_401(monkeypatch) -> None:
    client = _client(monkeypatch)
    # A valid Cloud→Core caller for other internal routes, but not roster.
    assert _get(client, _headers(caller="cloud-user")).status_code == 401


def test_missing_scope_returns_401(monkeypatch) -> None:
    client = _client(monkeypatch)
    assert _get(client, _headers(scope="freeze:write")).status_code == 401


def test_unknown_pair_returns_opaque_404(monkeypatch) -> None:
    client = _client(monkeypatch)
    _seed(client, characters=[_character()])
    # Right credential, wrong tenant → indistinguishable from missing.
    assert _get(client, _headers(), tenant_id="tenant-OTHER").status_code == 404


def test_valid_request_returns_roster_schema(monkeypatch) -> None:
    client = _client(monkeypatch)
    _seed(
        client,
        characters=[
            _character(
                character_id="c1",
                name="Yui",
                image_urls=("/uploads/users/u1/portrait.png",),
            ),
        ],
    )

    resp = _get(client, _headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["tenant_id"] == _TENANT
    assert body["account_id"] == _ACCOUNT
    assert len(body["characters"]) == 1
    character = body["characters"][0]
    assert character["character_id"] == "c1"
    assert character["display_name"] == "Yui"
    assert character["background_frozen"] is False
    assert character["avatar_object_ref"] == "users/u1/portrait.png"
    assert isinstance(character["presentation_version"], str)
    assert len(character["presentation_version"]) == 16
    # Schema is exactly the OpenAPI RosterCharacter field set.
    assert set(character.keys()) == {
        "character_id",
        "display_name",
        "background_frozen",
        "avatar_object_ref",
        "presentation_version",
    }
