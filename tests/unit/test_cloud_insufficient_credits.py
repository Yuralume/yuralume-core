"""Gateway ``insufficient_credits`` must reach the player as HTTP 402.

The cloud gateway answers an out-of-credits call with a deliberate,
non-retryable ``402 {"error": {"code": "insufficient_credits"}}``. Before this
suite that refusal bubbled out of every foreground route as an opaque **500**
("chat failed") — precisely at the moment a player is most willing to top up.

Two layers are covered here:

1. **Adapters** — the LLM / image / TTS / video cloud gateway adapters must all
   leave an :class:`ExpectedCloudRefusal` carrying ``code`` on the raised
   error's ``__cause__`` chain, so the refusal survives being re-wrapped into
   each medium's own error type.
2. **Routes** — every player-triggered foreground entry point maps that code to
   ``402 {"detail": {"code": "insufficient_credits"}}``; any *other* refusal
   (e.g. ``entitlement_denied``) keeps its existing loud behaviour.
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
    insufficient_credits_detail,
    insufficient_credits_guard,
)
from kokoro_link.application.services.character_image_service import (
    GenerationFailedError,
)
from kokoro_link.contracts.tts import TTSError
from kokoro_link.infrastructure.llm.cloud_refusal import (
    ExpectedCloudRefusal,
    expected_refusal_code,
    refusal_from_response,
)


_TEST_USER_ID = "alice"
_CHARACTER_ID = "char-1"
_CONVERSATION_ID = "conv-1"


def _body(code: str = INSUFFICIENT_CREDITS_CODE) -> str:
    return json.dumps({
        "error": {
            "code": code,
            "message": "insufficient credits for this request",
            "retryable": False,
        },
    })


def _response(status_code: int = 402, code: str = INSUFFICIENT_CREDITS_CODE):
    request = httpx.Request("POST", "https://gateway.example/v1/chat/completions")
    return httpx.Response(status_code, request=request, text=_body(code))


def _refusal(code: str = INSUFFICIENT_CREDITS_CODE) -> ExpectedCloudRefusal:
    refusal = refusal_from_response(_response(code=code), _body(code))
    assert refusal is not None
    return refusal


# ── layer 1: refusal plumbing ─────────────────────────────────────────


def test_refusal_from_response_carries_code_and_reason() -> None:
    refusal = _refusal()

    assert refusal.code == INSUFFICIENT_CREDITS_CODE
    assert refusal.reason == "insufficient credits for this request"


def test_refusal_from_response_ignores_non_refusals() -> None:
    request = httpx.Request("POST", "https://gateway.example/v1/images/generations")
    retryable = httpx.Response(
        429,
        request=request,
        text=json.dumps({"error": {"code": "busy", "retryable": True}}),
    )

    assert refusal_from_response(retryable, retryable.text) is None


def test_expected_refusal_code_walks_the_cause_chain() -> None:
    # The image path re-wraps twice: provider error → service error.
    try:
        try:
            raise GenerationFailedError("image failed") from _refusal()
        except GenerationFailedError as exc:
            raise RuntimeError("outer") from exc
    except RuntimeError as exc:
        assert expected_refusal_code(exc) == INSUFFICIENT_CREDITS_CODE


def test_expected_refusal_code_is_none_for_plain_faults() -> None:
    assert expected_refusal_code(RuntimeError("boom")) is None
    assert expected_refusal_code(None) is None


def test_insufficient_credits_detail_ignores_other_refusal_codes() -> None:
    assert insufficient_credits_detail(_refusal("entitlement_denied")) is None
    assert insufficient_credits_detail(_refusal()) == {
        "code": INSUFFICIENT_CREDITS_CODE,
        "message": "insufficient credits for this request",
    }


def test_guard_reraises_unrelated_errors_untouched() -> None:
    with pytest.raises(GenerationFailedError, match="disk full"):
        with insufficient_credits_guard():
            raise GenerationFailedError("disk full")


# ── layer 2: chat routes ──────────────────────────────────────────────


class _CharacterService:
    async def get_character_entity(self, character_id: str, *, user_id: str = ""):
        return SimpleNamespace(id=character_id, user_id=user_id or _TEST_USER_ID)


class _RefusingChatService:
    def __init__(self, code: str = INSUFFICIENT_CREDITS_CODE) -> None:
        self._code = code

    async def send_message(self, payload, **_kwargs):  # noqa: ANN001
        raise _refusal(self._code)

    async def send_message_stream(self, payload, **_kwargs):  # noqa: ANN001
        raise _refusal(self._code)


class _StreamFinalizer:
    def __init__(self) -> None:
        self.releases = 0

    @property
    def conversation_id(self) -> str:
        return _CONVERSATION_ID

    async def finish(self, assistant_text: str):  # noqa: ANN201
        raise AssertionError("finish must not run when the stream is refused")

    async def release_turn_lease(self) -> None:
        self.releases += 1


class _MidStreamRefusingChatService:
    """The gateway refuses only once the token stream is pulled."""

    def __init__(self, finalizer: _StreamFinalizer, code: str) -> None:
        self._finalizer = finalizer
        self._code = code

    async def send_message_stream(self, payload, **_kwargs):  # noqa: ANN001
        code = self._code

        async def _stream():  # noqa: ANN202
            raise _refusal(code)
            yield ""  # pragma: no cover - generator marker

        return _stream(), self._finalizer


def _chat_client(chat_service) -> TestClient:  # noqa: ANN001
    from kokoro_link.api.routes.chat import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.container = SimpleNamespace(
        chat_service=chat_service,
        character_service=_CharacterService(),
        conversation_repository=None,
        turn_undo_service=None,
        object_storage=None,
        app_settings=None,
        operator_profile_repository=None,
    )
    return TestClient(app)


def _chat_payload() -> dict:
    return {
        "character_id": _CHARACTER_ID,
        "conversation_id": _CONVERSATION_ID,
        "message": "hi",
    }


@pytest.mark.parametrize(
    "path",
    ["/api/v1/chat/messages", "/api/v1/chat/messages/stream"],
)
def test_chat_insufficient_credits_maps_to_402(path: str) -> None:
    client = _chat_client(_RefusingChatService())

    response = client.post(path, json=_chat_payload())

    assert response.status_code == 402, response.text
    assert response.json()["detail"]["code"] == INSUFFICIENT_CREDITS_CODE


@pytest.mark.parametrize(
    "path",
    ["/api/v1/chat/messages", "/api/v1/chat/messages/stream"],
)
def test_chat_other_refusals_keep_bubbling(path: str) -> None:
    client = _chat_client(_RefusingChatService("entitlement_denied"))

    with pytest.raises(ExpectedCloudRefusal):
        client.post(path, json=_chat_payload())


def test_chat_stream_emits_an_error_frame_when_credits_run_out() -> None:
    finalizer = _StreamFinalizer()
    client = _chat_client(
        _MidStreamRefusingChatService(finalizer, INSUFFICIENT_CREDITS_CODE),
    )

    response = client.post("/api/v1/chat/messages/stream", json=_chat_payload())

    # Headers are already on the wire, so the refusal has to travel as an SSE
    # frame rather than a status code.
    assert response.status_code == 200, response.text
    frames = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and line[6:].strip() != "[DONE]"
    ]
    errors = [f["error"] for f in frames if "error" in f]
    assert errors == [{
        "code": INSUFFICIENT_CREDITS_CODE,
        "message": "insufficient credits for this request",
    }]
    assert response.text.rstrip().endswith("data: [DONE]")
    # The turn never finalized, so the conversation lease must be freed.
    assert finalizer.releases == 1


def test_chat_stream_still_propagates_other_refusals() -> None:
    finalizer = _StreamFinalizer()
    client = _chat_client(
        _MidStreamRefusingChatService(finalizer, "entitlement_denied"),
    )

    with pytest.raises(ExpectedCloudRefusal):
        client.post("/api/v1/chat/messages/stream", json=_chat_payload())


# ── layer 2: character image routes ───────────────────────────────────


class _RefusingImageService:
    def __init__(self, code: str = INSUFFICIENT_CREDITS_CODE) -> None:
        self._code = code

    def _fail(self) -> None:
        # Mirrors the real chain: cloud provider → ImageGenerationError →
        # GenerationFailedError, refusal reachable only via ``__cause__``.
        raise GenerationFailedError("image gateway error 402") from _refusal(
            self._code,
        )

    async def generate_portrait(self, character_id: str, **_kwargs):  # noqa: ANN003
        self._fail()

    async def generate_candidates(self, character_id: str, **_kwargs):  # noqa: ANN003
        self._fail()


def _image_client(image_service) -> TestClient:  # noqa: ANN001
    from kokoro_link.api.routes.characters import router

    container = SimpleNamespace(
        character_image_service=image_service,
        character_service=_CharacterService(),
    )
    app = FastAPI()
    app.dependency_overrides[get_container] = lambda: container
    app.dependency_overrides[get_current_user_id] = lambda: _TEST_USER_ID
    app.include_router(router, prefix="/api/v1")
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            f"/api/v1/characters/{_CHARACTER_ID}/images/generate",
            {"positive": "in the rain", "aspect": "portrait"},
        ),
        (
            f"/api/v1/characters/{_CHARACTER_ID}/images/candidates",
            {"positive": "in the rain", "aspect": "portrait", "count": 2},
        ),
    ],
)
def test_image_generation_insufficient_credits_maps_to_402(
    path: str, payload: dict,
) -> None:
    client = _image_client(_RefusingImageService())

    response = client.post(path, json=payload)

    assert response.status_code == 402, response.text
    assert response.json()["detail"]["code"] == INSUFFICIENT_CREDITS_CODE


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            f"/api/v1/characters/{_CHARACTER_ID}/images/generate",
            {"positive": "in the rain", "aspect": "portrait"},
        ),
        (
            f"/api/v1/characters/{_CHARACTER_ID}/images/candidates",
            {"positive": "in the rain", "aspect": "portrait", "count": 2},
        ),
    ],
)
def test_image_generation_other_failures_still_502(
    path: str, payload: dict,
) -> None:
    client = _image_client(_RefusingImageService("entitlement_denied"))

    response = client.post(path, json=payload)

    assert response.status_code == 502, response.text


# ── layer 2: branching drama session routes ───────────────────────────


class _RefusingDramaService:
    def __init__(self, code: str = INSUFFICIENT_CREDITS_CODE) -> None:
        self._code = code

    async def get(self, drama_id: str):  # noqa: ANN201
        return SimpleNamespace(id=drama_id, character_ids=(_CHARACTER_ID,))

    async def start_session(self, drama_id: str, **_kwargs):  # noqa: ANN003
        raise _refusal(self._code)

    async def interact_session(self, session_id: str, **_kwargs):  # noqa: ANN003
        raise _refusal(self._code)

    async def advance_session(self, session_id: str, **_kwargs):  # noqa: ANN003
        raise _refusal(self._code)


def _drama_client(service) -> TestClient:  # noqa: ANN001
    from kokoro_link.api.routes.branching_drama import router

    container = SimpleNamespace(
        branching_drama_service=service,
        character_service=None,
        operator_profile_service=None,
    )
    app = FastAPI()
    app.dependency_overrides[get_container] = lambda: container
    app.dependency_overrides[get_current_user_id] = lambda: _TEST_USER_ID
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


_DRAMA_PATHS = [
    ("/api/v1/branching-dramas/drama-1/sessions", None),
    (
        "/api/v1/branching-dramas/drama-1/sessions/s-1/interact",
        {"player_input": "look around"},
    ),
    ("/api/v1/branching-dramas/drama-1/sessions/s-1/advance", None),
]


@pytest.mark.parametrize(("path", "payload"), _DRAMA_PATHS)
def test_branching_drama_insufficient_credits_maps_to_402(
    path: str, payload: dict | None,
) -> None:
    client = _drama_client(_RefusingDramaService())

    response = client.post(path, json=payload)

    assert response.status_code == 402, response.text
    assert response.json()["detail"]["code"] == INSUFFICIENT_CREDITS_CODE


@pytest.mark.parametrize(("path", "payload"), _DRAMA_PATHS)
def test_branching_drama_other_refusals_keep_bubbling(
    path: str, payload: dict | None,
) -> None:
    client = _drama_client(_RefusingDramaService("entitlement_denied"))

    with pytest.raises(ExpectedCloudRefusal):
        client.post(path, json=payload)


# ── layer 2: studio fusion→arc adaptation ─────────────────────────────


class _FusionStoryServiceStub:
    async def get(self, story_id: str):  # noqa: ANN201
        return SimpleNamespace(id=story_id, character_ids=(_CHARACTER_ID,))


class _RefusingAdaptService:
    def __init__(self, code: str = INSUFFICIENT_CREDITS_CODE) -> None:
        self._code = code

    async def adapt(self, story_id: str, **_kwargs):  # noqa: ANN003
        raise _refusal(self._code)


def _fusion_client(adapt_service) -> TestClient:  # noqa: ANN001
    from kokoro_link.api.routes.fusion_story import router

    container = SimpleNamespace(
        fusion_story_service=_FusionStoryServiceStub(),
        fusion_to_arc_draft_service=adapt_service,
        character_service=None,
        operator_profile_service=None,
    )
    app = FastAPI()
    app.dependency_overrides[get_container] = lambda: container
    app.dependency_overrides[get_current_user_id] = lambda: _TEST_USER_ID
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def test_fusion_adapt_to_arc_insufficient_credits_maps_to_402() -> None:
    """The one synchronous fusion entry point (the rest are 202 + polling)."""
    client = _fusion_client(_RefusingAdaptService())

    response = client.post("/api/v1/fusion-stories/story-1/adapt-to-arc")

    assert response.status_code == 402, response.text
    assert response.json()["detail"]["code"] == INSUFFICIENT_CREDITS_CODE


def test_fusion_adapt_to_arc_other_refusals_keep_bubbling() -> None:
    client = _fusion_client(_RefusingAdaptService("entitlement_denied"))

    with pytest.raises(ExpectedCloudRefusal):
        client.post("/api/v1/fusion-stories/story-1/adapt-to-arc")


# ── layer 2: TTS route ────────────────────────────────────────────────


class _RefusingTTSService:
    enabled = True

    def __init__(self, code: str = INSUFFICIENT_CREDITS_CODE) -> None:
        self._code = code

    async def synthesize(self, *, character_id: str, text: str):
        raise TTSError("cloud TTS gateway error 402") from _refusal(self._code)


@pytest.mark.asyncio
async def test_tts_insufficient_credits_maps_to_402() -> None:
    from fastapi import HTTPException

    from kokoro_link.api.routes.tts import TTSSynthRequest, synthesize_character_tts

    with pytest.raises(HTTPException) as excinfo:
        await synthesize_character_tts(
            character_id=_CHARACTER_ID,
            payload=TTSSynthRequest(text="hello"),
            container=SimpleNamespace(
                nsfw_mode_service=None,
                tts_service=_RefusingTTSService(),
            ),
            current_user_id=_TEST_USER_ID,
            _owned_character_id=_CHARACTER_ID,
        )

    assert excinfo.value.status_code == 402
    assert excinfo.value.detail["code"] == INSUFFICIENT_CREDITS_CODE


@pytest.mark.asyncio
async def test_tts_other_failures_still_502() -> None:
    from fastapi import HTTPException

    from kokoro_link.api.routes.tts import TTSSynthRequest, synthesize_character_tts

    with pytest.raises(HTTPException) as excinfo:
        await synthesize_character_tts(
            character_id=_CHARACTER_ID,
            payload=TTSSynthRequest(text="hello"),
            container=SimpleNamespace(
                nsfw_mode_service=None,
                tts_service=_RefusingTTSService("entitlement_denied"),
            ),
            current_user_id=_TEST_USER_ID,
            _owned_character_id=_CHARACTER_ID,
        )

    assert excinfo.value.status_code == 502


# ── self-host parity ──────────────────────────────────────────────────


def test_self_host_chat_failures_are_unchanged() -> None:
    """No cloud gateway in play → no 402 path, identical bubbling."""

    class _LocalProviderChatService:
        async def send_message(self, payload, **_kwargs):  # noqa: ANN001
            raise httpx.ConnectError("comfy/ollama unreachable")

    client = _chat_client(_LocalProviderChatService())

    with pytest.raises(httpx.ConnectError):
        client.post("/api/v1/chat/messages", json=_chat_payload())
