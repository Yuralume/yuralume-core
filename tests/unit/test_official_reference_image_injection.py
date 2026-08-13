"""EC6 — a managed character is drawn from the partner's reference art.

Two halves of one promise. **Every** generation surface anchors a managed
character to the art Cloud marked (portrait, gacha batch, the picture drawn
mid-chat, and the feed post's background), and the cloud adapter turns that
art into a real image input on the request instead of a URL the upstream
model could never fetch.

The other half is what must *not* change: a character the player made
themselves keeps sending byte-for-byte the request it sent before EC6, and
the self-host image adapters are untouched.
"""

from __future__ import annotations

import base64
import io
import json
import os
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest
from PIL import Image

from kokoro_link.application.services.character_image_service import (
    CharacterImageService,
    OfficialCardArtImage,
)
from kokoro_link.application.services.feed_composer_service import (
    FeedComposerService,
)
from kokoro_link.contracts.cloud_gateway import (
    CloudGatewayIdentity,
    CloudResourceContext,
)
from kokoro_link.contracts.image_provider import ImageGenerationError
from kokoro_link.contracts.tool import ToolContext
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.image.cloud_gateway_provider import (
    CloudGatewayImageProvider,
)
from kokoro_link.infrastructure.image.openai_provider import OpenAIImageProvider
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_feed_posts import (
    InMemoryFeedPostRepository,
)
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage
from kokoro_link.infrastructure.tools.comfyui.tool import ComfyImageTool
from tests.unit._image_provider_stub import StaticActiveImageProvider

_CARD_ID = "mio-cafe"
_PLAYER = "cloud:acct_1"
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_REFERENCE_BYTES = b"\x89PNG\r\n\x1a\nreference"


# --- fixtures --------------------------------------------------------

class _CaptureProvider:
    """Records what each generation entry point asked the provider for."""

    provider_id = "stub-image"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def generate(self, **kwargs) -> list[bytes]:  # noqa: ANN003
        self.calls.append(dict(kwargs))
        return [_PNG_BYTES]

    @property
    def attachments(self) -> tuple[str, ...]:
        assert self.calls, "provider.generate was never called"
        return tuple(self.calls[-1].get("user_attachment_urls") or ())


class _UnusedFeedCollaborator:
    """The first-frame path reaches neither collector nor composer.

    Constructor-required, call-forbidden: if either ever runs, the test is
    exercising the post-writing pipeline rather than the render seam.
    """

    async def collect(self, character, **kwargs):  # noqa: ANN003
        raise AssertionError("feed candidate collection is not this seam")

    async def compose(self, payload):
        raise AssertionError("feed composition is not this seam")


class _IdentityResolver:
    async def resolve_context(
        self, context: CloudResourceContext,
    ) -> CloudGatewayIdentity:
        return CloudGatewayIdentity(
            operator_id=context.operator_id,
            account_id="acct_1",
            tenant_id="tenant_1",
            character_ref="chr_abc",
            character_origin=None,
        )


class _MockAsyncClient(httpx.AsyncClient):
    def __init__(self, handler, **kwargs) -> None:
        super().__init__(
            transport=httpx.MockTransport(handler),
            timeout=kwargs["timeout"],
        )


def _character(*, managed: bool) -> Character:
    return Character.create(
        name="美緒",
        summary="咖啡廳打工女大生",
        user_id=_PLAYER,
        personality=[],
        interests=[],
        speaking_style="natural",
        boundaries=[],
        appearance="green hair",
        state=CharacterState(
            emotion="calm", affection=0, fatigue=0, trust=0, energy=100,
        ),
        allowed_tools=["generate_image"],
        origin_official_card_id=_CARD_ID if managed else None,
    )


async def _with_official_art(
    repo: InMemoryCharacterRepository,
    storage: InMemoryObjectStorage,
    tmp_path: Path,
    *,
    managed: bool,
) -> Character:
    """A character in the repo, with a managed one carrying EC4 art.

    Two images on purpose: only the reference-marked one is the likeness
    lock, so a test that passes because *all* official art was injected
    would be testing nothing.
    """
    character = _character(managed=managed)
    await repo.save(character)
    if not managed:
        return character
    service = CharacterImageService(
        character_repository=repo,
        uploads_dir=tmp_path,
        object_storage=storage,
    )
    return await service.attach_official_card_art(
        character.id,
        images=[
            OfficialCardArtImage(
                index=0, data=_PNG_BYTES, content_type="image/png",
            ),
            OfficialCardArtImage(
                index=1,
                data=_REFERENCE_BYTES,
                content_type="image/png",
                reference=True,
            ),
        ],
    )


