from __future__ import annotations

import base64
import json

import httpx
import pytest

from kokoro_link.contracts.cloud_gateway import (
    CloudGatewayIdentity,
    CloudResourceContext,
)
from kokoro_link.contracts.generation_trigger import (
    GenerationTrigger,
    generation_trigger_scope,
)
from kokoro_link.contracts.image_provider import ImageTokenUsage
from kokoro_link.contracts.interaction_context import (
    BILLING_COVERED_HEADER_NAME,
    InteractionContext,
    confirm_deferred_deliveries,
    interaction_scope,
)
from kokoro_link.contracts.tts import TTSRequest
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.image.cloud_gateway_provider import (
    CloudGatewayImageProvider,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.tts.cloud_gateway import CloudGatewayTTSAdapter
from kokoro_link.infrastructure.video.cloud_gateway_provider import (
    CloudGatewayVideoProvider,
)


class _MockAsyncClient(httpx.AsyncClient):
    def __init__(self, handler, **kwargs) -> None:
        super().__init__(
            transport=httpx.MockTransport(handler),
            timeout=kwargs["timeout"],
        )


class _IdentityResolver:
    def __init__(self, *, character_origin: str | None = None) -> None:
        self._character_origin = character_origin

    async def resolve_context(
        self, context: CloudResourceContext
    ) -> CloudGatewayIdentity:
        return CloudGatewayIdentity(
            operator_id=context.operator_id,
            account_id="acct_1",
            tenant_id="tenant_1",
            character_ref="chr_abc",
            character_origin=self._character_origin,
        )


def _character() -> Character:
    return Character.create(
        name="Kokoro",
        summary="A helpful companion",
        user_id="cloud:acct_1",
        personality=[],
        interests=[],
        speaking_style="gentle",
        boundaries=[],
        appearance="green hair",
        state=CharacterState(
            emotion="calm",
            affection=50,
            fatigue=0,
            trust=50,
            energy=80,
        ),
    )


def _animal_character() -> Character:
    return Character.create(
        name="Mochi",
        summary="balcony cat",
        user_id="cloud:acct_1",
        personality=[],
        interests=[],
        speaking_style="gentle",
        boundaries=[],
        appearance="一隻短毛橘貓，四足姿態，圓眼睛",
        gender_identity="非二元",
        third_person_pronoun="TA",
        visual_gender_presentation="可愛寵物貓",
        visual_subject_type="animal",
        state=CharacterState(
            emotion="calm",
            affection=50,
            fatigue=0,
            trust=50,
            energy=80,
        ),
    )


@pytest.mark.asyncio
async def test_cloud_gateway_image_provider_sends_identity_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["payload"] = json.loads(request.content.decode())
        return httpx.Response(200, json={
            "data": [
                {"b64_json": base64.b64encode(b"png").decode()},
            ],
        })

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )
    provider = CloudGatewayImageProvider(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        preset="yuralume-anime",
        feature_key="image_portrait",
        identity_resolver=_IdentityResolver(),
    )

    with generation_trigger_scope(GenerationTrigger.BACKGROUND):
        result = await provider.generate(
            character=_character(),
            positive="at a cafe",
            aspect="square",
        )

    assert result == [b"png"]
    assert seen["url"] == "https://gateway.example/v1/images/generations"
    headers = seen["headers"]
    assert headers["authorization"] == "Bearer ykl_deploy"
    assert headers["x-yuralume-deployment"] == "hosted-primary"
    assert headers["x-yuralume-audience"] == "yuralume-gateway"
    assert headers["x-yuralume-account"] == "acct_1"
    assert headers["x-yuralume-tenant"] == "tenant_1"
    assert headers["x-yuralume-feature"] == "image_portrait"
    assert headers["x-yuralume-character"] == "chr_abc"
    # EC7: non-managed character — no origin header at all.
    assert "x-yuralume-character-origin" not in headers
    assert headers["x-yuralume-trigger"] == "v1;source=background"
    assert str(headers["x-request-id"]).startswith("img-")
    assert provider.last_request_id == headers["x-request-id"]
    payload = seen["payload"]
    assert payload["model"] == "yuralume-anime"
    assert payload["size"] == "1024x1024"
    assert "green hair" in payload["prompt"]


