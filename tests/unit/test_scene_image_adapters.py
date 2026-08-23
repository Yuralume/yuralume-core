"""BD1 — the two :class:`SceneImagePort` adapters plus container selection.

What matters here is that the port hides *which* backend drew the scene
while preserving the one behaviour the caller depends on: a failure comes
back as ``SceneImageError`` (so the drama service's fail-soft skip stays
exactly as wide as it was) and never as a backend-specific exception.
"""

from __future__ import annotations

import pytest

from kokoro_link.application.services.feature_keys import (
    FEATURE_BRANCHING_DRAMA_SCENE,
    FEATURE_LABELS,
    IMAGE_FEATURE_KEYS,
)
from kokoro_link.contracts.image_provider import (
    ImageGenerationError,
    ImageNoOutputError,
    ImageTimeoutError,
)
from kokoro_link.contracts.scene_image import (
    SceneImageError,
    SceneImageNoOutputError,
    SceneImageTimeoutError,
)
from kokoro_link.infrastructure.image.active_provider_scene_image import (
    ActiveProviderSceneImageAdapter,
)
from kokoro_link.infrastructure.image.comfy_scene_image import (
    ComfySceneImageAdapter,
)
from kokoro_link.infrastructure.tools.comfyui.scene_generator import (
    SceneGenerationError,
    SceneNoOutputError,
    SceneTimeoutError,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState


# ── stubs ─────────────────────────────────────────────────────────────


def _character() -> Character:
    return Character.create(
        name="測試角色",
        summary="s",
        personality=["calm"],
        interests=["coffee"],
        speaking_style="quiet",
        boundaries=[],
        state=CharacterState(
            emotion="平靜", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


class _StubComfyGenerator:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self._raises = raises
        self.calls: list[tuple[str, str]] = []

    async def generate(self, *, positive: str, aspect: str = "landscape") -> bytes:
        self.calls.append((positive, aspect))
        if self._raises is not None:
            raise self._raises
        return b"COMFY-PNG"


class _StubImageProvider:
    def __init__(
        self,
        *,
        images: list[bytes] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._images = images if images is not None else [b"HOSTED-PNG"]
        self._raises = raises
        self.kwargs: dict | None = None

    async def generate(self, **kwargs) -> list[bytes]:
        self.kwargs = kwargs
        if self._raises is not None:
            raise self._raises
        return self._images


class _StubActiveImageProvider:
    def __init__(self, provider) -> None:
        self._provider = provider
        self.resolved: list[tuple[str | None, str | None]] = []

    async def resolve(self, feature_key=None, *, character=None):
        self.resolved.append(
            (feature_key, getattr(character, "id", None)),
        )
        return self._provider

    async def resolve_profile_id(self, feature_key=None, *, character=None):
        return "stub"


# ── feature key registration ──────────────────────────────────────────


def test_scene_feature_key_is_registered_for_the_image_picker() -> None:
    assert FEATURE_BRANCHING_DRAMA_SCENE == "branching_drama_scene"
    assert FEATURE_BRANCHING_DRAMA_SCENE in IMAGE_FEATURE_KEYS
    assert FEATURE_LABELS[FEATURE_BRANCHING_DRAMA_SCENE]


# ── adapter A: local ComfyUI ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_comfy_adapter_passes_prompt_through_untouched() -> None:
    generator = _StubComfyGenerator()
    adapter = ComfySceneImageAdapter(generator)  # type: ignore[arg-type]

    data = await adapter.generate(
        positive="夜晚的教室, 空桌椅", aspect="landscape",
        character=_character(),
    )

    assert data == b"COMFY-PNG"
    # The character anchor is meaningless to a local GPU and must not
    # leak into the prompt or the call.
    assert generator.calls == [("夜晚的教室, 空桌椅", "landscape")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (SceneTimeoutError("slow"), SceneImageTimeoutError),
        (SceneNoOutputError("empty"), SceneImageNoOutputError),
        (SceneGenerationError("boom"), SceneImageError),
    ],
)
async def test_comfy_adapter_translates_the_comfy_error_family(
    raised: Exception, expected: type[Exception],
) -> None:
    adapter = ComfySceneImageAdapter(
        _StubComfyGenerator(raises=raised),  # type: ignore[arg-type]
    )

    with pytest.raises(expected) as excinfo:
        await adapter.generate(positive="x")

    assert isinstance(excinfo.value, SceneImageError)
    assert excinfo.value.__cause__ is raised


# ── adapter B: hosted active image provider ───────────────────────────


@pytest.mark.asyncio
async def test_hosted_adapter_routes_on_the_scene_feature_key() -> None:
    provider = _StubImageProvider()
    active = _StubActiveImageProvider(provider)
    adapter = ActiveProviderSceneImageAdapter(
        image_provider=active,  # type: ignore[arg-type]
        feature_key=FEATURE_BRANCHING_DRAMA_SCENE,
    )
    character = _character()

    data = await adapter.generate(
        positive="海邊的黃昏", aspect="landscape", character=character,
    )

    assert data == b"HOSTED-PNG"
    assert active.resolved == [
        (FEATURE_BRANCHING_DRAMA_SCENE, character.id),
    ]
    assert provider.kwargs is not None
    assert provider.kwargs["positive"] == "海邊的黃昏"
    assert provider.kwargs["aspect"] == "landscape"
    assert provider.kwargs["batch"] == 1
    # A scripted drama beat is not a snapshot of today's mood.
    assert provider.kwargs["use_runtime_state"] is False


@pytest.mark.asyncio
async def test_hosted_adapter_refuses_without_a_character_anchor() -> None:
    provider = _StubImageProvider()
    active = _StubActiveImageProvider(provider)
    adapter = ActiveProviderSceneImageAdapter(
        image_provider=active,  # type: ignore[arg-type]
        feature_key=FEATURE_BRANCHING_DRAMA_SCENE,
    )

    with pytest.raises(SceneImageError):
        await adapter.generate(positive="x")

    # Never billed against a guessed account.
    assert active.resolved == []
    assert provider.kwargs is None


@pytest.mark.asyncio
async def test_hosted_adapter_reports_an_unresolved_provider_as_scene_error() -> None:
    adapter = ActiveProviderSceneImageAdapter(
        image_provider=_StubActiveImageProvider(None),  # type: ignore[arg-type]
        feature_key=FEATURE_BRANCHING_DRAMA_SCENE,
    )

    with pytest.raises(SceneImageError):
        await adapter.generate(positive="x", character=_character())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (ImageTimeoutError("slow"), SceneImageTimeoutError),
        (ImageNoOutputError("empty"), SceneImageNoOutputError),
        (ImageGenerationError("boom"), SceneImageError),
    ],
)
async def test_hosted_adapter_translates_the_image_error_family(
    raised: Exception, expected: type[Exception],
) -> None:
    adapter = ActiveProviderSceneImageAdapter(
        image_provider=_StubActiveImageProvider(  # type: ignore[arg-type]
            _StubImageProvider(raises=raised),
        ),
        feature_key=FEATURE_BRANCHING_DRAMA_SCENE,
    )

    with pytest.raises(expected) as excinfo:
        await adapter.generate(positive="x", character=_character())

    assert isinstance(excinfo.value, SceneImageError)
    assert excinfo.value.__cause__ is raised


@pytest.mark.asyncio
@pytest.mark.parametrize("images", [[], [b""]])
async def test_hosted_adapter_treats_empty_output_as_no_output(
    images: list[bytes],
) -> None:
    adapter = ActiveProviderSceneImageAdapter(
        image_provider=_StubActiveImageProvider(  # type: ignore[arg-type]
            _StubImageProvider(images=images),
        ),
        feature_key=FEATURE_BRANCHING_DRAMA_SCENE,
    )

    with pytest.raises(SceneImageNoOutputError):
        await adapter.generate(positive="x", character=_character())


# ── container selection ───────────────────────────────────────────────


def _settings(*, cloud_enabled: bool, comfyui_enabled: bool):
    from kokoro_link.bootstrap.settings import (
        AppSettings,
        CloudSettings,
        ComfyUISettings,
    )

    # ``ComfyUISettings.enabled`` is derived from a configured server.
    return AppSettings(
        cloud=CloudSettings(enabled=cloud_enabled),
        comfyui=ComfyUISettings(
            server="http://127.0.0.1:8188" if comfyui_enabled else "",
        ),
    )


def test_container_picks_the_hosted_adapter_in_cloud_mode() -> None:
    from kokoro_link.bootstrap.container import _build_scene_image_port

    port = _build_scene_image_port(
        settings=_settings(cloud_enabled=True, comfyui_enabled=False),
        active_image_provider=_StubActiveImageProvider(  # type: ignore[arg-type]
            _StubImageProvider(),
        ),
    )

    assert isinstance(port, ActiveProviderSceneImageAdapter)


def test_container_picks_the_local_adapter_when_comfyui_is_enabled() -> None:
    from kokoro_link.bootstrap.container import _build_scene_image_port

    port = _build_scene_image_port(
        settings=_settings(cloud_enabled=False, comfyui_enabled=True),
        active_image_provider=_StubActiveImageProvider(  # type: ignore[arg-type]
            _StubImageProvider(),
        ),
    )

    assert isinstance(port, ComfySceneImageAdapter)


def test_container_leaves_the_port_unwired_when_neither_is_available() -> None:
    from kokoro_link.bootstrap.container import _build_scene_image_port

    port = _build_scene_image_port(
        settings=_settings(cloud_enabled=False, comfyui_enabled=False),
        active_image_provider=_StubActiveImageProvider(  # type: ignore[arg-type]
            _StubImageProvider(),
        ),
    )

    assert port is None


def test_prefetch_depth_knob_defaults_to_the_historical_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kokoro_link.bootstrap.container import _drama_image_prefetch_depth
    from kokoro_link.domain.entities.branching_drama import (
        IMAGE_PREFETCH_DEPTH,
    )

    monkeypatch.delenv("KOKORO_DRAMA_IMAGE_PREFETCH_DEPTH", raising=False)
    assert _drama_image_prefetch_depth() == IMAGE_PREFETCH_DEPTH

    monkeypatch.setenv("KOKORO_DRAMA_IMAGE_PREFETCH_DEPTH", "1")
    assert _drama_image_prefetch_depth() == 1

    monkeypatch.setenv("KOKORO_DRAMA_IMAGE_PREFETCH_DEPTH", "0")
    assert _drama_image_prefetch_depth() == 0

    # Malformed / negative values keep the default rather than silently
    # turning drama art off.
    monkeypatch.setenv("KOKORO_DRAMA_IMAGE_PREFETCH_DEPTH", "not-a-number")
    assert _drama_image_prefetch_depth() == IMAGE_PREFETCH_DEPTH
    monkeypatch.setenv("KOKORO_DRAMA_IMAGE_PREFETCH_DEPTH", "-3")
    assert _drama_image_prefetch_depth() == IMAGE_PREFETCH_DEPTH
