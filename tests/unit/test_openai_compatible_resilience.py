"""Signal-driven resilience of OpenAICompatibleChatModel (2026-07-16).

Covers the four adapter self-heals added after the provider
compatibility audit (docs/PROVIDER_COMPAT_AUDIT.md):

* stream-verification fallback — OpenAI 400 "must be verified to
  stream" → non-stream request served as a single chunk, memoized;
* unrecognized-argument drop-and-retry — 400/422 naming a param we
  actually sent → drop it, memoize, retry once;
* Responses-only model signal — "only supported in v1/responses"
  (param=model) joins "not a chat model" in the override fallback;
* system-role rejection merge — chat templates that refuse the system
  role → system prompt merged into the user turn, memoized.

Plus the hardening rounds on top of them:

* learned quirks are MODEL-scoped (one aggregator connection fronts many
  models — a lesson from one must not degrade its siblings) yet still
  shared across per-call clones;
* an adaptation that reproduces the exact payload that just failed
  (e.g. ``max_tokens`` re-entering via ``extra_request_params``) stops
  the loop instead of hammering to ``_MAX_PRESCRIBED_RETRIES``;
* unrecognized-param extraction is anchored to the marker phrase, so a
  server echoing our request cannot get unrelated params dropped;
* the stream→non-stream fallback never yields a non-str (``content:
  null`` refusals end the stream with zero chunks).

Every branch asserts three things: the signal fires (retry/fallback),
an unrelated error stays untouched, and the memo persists across calls.
No real network: ``httpx.MockTransport`` serves canned rejections.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from kokoro_link.contracts.llm import ReasoningOverrides
from kokoro_link.infrastructure.llm.openai_compatible import (
    OpenAICompatibleChatModel,
)

_LOGGER_NAME = "kokoro_link.infrastructure.llm.openai_compatible"


def _patch_transport(transport: httpx.MockTransport) -> Any:
    """Force every fresh ``httpx.AsyncClient`` to use the given mock.

    Mirrors the helper in ``test_openai_compatible_models.py`` — respx
    isn't a dependency, so we swap ``AsyncClient.__init__`` for the
    duration of the test."""
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


def _build(model: str = "gpt-5-mini", **kwargs: Any) -> OpenAICompatibleChatModel:
    return OpenAICompatibleChatModel(
        provider_id="openai",
        base_url="https://api.example.invalid/v1",
        api_key="sk-test",
        model=model,
        **kwargs,
    )


def _chat_ok(text: str = "ok") -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"content": text}}],
    })


def _sse_ok(text: str = "ok") -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=(
            f'data: {{"choices":[{{"delta":{{"content":"{text}"}}}}]}}\n\n'
            "data: [DONE]\n\n"
        ).encode("utf-8"),
    )


# ---------------------------------------------------------------------------
# 1. stream-verification fallback (org must be verified to stream)
# ---------------------------------------------------------------------------


_STREAM_VERIFICATION_BODY = {
    "error": {
        "message": (
            "Your organization must be verified to stream this model. "
            "Please go to: https://platform.openai.com/settings/"
            "organization/general and click on Verify Organization."
        ),
        "type": "invalid_request_error",
        "param": "stream",
        "code": "unsupported_value",
    },
}


def _verification_handler(bodies: list[dict]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        bodies.append(body)
        if body.get("stream"):
            return httpx.Response(400, json=_STREAM_VERIFICATION_BODY)
        return _chat_ok("full completion")
    return handler


@pytest.mark.asyncio
async def test_stream_verification_falls_back_to_single_chunk() -> None:
    bodies: list[dict] = []
    chat = _build()
    with _patch_transport(httpx.MockTransport(_verification_handler(bodies))):
        chunks = [c async for c in chat.generate_stream("hi")]

    assert chunks == ["full completion"], "one chunk carrying the full text"
    assert bodies[0].get("stream") is True
    assert "stream" not in bodies[1], "fallback request must be non-stream"
    assert len(bodies) == 2


@pytest.mark.asyncio
async def test_stream_verification_memo_skips_stream_on_later_calls() -> None:
    bodies: list[dict] = []
    chat = _build()
    with _patch_transport(httpx.MockTransport(_verification_handler(bodies))):
        [c async for c in chat.generate_stream("hi")]
        chunks = [c async for c in chat.generate_stream("hi again")]

    assert chunks == ["full completion"]
    # Call 1: failed stream + non-stream fallback. Call 2: straight to
    # non-stream — no re-paid streaming rejection.
    assert len(bodies) == 3
    assert "stream" not in bodies[2]


@pytest.mark.asyncio
async def test_stream_verification_warns_operator_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bodies: list[dict] = []
    chat = _build()
    with _patch_transport(httpx.MockTransport(_verification_handler(bodies))):
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            [c async for c in chat.generate_stream("hi")]
            [c async for c in chat.generate_stream("hi again")]

    hits = [
        r for r in caplog.records
        if "organization verification" in r.getMessage()
    ]
    assert len(hits) == 1, "the degradation warning fires exactly once"


@pytest.mark.asyncio
async def test_unrelated_stream_400_does_not_fall_back() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"error": {"message": "bad prompt"}})

    chat = _build()
    with _patch_transport(httpx.MockTransport(handler)):
        with pytest.raises(httpx.HTTPStatusError):
            async for _ in chat.generate_stream("hi"):
                pass

    assert calls["n"] == 1
    assert chat._quirks_for("gpt-5-mini").non_stream_fallback is False


@pytest.mark.asyncio
async def test_non_stream_generate_ignores_verification_wording() -> None:
    """The verification signal requires ``stream: true`` in OUR payload —
    a non-stream 400 with similar wording must surface as a plain error,
    not flip the fallback memo."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=_STREAM_VERIFICATION_BODY)

    chat = _build()
    with _patch_transport(httpx.MockTransport(handler)):
        with pytest.raises(httpx.HTTPStatusError):
            await chat.generate("hi")
    assert chat._quirks_for("gpt-5-mini").non_stream_fallback is False


