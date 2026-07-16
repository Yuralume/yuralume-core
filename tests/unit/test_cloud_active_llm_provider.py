from __future__ import annotations

import pytest

from kokoro_link.application.services.cloud_active_llm_provider import (
    CloudActiveLLMProvider,
)
from kokoro_link.application.services.cloud_identity_resolver import (
    CloudOperatorIdentityResolver,
)
from kokoro_link.application.services.account_runtime_profile import (
    AccountRuntimeProfileResolver,
)
from kokoro_link.application.services.feature_keys import (
    FEATURE_CHARACTER_DRAFT,
    FEATURE_CHAT,
    FEATURE_POST_TURN,
)
from kokoro_link.application.services.cloud_routing_profile_cache import (
    CachedCloudRoutingProfileResolver,
)
from kokoro_link.contracts.cloud_gateway import CloudGatewayIdentity
from kokoro_link.contracts.cloud_routing_profile import CloudRoutingProfile
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.llm.cloud_gateway_model import CloudGatewayChatModel
from kokoro_link.infrastructure.repositories.in_memory_operator_profile import (
    InMemoryOperatorProfileRepository,
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
        state=CharacterState(
            emotion="calm",
            affection=50,
            fatigue=0,
            trust=50,
            energy=80,
        ),
    )


def _model_factory(
    feature_key: str,
    identity: CloudGatewayIdentity | None,
    default_model: str,
) -> ChatModelPort:
    return CloudGatewayChatModel(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        default_model=default_model,
        feature_key=feature_key,
        identity=identity,
    )