def _reference_url(character: Character) -> str:
    return next(
        url for url in character.image_urls
        if "/official-reference/" in url
    )


def _image_service(repo, storage, tmp_path, provider) -> CharacterImageService:
    return CharacterImageService(
        character_repository=repo,
        uploads_dir=tmp_path,
        object_storage=storage,
        image_provider=StaticActiveImageProvider(provider),
    )


# --- the cloud adapter: art becomes a real image input ----------------

def _install_gateway(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )


async def _capture_payload(
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider: CloudGatewayImageProvider,
    character: Character,
    user_attachment_urls: Sequence[str] = (),
) -> tuple[dict, bytes]:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["raw"] = request.content
        return httpx.Response(200, json={
            "data": [{"b64_json": base64.b64encode(b"png").decode()}],
        })

    _install_gateway(monkeypatch, handler)
    await provider.generate(
        character=character,
        positive="at a cafe",
        user_attachment_urls=user_attachment_urls,
    )
    raw = seen["raw"]
    assert isinstance(raw, bytes)
    return json.loads(raw.decode()), raw


def _cloud_provider(storage, **kwargs) -> CloudGatewayImageProvider:
    return CloudGatewayImageProvider(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        preset="yuralume-anime",
        feature_key="image_portrait",
        identity_resolver=_IdentityResolver(),
        object_storage=storage,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_reference_art_travels_as_inline_image_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    repo = InMemoryCharacterRepository()
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    character = await _with_official_art(repo, storage, tmp_path, managed=True)
    url = _reference_url(character)

    payload, _ = await _capture_payload(
        monkeypatch,
        provider=_cloud_provider(storage),
        character=character,
        user_attachment_urls=(url,),
    )

    expected = base64.b64encode(_REFERENCE_BYTES).decode("ascii")
    assert payload["image"] == [f"data:image/png;base64,{expected}"]
    # The bytes are in the body, so the prompt must name them as the subject
    # — and must not fall back to a URL no upstream model could fetch.
    assert "Reference images attached: 1" in payload["prompt"]
    assert "User visual references" not in payload["prompt"]


@pytest.mark.asyncio
async def test_reference_art_leads_the_players_own_attachment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Card first: the photo directs the shot, the card decides the face."""
    repo = InMemoryCharacterRepository()
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    character = await _with_official_art(repo, storage, tmp_path, managed=True)
    player_photo = "data:image/png;base64,QUFB"

    payload, _ = await _capture_payload(
        monkeypatch,
        provider=_cloud_provider(storage),
        character=character,
        user_attachment_urls=(_reference_url(character), player_photo),
    )

    reference = base64.b64encode(_REFERENCE_BYTES).decode("ascii")
    assert payload["image"] == [
        f"data:image/png;base64,{reference}",
        player_photo,
    ]


@pytest.mark.asyncio
async def test_an_unreadable_reference_degrades_instead_of_failing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """One lost object must not close every generation surface."""
    repo = InMemoryCharacterRepository()
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    character = await _with_official_art(repo, storage, tmp_path, managed=True)
    url = _reference_url(character)
    await storage.delete(object_key=storage.object_key_from_url(url))

    payload, _ = await _capture_payload(
        monkeypatch,
        provider=_cloud_provider(storage),
        character=character,
        user_attachment_urls=(url,),
    )

    assert "image" not in payload
    assert payload["prompt"]


@pytest.mark.asyncio
async def test_a_player_character_sends_the_same_bytes_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No attachments, no change — down to the request body's bytes."""
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    character = _character(managed=False)

    _, with_storage = await _capture_payload(
        monkeypatch, provider=_cloud_provider(storage), character=character,
    )
    _, without_storage = await _capture_payload(
        monkeypatch, provider=_cloud_provider(None), character=character,
    )

    assert with_storage == without_storage
    assert set(json.loads(with_storage.decode())) == {
        "model", "prompt", "size", "n",
    }


@pytest.mark.asyncio
async def test_self_host_image_adapter_is_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inlining lives in the cloud adapter alone."""
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content.decode())
        return httpx.Response(200, json={
            "data": [{"b64_json": base64.b64encode(b"png").decode()}],
        })

    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )
    provider = OpenAIImageProvider(api_key="sk-test")

    await provider.generate(
        character=_character(managed=False),
        positive="at a cafe",
        user_attachment_urls=("/uploads/characters/x/official-reference/0.png",),
    )

    payload = seen["payload"]
    assert isinstance(payload, dict)
    assert "image" not in payload


# --- FC1: the request body is a finite thing -------------------------
#
# EC6 put the art *in* the request, which makes the art's file size a
# property of the HTTP call: base64 inflates it by 4/3 and the Gateway
# rejects an oversized body. And even a body that fits can be refused by an
# upstream that has no ``image`` parameter at all. Both must end in a
# picture the player can see, drawn without the lock — never in nothing.


def _noise_png(size: int = 1024) -> bytes:
    """A PNG that is genuinely large: random pixels do not compress."""
    image = Image.frombytes("RGB", (size, size), os.urandom(size * size * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def _replace_reference_bytes(
    storage: InMemoryObjectStorage,
    character: Character,
    content: bytes,
) -> None:
    await storage.put_bytes(
        object_key=storage.object_key_from_url(_reference_url(character)),
        content=content,
        content_type="image/png",
    )


@pytest.mark.asyncio
async def test_an_oversized_reference_is_shrunk_to_fit_the_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Print-resolution art still anchors the likeness — just smaller."""
    repo = InMemoryCharacterRepository()
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    character = await _with_official_art(repo, storage, tmp_path, managed=True)
    original = _noise_png()
    await _replace_reference_bytes(storage, character, original)

    payload, _ = await _capture_payload(
        monkeypatch,
        provider=_cloud_provider(storage, max_reference_image_bytes=400_000),
        character=character,
        user_attachment_urls=(_reference_url(character),),
    )

    assert len(payload["image"]) == 1
    header, _, encoded = payload["image"][0].partition(",")
    shrunk = base64.b64decode(encoded)
    assert len(shrunk) <= 400_000
    assert len(shrunk) < len(original)
    assert header.startswith("data:image/")
    assert "Reference images attached: 1" in payload["prompt"]


@pytest.mark.asyncio
async def test_a_reference_that_cannot_be_shrunk_is_dropped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Unshrinkable is the same outcome as unreadable: draw it anyway."""
    repo = InMemoryCharacterRepository()
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    character = await _with_official_art(repo, storage, tmp_path, managed=True)
    await _replace_reference_bytes(storage, character, b"\x00" * 300_000)

    payload, _ = await _capture_payload(
        monkeypatch,
        provider=_cloud_provider(storage, max_reference_image_bytes=1_000),
        character=character,
        user_attachment_urls=(_reference_url(character),),
    )

    assert "image" not in payload
    assert payload["prompt"]


@pytest.mark.asyncio
async def test_inline_inputs_stop_at_the_request_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The reference art leads, so it is the photo that gets left behind."""
    repo = InMemoryCharacterRepository()
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    character = await _with_official_art(repo, storage, tmp_path, managed=True)
    fat_photo = "data:image/png;base64," + ("Q" * 4_000)

    payload, _ = await _capture_payload(
        monkeypatch,
        provider=_cloud_provider(storage, inline_payload_budget_bytes=2_000),
        character=character,
        user_attachment_urls=(_reference_url(character), fat_photo),
    )

    reference = base64.b64encode(_REFERENCE_BYTES).decode("ascii")
    assert payload["image"] == [f"data:image/png;base64,{reference}"]
    assert fat_photo not in payload["prompt"]


def _gateway_refusing_image_input(
    calls: list[dict],
    *,
    status: int,
    code: str,
    retryable: bool,
):
    """A gateway that answers anything carrying ``image``, and only that."""

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        calls.append(body)
        if "image" in body:
            return httpx.Response(status, json={"error": {
                "code": code,
                "message": "image provider returned 400",
                "retryable": retryable,
            }})
        return httpx.Response(200, json={
            "data": [{"b64_json": base64.b64encode(b"png").decode()}],
        })

    return handler


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        # The upstream images API has no ``image`` parameter: it 400s, and
        # the Gateway relabels that as its own 502 provider_error.
        (502, "provider_error", True),
        # The body cleared our own budget but not the Gateway's inbound cap.
        (413, "payload_too_large", False),
    ],
)
async def test_a_rejected_image_input_degrades_instead_of_failing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status: int,
    code: str,
    retryable: bool,
) -> None:
    """Losing the likeness lock beats the player getting no picture."""
    repo = InMemoryCharacterRepository()
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    character = await _with_official_art(repo, storage, tmp_path, managed=True)
    calls: list[dict] = []
    _install_gateway(monkeypatch, _gateway_refusing_image_input(
        calls, status=status, code=code, retryable=retryable,
    ))

    images = await _cloud_provider(storage).generate(
        character=character,
        positive="at a cafe",
        user_attachment_urls=(_reference_url(character),),
    )

    assert images == [b"png"]
    assert len(calls) == 2, "the degraded retry runs exactly once"
    assert "image" in calls[0]
    assert "image" not in calls[1]
    # A prompt that still claims attached references would be lying to the
    # model about inputs the retry deliberately removed.
    assert "Reference images attached" not in calls[1]["prompt"]
    assert _reference_url(character) not in calls[1]["prompt"]