@pytest.mark.asyncio
async def test_stream_fallback_with_null_content_yields_no_chunks() -> None:
    """Upstream refusal / tool-call-only completions carry ``content:
    null`` — the non-stream fallback must not leak ``None`` into the
    ``AsyncIterator[str]``. Covers BOTH yield sites: the post-rejection
    fallback on the first call and the memoized fast path on the second."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if body.get("stream"):
            return httpx.Response(400, json=_STREAM_VERIFICATION_BODY)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": None}}],
        })

    chat = _build()
    with _patch_transport(httpx.MockTransport(handler)):
        chunks = [c async for c in chat.generate_stream("hi")]
        chunks_memoized = [c async for c in chat.generate_stream("hi again")]

    assert chunks == [], "stream ends cleanly with zero chunks"
    assert chunks_memoized == [], "memoized fallback path too"


# ---------------------------------------------------------------------------
# 2. unrecognized-argument drop-and-retry
# ---------------------------------------------------------------------------


def _unrecognized(name: str) -> httpx.Response:
    return httpx.Response(400, json={"error": {
        "message": f"Unrecognized request argument supplied: {name}",
        "type": "invalid_request_error",
    }})


@pytest.mark.asyncio
async def test_generate_drops_named_param_and_retries() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        bodies.append(body)
        if "chat_template_kwargs" in body:
            return _unrecognized("chat_template_kwargs")
        return _chat_ok()

    chat = _build(disable_reasoning=True)
    with _patch_transport(httpx.MockTransport(handler)):
        assert await chat.generate("hi") == "ok"
        # Memoized: the second call must never re-send the dropped key.
        assert await chat.generate("hi again") == "ok"

    assert "chat_template_kwargs" in bodies[0]
    assert "chat_template_kwargs" not in bodies[1]
    assert len(bodies) == 3, "second generate must not re-hit the rejection"
    assert "chat_template_kwargs" not in bodies[2]


@pytest.mark.asyncio
async def test_drop_requires_named_param_present_in_payload() -> None:
    """The server names a param we never sent → nothing to drop, no
    retry, the 400 surfaces as-is."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _unrecognized("foobar_param")

    chat = _build(disable_reasoning=True)
    with _patch_transport(httpx.MockTransport(handler)):
        with pytest.raises(httpx.HTTPStatusError):
            await chat.generate("hi")
    assert calls["n"] == 1
    assert chat._quirks_for("gpt-5-mini").dropped_params == set()