def _operator() -> OperatorProfile:
    return OperatorProfile(
        id="cloud:acct_1",
        display_name="Player",
        cloud_account_id="acct_1",
        cloud_tenant_id="tenant_1",
        auth_provider="cloud",
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


@pytest.mark.asyncio
async def test_cloud_active_llm_provider_resolves_gateway_model_by_feature() -> None:
    repo = InMemoryOperatorProfileRepository()
    await repo.save(_operator())
    provider = CloudActiveLLMProvider(
        identity_resolver=CloudOperatorIdentityResolver(repository=repo),
        model_factory=_model_factory,
        model_presets={
            FEATURE_CHAT: "preset-chat",
            FEATURE_POST_TURN: "preset-post-turn",
            "default": "preset-default",
        },
    )
    character = _character()

    model = await provider.resolve(FEATURE_CHAT, character=character)
    model_id = await provider.resolve_model_id(
        FEATURE_POST_TURN,
        character=character,
    )

    assert model.provider_id == "yuralume_cloud"
    assert await model.list_models() == ["preset-chat"]
    assert model_id == "preset-post-turn"
    assert await provider.is_fake(FEATURE_CHAT, character=character) is False


@pytest.mark.asyncio
async def test_cloud_active_llm_provider_requires_demo_feature_preset() -> None:
    repo = InMemoryOperatorProfileRepository()
    await repo.save(_demo_operator())
    factory_calls: list[tuple[str, CloudGatewayIdentity | None, str]] = []

    def factory(
        feature_key: str,
        identity: CloudGatewayIdentity | None,
        default_model: str,
    ) -> ChatModelPort:
        factory_calls.append((feature_key, identity, default_model))
        return _model_factory(feature_key, identity, default_model)

    provider = CloudActiveLLMProvider(
        identity_resolver=CloudOperatorIdentityResolver(repository=repo),
        model_factory=factory,
        model_presets={"default": "preset-default"},
        account_runtime_profile_resolver=AccountRuntimeProfileResolver(repo),
    )

    with pytest.raises(RuntimeError, match="strict no-fallback"):
        await provider.resolve(FEATURE_CHAT, character=_character())

    assert factory_calls == []


@pytest.mark.asyncio
async def test_cloud_active_llm_provider_allows_demo_explicit_feature_preset() -> None:
    repo = InMemoryOperatorProfileRepository()
    await repo.save(_demo_operator())
    provider = CloudActiveLLMProvider(
        identity_resolver=CloudOperatorIdentityResolver(repository=repo),
        model_factory=_model_factory,
        model_presets={
            FEATURE_CHAT: "preset-demo-chat",
            "default": "preset-default",
        },
        account_runtime_profile_resolver=AccountRuntimeProfileResolver(repo),
    )

    model = await provider.resolve(FEATURE_CHAT, character=_character())

    assert await model.list_models() == ["preset-demo-chat"]


@pytest.mark.asyncio
async def test_cloud_active_llm_provider_resolves_operator_scoped_demo_call() -> None:
    repo = InMemoryOperatorProfileRepository()
    await repo.save(_demo_operator())
    factory_calls: list[tuple[str, CloudGatewayIdentity | None, str]] = []

    def factory(
        feature_key: str,
        identity: CloudGatewayIdentity | None,
        default_model: str,
    ) -> ChatModelPort:
        factory_calls.append((feature_key, identity, default_model))
        return _model_factory(feature_key, identity, default_model)

    provider = CloudActiveLLMProvider(
        identity_resolver=CloudOperatorIdentityResolver(repository=repo),
        model_factory=factory,
        model_presets={
            FEATURE_CHARACTER_DRAFT: "preset-demo-draft",
            "default": "preset-default",
        },
        account_runtime_profile_resolver=AccountRuntimeProfileResolver(repo),
    )

    model = await provider.resolve(
        FEATURE_CHARACTER_DRAFT,
        operator_id="cloud:acct_1",
    )

    assert await model.list_models() == ["preset-demo-draft"]
    feature_key, identity, default_model = factory_calls[0]
    assert feature_key == FEATURE_CHARACTER_DRAFT
    assert default_model == "preset-demo-draft"
    assert identity is not None
    assert identity.account_id == "acct_1"
    assert identity.tenant_id == "tenant_1"
    assert identity.character_ref == ""


@pytest.mark.asyncio
async def test_cloud_active_llm_provider_enforces_demo_preset_for_operator_call() -> None:
    repo = InMemoryOperatorProfileRepository()
    await repo.save(_demo_operator())
    provider = CloudActiveLLMProvider(
        identity_resolver=CloudOperatorIdentityResolver(repository=repo),
        model_factory=_model_factory,
        model_presets={"default": "preset-default"},
        account_runtime_profile_resolver=AccountRuntimeProfileResolver(repo),
    )

    with pytest.raises(RuntimeError, match="strict no-fallback"):
        await provider.resolve(FEATURE_CHARACTER_DRAFT, operator_id="cloud:acct_1")


@pytest.mark.asyncio
async def test_cloud_active_llm_provider_uses_default_preset() -> None:
    repo = InMemoryOperatorProfileRepository()
    await repo.save(_operator())
    provider = CloudActiveLLMProvider(
        identity_resolver=CloudOperatorIdentityResolver(repository=repo),
        model_factory=_model_factory,
        model_presets={"default": "preset-default"},
    )

    assert await provider.resolve_model_id("unknown", character=_character()) == (
        "preset-default"
    )


def _routing_profile(
    *, llm_presets: dict[str, str], strict: bool
) -> CloudRoutingProfile:
    return CloudRoutingProfile(
        llm_feature_presets=llm_presets,
        image_feature_presets={},
        video_feature_presets={},
        tts_voice_defaults={},
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


@pytest.mark.asyncio
async def test_profile_mode_resolves_preset_with_no_env_presets() -> None:
    repo = InMemoryOperatorProfileRepository()
    await repo.save(_demo_operator())
    port = _RecordingProfilePort(
        _routing_profile(llm_presets={FEATURE_CHAT: "demo-gb10-chat"}, strict=True)
    )
    provider = CloudActiveLLMProvider(
        identity_resolver=CloudOperatorIdentityResolver(repository=repo),
        model_factory=_model_factory,
        model_presets={},  # no YURALUME_CLOUD_LLM_PRESETS
        routing_profile_port=port,
    )

    model = await provider.resolve(FEATURE_CHAT, character=_character())

    assert await model.list_models() == ["demo-gb10-chat"]
    # The profile is fetched for the demo-tier scope of the forwarded identity, with
    # user_id = the player's cloud account id so user-scope preferences resolve (§6).
    assert port.scopes == [("tenant_1", "acct_1", "acct_1", "demo")]


@pytest.mark.asyncio
async def test_profile_mode_disabled_feature_fails_closed() -> None:
    repo = InMemoryOperatorProfileRepository()
    await repo.save(_demo_operator())
    profile = CloudRoutingProfile(
        llm_feature_presets={FEATURE_CHAT: "demo-gb10-chat"},
        image_feature_presets={},
        video_feature_presets={},
        tts_voice_defaults={},
        strict_no_fallback=True,
        disabled_features=frozenset({"llm:chat"}),
        catalog_version=7,
        routing_policy_version=42,
    )
    provider = CloudActiveLLMProvider(
        identity_resolver=CloudOperatorIdentityResolver(repository=repo),
        model_factory=_model_factory,
        model_presets={},
        routing_profile_port=_RecordingProfilePort(profile),
    )

    with pytest.raises(RuntimeError, match="disabled by policy"):
        await provider.resolve(FEATURE_CHAT, character=_character())


@pytest.mark.asyncio
async def test_profile_mode_strict_missing_names_feature_key_and_source() -> None:
    repo = InMemoryOperatorProfileRepository()
    await repo.save(_demo_operator())
    port = _RecordingProfilePort(_routing_profile(llm_presets={}, strict=True))
    provider = CloudActiveLLMProvider(
        identity_resolver=CloudOperatorIdentityResolver(repository=repo),
        model_factory=_model_factory,
        model_presets={},
        routing_profile_port=port,
    )

    with pytest.raises(RuntimeError) as excinfo:
        await provider.resolve(FEATURE_CHARACTER_DRAFT, character=_character())

    message = str(excinfo.value)
    assert "character_draft" in message
    assert "control-plane" in message


@pytest.mark.asyncio
async def test_profile_mode_hot_path_makes_no_sync_call_per_turn() -> None:
    repo = InMemoryOperatorProfileRepository()
    await repo.save(_demo_operator())
    client = _RecordingProfilePort(
        _routing_profile(llm_presets={FEATURE_CHAT: "demo-gb10-chat"}, strict=True)
    )
    cache = CachedCloudRoutingProfileResolver(client=client, refresh_interval_seconds=1000)
    provider = CloudActiveLLMProvider(
        identity_resolver=CloudOperatorIdentityResolver(repository=repo),
        model_factory=_model_factory,
        model_presets={},
        routing_profile_port=cache,
    )
    character = _character()

    await provider.resolve(FEATURE_CHAT, character=character)
    await provider.resolve(FEATURE_CHAT, character=character)

    # Warm cache: only the cold miss hit the client, not every turn.
    assert len(client.scopes) == 1
