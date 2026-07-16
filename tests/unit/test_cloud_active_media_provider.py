"""Phase 3 tail: cloud image/video/TTS resolve their preset/voice from the
control-plane routing profile (env presets are the deprecated fallback), mirroring
the LLM profile path. A strict (demo) account fails closed on a missing media preset
so it can never silently fall through to a paid env preset.
"""

from __future__ import annotations

import json

import httpx
import pytest

from kokoro_link.application.services.cloud_active_media_provider import (
    CloudActiveImageProvider,
    CloudActiveVideoProvider,
)
from kokoro_link.application.services.cloud_identity_resolver import (
    CloudOperatorIdentityResolver,
)
from kokoro_link.application.services.feature_keys import (
    FEATURE_IMAGE_PORTRAIT,
    FEATURE_VIDEO_FEED,
)
from kokoro_link.contracts.cloud_gateway import CloudGatewayIdentity
from kokoro_link.contracts.cloud_routing_profile import CloudRoutingProfile
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_operator_profile import (
    InMemoryOperatorProfileRepository,
)
from kokoro_link.infrastructure.tts.cloud_gateway import CloudGatewayTTSAdapter
from kokoro_link.contracts.tts import TTSRequest


def _character() -> Character:
    return Character.create(
        name="Kokoro",
        summary="A helpful companion",
        user_id="cloud:acct_1",
        personality=[],
        interests=[],
        speaking_style="gentle",
        boundaries=[],
        state=CharacterState(emotion="calm", affection=50, fatigue=0, trust=50, energy=80),
    )


def _demo_operator() -> OperatorProfile:
    return OperatorProfile(
        id="cloud:acct_1",
        display_name="Demo Player",
        cloud_account_id="acct_1",
        cloud_tenant_id="tenant_1",
        cloud_tenant_tier="demo",
        auth_provider="cloud",
    )


def _routing_profile(
    *,
    image: dict[str, str] | None = None,
    video: dict[str, str] | None = None,
    tts: dict[str, str] | None = None,
    strict: bool = True,
) -> CloudRoutingProfile:
    return CloudRoutingProfile(
        llm_feature_presets={},
        image_feature_presets=image or {},
        video_feature_presets=video or {},
        tts_voice_defaults=tts or {},
        strict_no_fallback=strict,
        disabled_features=frozenset(),
        catalog_version=7,
        routing_policy_version=42,
    )


class _RecordingProfilePort:
    def __init__(self, profile: CloudRoutingProfile) -> None:
        self._profile = profile
        self.scopes: list[tuple[str, str, str, str]] = []

    async def get_profile(
        self, *, tenant_id: str, account_id: str, tier: str, user_id: str = ""
    ) -> CloudRoutingProfile:
        self.scopes.append((tenant_id, account_id, user_id, tier))
        return self._profile


class _RecordingImageProvider:
    def __init__(self, feature_key: str, preset: str) -> None:
        self.feature_key = feature_key
        self.preset = preset


# --------------------------------------------------------------------- image

@pytest.mark.asyncio
async def test_image_profile_mode_resolves_preset_with_no_env_default() -> None:
    repo = InMemoryOperatorProfileRepository()
    await repo.save(_demo_operator())
    port = _RecordingProfilePort(
        _routing_profile(image={FEATURE_IMAGE_PORTRAIT: "demo-image"}, strict=True)
    )
    built: list[_RecordingImageProvider] = []
    provider = CloudActiveImageProvider(
        provider_factory=lambda fk, preset: built.append(_RecordingImageProvider(fk, preset))  # type: ignore[func-returns-value]
        or built[-1],
        identity_resolver=CloudOperatorIdentityResolver(repository=repo),
        routing_profile_port=port,
        default_preset="",
    )

    await provider.resolve(FEATURE_IMAGE_PORTRAIT, character=_character())

    assert built[-1].preset == "demo-image"
    assert port.scopes == [("tenant_1", "acct_1", "acct_1", "demo")]


@pytest.mark.asyncio
async def test_image_profile_disabled_feature_fails_closed() -> None:
    repo = InMemoryOperatorProfileRepository()
    await repo.save(_demo_operator())
    profile = CloudRoutingProfile(
        llm_feature_presets={},
        image_feature_presets={FEATURE_IMAGE_PORTRAIT: "demo-image"},
        video_feature_presets={},
        tts_voice_defaults={},
        strict_no_fallback=True,
        disabled_features=frozenset({"image"}),
        catalog_version=7,
        routing_policy_version=42,
    )
    provider = CloudActiveImageProvider(
        provider_factory=lambda fk, preset: pytest.fail("disabled feature must not build a provider"),
        identity_resolver=CloudOperatorIdentityResolver(repository=repo),
        routing_profile_port=_RecordingProfilePort(profile),
        default_preset="",
    )

    with pytest.raises(RuntimeError, match="disabled by policy"):
        await provider.resolve(FEATURE_IMAGE_PORTRAIT, character=_character())