@pytest.mark.asyncio
async def test_echoed_request_dump_does_not_drop_unrelated_params() -> None:
    """The server names 'bogus' (never sent) as unrecognized AND echoes
    our full request in the error body. Extraction is anchored to the
    marker phrase — params merely mentioned in the echoed dump
    (reasoning_effort here) must not be treated as drop prescriptions."""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        bodies.append(body)
        return httpx.Response(400, json={"error": {
            "message": (
                "Unrecognized request argument supplied: bogus. "
                f"Request was: {json.dumps(body)}"
            ),
        }})

    chat = _build(reasoning_effort="high")
    with _patch_transport(httpx.MockTransport(handler)):
        with pytest.raises(httpx.HTTPStatusError):
            await chat.generate("hi")

    assert len(bodies) == 1, "nothing WE sent was named — no retry"
    assert "reasoning_effort" in bodies[0], "the param was sent, echoed…"
    assert chat._quirks_for("gpt-5-mini").dropped_params == set()


@pytest.mark.asyncio
async def test_marker_named_param_dropped_despite_request_echo() -> None:
    """The body names reasoning_effort right after the marker AND echoes
    the whole request (which mentions chat_template_kwargs) — only the
    marker-anchored name is dropped."""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        bodies.append(body)
        if "reasoning_effort" in body:
            return httpx.Response(400, json={"error": {
                "message": (
                    "Unrecognized request argument supplied: "
                    f"reasoning_effort. Request was: {json.dumps(body)}"
                ),
            }})
        return _chat_ok()

    chat = _build(disable_reasoning=True, reasoning_effort="high")
    with _patch_transport(httpx.MockTransport(handler)):
        assert await chat.generate("hi") == "ok"

    assert len(bodies) == 2
    assert "reasoning_effort" not in bodies[1]
    assert bodies[1].get("chat_template_kwargs") == {
        "enable_thinking": False,
    }, "params merely echoed in the request dump survive"
    assert chat._quirks_for("gpt-5-mini").dropped_params == {
        "reasoning_effort",
    }


