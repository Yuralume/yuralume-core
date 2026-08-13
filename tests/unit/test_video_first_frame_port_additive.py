"""CV1: ``VideoProviderPort.generate(first_frame_url=...)`` is inert.

The plan's self-host red line (VIDEO_ASYNC_I2V_PLAN §3) is that port
growth must not move a single byte on any existing deployment. The
danger with an optional kwarg is not that someone wires it up on
purpose — it is that a later refactor threads it into a prompt builder
or a payload dict "while we're here" and nobody notices, because the
adapters have no test that says *nothing changes*.

So each of the four shipped adapters gets the same characterization:
issue the identical call twice, once without ``first_frame_url`` and
once with a real-looking URL, and assert the outgoing request and the
returned bytes are identical. Nondeterministic fields (per-call request
ids, per-call seeds) are pinned or excluded so a diff can only come from
the new parameter.
"""

from __future__ import annotations

import base64
import json
import secrets

import httpx
import pytest

from kokoro_link.contracts.generation_trigger import (
    GenerationTrigger,
    generation_trigger_scope,
)
from kokoro_link.contracts.cloud_gateway import (
    CloudGatewayIdentity,
    CloudResourceContext,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.tools.comfyui.video_generator import (
    ComfyVideoGenerator,
)
from kokoro_link.infrastructure.tools.comfyui.wan_video_workflow import (
    WanVideoWorkflowBuilder,
)
from kokoro_link.infrastructure.video.cloud_gateway_provider import (
    CloudGatewayVideoProvider,
)
from kokoro_link.infrastructure.video.external_api_provider import (
    ExternalVideoApiProvider,
)
from kokoro_link.infrastructure.video.google_veo_provider import (
    GoogleVeoVideoProvider,
)

FIRST_FRAME_URL = "https://objects.example/media/first-frame.png"
_VOLATILE_HEADERS = frozenset({"x-request-id", "x-yuralume-trigger"})


class _MockAsyncClient(httpx.AsyncClient):
    def __init__(self, handler, **kwargs) -> None:
        super().__init__(
            transport=httpx.MockTransport(handler),
            timeout=kwargs.get("timeout"),
        )


class _IdentityResolver:
    async def resolve_context(
        self, context: CloudResourceContext,
    ) -> CloudGatewayIdentity:
        return CloudGatewayIdentity(
            operator_id=context.operator_id,
            account_id="acct_1",
            tenant_id="tenant_1",
            character_ref="chr_abc",
        )


class _FakeComfyClient:
    def __init__(self) -> None:
        self.queued_prompts: list[dict] = []

    async def queue_prompt(self, prompt: dict) -> str:
        self.queued_prompts.append(prompt)
        return "pid-1"

    async def wait_for_completion(self, prompt_id: str) -> dict:
        return {
            "outputs": {
                "31": {
                    "images": [{
                        "filename": "clip_00001_.mp4",
                        "subfolder": "kokoro/feed",
                        "type": "output",
                    }],
                    "animated": [True],
                },
            },
        }

    async def download_image(
        self, *, filename: str, subfolder: str, folder_type: str,
    ) -> bytes:
        return b"FAKE_MP4_BYTES"


def _character() -> Character:
    return Character.create(
        name="Probe",
        summary="A companion",
        user_id="cloud:acct_1",
        personality=[], interests=[], speaking_style="gentle",
        boundaries=[],
        appearance="short dark hair",
        state=CharacterState(
            emotion="calm", affection=50, fatigue=0, trust=50, energy=80,
        ),
    )


def _recorded(request: httpx.Request) -> dict:
    """Everything about an outgoing request that a new kwarg could move."""
    headers = {
        name: value for name, value in request.headers.items()
        if name.lower() not in _VOLATILE_HEADERS
    }
    body = request.content.decode() if request.content else ""
    try:
        parsed = json.loads(body) if body else None
    except json.JSONDecodeError:
        parsed = body
    return {
        "method": request.method,
        "url": str(request.url),
        "headers": headers,
        "body": parsed,
    }


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )


@pytest.mark.asyncio
async def test_external_api_adapter_ignores_first_frame_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_recorded(request))
        return httpx.Response(200, json={
            "data": [{"b64_json": base64.b64encode(b"mp4").decode()}],
        })

    _install_transport(monkeypatch, handler)
    provider = ExternalVideoApiProvider(
        base_url="https://api.example",
        api_key="key",
        model="wan-2.2",
    )

    plain = await provider.generate(
        character=_character(), positive="a walk downtown",
        aspect="landscape", length_frames=80,
    )
    with_frame = await provider.generate(
        character=_character(), positive="a walk downtown",
        aspect="landscape", length_frames=80,
        first_frame_url=FIRST_FRAME_URL,
    )

    assert plain == with_frame == b"mp4"
    assert len(seen) == 2
    assert seen[0] == seen[1]
    assert FIRST_FRAME_URL not in json.dumps(seen[1])


@pytest.mark.asyncio
async def test_google_veo_adapter_ignores_first_frame_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_recorded(request))
        if request.method == "POST":
            return httpx.Response(200, json={"name": "operations/op-1"})
        return httpx.Response(200, json={
            "done": True,
            "response": {
                "generateVideoResponse": {
                    "generatedSamples": [
                        {"video": {"videoBytes": base64.b64encode(
                            b"veo-mp4",
                        ).decode()}},
                    ],
                },
            },
        })

    _install_transport(monkeypatch, handler)
    provider = GoogleVeoVideoProvider(api_key="key")

    plain = await provider.generate(
        character=_character(), positive="a walk downtown", length_frames=80,
    )
    with_frame = await provider.generate(
        character=_character(), positive="a walk downtown", length_frames=80,
        first_frame_url=FIRST_FRAME_URL,
    )

    assert plain == with_frame == b"veo-mp4"
    assert len(seen) == 4
    assert seen[0:2] == seen[2:4]
    assert FIRST_FRAME_URL not in json.dumps(seen[2:4])


@pytest.mark.asyncio
async def test_cloud_gateway_adapter_ignores_first_frame_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cloud adapter is the one that *does* grow i2v — but only on the
    async job path. Its synchronous ``/v1/videos/generations`` call must
    stay exactly as it was."""
    seen: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_recorded(request))
        return httpx.Response(200, json={
            "duration_seconds": 5,
            "data": [{"b64_json": base64.b64encode(b"mp4").decode()}],
        })

    _install_transport(monkeypatch, handler)
    provider = CloudGatewayVideoProvider(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        preset="yuralume-video",
        feature_key="video_feed",
        identity_resolver=_IdentityResolver(),
    )

    with generation_trigger_scope(GenerationTrigger.BACKGROUND):
        plain = await provider.generate(
            character=_character(), positive="a walk downtown",
            aspect="landscape", length_frames=80,
        )
        plain_duration = provider.last_duration_seconds
        with_frame = await provider.generate(
            character=_character(), positive="a walk downtown",
            aspect="landscape", length_frames=80,
            first_frame_url=FIRST_FRAME_URL,
        )

    assert plain == with_frame == b"mp4"
    assert provider.last_duration_seconds == plain_duration == 5
    assert len(seen) == 2
    assert seen[0] == seen[1]
    assert seen[0]["url"] == (
        "https://gateway.example/v1/videos/generations"
    )
    assert FIRST_FRAME_URL not in json.dumps(seen[1])


@pytest.mark.asyncio
async def test_comfy_wan_adapter_ignores_first_frame_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Wan2.2 picks a fresh seed per call by design; pin it so the only
    # possible difference between the two graphs is the new kwarg.
    monkeypatch.setattr(secrets, "randbelow", lambda upper: 4242)

    client = _FakeComfyClient()
    generator = ComfyVideoGenerator(
        client=client,  # type: ignore[arg-type]
        workflow_builder=WanVideoWorkflowBuilder(),
    )
    # One instance for both calls: the save-node filename prefix embeds
    # ``character.id``, which ``Character.create`` mints fresh each time.
    character = _character()

    plain = await generator.generate(
        character=character, positive="a girl tilts her head",
    )
    with_frame = await generator.generate(
        character=character, positive="a girl tilts her head",
        first_frame_url=FIRST_FRAME_URL,
    )

    assert plain == with_frame == b"FAKE_MP4_BYTES"
    assert len(client.queued_prompts) == 2
    assert client.queued_prompts[0] == client.queued_prompts[1]
    assert FIRST_FRAME_URL not in json.dumps(client.queued_prompts[1])