@pytest.mark.asyncio
async def test_cloud_gateway_image_provider_captures_usage_and_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "model": "gpt-image2",
            "data": [
                {"b64_json": base64.b64encode(b"png").decode()},
            ],
            "usage": {
                "unit": "token",
                "input_tokens": 120,
                "input_text_tokens": 20,
                "input_image_tokens": 100,
                "output_tokens": 300,
                "output_image_tokens": 300,
                "total_tokens": 420,
                "estimated": False,
            },
            "yuralume": {
                "provider": "openai_image",
                "provider_model": "gpt-image-2",
                "cost_estimate": {
                    "unit": "token",
                    "quantity": 420,
                    "total_usd": 0.0123,
                },
            },
        })

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )
    provider = CloudGatewayImageProvider(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        preset="gpt-image2",
        feature_key="image_portrait",
        identity_resolver=_IdentityResolver(),
    )

    result = await provider.generate(
        character=_character(),
        positive="at a cafe",
        aspect="square",
    )

    assert result == [b"png"]
    assert provider.last_provider_id == "openai_image"
    assert provider.last_model_id == "gpt-image-2"
    assert provider.last_usage == ImageTokenUsage(
        input_tokens=120,
        input_text_tokens=20,
        input_image_tokens=100,
        output_tokens=300,
        output_image_tokens=300,
        total_tokens=420,
        estimated=False,
    )
    assert provider.last_cost_amount_usd == 0.0123


@pytest.mark.asyncio
async def test_cloud_gateway_image_provider_includes_non_human_animal_body_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content.decode())
        return httpx.Response(200, json={
            "data": [
                {"b64_json": base64.b64encode(b"png").decode()},
            ],
        })

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )
    provider = CloudGatewayImageProvider(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        preset="yuralume-anime",
        feature_key="image_portrait",
        identity_resolver=_IdentityResolver(),
    )

    await provider.generate(
        character=_animal_character(),
        positive="at a windowsill",
        aspect="square",
    )

    prompt = seen["payload"]["prompt"]  # type: ignore[index]
    assert "Visual subject type: non-human animal." in prompt
    assert "Species/body plan: domestic cat." in prompt
    assert "Do NOT anthropomorphize" in prompt
    assert "human face" in prompt
    assert "at a windowsill" in prompt


@pytest.mark.asyncio
async def test_cloud_gateway_video_provider_sends_identity_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["payload"] = json.loads(request.content.decode())
        return httpx.Response(200, json={
            "data": [
                {"b64_json": base64.b64encode(b"mp4").decode()},
            ],
        })

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )
    provider = CloudGatewayVideoProvider(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        preset="yuralume-video",
        feature_key="feed_video",
        identity_resolver=_IdentityResolver(),
    )

    with generation_trigger_scope(GenerationTrigger.BACKGROUND):
        result = await provider.generate(
            character=_character(),
            positive="walking through town",
            aspect="landscape",
            length_frames=80,
        )

    assert result == b"mp4"
    assert seen["url"] == "https://gateway.example/v1/videos/generations"
    headers = seen["headers"]
    assert headers["authorization"] == "Bearer ykl_deploy"
    assert headers["x-yuralume-deployment"] == "hosted-primary"
    assert headers["x-yuralume-audience"] == "yuralume-gateway"
    assert headers["x-yuralume-account"] == "acct_1"
    assert headers["x-yuralume-tenant"] == "tenant_1"
    assert headers["x-yuralume-feature"] == "feed_video"
    assert headers["x-yuralume-character"] == "chr_abc"
    # EC7: non-managed character — no origin header at all.
    assert "x-yuralume-character-origin" not in headers
    assert headers["x-yuralume-trigger"] == "v1;source=background"
    assert str(headers["x-request-id"]).startswith("vid-")
    assert provider.last_request_id == headers["x-request-id"]
    payload = seen["payload"]
    assert payload["model"] == "yuralume-video"
    assert payload["aspect_ratio"] == "16:9"
    assert payload["duration_seconds"] == 5
    # CV0-3: the feed usage ledger hard-coded 81 frames / 6 seconds for every
    # video regardless of what was actually requested/rendered. This adapter
    # is the only one wired to a cloud billing gateway, so it must expose the
    # real duration via the same additive-attribute shape as
    # ``last_request_id`` above, for the ledger to read.
    assert provider.last_duration_seconds == 5