@pytest.mark.asyncio
async def test_drop_never_touches_structural_keys() -> None:
    """An argument error naming ``messages`` (or model/stream) is a
    different problem — dropping it would produce a nonsense request."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"error": {
            "message": "Unknown parameter: 'messages'.",
        }})

    chat = _build()
    with _patch_transport(httpx.MockTransport(handler)):
        with pytest.raises(httpx.HTTPStatusError):
            await chat.generate("hi")
    assert calls["n"] == 1
    assert chat._quirks_for("gpt-5-mini").dropped_params == set()


@pytest.mark.asyncio
async def test_mistral_style_422_extra_inputs_dropped() -> None:
    """Mistral's pydantic validator 422s with 'Extra inputs are not
    permitted' and names the offending field in ``detail[].loc`` — the
    drop must key off that shape too (chat items of the DeepSeek +
    Mistral audit section)."""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        bodies.append(body)
        extras = [k for k in ("seed", "logit_bias") if k in body]
        if extras:
            return httpx.Response(422, json={
                "object": "error",
                "message": "Extra inputs are not permitted",
                "detail": [
                    {
                        "type": "extra_forbidden",
                        "loc": ["body", name],
                        "msg": "Extra inputs are not permitted",
                    }
                    for name in extras
                ],
            })
        return _chat_ok()

    chat = _build(extra_request_params={"seed": 42, "logit_bias": {}})
    with _patch_transport(httpx.MockTransport(handler)):
        assert await chat.generate("hi") == "ok"

    assert "seed" in bodies[0] and "logit_bias" in bodies[0]
    # Both named extras dropped in the single retry.
    assert len(bodies) == 2
    assert "seed" not in bodies[1] and "logit_bias" not in bodies[1]
    assert chat._quirks_for("gpt-5-mini").dropped_params == {
        "seed", "logit_bias",
    }


@pytest.mark.asyncio
async def test_stream_drops_named_param_and_retries() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        bodies.append(body)
        if "chat_template_kwargs" in body:
            return _unrecognized("chat_template_kwargs")
        return _sse_ok()

    chat = _build(disable_reasoning=True)
    with _patch_transport(httpx.MockTransport(handler)):
        chunks = [c async for c in chat.generate_stream("hi")]

    assert "".join(chunks) == "ok"
    assert "chat_template_kwargs" in bodies[0]
    assert "chat_template_kwargs" not in bodies[1]
    assert bodies[1].get("stream") is True, "retry stays on the stream path"


@pytest.mark.asyncio
async def test_rename_then_drop_chain_in_one_call() -> None:
    """Two prescribed fixes in sequence (max_tokens rename, then an
    unrecognized-param drop) are both honored within one generate()."""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        bodies.append(body)
        if "max_tokens" in body:
            return httpx.Response(400, json={"error": {
                "message": "Unsupported parameter: 'max_tokens' is not "
                "supported with this model. Use 'max_completion_tokens' "
                "instead.",
            }})
        if "chat_template_kwargs" in body:
            return _unrecognized("chat_template_kwargs")
        return _chat_ok()

    chat = _build(max_tokens=4096, disable_reasoning=True)
    with _patch_transport(httpx.MockTransport(handler)):
        assert await chat.generate("hi") == "ok"

    assert len(bodies) == 3
    assert "max_completion_tokens" in bodies[2]
    assert "chat_template_kwargs" not in bodies[2]


@pytest.mark.asyncio
async def test_extra_params_max_tokens_rename_cannot_loop_to_cap() -> None:
    """``max_tokens`` originating from ``extra_request_params`` re-enters
    every rebuild, so the rename adaptation cannot change the payload.
    The adapter must notice the adapted payload equals the one that just
    failed, stop adapting, and surface the error — never hammer the same
    rejection for all ``_MAX_PRESCRIBED_RETRIES`` rounds."""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        bodies.append(body)
        return httpx.Response(400, json={"error": {
            "message": "Unsupported parameter: 'max_tokens' is not "
            "supported with this model. Use 'max_completion_tokens' "
            "instead.",
        }})

    chat = _build(extra_request_params={"max_tokens": 4096})
    with _patch_transport(httpx.MockTransport(handler)):
        with pytest.raises(httpx.HTTPStatusError):
            await chat.generate("hi")

    assert len(bodies) <= 2, (
        "original + at most one adapted attempt — no 8-round hammering"
    )
    assert all(b.get("max_tokens") == 4096 for b in bodies)


# ---------------------------------------------------------------------------
# 3. Responses-only model signal (extends the non-chat-model fallback)
# ---------------------------------------------------------------------------


_RESPONSES_ONLY_BODY = {
    "error": {
        "message": (
            "This model is only supported in v1/responses and not in "
            "v1/chat/completions."
        ),
        "type": "invalid_request_error",
        "param": "model",
        "code": None,
    },
}


@pytest.mark.asyncio
async def test_responses_only_override_falls_back_to_default() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requested.append(body["model"])
        if body["model"] == "gpt-5-pro":
            return httpx.Response(400, json=_RESPONSES_ONLY_BODY)
        return _chat_ok()

    chat = _build("gpt-5-mini")
    with _patch_transport(httpx.MockTransport(handler)):
        assert await chat.generate("hi", model="gpt-5-pro") == "ok"
        # Memoized like every other non-chat model.
        assert await chat.generate("hi", model="gpt-5-pro") == "ok"

    assert requested == ["gpt-5-pro", "gpt-5-mini", "gpt-5-mini"]


@pytest.mark.asyncio
async def test_responses_only_default_model_fails_loudly() -> None:
    """Conservative guard: the fallback is for per-call OVERRIDES only.
    A Responses-only DEFAULT model must surface the upstream error, not
    silently retry onto itself."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json=_RESPONSES_ONLY_BODY)

    chat = _build("gpt-5-pro")
    with _patch_transport(httpx.MockTransport(handler)):
        with pytest.raises(httpx.HTTPStatusError):
            await chat.generate("hi")
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# 4. system-role rejection → merge system prompt into the user turn
# ---------------------------------------------------------------------------


