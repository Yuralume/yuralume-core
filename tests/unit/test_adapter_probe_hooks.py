"""Adapter-owned probe hook tests (httpx.MockTransport-driven).

The 2026-07-16 unification moved probe request shapes INTO the runtime
adapters (``probe_chat`` / ``probe_embedding`` / ``probe_tts`` /
``probe_image_generation``). These tests pin the core property the
architecture exists for: the probe inherits the adapter's own
signal-driven retry/memo machinery, so a quirk fixed in the adapter can
never again be missed by the probe (the max_tokens incident).
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import httpx

from kokoro_link.infrastructure.embedder.lm_studio import LMStudioEmbedder
from kokoro_link.infrastructure.image.xai_provider import XAIImageProvider
from kokoro_link.infrastructure.llm.anthropic import AnthropicChatModel
from kokoro_link.infrastructure.llm.openai_compatible import (
    OpenAICompatibleChatModel,
)
from kokoro_link.infrastructure.tts.external_api import (
    ExternalTTSAdapter,
    OpenAITTSAdapter,
)


def _chat_model(**overrides: Any) -> OpenAICompatibleChatModel:
    kwargs: dict[str, Any] = {
        "provider_id": "openai",
        "base_url": "https://api.example.test/v1",
        "api_key": "sk-unit",
        "model": "gpt-x",
    }
    kwargs.update(overrides)
    return OpenAICompatibleChatModel(**kwargs)


# ---------------------------------------------------------------------------
# probe_chat — retry inheritance via the adapter's own machinery
# ---------------------------------------------------------------------------


def test_probe_chat_inherits_max_completion_tokens_rename() -> None:
    """The rename happens inside probe_chat via the adapter's
    ``_adapted_payload_for_rejection`` (not probe-local code) and the
    lesson is memoized on the instance for later runtime calls."""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "gpt-x"}]})
        body = json.loads(request.content)
        bodies.append(body)
        if "max_tokens" in body:
            return httpx.Response(400, json={"error": {
                "message": "Unsupported parameter: 'max_tokens'. "
                "Use 'max_completion_tokens' instead.",
            }})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}],
        })

    model = _chat_model()
    checks = asyncio.run(
        model.probe_chat(transport=httpx.MockTransport(handler)),
    )

    assert [(c.action, c.ok) for c in checks] == [
        ("listed_models", True),
        ("chat_completion", True),
    ]
    assert "max_completion_tokens" in checks[1].detail
    assert [("max_tokens" in b, "max_completion_tokens" in b) for b in bodies] == [
        (True, False),
        (False, True),
    ]
    # The lesson lives on the adapter instance — runtime generate()
    # would now skip the failed round.
    assert model._max_tokens_param == "max_completion_tokens"


def test_probe_chat_inherits_system_role_merge_retry() -> None:
    """A retry the old probe-local payload could never exercise (it sent
    no system message at all): the Gemma-2-class system-role rejection
    is healed by the adapter's merge memo, inside the probe."""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "gpt-x"}]})
        body = json.loads(request.content)
        bodies.append(body)
        roles = [m.get("role") for m in body["messages"]]
        if "system" in roles:
            return httpx.Response(400, text="System role not supported")
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}],
        })

    model = _chat_model()
    checks = asyncio.run(
        model.probe_chat(transport=httpx.MockTransport(handler)),
    )

    assert checks[1].ok is True
    assert len(bodies) == 2
    assert [m.get("role") for m in bodies[1]["messages"]] == ["user"]
    # System prompt merged into the user turn, memoized on the instance.
    assert bodies[1]["messages"][0]["content"].startswith(
        "You are a roleplay character backend.",
    )
    # Model-scoped memo (keyed by the resolved model the probe sent).
    assert model._quirks_for("gpt-x").merge_system_into_user is True


