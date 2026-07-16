"""BDD: OpenAICompatibleChatModel model selection behaviour.

Covers the per-call ``model`` override and the ``list_models()``
lookup that the new UI "model under provider" dropdown depends on.
We don't hit a real network: ``httpx.MockTransport`` serves canned
payloads for both ``/chat/completions`` and ``/models``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from kokoro_link.contracts.llm import ReasoningOverrides
from kokoro_link.infrastructure.llm.openai_compatible import (
    OpenAICompatibleChatModel,
)


def _build(model: str = "default-model") -> OpenAICompatibleChatModel:
    return OpenAICompatibleChatModel(
        provider_id="lmstudio",
        base_url="http://127.0.0.1:1234/v1",
        api_key=None,
        model=model,
    )


def _patch_transport(transport: httpx.MockTransport) -> Any:
    """Force every fresh ``httpx.AsyncClient`` to use the given mock.

    Matches the pattern used in ``test_webfetch_tool.py`` — keeps test
    plumbing out of the production signature."""
    original_init = httpx.AsyncClient.__init__

    def patched_init(self: httpx.AsyncClient, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        original_init(self, **kwargs)

    class _Ctx:
        def __enter__(self) -> None:
            httpx.AsyncClient.__init__ = patched_init  # type: ignore[method-assign]

        def __exit__(self, *_: Any) -> None:
            httpx.AsyncClient.__init__ = original_init  # type: ignore[method-assign]

    return _Ctx()


# ---- per-call model override -----------------------------------------


def test_default_model_used_when_no_override() -> None:
    model = _build("default-model")
    payload = model._build_payload("hi")
    assert payload["model"] == "default-model"


def test_explicit_override_wins_over_default() -> None:
    model = _build("default-model")
    payload = model._build_payload("hi", model="override-xyz")
    assert payload["model"] == "override-xyz"


def test_empty_string_override_falls_back_to_default() -> None:
    """Frontend may send ``""`` to mean "use the default" — we want the
    adapter to treat that the same as ``None``, not ship an empty model
    name (which every OpenAI-compatible server rejects)."""
    model = _build("default-model")
    payload = model._build_payload("hi", model="   ")
    assert payload["model"] == "default-model"


@pytest.mark.asyncio
async def test_non_chat_override_retries_with_default_model() -> None:
    requested_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        requested_models.append(payload["model"])
        if payload["model"] == "text-embedding-3-small":
            return httpx.Response(
                404,
                json={
                    "error": {
                        "message": "This is not a chat model and thus not supported in the v1/chat/completions endpoint.",
                        "type": "invalid_request_error",
                        "param": "model",
                        "code": None,
                    },
                },
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
        )

    model = _build("gpt-4o-mini")
    with _patch_transport(httpx.MockTransport(handler)):
        result = await model.generate("hi", model="text-embedding-3-small")
        result_after_cache = await model.generate(
            "hi",
            model="text-embedding-3-small",
        )

    assert result == "ok"
    assert result_after_cache == "ok"
    assert requested_models == [
        "text-embedding-3-small",
        "gpt-4o-mini",
        "gpt-4o-mini",
    ]


# ---- reasoning controls (payload shape) ------------------------------


def test_reasoning_fields_absent_by_default() -> None:
    """Regression pin: with no reasoning opt-ins the payload is exactly
    what it was before this feature — no reasoning keys leak in."""
    model = _build()
    payload = model._build_payload("hi")
    assert "chat_template_kwargs" not in payload
    assert "reasoning_effort" not in payload
    assert set(payload) == {"model", "messages"}


def test_disable_reasoning_emits_chat_template_kwargs() -> None:
    model = OpenAICompatibleChatModel(
        provider_id="lmstudio",
        base_url="http://127.0.0.1:1234/v1",
        api_key=None,
        model="m",
        disable_reasoning=True,
    )
    payload = model._build_payload("hi")
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}


def test_reasoning_effort_passed_through_verbatim() -> None:
    model = OpenAICompatibleChatModel(
        provider_id="lmstudio",
        base_url="http://127.0.0.1:1234/v1",
        api_key=None,
        model="m",
        reasoning_effort="low",
    )
    payload = model._build_payload("hi")
    assert payload["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_reasoning_effort_preflight_uses_selected_model_and_value() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "OK"}}]},
        )

    model = _build("default-model")
    with _patch_transport(httpx.MockTransport(handler)):
        await model.validate_reasoning_effort(
            "xhigh", model="gpt-5.6-luna",
        )

    assert requests[0]["model"] == "gpt-5.6-luna"
    assert requests[0]["reasoning_effort"] == "xhigh"


@pytest.mark.asyncio
async def test_reasoning_effort_preflight_surfaces_upstream_rejection() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "unsupported reasoning effort",
                    "param": "reasoning_effort",
                },
            },
        )

    model = _build("default-model")
    with _patch_transport(httpx.MockTransport(handler)):
        with pytest.raises(ValueError, match="unsupported reasoning effort"):
            await model.validate_reasoning_effort(
                "max", model="gpt-5.6-luna",
            )


def test_extra_request_params_merged_into_payload() -> None:
    model = OpenAICompatibleChatModel(
        provider_id="lmstudio",
        base_url="http://127.0.0.1:1234/v1",
        api_key=None,
        model="m",
        extra_request_params={"top_k": 40},
    )
    payload = model._build_payload("hi")
    assert payload["top_k"] == 40


def test_extra_request_params_can_override_earlier_keys() -> None:
    """Advanced-user intent wins: extra params merge last so they can
    deliberately reshape reasoning keys."""
    model = OpenAICompatibleChatModel(
        provider_id="lmstudio",
        base_url="http://127.0.0.1:1234/v1",
        api_key=None,
        model="m",
        reasoning_effort="low",
        extra_request_params={"reasoning_effort": "high"},
    )
    payload = model._build_payload("hi")
    assert payload["reasoning_effort"] == "high"


def test_three_reasoning_fields_are_independent() -> None:
    model = OpenAICompatibleChatModel(
        provider_id="lmstudio",
        base_url="http://127.0.0.1:1234/v1",
        api_key=None,
        model="m",
        disable_reasoning=True,
        reasoning_effort="medium",
        extra_request_params={"foo": "bar"},
    )
    payload = model._build_payload("hi")
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["reasoning_effort"] == "medium"
    assert payload["foo"] == "bar"


# ---- routing-level reasoning override (with_reasoning_overrides) -----


def test_reasoning_override_replaces_connection_trio() -> None:
    """A routing-level override takes over the WHOLE reasoning posture:
    the connection's ``disable_reasoning=True`` must not leak into a
    bound copy that only sets ``reasoning_effort``."""
    base = OpenAICompatibleChatModel(
        provider_id="lmstudio",
        base_url="http://127.0.0.1:1234/v1",
        api_key=None,
        model="m",
        disable_reasoning=True,
        reasoning_effort="low",
    )
    bound = base.with_reasoning_overrides(
        ReasoningOverrides(reasoning_effort="high"),
    )
    payload = bound._build_payload("hi")
    assert payload["reasoning_effort"] == "high"
    assert "chat_template_kwargs" not in payload


def test_reasoning_override_leaves_base_adapter_untouched() -> None:
    base = OpenAICompatibleChatModel(
        provider_id="lmstudio",
        base_url="http://127.0.0.1:1234/v1",
        api_key=None,
        model="m",
        reasoning_effort="low",
    )
    base.with_reasoning_overrides(
        ReasoningOverrides(disable_reasoning=True),
    )
    payload = base._build_payload("hi")
    assert payload["reasoning_effort"] == "low"
    assert "chat_template_kwargs" not in payload


def test_reasoning_override_keeps_connection_level_filters() -> None:
    """``strip_think_tags`` / ``extra_request_params`` are endpoint
    properties — a routing override must carry them through."""
    base = OpenAICompatibleChatModel(
        provider_id="lmstudio",
        base_url="http://127.0.0.1:1234/v1",
        api_key=None,
        model="m",
        extra_request_params={"top_k": 40},
        strip_think_tags=True,
    )
    bound = base.with_reasoning_overrides(
        ReasoningOverrides(disable_reasoning=True),
    )
    payload = bound._build_payload("hi")
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["top_k"] == 40
    assert bound._strip_think_tags is True


def test_reasoning_override_shares_non_chat_model_memory() -> None:
    """Bound copies are created per resolve() call — the learned
    "model X is not a chat model" set must be shared with the base
    adapter or every call would re-pay the failed probe request."""
    base = _build("default-model")
    bound = base.with_reasoning_overrides(
        ReasoningOverrides(reasoning_effort="high"),
    )
    bound._remember_non_chat_override("embedding-model")
    payload = base._build_payload("hi", model="embedding-model")
    assert payload["model"] == "default-model"


# ---- strip_think_tags (non-stream generate) --------------------------


@pytest.mark.asyncio
async def test_generate_strips_think_block_when_enabled() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [
                {"message": {"content": "Hi <think>secret</think>there"}},
            ],
        })

    model = OpenAICompatibleChatModel(
        provider_id="lmstudio",
        base_url="http://127.0.0.1:1234/v1",
        api_key=None,
        model="m",
        strip_think_tags=True,
    )
    with _patch_transport(httpx.MockTransport(handler)):
        out = await model.generate("hi")
    assert out == "Hi there"


@pytest.mark.asyncio
async def test_generate_keeps_think_block_when_disabled() -> None:
    """Default (opt-out) keeps content byte-for-byte, including a literal
    ``<think>`` a user might legitimately write."""
    raw = "Hi <think>secret</think>there"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": raw}}],
        })

    model = _build()
    with _patch_transport(httpx.MockTransport(handler)):
        out = await model.generate("hi")
    assert out == raw


@pytest.mark.asyncio
async def test_generate_stream_strips_think_across_chunks() -> None:
    sse = (
        'data: {"choices":[{"delta":{"content":"Hi <thi"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"nk>hidden</think>the"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"re"}}]}\n\n'
        'data: [DONE]\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=sse.encode("utf-8"),
            headers={"content-type": "text/event-stream"},
        )

    model = OpenAICompatibleChatModel(
        provider_id="lmstudio",
        base_url="http://127.0.0.1:1234/v1",
        api_key=None,
        model="m",
        strip_think_tags=True,
    )
    chunks: list[str] = []
    with _patch_transport(httpx.MockTransport(handler)):
        async for piece in model.generate_stream("hi"):
            chunks.append(piece)
    assert "".join(chunks) == "Hi there"


@pytest.mark.asyncio
async def test_generate_stream_untouched_when_strip_disabled() -> None:
    sse = (
        'data: {"choices":[{"delta":{"content":"Hi <think>x</think>"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"there"}}]}\n\n'
        'data: [DONE]\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=sse.encode("utf-8"),
            headers={"content-type": "text/event-stream"},
        )

    model = _build()
    chunks: list[str] = []
    with _patch_transport(httpx.MockTransport(handler)):
        async for piece in model.generate_stream("hi"):
            chunks.append(piece)
    assert "".join(chunks) == "Hi <think>x</think>there"


# ---- list_models -----------------------------------------------------


@pytest.mark.asyncio
async def test_list_models_parses_openai_shape() -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return httpx.Response(200, json={
            "data": [
                {"id": "gemma-4-31b"},
                {"id": "qwen-3-7b"},
                {"id": "llama-4-70b"},
            ],
        })

    model = _build("gemma-4-31b")
    with _patch_transport(httpx.MockTransport(handler)):
        models = await model.list_models()

    assert models == ["gemma-4-31b", "qwen-3-7b", "llama-4-70b"]
    assert captured and captured[0].endswith("/models")


@pytest.mark.asyncio
async def test_list_models_inserts_default_when_missing() -> None:
    """LM Studio may omit unloaded models; the default must still be
    clickable in the UI so the operator can load + pick it."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "data": [{"id": "other-model"}],
        })

    model = _build("my-default")
    with _patch_transport(httpx.MockTransport(handler)):
        models = await model.list_models()

    assert "my-default" in models
    assert models[0] == "my-default"


@pytest.mark.asyncio
async def test_list_models_moves_default_to_front_when_provider_lists_it_later() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "data": [
                {"id": "text-embedding-3-small"},
                {"id": "gpt-4o-mini"},
                {"id": "gpt-image-2"},
            ],
        })

    model = _build("gpt-4o-mini")
    with _patch_transport(httpx.MockTransport(handler)):
        models = await model.list_models()

    assert models == [
        "gpt-4o-mini",
        "text-embedding-3-small",
        "gpt-image-2",
    ]


@pytest.mark.asyncio
async def test_list_models_returns_default_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    model = _build("fallback-model")
    with _patch_transport(httpx.MockTransport(handler)):
        models = await model.list_models()

    assert models == ["fallback-model"]


@pytest.mark.asyncio
async def test_list_models_cached_within_ttl() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"data": [{"id": "x"}]})

    model = _build("x")
    with _patch_transport(httpx.MockTransport(handler)):
        await model.list_models()
        await model.list_models()

    # Second call served from cache.
    assert len(calls) == 1