def _has_system(body: dict) -> bool:
    return any(m.get("role") == "system" for m in body["messages"])


@pytest.mark.asyncio
async def test_gemma_template_rejection_merges_system_into_user() -> None:
    """llama.cpp --jinja surfaces Gemma-2's ``raise_exception`` as a 500
    'System role not supported' — retry with the system text carried in
    the user turn, and remember it for later calls."""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        bodies.append(body)
        if _has_system(body):
            return httpx.Response(500, json={"error": {
                "code": 500,
                "message": "System role not supported",
                "type": "server_error",
            }})
        return _chat_ok()

    chat = _build("gemma-2-9b-it")
    with _patch_transport(httpx.MockTransport(handler)):
        assert await chat.generate("hi") == "ok"
        assert await chat.generate("hi again") == "ok"

    assert _has_system(bodies[0])
    retry = bodies[1]
    assert [m["role"] for m in retry["messages"]] == ["user"]
    assert retry["messages"][0]["content"].startswith(
        "You are a roleplay character backend.",
    )
    assert retry["messages"][0]["content"].endswith("hi")
    # Memoized: the second generate goes straight to the merged shape.
    assert len(bodies) == 3
    assert not _has_system(bodies[2])


@pytest.mark.asyncio
async def test_mistral_template_rejection_merges_on_400() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        bodies.append(body)
        if _has_system(body):
            return httpx.Response(400, json={"error": {
                "message": "Only user, assistant and tool roles are "
                "supported, got system",
            }})
        return _chat_ok()

    chat = _build("ministral-8b")
    with _patch_transport(httpx.MockTransport(handler)):
        assert await chat.generate("hi") == "ok"

    assert len(bodies) == 2
    assert not _has_system(bodies[1])


@pytest.mark.asyncio
async def test_stream_system_role_rejection_merges() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        bodies.append(body)
        if _has_system(body):
            return httpx.Response(400, text="System role not supported")
        return _sse_ok()

    chat = _build("gemma-2-9b-it")
    with _patch_transport(httpx.MockTransport(handler)):
        chunks = [c async for c in chat.generate_stream("hi")]

    assert "".join(chunks) == "ok"
    assert len(bodies) == 2
    assert not _has_system(bodies[1])
    assert bodies[1].get("stream") is True


@pytest.mark.asyncio
async def test_unrelated_400_does_not_merge_system() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"error": {"message": "bad prompt"}})

    chat = _build()
    with _patch_transport(httpx.MockTransport(handler)):
        with pytest.raises(httpx.HTTPStatusError):
            await chat.generate("hi")
    assert calls["n"] == 1
    assert chat._quirks_for("gpt-5-mini").merge_system_into_user is False