def test_probe_chat_carries_configured_reasoning_knobs() -> None:
    """The probe payload is built by ``_build_payload``, so the opt-in
    knobs the runtime would send (disable_reasoning etc.) ride along —
    a knob-induced upstream rejection fails the Test button instead of
    the first real chat."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}],
        })

    model = _chat_model(disable_reasoning=True, reasoning_effort="high")
    checks = asyncio.run(
        model.probe_chat(transport=httpx.MockTransport(handler)),
    )

    assert checks[1].ok is True
    assert seen["chat_template_kwargs"] == {"enable_thinking": False}
    assert seen["reasoning_effort"] == "high"
    assert seen["max_tokens"] == 1  # probe cap via max_tokens_override


def test_probe_chat_cap_does_not_change_generate_semantics() -> None:
    """The 1-token probe cap is an internal override: a runtime
    generate() on the same instance still omits max_tokens when the
    operator never configured one."""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}],
        })

    model = _chat_model()
    transport = httpx.MockTransport(handler)
    asyncio.run(model.probe_chat(transport=transport))

    async def _generate() -> str:
        real_client = httpx.AsyncClient

        def patched(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs.pop("transport", None)
            return real_client(transport=transport, **kwargs)

        httpx.AsyncClient = patched  # type: ignore[misc]
        try:
            return await model.generate("hello")
        finally:
            httpx.AsyncClient = real_client  # type: ignore[misc]

    assert asyncio.run(_generate()) == "ok"
    assert "max_tokens" in bodies[0]  # probe capped
    assert "max_tokens" not in bodies[1]  # runtime untouched
    assert "max_completion_tokens" not in bodies[1]


# ---------------------------------------------------------------------------
# Anthropic probe_chat — URL parity with the runtime adapter
# ---------------------------------------------------------------------------


def test_anthropic_probe_and_runtime_share_url_rule() -> None:
    """A pasted base_url ending in /v1 resolves to /v1/messages for BOTH
    the probe hook and runtime generate() — the probe can no longer
    green-light a config the runtime would 404 on."""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        body = json.loads(request.content)
        assert "thinking" not in body  # probe cap can't satisfy budget<max
        return httpx.Response(
            200, json={"content": [{"type": "text", "text": "pong"}]},
        )

    model = AnthropicChatModel(
        api_key="sk-ant-unit",
        base_url="https://api.anthropic.com/v1",  # pasted /v1 suffix
        thinking_budget_tokens=2048,
    )
    checks = asyncio.run(
        model.probe_chat(transport=httpx.MockTransport(handler)),
    )

    assert [(c.action, c.ok) for c in checks] == [("chat_completion", True)]
    assert paths == ["/v1/messages"]  # not /v1/v1/messages
    # Runtime resolves the identical URL from the identical config.
    assert model._api_url("messages") == "https://api.anthropic.com/v1/messages"


def test_anthropic_probe_caps_max_tokens_to_one() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200, json={"content": [{"type": "text", "text": "pong"}]},
        )

    model = AnthropicChatModel(api_key="sk-ant-unit", max_tokens=4096)
    checks = asyncio.run(
        model.probe_chat(transport=httpx.MockTransport(handler)),
    )

    assert checks[0].ok is True
    assert seen["max_tokens"] == 1
    assert seen["system"]  # runtime shape: top-level system prompt rides along


# ---------------------------------------------------------------------------
# embedder / tts hooks
# ---------------------------------------------------------------------------


def test_probe_embedding_flags_dimension_mismatch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"embedding": [0.0] * 1536}]})

    embedder = LMStudioEmbedder(
        base_url="http://127.0.0.1:1234/v1",
        model="text-embedding-3-small",
        dimension=1024,
    )
    checks = asyncio.run(
        embedder.probe_embedding(transport=httpx.MockTransport(handler)),
    )

    assert [(c.action, c.ok) for c in checks] == [("embedded", False)]
    assert "1536" in checks[0].detail
    assert "1024" in checks[0].detail
    assert "Request dimensions" in checks[0].detail


def test_probe_embedding_sends_runtime_payload() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"embedding": [0.0] * 1024}]})

    embedder = LMStudioEmbedder(
        base_url="http://127.0.0.1:1234/v1",
        model="text-embedding-3-small",
        dimension=1024,
        request_dimensions=True,
    )
    checks = asyncio.run(
        embedder.probe_embedding(transport=httpx.MockTransport(handler)),
    )

    assert checks[0].ok is True
    assert seen == {
        "model": "text-embedding-3-small",
        "input": ["ping"],
        "dimensions": 1024,
    }


def test_openai_tts_probe_synthesizes_with_runtime_payload() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/audio/speech"
        seen.update(json.loads(request.content))
        return httpx.Response(200, content=b"RIFF0000WAVE")

    adapter = OpenAITTSAdapter(api_key="sk-unit")
    checks = asyncio.run(
        adapter.probe_tts(transport=httpx.MockTransport(handler)),
    )

    assert [(c.action, c.ok) for c in checks] == [("synthesized_speech", True)]
    assert "12 bytes" in checks[0].detail
    assert seen == {
        "model": "gpt-4o-mini-tts",
        "voice": "marin",
        "input": "Hi",
        "response_format": "wav",
    }


def test_custom_tts_probe_lists_voices_only() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        assert request.url.path == "/v1/voices"
        return httpx.Response(200, json={"voices": [{"id": "v1"}]})

    adapter = ExternalTTSAdapter(base_url="https://tts.example.test/v1")
    checks = asyncio.run(
        adapter.probe_tts(transport=httpx.MockTransport(handler)),
    )

    assert [(c.action, c.ok) for c in checks] == [("listed_voices", True)]
    assert checks[0].detail == "1 voices"
    assert methods == ["GET"]  # never a billed synthesis


# ---------------------------------------------------------------------------
# image hook — signal-driven memo shared with the runtime path
# ---------------------------------------------------------------------------


def test_xai_probe_learns_aspect_ratio_drop_on_instance() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "aspect_ratio" in body:
            return httpx.Response(400, json={
                "code": "400",
                "error": "Argument not supported: aspect_ratio",
            })
        return httpx.Response(200, json={
            "data": [{"b64_json": base64.b64encode(b"png").decode()}],
        })

    provider = XAIImageProvider(api_key="xai-unit", model="grok-2-image-1212")
    checks = asyncio.run(
        provider.probe_image_generation(
            prompt="a tiny plain blue circle",
            transport=httpx.MockTransport(handler),
        ),
    )

    assert [(c.action, c.ok) for c in checks] == [("generated_image", True)]
    assert len(bodies) == 2
    assert "aspect_ratio" not in bodies[1]
    # The memo is the ADAPTER's own — runtime generate() on this
    # instance would skip the failed round too.
    assert provider._send_aspect_ratio is False