@pytest.mark.asyncio
async def test_image_profile_strict_missing_names_feature_key_and_source() -> None:
    repo = InMemoryOperatorProfileRepository()
    await repo.save(_demo_operator())
    port = _RecordingProfilePort(_routing_profile(image={}, strict=True))
    provider = CloudActiveImageProvider(
        provider_factory=lambda fk, preset: _RecordingImageProvider(fk, preset),
        identity_resolver=CloudOperatorIdentityResolver(repository=repo),
        routing_profile_port=port,
        default_preset="yuralume-anime",
    )

    with pytest.raises(RuntimeError) as excinfo:
        await provider.resolve(FEATURE_IMAGE_PORTRAIT, character=_character())

    message = str(excinfo.value)
    assert FEATURE_IMAGE_PORTRAIT in message
    assert "control-plane" in message


@pytest.mark.asyncio
async def test_image_env_fallback_when_no_profile_port() -> None:
    built: list[_RecordingImageProvider] = []
    provider = CloudActiveImageProvider(
        provider_factory=lambda fk, preset: built.append(_RecordingImageProvider(fk, preset))  # type: ignore[func-returns-value]
        or built[-1],
        default_preset="yuralume-anime",
    )

    await provider.resolve(FEATURE_IMAGE_PORTRAIT, character=_character())

    assert built[-1].preset == "yuralume-anime"


# --------------------------------------------------------------------- video

@pytest.mark.asyncio
async def test_video_profile_mode_resolves_preset() -> None:
    repo = InMemoryOperatorProfileRepository()
    await repo.save(_demo_operator())
    port = _RecordingProfilePort(
        _routing_profile(video={FEATURE_VIDEO_FEED: "demo-video"}, strict=True)
    )
    built: list[_RecordingImageProvider] = []
    provider = CloudActiveVideoProvider(
        provider_factory=lambda fk, preset: built.append(_RecordingImageProvider(fk, preset))  # type: ignore[func-returns-value]
        or built[-1],
        identity_resolver=CloudOperatorIdentityResolver(repository=repo),
        routing_profile_port=port,
        default_preset="",
    )

    await provider.resolve(FEATURE_VIDEO_FEED, character=_character())

    assert built[-1].preset == "demo-video"


# ----------------------------------------------------------------------- tts

class _MockAsyncClient(httpx.AsyncClient):
    def __init__(self, handler, **kwargs) -> None:
        super().__init__(transport=httpx.MockTransport(handler), timeout=kwargs["timeout"])


class _DemoIdentityResolver:
    async def resolve_context(self, context) -> CloudGatewayIdentity:
        return CloudGatewayIdentity(
            operator_id=context.operator_id,
            account_id="acct_1",
            tenant_id="tenant_1",
            character_ref="chr_abc",
            tenant_tier="demo",
        )


@pytest.mark.asyncio
async def test_tts_profile_mode_resolves_voice_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content.decode())
        return httpx.Response(200, content=b"wav", headers={"content-type": "audio/wav"})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _MockAsyncClient(handler, **kwargs))
    repo = InMemoryCharacterRepository()
    character = _character()
    await repo.save(character)
    adapter = CloudGatewayTTSAdapter(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        default_voice_id="env_voice",
        character_repository=repo,
        identity_resolver=_DemoIdentityResolver(),
        routing_profile_port=_RecordingProfilePort(
            _routing_profile(tts={"tts_synthesis": "profile_voice"}, strict=True)
        ),
    )

    await adapter.synthesize(TTSRequest(text="hi", character_id=character.id, text_lang="en"))

    # The profile voice wins over the env default when no per-request voice is set.
    assert seen["payload"]["voice_id"] == "profile_voice"  # type: ignore[index]


@pytest.mark.asyncio
async def test_tts_falls_back_to_env_voice_when_profile_has_no_voice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content.decode())
        return httpx.Response(200, content=b"wav", headers={"content-type": "audio/wav"})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _MockAsyncClient(handler, **kwargs))
    repo = InMemoryCharacterRepository()
    character = _character()
    await repo.save(character)
    adapter = CloudGatewayTTSAdapter(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        default_voice_id="env_voice",
        character_repository=repo,
        identity_resolver=_DemoIdentityResolver(),
        routing_profile_port=_RecordingProfilePort(_routing_profile(tts={}, strict=True)),
    )

    await adapter.synthesize(TTSRequest(text="hi", character_id=character.id, text_lang="en"))

    # TTS voice is not a cost/safety boundary, so a profile miss degrades gracefully.
    assert seen["payload"]["voice_id"] == "env_voice"  # type: ignore[index]