def test_merged_system_prefixes_multimodal_text_part() -> None:
    """Vision payloads keep the merge too: the system text lands in the
    text part of the multimodal array."""
    chat = _build(supports_vision=True)
    chat._quirks_for("gpt-5-mini").merge_system_into_user = True
    payload = chat._build_payload(
        "describe", image_urls=("data:image/png;base64,AAA=",),
    )
    assert [m["role"] for m in payload["messages"]] == ["user"]
    text_part = payload["messages"][0]["content"][0]
    assert text_part["type"] == "text"
    assert text_part["text"].startswith("You are a roleplay character backend.")
    assert text_part["text"].endswith("describe")


# ---------------------------------------------------------------------------
# learned-quirk state is MODEL-scoped and shared across per-call clones
# ---------------------------------------------------------------------------


def test_quirks_shared_between_base_and_clones() -> None:
    """Per-call clones (reasoning/vision overrides) must learn together
    with the base adapter — a lesson from any copy applies everywhere,
    same rationale as the shared non-chat-model set. The memo map is
    keyed by resolved model id, and the MAP object is what clones share."""
    base = _build()
    reasoning_clone = base.with_reasoning_overrides(
        ReasoningOverrides(reasoning_effort="high"),
    )
    vision_clone = base.with_supports_vision(True)

    reasoning_clone._remember_non_stream_fallback("gpt-5-mini")
    vision_clone._remember_dropped_param("gpt-5-mini", "chat_template_kwargs")

    assert base._quirks_for("gpt-5-mini").non_stream_fallback is True
    assert "chat_template_kwargs" in base._quirks_for(
        "gpt-5-mini",
    ).dropped_params
    assert (
        base._quirks_by_model
        is reasoning_clone._quirks_by_model
        is vision_clone._quirks_by_model
    )


@pytest.mark.asyncio
async def test_stream_lesson_for_model_a_does_not_degrade_model_b() -> None:
    """One aggregator connection fronts many models: the stream-
    verification block learned on model A must not stop model B from
    streaming — and model A's next call skips the failed round."""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        bodies.append(body)
        if body.get("stream") and body["model"] == "model-a":
            return httpx.Response(400, json=_STREAM_VERIFICATION_BODY)
        if body.get("stream"):
            return _sse_ok("streamed")
        return _chat_ok("full completion")

    chat = _build("model-a")
    with _patch_transport(httpx.MockTransport(handler)):
        a_first = [c async for c in chat.generate_stream("hi", model="model-a")]
        b_chunks = [c async for c in chat.generate_stream("hi", model="model-b")]
        a_again = [c async for c in chat.generate_stream("hi", model="model-a")]

    assert a_first == ["full completion"]
    assert b_chunks == ["streamed"], "model B keeps streaming"
    assert a_again == ["full completion"]
    assert [(b["model"], bool(b.get("stream"))) for b in bodies] == [
        ("model-a", True),   # the round that teaches the lesson
        ("model-a", False),  # fallback completion
        ("model-b", True),   # unaffected by model A's memo
        ("model-a", False),  # memo applied — no re-paid stream round
    ]


@pytest.mark.asyncio
async def test_dropped_param_is_scoped_to_the_offending_model() -> None:
    """A param one model's endpoint rejects (its chat template / feature
    set) stays available to sibling models on the same connection."""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        bodies.append(body)
        if body["model"] == "model-a" and "chat_template_kwargs" in body:
            return _unrecognized("chat_template_kwargs")
        return _chat_ok()

    chat = _build("model-a", disable_reasoning=True)
    with _patch_transport(httpx.MockTransport(handler)):
        assert await chat.generate("hi") == "ok"
        assert await chat.generate("hi", model="model-b") == "ok"
        assert await chat.generate("hi again") == "ok"

    assert [b["model"] for b in bodies] == [
        "model-a", "model-a", "model-b", "model-a",
    ]
    assert "chat_template_kwargs" in bodies[0]
    assert "chat_template_kwargs" not in bodies[1]
    assert "chat_template_kwargs" in bodies[2], (
        "model B still sends the param model A's endpoint rejected"
    )
    assert "chat_template_kwargs" not in bodies[3], (
        "model A's memo persists — no re-paid failed round"
    )
