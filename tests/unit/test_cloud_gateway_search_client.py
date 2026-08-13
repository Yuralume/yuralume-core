"""BDD for ``CloudGatewaySearchClient`` (hosted ``web_search``).

Covers the routed happy path (preset from the control-plane profile, identity
headers, provider-neutral response parsing), the deliberate absence of a preset
fallback, the disabled-feature and error-mapping branches, and the defensive
result parsing that must not let one malformed row kill an otherwise usable
answer.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from kokoro_link.application.services.cloud_identity_context import cloud_actor_scope
from kokoro_link.contracts.cloud_gateway import CloudGatewayIdentity
from kokoro_link.contracts.cloud_routing_profile import CloudRoutingProfile
from kokoro_link.infrastructure.tools.websearch import (
    CloudGatewaySearchClient,
    SearchError,
)

_IDENTITY = CloudGatewayIdentity(
    operator_id="operator-1",
    tenant_id="tenant-1",
    account_id="account-1",
    character_ref="char-1",
    tenant_tier="plus",
)


class _StubRoutingProfilePort:
    def __init__(self, profile: CloudRoutingProfile | Exception) -> None:
        self._profile = profile
        self.calls: list[dict[str, str]] = []

    async def get_profile(
        self, *, tenant_id: str, account_id: str, tier: str, user_id: str = "",
    ) -> CloudRoutingProfile:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "account_id": account_id,
                "tier": tier,
                "user_id": user_id,
            },
        )
        if isinstance(self._profile, Exception):
            raise self._profile
        return self._profile


def _profile(**overrides: Any) -> CloudRoutingProfile:
    base: dict[str, Any] = {
        "llm_feature_presets": {},
        "image_feature_presets": {},
        "video_feature_presets": {},
        "tts_voice_defaults": {},
        "strict_no_fallback": False,
        "disabled_features": frozenset(),
        "catalog_version": 1,
        "routing_policy_version": 1,
        "search_feature_presets": {"web_search": "openai-web-search"},
    }
    base.update(overrides)
    return CloudRoutingProfile(**base)


async def _run_with_transport(coro_factory, transport: httpx.MockTransport):
    original_init = httpx.AsyncClient.__init__

    def patched_init(self: httpx.AsyncClient, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        original_init(self, **kwargs)

    httpx.AsyncClient.__init__ = patched_init  # type: ignore[method-assign]
    try:
        return await coro_factory()
    finally:
        httpx.AsyncClient.__init__ = original_init  # type: ignore[method-assign]


def _client(routing: _StubRoutingProfilePort | None) -> CloudGatewaySearchClient:
    return CloudGatewaySearchClient(
        base_url="https://gateway.example",
        deployment_token="deploy-token",
        identity=_IDENTITY,
        routing_profile_port=routing,
    )


@pytest.mark.asyncio
async def test_routed_search_sends_identity_and_parses_results() -> None:
    routing = _StubRoutingProfilePort(_profile())
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = request.read().decode()
        return httpx.Response(
            200,
            json={
                "model": "openai-web-search",
                "answer": "Yuralume shipped hosted search.",
                "results": [
                    {
                        "title": "Release notes",
                        "url": "https://example.com/a",
                        "snippet": "hosted search",
                    },
                ],
            },
        )

    client = _client(routing)
    response = await _run_with_transport(
        lambda: client.search(query="yuralume hosted search", max_results=5),
        httpx.MockTransport(handler),
    )

    assert response.answer == "Yuralume shipped hosted search."
    assert [item.url for item in response.results] == ["https://example.com/a"]
    assert seen["url"] == "https://gateway.example/v1/search"
    assert '"model":"openai-web-search"' in seen["body"]
    assert seen["headers"]["x-yuralume-feature"] == "web_search"
    assert seen["headers"]["x-yuralume-tenant"] == "tenant-1"
    assert seen["headers"]["x-yuralume-account"] == "account-1"
    assert seen["headers"]["authorization"] == "Bearer deploy-token"
    # The per-player scope is what makes a user-level route resolve at all.
    assert routing.calls == [
        {
            "tenant_id": "tenant-1",
            "account_id": "account-1",
            "tier": "plus",
            "user_id": "account-1",
        },
    ]


@pytest.mark.asyncio
async def test_unrouted_identity_never_falls_back_to_a_default_preset() -> None:
    """No route must mean no search, not "use whatever the deployment has".

    A search preset names a paid upstream; a fallback here would let an
    identity nobody priced reach it."""
    routing = _StubRoutingProfilePort(_profile(search_feature_presets={}))
    called = False

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    client = _client(routing)
    with pytest.raises(SearchError):
        await _run_with_transport(
            lambda: client.search(query="q", max_results=5),
            httpx.MockTransport(handler),
        )
    assert called is False


@pytest.mark.asyncio
async def test_disabled_feature_is_refused_before_the_gateway_call() -> None:
    routing = _StubRoutingProfilePort(
        _profile(disabled_features=frozenset({"search:web_search"})),
    )
    called = False

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    client = _client(routing)
    with pytest.raises(SearchError):
        await _run_with_transport(
            lambda: client.search(query="q", max_results=5),
            httpx.MockTransport(handler),
        )
    assert called is False


@pytest.mark.asyncio
async def test_missing_routing_port_is_a_search_error_not_a_crash() -> None:
    client = _client(None)
    with pytest.raises(SearchError):
        await client.search(query="q", max_results=5)


@pytest.mark.asyncio
async def test_insufficient_credits_reads_as_balance_not_failure() -> None:
    routing = _StubRoutingProfilePort(_profile())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            402,
            json={"error": {"code": "insufficient_credits", "message": "no credits"}},
        )

    client = _client(routing)
    with pytest.raises(SearchError) as excinfo:
        await _run_with_transport(
            lambda: client.search(query="q", max_results=5),
            httpx.MockTransport(handler),
        )
    assert "螢火不足" in str(excinfo.value)


@pytest.mark.asyncio
async def test_malformed_result_rows_cost_their_row_not_the_answer() -> None:
    routing = _StubRoutingProfilePort(_profile())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "answer": "still useful",
                "results": [
                    "not-an-object",
                    {"title": "no url"},
                    {"url": "https://example.com/b"},
                ],
            },
        )

    client = _client(routing)
    response = await _run_with_transport(
        lambda: client.search(query="q", max_results=5),
        httpx.MockTransport(handler),
    )

    assert response.answer == "still useful"
    assert [item.url for item in response.results] == ["https://example.com/b"]
    # A row without a title falls back to its url rather than rendering blank.
    assert response.results[0].title == "https://example.com/b"


@pytest.mark.asyncio
async def test_max_results_caps_the_rows_the_tool_renders() -> None:
    routing = _StubRoutingProfilePort(_profile())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "answer": "a",
                "results": [
                    {"title": f"t{index}", "url": f"https://example.com/{index}"}
                    for index in range(10)
                ],
            },
        )

    client = _client(routing)
    response = await _run_with_transport(
        lambda: client.search(query="q", max_results=2),
        httpx.MockTransport(handler),
    )

    assert len(response.results) == 2


@pytest.mark.asyncio
async def test_transport_failure_is_a_search_error() -> None:
    routing = _StubRoutingProfilePort(_profile())

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = _client(routing)
    with pytest.raises(SearchError):
        await _run_with_transport(
            lambda: client.search(query="q", max_results=5),
            httpx.MockTransport(handler),
        )


# ---------------------------------------------------------------------------
# Hardening after the 2026-08-10 adversarial review. Each of these is a way the
# Gateway can misbehave that must not become "your search found nothing", a
# leaked token, or a dead worker.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_coerced_non_string_preset_is_not_a_route() -> None:
    """No fallback means "routed" is the whole decision, so it must be exact.

    ``str(123)`` would turn a malformed control-plane value into the preset
    ``"123"`` — a real route as far as this client is concerned, and money spent
    on an upstream nobody pointed here."""
    profile = CloudRoutingProfile.from_payload(
        {"search_feature_presets": {"web_search": 123}},
    )
    assert profile.search_feature_presets == {}

    routing = _StubRoutingProfilePort(profile)
    client = _client(routing)
    with pytest.raises(SearchError):
        await client.search(query="q", max_results=5)


@pytest.mark.asyncio
async def test_a_real_string_preset_still_parses() -> None:
    profile = CloudRoutingProfile.from_payload(
        {"search_feature_presets": {"web_search": " openai-web-search "}},
    )
    assert profile.search_feature_presets == {"web_search": "openai-web-search"}


@pytest.mark.asyncio
async def test_a_malformed_2xx_body_is_an_error_not_an_empty_result() -> None:
    """Reporting a contract violation as "no results" tells the player it worked."""
    routing = _StubRoutingProfilePort(_profile())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "an", "object"])

    client = _client(routing)
    with pytest.raises(SearchError):
        await _run_with_transport(
            lambda: client.search(query="q", max_results=5),
            httpx.MockTransport(handler),
        )


@pytest.mark.asyncio
async def test_a_non_array_results_field_is_an_error() -> None:
    routing = _StubRoutingProfilePort(_profile())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"answer": "a", "results": "nope"})

    client = _client(routing)
    with pytest.raises(SearchError):
        await _run_with_transport(
            lambda: client.search(query="q", max_results=5),
            httpx.MockTransport(handler),
        )


@pytest.mark.asyncio
async def test_a_well_formed_empty_answer_is_still_a_normal_no_results() -> None:
    """The distinguishable case is a broken shape, not an honest empty search."""
    routing = _StubRoutingProfilePort(_profile())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"answer": "", "results": []})

    client = _client(routing)
    response = await _run_with_transport(
        lambda: client.search(query="q", max_results=5),
        httpx.MockTransport(handler),
    )

    assert response.answer == ""
    assert response.results == []


@pytest.mark.asyncio
async def test_an_oversized_body_fails_instead_of_exhausting_memory() -> None:
    routing = _StubRoutingProfilePort(_profile())
    oversized = b'{"answer":"' + b"x" * (3 * 1024 * 1024) + b'","results":[]}'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=oversized)

    client = _client(routing)
    with pytest.raises(SearchError):
        await _run_with_transport(
            lambda: client.search(query="q", max_results=5),
            httpx.MockTransport(handler),
        )


@pytest.mark.asyncio
async def test_the_gateway_error_body_never_reaches_the_log(caplog) -> None:
    """A proxy 401 page can echo the Authorization header; the log must not."""
    routing = _StubRoutingProfilePort(_profile())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            text="Unauthorized. Authorization: Bearer deploy-token was rejected",
        )

    client = _client(routing)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(SearchError):
            await _run_with_transport(
                lambda: client.search(query="q", max_results=5),
                httpx.MockTransport(handler),
            )

    assert "deploy-token" not in caplog.text
    assert "status=401" in caplog.text


@pytest.mark.asyncio
async def test_result_fields_are_clamped_before_they_reach_the_next_turn() -> None:
    routing = _StubRoutingProfilePort(_profile())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "answer": "a",
                "results": [
                    {
                        "title": "t" * 5000,
                        "url": "https://example.com/" + "u" * 5000,
                        "snippet": "s" * 5000,
                    },
                ],
            },
        )

    client = _client(routing)
    response = await _run_with_transport(
        lambda: client.search(query="q", max_results=5),
        httpx.MockTransport(handler),
    )

    item = response.results[0]
    assert len(item.title) <= 200
    assert len(item.url) <= 500
    assert len(item.snippet) <= 600


@pytest.mark.asyncio
async def test_an_identity_resolver_blowup_is_still_a_search_error() -> None:
    """The contract owed to WebSearchTool is that every failure is a SearchError."""

    class _ExplodingResolver:
        async def resolve_context(self, context):
            raise RuntimeError("resolver exploded")

    client = CloudGatewaySearchClient(
        base_url="https://gateway.example",
        deployment_token="deploy-token",
        identity_resolver=_ExplodingResolver(),
        routing_profile_port=_StubRoutingProfilePort(_profile()),
    )

    with cloud_actor_scope(operator_id="operator-1"):
        with pytest.raises(SearchError):
            await client.search(query="q", max_results=5)
