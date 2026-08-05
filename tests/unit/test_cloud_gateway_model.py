from __future__ import annotations

import json
import logging

import httpx
import pytest

from kokoro_link.contracts.cloud_gateway import (
    CloudGatewayIdentity,
    CloudGatewayRequestTooLargeError,
)
from kokoro_link.contracts.llm import ImageInputRejectedError
from kokoro_link.contracts.generation_trigger import (
    GenerationTrigger,
    generation_trigger_scope,
)
from kokoro_link.contracts.interaction_context import (
    BILLING_COVERED_HEADER_NAME,
    InteractionContext,
    interaction_scope,
)
from kokoro_link.infrastructure.llm.cloud_gateway_model import (
    CloudGatewayChatModel,
)
from kokoro_link.infrastructure.llm.cloud_refusal import ExpectedCloudRefusal


class _MockAsyncClient(httpx.AsyncClient):
    def __init__(self, handler, **kwargs) -> None:
        super().__init__(
            transport=httpx.MockTransport(handler),
            timeout=kwargs["timeout"],
        )


@pytest.mark.asyncio
async def test_cloud_gateway_model_posts_openai_shape_with_cloud_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["payload"] = json.loads(request.content.decode())
        return httpx.Response(200, json={
            "choices": [
                {"message": {"content": "hello from gateway"}},
            ],
        })

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )
    model = CloudGatewayChatModel(
        base_url="https://gateway.example/",
        deployment_token="ykl_deploy",
        default_model="preset-chat",
        feature_key="chat",
        identity=CloudGatewayIdentity(
            operator_id="cloud:acct_1",
            account_id="acct_1",
            tenant_id="tenant_1",
            character_ref="chr_abc",
        ),
    )

    with generation_trigger_scope(GenerationTrigger.BACKGROUND):
        result = await model.generate("Say hi", model="preset-fast")

    assert result == "hello from gateway"
    assert seen["url"] == "https://gateway.example/v1/chat/completions"
    headers = seen["headers"]
    assert headers["authorization"] == "Bearer ykl_deploy"
    assert headers["x-yuralume-deployment"] == "hosted-primary"
    assert headers["x-yuralume-audience"] == "yuralume-gateway"
    assert headers["x-yuralume-account"] == "acct_1"
    assert headers["x-yuralume-tenant"] == "tenant_1"
    assert headers["x-yuralume-feature"] == "chat"
    assert headers["x-yuralume-character"] == "chr_abc"
    assert headers["x-yuralume-trigger"] == "v1;source=background"
    assert str(headers["x-request-id"]).startswith("llm-")
    assert model.last_request_id == headers["x-request-id"]
    payload = seen["payload"]
    assert payload["model"] == "preset-fast"
    assert payload["messages"][1] == {"role": "user", "content": "Say hi"}


@pytest.mark.asyncio
async def test_cloud_gateway_model_supports_vision_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content.decode())
        return httpx.Response(200, json={
            "choices": [
                {"message": {"content": "vision ok"}},
            ],
        })

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )
    model = CloudGatewayChatModel(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        default_model="preset-chat",
        feature_key="chat",
        identity=CloudGatewayIdentity(
            operator_id="cloud:acct_1",
            account_id="acct_1",
            tenant_id="tenant_1",
            character_ref="chr_abc",
        ),
    )

    await model.generate("Look", image_urls=("https://img.example/a.png",))

    content = seen["payload"]["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": "Look"}
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": "https://img.example/a.png"},
    }


def test_cloud_gateway_model_lists_default_model() -> None:
    model = CloudGatewayChatModel(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        default_model="preset-chat",
        feature_key="chat",
        identity=CloudGatewayIdentity(
            operator_id="cloud:acct_1",
            account_id="acct_1",
            tenant_id="tenant_1",
            character_ref="chr_abc",
        ),
    )

    assert model.provider_id == "yuralume_cloud"
    assert model.supports_vision is True


@pytest.mark.asyncio
async def test_cloud_gateway_model_http_error_logs_upstream_body(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "error": {"message": "invalid model preset"},
        })

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )
    caplog.set_level(
        logging.ERROR,
        logger="kokoro_link.infrastructure.llm.cloud_gateway_model",
    )
    model = CloudGatewayChatModel(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        default_model="preset-chat",
        feature_key="chat",
        identity=CloudGatewayIdentity(
            operator_id="cloud:acct_1",
            account_id="acct_1",
            tenant_id="tenant_1",
            character_ref="chr_abc",
        ),
    )

    with pytest.raises(httpx.HTTPStatusError):
        await model.generate("Say hi", model="bad-preset")

    assert "Cloud Gateway LLM" in caplog.text
    assert "HTTP 400" in caplog.text
    assert "invalid model preset" in caplog.text
    assert "https://gateway.example/v1/chat/completions" in caplog.text