@pytest.mark.asyncio
async def test_cloud_gateway_video_provider_prefers_gateway_reported_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the Gateway response itself reports a ``duration_seconds`` (e.g.
    the upstream renderer trimmed/extended the clip), that value is the
    ground truth — prefer it over what we merely requested."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "duration_seconds": 8,
            "data": [{"b64_json": base64.b64encode(b"mp4").decode()}],
        })

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )
    provider = CloudGatewayVideoProvider(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        preset="yuralume-video",
        feature_key="feed_video",
        identity_resolver=_IdentityResolver(),
    )

    with generation_trigger_scope(GenerationTrigger.BACKGROUND):
        result = await provider.generate(
            character=_character(),
            positive="walking through town",
            length_frames=80,
        )

    assert result == b"mp4"
    assert provider.last_duration_seconds == 8


@pytest.mark.asyncio
async def test_cloud_gateway_tts_adapter_sends_identity_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            content=b"wav",
            headers={"content-type": "audio/wav"},
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )
    repo = InMemoryCharacterRepository()
    character = _character()
    await repo.save(character)
    adapter = CloudGatewayTTSAdapter(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        default_voice_id="voice_default",
        character_repository=repo,
        identity_resolver=_IdentityResolver(),
    )

    with generation_trigger_scope(GenerationTrigger.BACKGROUND):
        result = await adapter.synthesize(TTSRequest(
            text="hello",
            character_id=character.id,
            text_lang="en",
        ))

    assert result.audio == b"wav"
    assert seen["url"] == "https://gateway.example/v1/tts/synthesize"
    headers = seen["headers"]
    assert headers["authorization"] == "Bearer ykl_deploy"
    assert headers["x-yuralume-deployment"] == "hosted-primary"
    assert headers["x-yuralume-audience"] == "yuralume-gateway"
    assert headers["x-yuralume-account"] == "acct_1"
    assert headers["x-yuralume-tenant"] == "tenant_1"
    # Gateway billing classifies the player-visible synthesis action by this
    # trigger key.  The request body keeps the TTS service's capability key
    # (``tts``), but the trusted attribution header must name the foreground
    # feature or Gateway deliberately treats it as unknown/no-charge.
    assert headers["x-yuralume-feature"] == "tts_synthesis"
    assert headers["x-yuralume-character"] == "chr_abc"
    # EC7: non-managed character — no origin header at all.
    assert "x-yuralume-character-origin" not in headers
    assert headers["x-yuralume-trigger"] == "v1;source=background"
    assert str(headers["x-request-id"]).startswith("tts-")
    assert adapter.last_request_id == headers["x-request-id"]
    payload = seen["payload"]
    assert payload["text"] == "hello"
    assert payload["feature_key"] == "tts"
    assert payload["voice_id"] == "voice_default"
    assert payload["options"]["text_lang"] == "en"


@pytest.mark.asyncio
async def test_cloud_gateway_image_provider_sends_origin_for_managed_character(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={
            "data": [{"b64_json": base64.b64encode(b"png").decode()}],
        })

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )
    provider = CloudGatewayImageProvider(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        preset="yuralume-anime",
        feature_key="image_portrait",
        identity_resolver=_IdentityResolver(character_origin="official-yumi"),
    )

    await provider.generate(
        character=_character(),
        positive="at a cafe",
        aspect="square",
    )

    assert seen["headers"]["x-yuralume-character-origin"] == "official-yumi"


@pytest.mark.asyncio
async def test_cloud_gateway_video_provider_sends_origin_for_managed_character(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={
            "data": [{"b64_json": base64.b64encode(b"mp4").decode()}],
        })

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )
    provider = CloudGatewayVideoProvider(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        preset="yuralume-video",
        feature_key="feed_video",
        identity_resolver=_IdentityResolver(character_origin="official-yumi"),
    )

    await provider.generate(
        character=_character(),
        positive="walking through town",
        aspect="landscape",
        length_frames=80,
    )

    assert seen["headers"]["x-yuralume-character-origin"] == "official-yumi"


