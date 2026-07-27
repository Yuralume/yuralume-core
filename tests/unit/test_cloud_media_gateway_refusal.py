"""The image / TTS / video cloud gateway adapters must preserve refusal codes.

Each medium re-wraps upstream failures into its own error type
(``ImageGenerationError`` / ``TTSError`` / ``VideoGenerationError``), which used
to erase *why* the cloud said no — a deliberate ``insufficient_credits`` became
indistinguishable from a broken upstream. Attaching the typed
:class:`ExpectedCloudRefusal` as the ``__cause__`` keeps the machine-readable
code reachable at the HTTP boundary without changing any of the error types the
application layer already catches.
"""

from __future__ import annotations

import json

import httpx
import pytest

from kokoro_link.contracts.cloud_gateway import CloudGatewayIdentity
from kokoro_link.contracts.image_provider import ImageGenerationError
from kokoro_link.contracts.tts import TTSError, TTSRequest
from kokoro_link.contracts.video_provider import VideoGenerationError
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.image.cloud_gateway_provider import (
    CloudGatewayImageProvider,
)
from kokoro_link.infrastructure.llm.cloud_refusal import expected_refusal_code
from kokoro_link.infrastructure.tts.cloud_gateway import CloudGatewayTTSAdapter
from kokoro_link.infrastructure.video.cloud_gateway_provider import (
    CloudGatewayVideoProvider,
)

_INSUFFICIENT = "insufficient_credits"


class _MockAsyncClient(httpx.AsyncClient):
    def __init__(self, handler, **kwargs) -> None:  # noqa: ANN001
        super().__init__(
            transport=httpx.MockTransport(handler),
            timeout=kwargs.get("timeout"),
        )


def _refusal_handler(code: str = _INSUFFICIENT, status_code: int = 402):
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            text=json.dumps({
                "error": {
                    "code": code,
                    "message": "insufficient credits for this request",
                    "retryable": False,
                },
            }),
        )

    return handler


def _identity() -> CloudGatewayIdentity:
    return CloudGatewayIdentity(
        operator_id="cloud:acct_1",
        account_id="acct_1",
        tenant_id="tenant_1",
        character_ref="chr_abc",
    )


class _IdentityResolver:
    async def resolve_context(self, _context):  # noqa: ANN001, ANN201
        return _identity()


def _character() -> Character:
    return Character(
        id="chr_abc",
        name="Mio",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="calm", affection=50, fatigue=20, trust=50, energy=60,
        ),
        appearance="short black hair",
    )


def _patch_client(monkeypatch: pytest.MonkeyPatch, handler) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )


# ── image ─────────────────────────────────────────────────────────────


def _image_provider() -> CloudGatewayImageProvider:
    return CloudGatewayImageProvider(
        base_url="https://gateway.example/",
        deployment_token="ykl_deploy",
        preset="preset-image",
        feature_key="character_portrait",
        identity_resolver=_IdentityResolver(),
    )


@pytest.mark.asyncio
async def test_image_gateway_keeps_insufficient_credits_on_the_cause_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_client(monkeypatch, _refusal_handler())

    with pytest.raises(ImageGenerationError) as excinfo:
        await _image_provider().generate(
            character=_character(), positive="in the rain",
        )

    assert expected_refusal_code(excinfo.value) == _INSUFFICIENT


@pytest.mark.asyncio
async def test_image_gateway_plain_upstream_error_has_no_refusal_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream exploded")

    _patch_client(monkeypatch, handler)

    with pytest.raises(ImageGenerationError) as excinfo:
        await _image_provider().generate(
            character=_character(), positive="in the rain",
        )

    assert expected_refusal_code(excinfo.value) is None


# ── TTS ───────────────────────────────────────────────────────────────


class _CharacterRepository:
    async def get(self, _character_id: str):  # noqa: ANN201
        return _character()


def _tts_adapter() -> CloudGatewayTTSAdapter:
    return CloudGatewayTTSAdapter(
        base_url="https://gateway.example/",
        deployment_token="ykl_deploy",
        character_repository=_CharacterRepository(),
        identity_resolver=_IdentityResolver(),
        default_voice_id="marin",
    )


@pytest.mark.asyncio
async def test_tts_gateway_keeps_insufficient_credits_on_the_cause_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_client(monkeypatch, _refusal_handler())

    with pytest.raises(TTSError) as excinfo:
        await _tts_adapter().synthesize(
            TTSRequest(text="hello", character_id="chr_abc"),
        )

    assert expected_refusal_code(excinfo.value) == _INSUFFICIENT


# ── video ─────────────────────────────────────────────────────────────


def _video_provider() -> CloudGatewayVideoProvider:
    return CloudGatewayVideoProvider(
        base_url="https://gateway.example/",
        deployment_token="ykl_deploy",
        preset="preset-video",
        feature_key="video",
        identity_resolver=_IdentityResolver(),
    )


@pytest.mark.asyncio
async def test_video_gateway_keeps_insufficient_credits_on_the_cause_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_client(monkeypatch, _refusal_handler())

    with pytest.raises(VideoGenerationError) as excinfo:
        await _video_provider().generate(
            character=_character(), positive="in the rain",
        )

    assert expected_refusal_code(excinfo.value) == _INSUFFICIENT
