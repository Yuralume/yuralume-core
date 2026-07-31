"""R9 close-out: the portrait and draft routes carry the player's own quote.

Chat closed R9 first (``quoted_price_cr`` on the turn body); ``image_portrait``
and ``character_draft`` were the two entry points left binding their charge to
Core's *process* price cache — a number that, across replicas or straight after
a back-office edit, this player may never have been shown.

What is pinned here is the plumbing the service-level tests cannot see:

* the JSON body field and the multipart form field both reach the service;
* omitting them keeps the pre-R9 behaviour byte for byte (``None``);
* a negative quote is rejected by the schema rather than travelling on;
* the draft route now sits inside the billing guard, so the two refusals that
  the quote makes reachable — ``402`` empty wallet, ``409`` price moved —
  arrive as structured answers instead of a 500.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.dependencies import get_container, get_current_user_id
from kokoro_link.api.routes._cloud_errors import (
    INSUFFICIENT_CREDITS_CODE,
    PRICE_CHANGED_CODE,
)
from kokoro_link.application.dto.character_draft import CharacterDraftResponse
from kokoro_link.contracts.cloud_action_billing import ActionPriceChanged
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.llm.cloud_refusal import refusal_from_response

_USER_ID = "alice"
_CHARACTER_ID = "char-1"


def _out_of_credits() -> Exception:
    """The gateway's real 402, built the way the adapters see it."""
    body = json.dumps({
        "error": {
            "code": INSUFFICIENT_CREDITS_CODE,
            "message": "insufficient credits for this request",
            "retryable": False,
        },
    })
    response = httpx.Response(
        402,
        request=httpx.Request("POST", "https://gateway.invalid/v1"),
        text=body,
    )
    refusal = refusal_from_response(response, body)
    assert refusal is not None
    return refusal


class _CharacterService:
    async def get_character_entity(self, character_id: str, *, user_id: str = ""):
        return SimpleNamespace(id=character_id, user_id=user_id or _USER_ID)