@pytest.mark.asyncio
async def test_cloud_gateway_tts_adapter_sends_origin_for_managed_character(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        return httpx.Response(
            200, content=b"wav", headers={"content-type": "audio/wav"},
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )
    repo = InMemoryCharacterRepository()
    character = _character()
    await repo.save(character)
    adapter = CloudGatewayTTSAdapter(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        default_voice_id="voice_default",
        character_repository=repo,
        identity_resolver=_IdentityResolver(character_origin="official-yumi"),
    )

    await adapter.synthesize(TTSRequest(
        text="hello",
        character_id=character.id,
        text_lang="en",
    ))

    assert seen["headers"]["x-yuralume-character-origin"] == "official-yumi"


@pytest.mark.asyncio
async def test_cloud_gateway_image_provider_stamps_the_covering_interaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AP2: ``image_portrait`` / ``image_chat_tool`` are action-priced, so
    their Gateway call must name the charge that already covers it."""
    seen: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        return httpx.Response(200, json={
            "data": [{"b64_json": base64.b64encode(b"png").decode()}],
        })

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )
    provider = CloudGatewayImageProvider(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        preset="yuralume-anime",
        feature_key="image_portrait",
        identity_resolver=_IdentityResolver(),
    )

    with interaction_scope(
        InteractionContext(interaction_id="int-1", charge_id="chg-1"),
    ):
        await provider.generate(
            character=_character(), positive="a", aspect="square",
        )
    await provider.generate(
        character=_character(), positive="b", aspect="square",
    )

    assert seen[0]["x-yuralume-interaction"] == "v1;id=int-1;charge=chg-1"
    assert "x-yuralume-interaction" not in seen[1]


def _image_provider() -> CloudGatewayImageProvider:
    return CloudGatewayImageProvider(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        preset="yuralume-anime",
        feature_key="image_portrait",
        identity_resolver=_IdentityResolver(),
    )


@pytest.mark.asyncio
async def test_a_covered_image_is_consumed_only_once_it_is_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C2/C4': waived by the Gateway, delivered as bytes — and *stored*.

    Bytes in memory are not the thing the player bought. Until the caller has
    persisted them the charge stays refundable, because a storage failure after
    this point would otherwise leave a paid-for picture that exists nowhere.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(b"png").decode()}]},
            headers={BILLING_COVERED_HEADER_NAME: "1"},
        )

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )

    context = InteractionContext(interaction_id="int-1", charge_id="chg-1")
    with interaction_scope(context):
        images = await _image_provider().generate(
            character=_character(), positive="a", aspect="square",
        )
        assert context.usage.consumed is False  # bytes only, nothing stored

        assert confirm_deferred_deliveries() == 1

    assert images == [b"png"]
    assert context.usage.consumed is True


@pytest.mark.asyncio
async def test_an_image_that_is_never_persisted_stays_refundable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The C4' hole: the storage write fails, so the player keeps their money."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(b"png").decode()}]},
            headers={BILLING_COVERED_HEADER_NAME: "1"},
        )

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )

    context = InteractionContext(interaction_id="int-1", charge_id="chg-1")
    with interaction_scope(context):
        await _image_provider().generate(
            character=_character(), positive="a", aspect="square",
        )
        # the caller's object-storage write raised — nothing confirms

    assert context.usage.consumed is False


@pytest.mark.asyncio
async def test_an_image_the_gateway_billed_itself_is_not_consumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(b"png").decode()}]},
        )

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )

    context = InteractionContext(interaction_id="int-1", charge_id="chg-1")
    with interaction_scope(context):
        await _image_provider().generate(
            character=_character(), positive="a", aspect="square",
        )

    assert context.usage.consumed is False


@pytest.mark.asyncio
async def test_a_covered_answer_with_no_downloadable_image_is_not_consumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The job answered but produced nothing — the player keeps their money."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": []}, headers={BILLING_COVERED_HEADER_NAME: "1"},
        )

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )

    context = InteractionContext(interaction_id="int-1", charge_id="chg-1")
    with interaction_scope(context):
        with pytest.raises(Exception):
            await _image_provider().generate(
                character=_character(), positive="a", aspect="square",
            )

    assert context.usage.consumed is False


@pytest.mark.asyncio
async def test_an_image_whose_artifact_download_fails_is_not_consumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The generation call was waived, but nothing was ever delivered."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/generations"):
            return httpx.Response(
                200,
                json={"data": [{"url": "/artifacts/a.png"}]},
                headers={BILLING_COVERED_HEADER_NAME: "1"},
            )
        return httpx.Response(500, text="gone")

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )

    context = InteractionContext(interaction_id="int-1", charge_id="chg-1")
    with interaction_scope(context):
        with pytest.raises(Exception):
            await _image_provider().generate(
                character=_character(), positive="a", aspect="square",
            )

    assert context.usage.consumed is False