@pytest.mark.asyncio
async def test_cloud_gateway_model_entitlement_denied_warns_not_errors(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={
            "error": {
                "code": "entitlement_denied",
                "message": "forwarded account is inactive or outside the forwarded tenant",
                "retryable": False,
            },
        })

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )
    caplog.set_level(
        logging.DEBUG,
        logger="kokoro_link.infrastructure.llm.cloud_gateway_model",
    )
    model = CloudGatewayChatModel(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        default_model="preset-chat",
        feature_key="dialogue_summary",
        identity=CloudGatewayIdentity(
            operator_id="cloud:acct_1",
            account_id="acct_1",
            tenant_id="tenant_1",
            character_ref="chr_abc",
        ),
    )

    with pytest.raises(ExpectedCloudRefusal) as excinfo:
        await model.generate("Say hi")

    # Subclasses HTTPStatusError so existing transport handling still catches it.
    assert isinstance(excinfo.value, httpx.HTTPStatusError)
    assert excinfo.value.code == "entitlement_denied"

    # A deliberate refusal is a WARNING with identity context — never an ERROR.
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)
    refusals = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(refusals) == 1
    text = refusals[0].getMessage()
    assert "entitlement_denied" in text
    assert "feature=dialogue_summary" in text
    assert "character=chr_abc" in text
    assert "account=acct_1" in text