@pytest.mark.asyncio
async def test_a_deliberate_refusal_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """No credits is not the image's fault; retrying only burns a call."""
    repo = InMemoryCharacterRepository()
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    character = await _with_official_art(repo, storage, tmp_path, managed=True)
    calls: list[dict] = []
    _install_gateway(monkeypatch, _gateway_refusing_image_input(
        calls, status=402, code="insufficient_credits", retryable=False,
    ))

    with pytest.raises(ImageGenerationError):
        await _cloud_provider(storage).generate(
            character=character,
            positive="at a cafe",
            user_attachment_urls=(_reference_url(character),),
        )

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_request_without_image_input_is_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retry exists to drop ``image``; with none there is nothing to drop."""
    calls: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content.decode()))
        return httpx.Response(502, json={"error": {
            "code": "provider_error", "message": "upstream", "retryable": True,
        }})

    _install_gateway(monkeypatch, handler)

    with pytest.raises(ImageGenerationError):
        await _cloud_provider(None).generate(
            character=_character(managed=False), positive="at a cafe",
        )

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_successful_generation_calls_the_gateway_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The happy path must not have grown a second round trip."""
    repo = InMemoryCharacterRepository()
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    character = await _with_official_art(repo, storage, tmp_path, managed=True)
    calls: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={
            "data": [{"b64_json": base64.b64encode(b"png").decode()}],
        })

    _install_gateway(monkeypatch, handler)

    await _cloud_provider(storage).generate(
        character=character,
        positive="at a cafe",
        user_attachment_urls=(_reference_url(character),),
    )

    assert len(calls) == 1
    assert "image" in calls[0]