class _RecordingImageService:
    def __init__(self) -> None:
        self.calls: list[dict] = []

        self.candidate_calls: list[dict] = []

    async def generate_portrait(self, character_id: str, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        return self._character()

    async def generate_candidates(self, character_id: str, **kwargs):  # noqa: ANN003
        self.candidate_calls.append(kwargs)
        return self._character(), ["/uploads/c/1.png"]

    @staticmethod
    def _character() -> Character:
        return Character.create(
            name="Yui",
            summary="",
            personality=[], interests=[], speaking_style="", boundaries=[],
            state=CharacterState(
                emotion="neutral", affection=50, fatigue=0, trust=50,
                energy=100,
            ),
        )


class _RecordingDraftService:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._error = error

    async def generate(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return CharacterDraftResponse(name="Yui")


def _client(**services) -> TestClient:
    from kokoro_link.api.routes.characters import router

    container = SimpleNamespace(
        character_service=_CharacterService(), **services,
    )
    app = FastAPI()
    app.dependency_overrides[get_container] = lambda: container
    app.dependency_overrides[get_current_user_id] = lambda: _USER_ID
    app.include_router(router, prefix="/api/v1")
    return TestClient(app, raise_server_exceptions=False)


# --- image_portrait ---------------------------------------------------------


_PORTRAIT_PATH = f"/api/v1/characters/{_CHARACTER_ID}/images/generate"


def test_portrait_route_forwards_the_client_quote() -> None:
    images = _RecordingImageService()
    client = _client(character_image_service=images)

    response = client.post(
        _PORTRAIT_PATH,
        json={"positive": "cafe", "aspect": "portrait", "quoted_price_cr": 7.5},
    )

    assert response.status_code == 201, response.text
    assert images.calls[0]["quoted_price_cr"] == 7.5


def test_portrait_route_without_a_quote_is_unchanged() -> None:
    images = _RecordingImageService()
    client = _client(character_image_service=images)

    response = client.post(_PORTRAIT_PATH, json={"positive": "cafe"})

    assert response.status_code == 201, response.text
    assert images.calls[0]["quoted_price_cr"] is None


def test_portrait_route_rejects_a_negative_quote() -> None:
    # A quote below zero is not a price anyone was shown; refuse it at the
    # schema rather than letting it reach the wallet.
    images = _RecordingImageService()
    client = _client(character_image_service=images)

    response = client.post(
        _PORTRAIT_PATH, json={"positive": "cafe", "quoted_price_cr": -1},
    )

    assert response.status_code == 422
    assert images.calls == []


# --- image_portrait, as the picture button actually sends it ----------------
#
# The SPA's 產生 button posts a *candidate batch*, never ``/images/generate``.
# That is where the fixed charge has to bind, so the same three cases are
# pinned on the route the player really reaches.


_CANDIDATES_PATH = f"/api/v1/characters/{_CHARACTER_ID}/images/candidates"


def test_candidates_route_forwards_the_client_quote() -> None:
    images = _RecordingImageService()
    client = _client(character_image_service=images)

    response = client.post(
        _CANDIDATES_PATH,
        json={"positive": "cafe", "count": 4, "quoted_price_cr": 7.5},
    )

    assert response.status_code == 201, response.text
    # One quote for the whole batch: the button showed one number, and the
    # batch is one action however many candidates come back.
    assert images.candidate_calls[0]["quoted_price_cr"] == 7.5
    assert images.candidate_calls[0]["count"] == 4


def test_candidates_route_without_a_quote_is_unchanged() -> None:
    images = _RecordingImageService()
    client = _client(character_image_service=images)

    response = client.post(_CANDIDATES_PATH, json={"positive": "cafe"})

    assert response.status_code == 201, response.text
    assert images.candidate_calls[0]["quoted_price_cr"] is None


def test_candidates_route_rejects_a_negative_quote() -> None:
    images = _RecordingImageService()
    client = _client(character_image_service=images)

    response = client.post(
        _CANDIDATES_PATH, json={"positive": "cafe", "quoted_price_cr": -1},
    )

    assert response.status_code == 422
    assert images.candidate_calls == []


# --- character_draft --------------------------------------------------------


_DRAFT_PATH = "/api/v1/characters/draft"


def test_draft_route_forwards_the_client_quote() -> None:
    drafts = _RecordingDraftService()
    client = _client(character_draft_service=drafts)

    response = client.post(
        _DRAFT_PATH,
        data={"prompt": "一個溫柔的圖書館員", "quoted_price_cr": "2.25"},
    )

    assert response.status_code == 200, response.text
    assert drafts.calls[0]["quoted_price_cr"] == 2.25


def test_draft_route_without_a_quote_is_unchanged() -> None:
    drafts = _RecordingDraftService()
    client = _client(character_draft_service=drafts)

    response = client.post(_DRAFT_PATH, data={"prompt": "x"})

    assert response.status_code == 200, response.text
    assert drafts.calls[0]["quoted_price_cr"] is None


def test_draft_route_rejects_a_negative_quote() -> None:
    drafts = _RecordingDraftService()
    client = _client(character_draft_service=drafts)

    response = client.post(
        _DRAFT_PATH, data={"prompt": "x", "quoted_price_cr": "-1"},
    )

    assert response.status_code == 422
    assert drafts.calls == []


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (_out_of_credits(), 402, INSUFFICIENT_CREDITS_CODE),
        (
            ActionPriceChanged(
                action_key="character_draft",
                expected_price_cr=2.25,
                current_price_cr=3.0,
            ),
            409,
            PRICE_CHANGED_CODE,
        ),
    ],
)
def test_draft_route_maps_billing_refusals(
    error: Exception, status: int, code: str,
) -> None:
    # Both are answers the player can act on. Without the guard on this route
    # each would surface as a 500 at the exact moment they are most willing to
    # top up (or simply press the button again).
    client = _client(character_draft_service=_RecordingDraftService(error))

    response = client.post(_DRAFT_PATH, data={"prompt": "x"})

    assert response.status_code == status, response.text
    assert response.json()["detail"]["code"] == code