@pytest.mark.asyncio
async def test_cloud_gateway_model_stamps_the_covering_interaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AP2: inside an action scope the Gateway must be told which charge
    already covers this call; outside one, no header at all."""
    seen: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}],
        })

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )
    model = CloudGatewayChatModel(
        base_url="https://gateway.example/",
        deployment_token="ykl_deploy",
        default_model="preset-chat",
        feature_key="chat",
        identity=CloudGatewayIdentity(
            operator_id="cloud:acct_1",
            account_id="acct_1",
            tenant_id="tenant_1",
            character_ref="chr_abc",
        ),
    )

    with interaction_scope(
        InteractionContext(interaction_id="int-1", charge_id="chg-1"),
    ):
        await model.generate("inside")
    await model.generate("outside")

    assert seen[0]["x-yuralume-interaction"] == "v1;id=int-1;charge=chg-1"
    assert "x-yuralume-interaction" not in seen[1]


def _model() -> CloudGatewayChatModel:
    return CloudGatewayChatModel(
        base_url="https://gateway.example/",
        deployment_token="ykl_deploy",
        default_model="preset-chat",
        feature_key="chat",
        identity=CloudGatewayIdentity(
            operator_id="cloud:acct_1",
            account_id="acct_1",
            tenant_id="tenant_1",
            character_ref="chr_abc",
        ),
    )


def _covered_reply(content: str = "ok") -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": content}}]},
        headers={BILLING_COVERED_HEADER_NAME: "1"},
    )


def _install(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )


@pytest.mark.asyncio
async def test_cloud_gateway_sends_exact_compact_utf8_bytes_it_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["content"] = request.content
        return _covered_reply()

    _install(monkeypatch, handler)
    model = _model()

    assert await model.generate(
        "你好☕",
        image_urls=("https://img.example/a.png",),
    ) == "ok"

    expected = json.dumps(
        model._build_payload(
            "你好☕",
            image_urls=("https://img.example/a.png",),
            model=None,
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert seen["content"] == expected


@pytest.mark.asyncio
async def test_cloud_gateway_rejects_oversize_image_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _covered_reply("unexpected")

    _install(monkeypatch, handler)
    model = CloudGatewayChatModel(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        default_model="preset-chat",
        feature_key="chat",
        identity=CloudGatewayIdentity(
            operator_id="cloud:acct_1",
            account_id="acct_1",
            tenant_id="tenant_1",
            character_ref="chr_abc",
        ),
        max_request_bytes=1024,
    )

    with pytest.raises(ImageInputRejectedError) as excinfo:
        await model.generate(
            "look",
            image_urls=("data:image/png;base64," + "A" * 2048,),
        )

    assert excinfo.value.status_code == 413
    assert calls == 0


@pytest.mark.asyncio
async def test_cloud_gateway_rejects_oversize_text_as_non_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _covered_reply("unexpected")

    _install(monkeypatch, handler)
    model = CloudGatewayChatModel(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        default_model="preset-chat",
        feature_key="chat",
        identity=CloudGatewayIdentity(
            operator_id="cloud:acct_1",
            account_id="acct_1",
            tenant_id="tenant_1",
            character_ref="chr_abc",
        ),
        max_request_bytes=256,
    )

    with pytest.raises(CloudGatewayRequestTooLargeError) as excinfo:
        await model.generate("界" * 300)

    assert excinfo.value.retryable is False
    assert excinfo.value.actual_bytes > excinfo.value.max_bytes == 256
    assert calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 404, 413, 415, 422])
@pytest.mark.parametrize("streaming", [False, True])
async def test_cloud_gateway_image_4xx_is_typed_for_one_shot_degrade(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    streaming: bool,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={
            "error": {
                "code": "provider_error",
                "message": f"provider returned HTTP {status_code}",
                "retryable": False,
            },
        })

    _install(monkeypatch, handler)

    with pytest.raises(ImageInputRejectedError) as excinfo:
        if streaming:
            async for _ in _model().generate_stream(
                "look", image_urls=("https://img.example/a.png",),
            ):
                pass
        else:
            await _model().generate(
                "look",
                image_urls=("https://img.example/a.png",),
            )

    assert excinfo.value.status_code == status_code


@pytest.mark.asyncio
async def test_cloud_gateway_plain_provider_413_is_not_image_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(413, json={
            "error": {
                "code": "provider_error",
                "message": "provider returned HTTP 413",
                "retryable": False,
            },
        })

    _install(monkeypatch, handler)

    with pytest.raises(ExpectedCloudRefusal) as excinfo:
        await _model().generate("plain text")

    assert not isinstance(excinfo.value, ImageInputRejectedError)
    assert excinfo.value.code == "provider_error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "code"),
    [(400, "content_policy_denied"), (403, "entitlement_denied")],
)
async def test_cloud_gateway_policy_refusal_with_image_is_not_degraded(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    code: str,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={
            "error": {
                "code": code,
                "message": "request refused by policy",
                "retryable": False,
            },
        })

    _install(monkeypatch, handler)

    with pytest.raises(ExpectedCloudRefusal) as excinfo:
        await _model().generate(
            "look",
            image_urls=("https://img.example/a.png",),
        )

    assert not isinstance(excinfo.value, ImageInputRejectedError)
    assert excinfo.value.code == code


@pytest.mark.asyncio
async def test_cloud_gateway_stream_applies_same_wire_budget_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="data: [DONE]\n")

    _install(monkeypatch, handler)
    model = CloudGatewayChatModel(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        default_model="preset-chat",
        feature_key="chat",
        identity=CloudGatewayIdentity(
            operator_id="cloud:acct_1",
            account_id="acct_1",
            tenant_id="tenant_1",
            character_ref="chr_abc",
        ),
        max_request_bytes=1024,
    )

    with pytest.raises(ImageInputRejectedError):
        async for _ in model.generate_stream(
            "look",
            image_urls=("data:image/png;base64," + "A" * 2048,),
        ):
            pass

    assert calls == 0


@pytest.mark.asyncio
async def test_a_covered_call_is_marked_but_a_refused_one_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Consumption is what decides refund vs settle on a failed action.

    A covered 200 means the Gateway waived its own billing under the covering
    charge, so the tokens are spent whatever happens next. An upstream error
    means nothing was served, and the player must still get a full refund.
    """
    answers = [
        _covered_reply(),
        httpx.Response(502, json={"error": {"message": "upstream down"}}),
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        return answers.pop(0)

    _install(monkeypatch, handler)
    model = _model()

    served = InteractionContext(interaction_id="int-1", charge_id="chg-1")
    with interaction_scope(served):
        await model.generate("served")
    refused = InteractionContext(interaction_id="int-2", charge_id="chg-2")
    with interaction_scope(refused):
        with pytest.raises(Exception):
            await model.generate("refused")

    assert served.usage.consumed is True
    assert refused.usage.consumed is False


@pytest.mark.asyncio
async def test_a_call_the_gateway_billed_itself_is_not_consumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C2: no covered header ⇒ the Gateway charged this call per token.

    That is the verifier-failed / budget-exhausted fallback. Counting it as
    consumed would keep the fixed action charge on top of the per-call one and
    bill the same reply twice.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}]},
        )

    _install(monkeypatch, handler)

    context = InteractionContext(interaction_id="int-1", charge_id="chg-1")
    with interaction_scope(context):
        assert await _model().generate("hello") == "ok"

    assert context.usage.consumed is False