# --- the four entry points -------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [True, False])
async def test_portrait_entry_point_injects_reference_art(
    tmp_path: Path, managed: bool,
) -> None:
    repo = InMemoryCharacterRepository()
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    character = await _with_official_art(
        repo, storage, tmp_path, managed=managed,
    )
    provider = _CaptureProvider()

    await _image_service(repo, storage, tmp_path, provider).generate_portrait(
        character.id, positive="at a cafe",
    )

    expected = (_reference_url(character),) if managed else ()
    assert provider.attachments == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [True, False])
async def test_gacha_entry_point_injects_reference_art(
    tmp_path: Path, managed: bool,
) -> None:
    repo = InMemoryCharacterRepository()
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    character = await _with_official_art(
        repo, storage, tmp_path, managed=managed,
    )
    provider = _CaptureProvider()

    await _image_service(repo, storage, tmp_path, provider).generate_candidates(
        character.id, positive="at a cafe", count=2,
    )

    expected = (_reference_url(character),) if managed else ()
    assert provider.attachments == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [True, False])
async def test_chat_tool_entry_point_injects_reference_art(
    tmp_path: Path, managed: bool,
) -> None:
    """The player's photo survives the injection; it just goes second."""
    repo = InMemoryCharacterRepository()
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    character = await _with_official_art(
        repo, storage, tmp_path, managed=managed,
    )
    provider = _CaptureProvider()
    tool = ComfyImageTool(
        image_provider=StaticActiveImageProvider(provider),
        uploads_dir=tmp_path,
        object_storage=storage,
    )
    photo = "data:image/png;base64,QUFB"

    await tool.invoke(ToolContext(
        character=character,
        arguments={"positive": "at a cafe"},
        user_attachment_urls=(photo,),
    ))

    expected = (
        (_reference_url(character), photo) if managed else (photo,)
    )
    assert provider.attachments == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [True, False])
async def test_feed_background_entry_point_injects_reference_art(
    tmp_path: Path, managed: bool,
) -> None:
    repo = InMemoryCharacterRepository()
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    character = await _with_official_art(
        repo, storage, tmp_path, managed=managed,
    )
    provider = _CaptureProvider()
    service = FeedComposerService(
        repository=InMemoryFeedPostRepository(),
        candidates=_UnusedFeedCollaborator(),
        composer=_UnusedFeedCollaborator(),
        image_provider=StaticActiveImageProvider(provider),
        object_storage=storage,
    )

    frame = await service.generate_video_first_frame(character, "at a cafe")

    assert frame is not None
    expected = (_reference_url(character),) if managed else ()
    assert provider.attachments == expected
