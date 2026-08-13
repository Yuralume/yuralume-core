"""Cloud→Core official-card translation channel.

``/api/internal/v1/cloud/official-cards/translate`` — credential-gated with
its own ``official-cards:translate`` scope, never behind the operator JWT.
What is covered:

* the gate's states (unconfigured → 503, a wrong secret → 401, a credential
  minted for the showcase scope → 401, no scope header at all → 401),
* that the endpoint is **stateless** for card data: no character id and no
  database — the request carries its own text and the response is that
  text in another language; tenant/account are routing-only identity for
  the Gateway and are never used to load the card,
* the ``notes`` contract: a field the model dropped comes back as source
  text with an entry naming it, so the console can offer a retry for that
  field instead of failing the whole card.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Sequence

from fastapi.testclient import TestClient

from kokoro_link.application.services.cloud_identity_context import (
    current_cloud_actor,
)

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.application.services.official_card_translate import (
    REASON_LLM_UNAVAILABLE,
    REASON_MISSING,
    OfficialCardTranslator,
)
from kokoro_link.contracts.llm import ChatModelPort

_URL = "/api/internal/v1/cloud/official-cards/translate"
_SCOPE = "official-cards:translate"
_CREDENTIALS = (
    "core-kid|cloud-user|yuralume-core"
    "|freeze:write,tier:write,showcase:read,official-cards:translate|core-secret"
)


class _ScriptedModel(ChatModelPort):
    provider_id = "scripted"
    supports_vision = False

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[str] = []
        self.actors = []

    async def generate(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> str:
        self.calls.append(prompt)
        self.actors.append(current_cloud_actor())
        return self.response

    async def generate_stream(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> AsyncIterator[str]:
        yield await self.generate(prompt, image_urls=image_urls, model=model)

    async def list_models(self) -> list[str]:
        return ["scripted-model"]


def _client(monkeypatch, *, credentials: str | None = _CREDENTIALS) -> TestClient:
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")
    monkeypatch.setenv("CONFIG_ENCRYPTION_KEY", "unit-test-official-cards-key")
    monkeypatch.delenv("KOKORO_CLOUD_INTERNAL_TOKENS", raising=False)
    if credentials is None:
        monkeypatch.delenv("KOKORO_CLOUD_INTERNAL_CREDENTIALS", raising=False)
    else:
        monkeypatch.setenv("KOKORO_CLOUD_INTERNAL_CREDENTIALS", credentials)

    from kokoro_link.api.app import create_app

    return TestClient(create_app())


def _headers(scope: str = _SCOPE) -> dict[str, str]:
    return {
        "X-Yuralume-Service-Token": "core-secret",
        "X-Yuralume-Service-Key-Id": "core-kid",
        "X-Yuralume-Service-Caller": "cloud-user",
        "X-Yuralume-Service-Audience": "yuralume-core",
        "X-Yuralume-Service-Scope": scope,
    }


def _body(payload: dict[str, object], target_language: str = "en-US") -> dict:
    return {
        "payload": payload,
        "target_language": target_language,
        "routing_identity": {
            "tenant_id": "tenant-owner",
            "account_id": "account-owner",
            "tenant_tier": "internal_test",
        },
    }


def _wire_model(monkeypatch, response: str) -> _ScriptedModel:
    """Put a scripted model behind the endpoint's translator.

    The route builds its translator from the container at request time, so
    replacing the builder is enough — no provider plumbing needed.
    """
    model = _ScriptedModel(response)
    monkeypatch.setattr(
        "kokoro_link.api.routes.internal_cloud_official_cards"
        ".build_official_card_translator",
        lambda container: OfficialCardTranslator(ModelResolver(model=model)),
    )
    return model


def test_unconfigured_channel_is_503(monkeypatch) -> None:
    client = _client(monkeypatch, credentials=None)

    response = client.post(
        _URL,
        json=_body({"name": "美緒"}),
        headers=_headers(),
    )

    assert response.status_code == 503


def test_wrong_secret_is_401(monkeypatch) -> None:
    client = _client(monkeypatch)
    headers = _headers() | {"X-Yuralume-Service-Token": "not-the-secret"}

    response = client.post(
        _URL,
        json=_body({"name": "美緒"}),
        headers=headers,
    )

    assert response.status_code == 401


def test_a_credential_that_names_no_scope_at_all_is_401(monkeypatch) -> None:
    """The secret alone is not authorization.

    A caller that presents a valid key id and secret but no
    ``X-Yuralume-Service-Scope`` has asked for nothing, and this router's
    whole authorization story *is* the scope check — there is no tenant, no
    lookup, nothing else standing between the request and the deployment's
    model budget. So "unscoped" has to fail closed rather than fall through
    to whatever the credential happens to be allowed to do.
    """
    client = _client(monkeypatch)
    headers = _headers()
    del headers["X-Yuralume-Service-Scope"]

    response = client.post(
        _URL,
        json=_body({"name": "美緒"}),
        headers=headers,
    )

    assert response.status_code == 401


def test_a_showcase_scoped_credential_cannot_spend_models_here(monkeypatch) -> None:
    """Scope separation is the point: reading a character's feed and
    burning the deployment's models on catalog prose are different
    powers, even though both are ops calls from the same control plane."""
    client = _client(monkeypatch)

    response = client.post(
        _URL,
        json=_body({"name": "美緒"}),
        headers=_headers(scope="showcase:read"),
    )

    assert response.status_code == 401


def test_translates_the_payload_and_passes_the_rest_through(monkeypatch) -> None:
    client = _client(monkeypatch)
    model = _wire_model(
        monkeypatch,
        json.dumps(
            {
                "name": "Mio",
                "summary": "A college student working at a cafe.",
                "title": "The Seaside Cafe",
                "tags": ["daily", "healing"],
            },
            ensure_ascii=False,
        ),
    )

    response = client.post(
        _URL,
        json=_body(
            {
                "name": "美緒",
                "summary": "咖啡廳打工的女大生",
                "title": "海邊的咖啡廳",
                "tags": ["日常", "療癒"],
                "author": "Yuralume",
            },
        ),
        headers=_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["payload"] == {
        "name": "Mio",
        "summary": "A college student working at a cafe.",
        "title": "The Seaside Cafe",
        "tags": ["daily", "healing"],
        "author": "Yuralume",
    }
    assert body["notes"] == []
    assert "Target language: en-US" in model.calls[0]
    actor = model.actors[0]
    assert actor is not None
    assert actor.gateway_identity is not None
    assert actor.gateway_identity.tenant_id == "tenant-owner"
    assert actor.gateway_identity.account_id == "account-owner"
    assert actor.gateway_identity.tenant_tier == "internal_test"


def test_nested_card_fields_cross_the_internal_route(monkeypatch) -> None:
    client = _client(monkeypatch)
    _wire_model(
        monkeypatch,
        json.dumps(
            {
                "companions": [
                    {
                        "name": "Rinka Tachibana",
                        "role": "Childhood friend",
                        "brief_profile": "A dependable teammate.",
                        "relationship_snippet": "They grew up next door.",
                        "personality_sketch": ["direct", "caring"],
                    },
                ],
                "personality_type": {
                    "rationale": "She is energized by helping friends.",
                    "consistency_notes": ["Acts before overthinking."],
                },
            },
            ensure_ascii=False,
        ),
    )

    response = client.post(
        _URL,
        json=_body(
            {
                "companions": [
                    {
                        "name": "橘凜香",
                        "role": "青梅竹馬",
                        "brief_profile": "可靠的隊友。",
                        "relationship_snippet": "兩人從小住在隔壁。",
                        "personality_sketch": ["直率", "體貼"],
                    },
                ],
                "personality_type": {
                    "code": "ENFP",
                    "rationale": "幫助朋友會讓她充滿活力。",
                    "consistency_notes": ["總是先行動再思考。"],
                },
            },
        ),
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.json()["payload"]["companions"][0]["name"] == "Rinka Tachibana"
    assert response.json()["payload"]["personality_type"] == {
        "code": "ENFP",
        "rationale": "She is energized by helping friends.",
        "consistency_notes": ["Acts before overthinking."],
    }


def test_a_field_the_model_dropped_comes_back_as_source_text_with_a_note(
    monkeypatch,
) -> None:
    client = _client(monkeypatch)
    _wire_model(monkeypatch, json.dumps({"name": "Mio"}))

    response = client.post(
        _URL,
        json=_body({"name": "美緒", "description": "適合日常閒聊。"}),
        headers=_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["payload"]["name"] == "Mio"
    assert body["payload"]["description"] == "適合日常閒聊。"
    assert body["notes"] == [{"field": "description", "reason": REASON_MISSING}]


def test_an_unrouted_deployment_answers_200_with_untranslated_fields(
    monkeypatch,
) -> None:
    """Fail-soft, deliberately: the console shows the owner *why* the card
    is untranslated and lets them publish anyway (plan §3.4 — a missing
    route must never block an upload)."""
    client = _client(monkeypatch)

    response = client.post(
        _URL,
        json=_body({"name": "美緒"}),
        headers=_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["payload"] == {"name": "美緒"}
    assert body["notes"] == [{"field": "name", "reason": REASON_LLM_UNAVAILABLE}]


def test_missing_target_language_is_rejected(monkeypatch) -> None:
    client = _client(monkeypatch)

    response = client.post(
        _URL,
        json=_body({"name": "美緒"}, "  "),
        headers=_headers(),
    )

    assert response.status_code == 422


def test_an_oversized_payload_is_rejected_at_the_edge(monkeypatch) -> None:
    client = _client(monkeypatch)

    response = client.post(
        _URL,
        json=_body({"summary": "字" * 40_001}),
        headers=_headers(),
    )

    assert response.status_code == 422


def test_missing_routing_identity_is_rejected_at_the_edge(monkeypatch) -> None:
    client = _client(monkeypatch)
    _wire_model(monkeypatch, json.dumps({"name": "Mio"}))

    response = client.post(
        _URL,
        json={"payload": {"name": "美緒"}, "target_language": "en-US"},
        headers=_headers(),
    )

    assert response.status_code == 422