@pytest.mark.asyncio
async def test_a_covered_answer_that_never_parses_is_not_consumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C4: the charge buys a *result*, not an HTTP status.

    A 200 whose body carries no usable content delivered nothing, so the action
    is still refundable in full.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": []},
            headers={BILLING_COVERED_HEADER_NAME: "1"},
        )

    _install(monkeypatch, handler)

    context = InteractionContext(interaction_id="int-1", charge_id="chg-1")
    with interaction_scope(context):
        with pytest.raises(Exception):
            await _model().generate("hello")

    assert context.usage.consumed is False


@pytest.mark.asyncio
async def test_a_covered_stream_is_consumed_at_its_first_content_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C4: delivery, not connection — but before the consumer can abort.

    The mark lands as the first real token is handed over, so "send, then hit
    stop" still settles instead of becoming a free generation.
    """
    body = (
        'data: {"choices":[{"delta":{"content":"he"}}]}\n'
        'data: {"choices":[{"delta":{"content":"llo"}}]}\n'
        "data: [DONE]\n"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=body, headers={BILLING_COVERED_HEADER_NAME: "1"},
        )

    _install(monkeypatch, handler)

    context = InteractionContext(interaction_id="int-1", charge_id="chg-1")
    with interaction_scope(context):
        stream = _model().generate_stream("hello")
        first = await stream.__anext__()
        assert first == "he"
        assert context.usage.consumed is True
        await stream.aclose()

    # One covered call, however many chunks it produced.
    assert context.usage.covered_calls == 1


@pytest.mark.asyncio
async def test_a_covered_stream_with_no_content_is_not_consumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Headers on the wire are not a result: an empty stream stays refundable."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="data: [DONE]\n",
            headers={BILLING_COVERED_HEADER_NAME: "1"},
        )

    _install(monkeypatch, handler)

    context = InteractionContext(interaction_id="int-1", charge_id="chg-1")
    with interaction_scope(context):
        chunks = [chunk async for chunk in _model().generate_stream("hello")]

    assert chunks == []
    assert context.usage.consumed is False


@pytest.mark.asyncio
async def test_an_uncovered_stream_is_not_consumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text='data: {"choices":[{"delta":{"content":"hi"}}]}\ndata: [DONE]\n',
        )

    _install(monkeypatch, handler)

    context = InteractionContext(interaction_id="int-1", charge_id="chg-1")
    with interaction_scope(context):
        chunks = [chunk async for chunk in _model().generate_stream("hello")]

    assert chunks == ["hi"]
    assert context.usage.consumed is False


@pytest.mark.asyncio
async def test_with_supports_vision_binds_a_clone_and_leaves_the_base_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control-plane pin lands on a per-call copy, not on the base.

    The constructor default stays ``True`` so an unpinned preset keeps the
    pre-existing hosted behaviour; only the clone handed to the caller
    carries the pinned capability.
    """
    seen: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "url": str(request.url),
                "feature": request.headers.get("X-Yuralume-Feature"),
                "payload": json.loads(request.content.decode()),
            },
        )
        return _covered_reply("bound")

    _install(monkeypatch, handler)
    base = _model()
    assert base.supports_vision is True

    text_only = base.with_supports_vision(False)

    assert text_only is not base
    assert text_only.supports_vision is False
    # The base singleton is shared across every hosted call; a pin that
    # mutated it in place would leak onto unrelated features.
    assert base.supports_vision is True

    # Everything else about the clone still behaves like the base adapter.
    assert await text_only.generate("你好") == "bound"
    assert seen[0]["url"] == "https://gateway.example/v1/chat/completions"
    assert seen[0]["feature"] == "chat"
    assert seen[0]["payload"] == base._build_payload(
        "你好", image_urls=(), model=None,
    )
    assert text_only.prefers_public_image_urls is True
    assert text_only.provider_id == base.provider_id


@pytest.mark.asyncio
async def test_with_supports_vision_can_pin_true_explicitly() -> None:
    base = _model()

    pinned = base.with_supports_vision(True)

    assert pinned is not base
    assert pinned.supports_vision is True
