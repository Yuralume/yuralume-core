"""Live-probe engine unit tests (httpx.MockTransport-driven).

Every test drives ``probe_connection`` through a MockTransport so no
real network is touched; assertions pin the shared ProbeReport contract
(action enum values, fail-soft semantics, secret scrubbing, the
embedding-dimension guard, and gateway reachability rules).
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import httpx

from kokoro_link.infrastructure.provider_settings.catalog import catalog_by_id
from kokoro_link.infrastructure.provider_settings.live_probe import (
    probe_connection,
)


def _poison_handler(request: httpx.Request) -> httpx.Response:
    raise AssertionError(
        f"unexpected network call: {request.method} {request.url}",
    )


def _probe(
    provider_id: str,
    capabilities: list[str],
    *,
    config: dict[str, Any] | None = None,
    secret: dict[str, Any] | None = None,
    handler=None,
    deep: bool = False,
):
    transport = httpx.MockTransport(handler or _poison_handler)
    return asyncio.run(
        probe_connection(
            entry=catalog_by_id()[provider_id],
            capabilities=capabilities,
            config=config or {},
            secret=secret or {},
            deep=deep,
            transport=transport,
        ),
    )


# ---------------------------------------------------------------------------
# llm
# ---------------------------------------------------------------------------


def test_llm_openai_lists_models_then_chats() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/models":
            assert request.headers["Authorization"] == "Bearer sk-unit-key"
            return httpx.Response(
                200,
                json={"data": [{"id": "gpt-4o-mini"}, {"id": "gpt-4.1"}]},
            )
        if request.method == "POST" and request.url.path == "/v1/chat/completions":
            body = json.loads(request.content)
            assert body["model"] == "gpt-4o-mini"
            assert body["max_tokens"] == 1
            # The probe rides the runtime adapter's own payload builder
            # (2026-07-16 unification), so it now mirrors the runtime
            # request shape exactly — including the system message.
            assert body["messages"][0]["role"] == "system"
            assert body["messages"][-1] == {"role": "user", "content": "ping"}
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "pong"}}]},
            )
        raise AssertionError(f"{request.method} {request.url.path}")

    reports = _probe(
        "openai",
        ["llm"],
        secret={"api_key": "sk-unit-key"},
        handler=handler,
    )

    assert [report.action for report in reports] == [
        "listed_models",
        "chat_completion",
    ]
    assert all(report.ok for report in reports)
    assert all(report.capability == "llm" for report in reports)
    assert reports[0].detail == "2 models"
    assert "gpt-4o-mini" in reports[1].detail
    assert all(report.latency_ms >= 0 for report in reports)


def test_llm_auth_failure_yields_failed_probe() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    reports = _probe(
        "openai",
        ["llm"],
        secret={"api_key": "sk-bad"},
        handler=handler,
    )

    assert reports[0].action == "listed_models"
    assert reports[0].ok is False
    assert "401" in reports[0].detail


def test_llm_network_failure_is_fail_soft_and_scrubbed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "dns exploded for sk-supersecret12345",
            request=request,
        )

    reports = _probe(
        "openai",
        ["llm"],
        secret={"api_key": "sk-supersecret12345"},
        handler=handler,
    )

    assert reports  # never raises — failures become failed reports
    assert all(report.ok is False for report in reports)
    joined = " ".join(report.detail for report in reports)
    assert "connection failed" in joined
    assert "sk-supersecret12345" not in joined
    assert "[redacted]" in joined


def test_llm_anthropic_posts_v1_messages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/messages"
        assert request.headers["x-api-key"] == "sk-ant-unit"
        assert request.headers["anthropic-version"] == "2023-06-01"
        body = json.loads(request.content)
        assert body["max_tokens"] == 1
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "pong"}]},
        )

    reports = _probe(
        "anthropic",
        ["llm"],
        secret={"api_key": "sk-ant-unit"},
        handler=handler,
    )

    assert [(report.action, report.ok) for report in reports] == [
        ("chat_completion", True),
    ]
    assert "claude-sonnet-4-5" in reports[0].detail


def test_llm_yuralume_cloud_lists_gateway_options() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/llm-options"
        return httpx.Response(200, json={"options": [{"id": "yura-chat"}]})

    reports = _probe(
        "yuralume_cloud",
        ["llm"],
        config={"base_url": "https://gw.example/v1"},
        secret={"api_key": "cloud-key"},
        handler=handler,
    )

    assert [(report.action, report.ok) for report in reports] == [
        ("listed_models", True),
    ]
    assert reports[0].detail == "1 models"


# ---------------------------------------------------------------------------
# embedding
# ---------------------------------------------------------------------------


def test_embedding_dimension_match_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/embeddings"
        body = json.loads(request.content)
        assert body["input"] == ["ping"]
        return httpx.Response(200, json={"data": [{"embedding": [0.0] * 1024}]})

    reports = _probe(
        "local_openai_compatible",
        ["embedding"],
        config={
            "base_url": "http://127.0.0.1:1234/v1",
            "embedding_model": "text-embedding-bge-m3",
        },
        handler=handler,
    )

    assert [(report.action, report.ok) for report in reports] == [
        ("embedded", True),
    ]
    assert "1024" in reports[0].detail


def test_embedding_dimension_mismatch_fails_with_explanation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"embedding": [0.0] * 1536}]})

    reports = _probe(
        "local_openai_compatible",
        ["embedding"],
        config={
            "base_url": "http://127.0.0.1:1234/v1",
            "embedding_model": "text-embedding-3-small",
        },
        handler=handler,
    )

    assert reports[0].action == "embedded"
    assert reports[0].ok is False
    assert "1536" in reports[0].detail
    assert "1024" in reports[0].detail
    assert "Request dimensions" in reports[0].detail


def test_embedding_request_dimensions_param_forwarded() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"embedding": [0.0] * 1024}]})

    reports = _probe(
        "openai",
        ["embedding"],
        config={
            "embedding_model": "text-embedding-3-small",
            "request_dimensions": True,
        },
        secret={"api_key": "sk-unit"},
        handler=handler,
    )

    assert reports[0].ok is True
    assert seen["dimensions"] == 1024


# ---------------------------------------------------------------------------
# tts
# ---------------------------------------------------------------------------


def test_tts_custom_lists_voices() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/voices"
        return httpx.Response(200, json={"voices": [{"id": "v1"}, {"id": "v2"}]})

    reports = _probe(
        "custom_tts",
        ["tts"],
        config={"base_url": "https://tts.example.test/v1"},
        handler=handler,
    )

    assert [(report.action, report.ok) for report in reports] == [
        ("listed_voices", True),
    ]
    assert reports[0].detail == "2 voices"


def test_tts_openai_synthesizes_hi() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/audio/speech"
        body = json.loads(request.content)
        assert body["input"] == "Hi"
        assert body["voice"] == "marin"
        return httpx.Response(200, content=b"RIFF0000WAVE")

    reports = _probe(
        "openai",
        ["tts"],
        secret={"api_key": "sk-unit"},
        handler=handler,
    )

    assert [(report.action, report.ok) for report in reports] == [
        ("synthesized_speech", True),
    ]
    assert "12 bytes" in reports[0].detail
    assert "marin" in reports[0].detail


def test_tts_openrouter_probe_defaults_to_mp3_and_current_model() -> None:
    """OpenRouter's /audio/speech only accepts response_format mp3|pcm —
    wav is rejected with a ZodError before auth
    (https://openrouter.ai/docs/guides/overview/multimodal/tts) — and its
    speech catalog (GET /models?output_modalities=speech) no longer lists
    any openai/* TTS model, so the probe must mirror runtime_sync's
    provider-scoped defaults (x-ai/grok-voice-tts-1.0 + eve + mp3)."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/audio/speech"
        seen.update(json.loads(request.content))
        return httpx.Response(200, content=b"mp3-bytes")

    reports = _probe(
        "openrouter",
        ["tts"],
        secret={"api_key": "sk-or-unit"},
        handler=handler,
    )

    assert [(report.action, report.ok) for report in reports] == [
        ("synthesized_speech", True),
    ]
    assert seen["response_format"] == "mp3"
    assert seen["model"] == "x-ai/grok-voice-tts-1.0"
    assert seen["voice"] == "eve"


def test_tts_yuralume_cloud_not_probed_ok() -> None:
    # Poison default handler proves this branch makes no network calls.
    reports = _probe(
        "yuralume_cloud",
        ["tts"],
        config={"base_url": "https://gw.example/v1"},
    )

    assert reports[0].action == "not_probed"
    assert reports[0].ok is True
    assert "runtime" in reports[0].detail


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_searxng_runs_one_query(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        assert request.url.params["q"] == "ping"
        return httpx.Response(
            200,
            json={
                "results": [
                    {"url": "https://r.example", "title": "t", "content": "c"},
                ],
            },
        )

    # The websearch clients build their own AsyncClient, so the probe's
    # transport seam can't reach them — patch httpx.AsyncClient instead.
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def patched(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)

    reports = asyncio.run(
        probe_connection(
            entry=catalog_by_id()["searxng"],
            capabilities=["search"],
            config={"searxng_base_url": "https://sx.example.test"},
            secret={},
        ),
    )

    assert [(report.action, report.ok) for report in reports] == [
        ("searched", True),
    ]
    assert "1 result" in reports[0].detail


# ---------------------------------------------------------------------------
# image
# ---------------------------------------------------------------------------


def test_image_gateway_reachability_404_counts_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(404)

    reports = _probe(
        "custom_media_gateway",
        ["image"],
        config={
            "base_url": "https://gw.example.test/v1",
            "default_model": "yura-anime",
        },
        secret={"api_key": "gw-key"},
        handler=handler,
    )

    assert [(report.action, report.ok) for report in reports] == [
        ("reachability", True),
    ]
    assert "endpoint reachable" in reports[0].detail
    assert "run deep test" in reports[0].detail
    assert "404" in reports[0].detail


def test_image_gateway_connection_error_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("name resolution failed", request=request)

    reports = _probe(
        "custom_media_gateway",
        ["image"],
        config={
            "base_url": "https://gw.example.test/v1",
            "default_model": "yura-anime",
        },
        secret={"api_key": "gw-key"},
        handler=handler,
    )

    assert reports[0].action == "reachability"
    assert reports[0].ok is False
    assert "connection failed" in reports[0].detail


def test_image_deep_gateway_generates_b64() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/images/generations"
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "data": [
                    {"b64_json": base64.b64encode(b"png-bytes").decode()},
                ],
            },
        )

    reports = _probe(
        "custom_media_gateway",
        ["image"],
        config={
            "base_url": "https://gw.example.test/v1",
            "default_model": "yura-anime",
        },
        secret={"api_key": "gw-key"},
        handler=handler,
        deep=True,
    )

    assert [(report.action, report.ok) for report in reports] == [
        ("generated_image", True),
    ]
    # Spec-pinned body (docs/CUSTOM_MEDIA_GATEWAY_SPEC.md) + smallest size.
    assert seen == {
        "model": "yura-anime",
        "prompt": "a tiny plain blue circle",
        "size": "1024x1024",
        "n": 1,
    }
    assert "9 bytes" in reports[0].detail


def test_image_deep_gateway_url_item_not_downloaded() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        assert request.method == "POST"  # a GET would be an artifact download
        return httpx.Response(200, json={"data": [{"url": "/artifacts/abc"}]})

    reports = _probe(
        "custom_media_gateway",
        ["image"],
        config={
            "base_url": "https://gw.example.test/v1",
            "default_model": "yura-anime",
        },
        secret={"api_key": "gw-key"},
        handler=handler,
        deep=True,
    )

    assert reports[0].ok is True
    assert "not downloaded" in reports[0].detail
    assert methods == ["POST"]


def test_image_deep_xai_retries_without_aspect_ratio_on_signal() -> None:
    """The xAI deep probe mirrors XAIImageProvider's request shape AND
    its signal-driven coping: on the strict 400
    'Argument not supported: aspect_ratio'
    (https://docs.x.ai/developers/model-capabilities/images/generation)
    it retries once without the param, so the Test button matches what
    the runtime would experience."""
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/images/generations"
        body = json.loads(request.content)
        bodies.append(body)
        if "aspect_ratio" in body:
            return httpx.Response(400, json={
                "code": "400",
                "error": "Argument not supported: aspect_ratio",
            })
        return httpx.Response(200, json={
            "data": [{"b64_json": base64.b64encode(b"png-bytes").decode()}],
        })

    reports = _probe(
        "xai",
        ["image"],
        secret={"api_key": "xai-unit"},
        handler=handler,
        deep=True,
    )

    assert [(report.action, report.ok) for report in reports] == [
        ("generated_image", True),
    ]
    # Default model mirrors runtime_sync._IMAGE_DEFAULTS (current
    # grok-imagine slug, not the legacy grok-2-image-1212).
    assert bodies[0]["model"] == "grok-imagine-image-quality"
    assert len(bodies) == 2
    assert "aspect_ratio" not in bodies[1]


def test_image_deep_gemini_generates_via_image_config() -> None:
    """The Gemini deep probe posts the documented generateContent shape:
    generationConfig.imageConfig.aspectRatio
    (https://ai.google.dev/gemini-api/docs/image-generation)."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        seen["path"] = request.url.path
        seen["api_key"] = request.headers.get("x-goog-api-key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "candidates": [{
                "content": {
                    "parts": [{
                        "inlineData": {
                            "mimeType": "image/png",
                            "data": base64.b64encode(b"png-bytes").decode(),
                        },
                    }],
                },
            }],
        })

    reports = _probe(
        "google_gemini",
        ["image"],
        secret={"api_key": "gemini-unit"},
        handler=handler,
        deep=True,
    )

    assert [(report.action, report.ok) for report in reports] == [
        ("generated_image", True),
    ]
    assert seen["path"].endswith(
        "/models/gemini-3.1-flash-image-preview:generateContent",
    )
    assert seen["api_key"] == "gemini-unit"
    assert seen["body"]["generationConfig"] == {
        "imageConfig": {"aspectRatio": "1:1"},
    }


def test_image_deep_nanogpt_default_model_is_catalog_listed() -> None:
    """'flux-1.1-pro' vanished from NanoGPT's image catalog (GET
    /api/v1/image-models, re-checked 2026-07-16 — FLUX ids are now
    'flux-pro/v1.1' and lack our portrait/landscape sizes); the shipped
    default must be a listed model that supports the pinned
    {1024x1024, 1024x1536, 1536x1024} size set → gpt-image-1
    (https://docs.nano-gpt.com/api-reference/endpoint/image-generation-openai)."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/images/generations"
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={
            "data": [{"b64_json": base64.b64encode(b"png-bytes").decode()}],
        })

    reports = _probe(
        "nanogpt",
        ["image"],
        secret={"api_key": "sk-nano-unit"},
        handler=handler,
        deep=True,
    )

    assert [(report.action, report.ok) for report in reports] == [
        ("generated_image", True),
    ]
    assert seen["model"] == "gpt-image-1"


# ---------------------------------------------------------------------------
# video + no-strategy pairs
# ---------------------------------------------------------------------------


def test_video_reachability_only_even_when_deep() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(405)

    reports = _probe(
        "google_veo",
        ["video"],
        secret={"api_key": "veo-key"},
        handler=handler,
        deep=True,
    )

    assert [(report.action, report.ok) for report in reports] == [
        ("reachability", True),
    ]
    assert "intentionally unsupported" in reports[0].detail


def test_unknown_capability_is_not_probed_ok() -> None:
    # Poison default handler proves this branch makes no network calls.
    reports = _probe("openai", ["telepathy"], secret={"api_key": "sk-unit"})

    assert reports[0].action == "not_probed"
    assert reports[0].ok is True
    assert "telepathy" in reports[0].detail


def test_capability_failure_does_not_abort_other_capabilities() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/v1/models", "/v1/chat/completions"):
            raise httpx.ConnectError("llm backend down", request=request)
        if request.url.path == "/v1/audio/speech":
            return httpx.Response(200, content=b"RIFF")
        raise AssertionError(request.url.path)

    reports = _probe(
        "openai",
        ["llm", "tts"],
        secret={"api_key": "sk-unit"},
        handler=handler,
    )

    llm = [report for report in reports if report.capability == "llm"]
    tts = [report for report in reports if report.capability == "tts"]
    assert llm and all(report.ok is False for report in llm)
    assert tts and tts[0].ok is True


def test_probe_details_redact_known_secret_values() -> None:
    """A provider response echoing the exact API key (any shape) must be
    scrubbed from probe details — pattern scrubbing alone can't catch
    arbitrarily-shaped credentials (review finding 2026-07-16)."""
    import httpx

    from kokoro_link.infrastructure.provider_settings.catalog import catalog_by_id

    leaked_key = "weird.shaped.credential.42"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"invalid credential: {leaked_key}")

    entry = catalog_by_id()["nanogpt"]
    reports = asyncio.run(
        probe_connection(
            entry=entry,
            capabilities=["llm"],
            config={"base_url": "https://api.example.invalid/v1"},
            secret={"api_key": leaked_key},
            transport=httpx.MockTransport(handler),
        ),
    )
    assert reports, "expected at least one probe report"
    for report in reports:
        assert leaked_key not in report.detail
        assert not report.ok


def test_sanitize_error_is_case_insensitive() -> None:
    from kokoro_link.infrastructure.security.error_sanitizer import sanitize_error

    assert "SK-ABCDEFGH12345" not in sanitize_error("denied: SK-ABCDEFGH12345")
    assert "[redacted]" in sanitize_error("denied: SK-ABCDEFGH12345")


def test_llm_chat_retries_with_max_completion_tokens_on_signal() -> None:
    """gpt-5+/o-series models 400 on max_tokens and prescribe
    max_completion_tokens — the probe must retry on that explicit signal
    and surface the quirk in the detail (field report 2026-07-16)."""
    import httpx

    from kokoro_link.infrastructure.provider_settings.catalog import catalog_by_id

    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "gpt-5.6-luna"}]})
        body = json.loads(request.content.decode("utf-8"))
        bodies.append(body)
        if "max_tokens" in body:
            return httpx.Response(400, json={"error": {
                "message": "Unsupported parameter: 'max_tokens' is not "
                "supported with this model. Use 'max_completion_tokens' "
                "instead.",
                "type": "invalid_request_error",
            }})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}],
        })

    entry = catalog_by_id()["openai"]
    reports = asyncio.run(
        probe_connection(
            entry=entry,
            capabilities=["llm"],
            config={"default_model": "gpt-5.6-luna"},
            secret={"api_key": "sk-test-123456789"},
            transport=httpx.MockTransport(handler),
        ),
    )
    chat = [r for r in reports if r.action == "chat_completion"]
    assert chat and chat[0].ok, chat
    assert "max_completion_tokens" in chat[0].detail
    assert [list(b.keys())[-1] for b in bodies] == [
        "max_tokens", "max_completion_tokens",
    ]


def test_llm_chat_plain_400_is_not_retried() -> None:
    """A 400 that does not prescribe the rename must fail once, verbatim."""
    import httpx

    from kokoro_link.infrastructure.provider_settings.catalog import catalog_by_id

    calls = {"chat": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m"}]})
        calls["chat"] += 1
        return httpx.Response(400, json={"error": {"message": "bad prompt"}})

    entry = catalog_by_id()["openai"]
    reports = asyncio.run(
        probe_connection(
            entry=entry,
            capabilities=["llm"],
            config={"default_model": "m"},
            secret={"api_key": "sk-test-123456789"},
            transport=httpx.MockTransport(handler),
        ),
    )
    chat = [r for r in reports if r.action == "chat_completion"]
    assert chat and not chat[0].ok
    assert calls["chat"] == 1
