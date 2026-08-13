"""AP2 — Core-side HTTP client for the User service action-charge endpoints.

Mirrors ``test_cloud_credit_balance_client`` transport mocking. The three
outcomes that matter are the credential/path contract, the 402 → U4 refusal
mapping, and the "never strand an unnameable reservation" rule.
"""

from __future__ import annotations

import json

import httpx
import pytest

from kokoro_link.contracts.cloud_action_billing import (
    ACTION_KIND_QUOTA_OVERAGE,
    ActionChargeUnavailable,
    ActionPriceChanged,
)
from kokoro_link.infrastructure.cloud.action_charge_client import (
    ActionChargeClient,
)
from kokoro_link.infrastructure.llm.cloud_refusal import (
    INSUFFICIENT_CREDITS_CODE,
    ExpectedCloudRefusal,
)

_CREDENTIAL = "core-kid|core|yuralume-user|credits:charge|core-secret"


class _MockAsyncClient(httpx.AsyncClient):
    def __init__(self, handler, **kwargs) -> None:
        super().__init__(
            transport=httpx.MockTransport(handler),
            base_url=kwargs["base_url"],
            timeout=kwargs["timeout"],
        )


def _install(monkeypatch, handler) -> None:
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )


def _client() -> ActionChargeClient:
    return ActionChargeClient(
        base_url="https://user.example",
        internal_credential=_CREDENTIAL,
    )


@pytest.mark.asyncio
async def test_charge_posts_the_action_body_with_the_charge_scope(
    monkeypatch,
) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["scope"] = request.headers.get("x-yuralume-service-scope")
        seen["caller"] = request.headers.get("x-yuralume-service-caller")
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "charge_id": "chg-1",
                "price_cr": "3.50",
                "no_charge": False,
                "gift": 1.0,
                "purchased": 2.5,
            },
        )

    _install(monkeypatch, handler)
    charge = await _client().charge(
        tenant_id="tenant-1",
        action_key="chat",
        interaction_id="int-1",
        action_kind=ACTION_KIND_QUOTA_OVERAGE,
    )

    assert seen["path"] == "/internal/v1/credits/actions/charge"
    assert seen["scope"] == "credits:charge"
    assert seen["caller"] == "core"
    assert seen["body"] == {
        "tenant_id": "tenant-1",
        "action_key": "chat",
        "interaction_id": "int-1",
        "action_kind": "quota_overage",
    }
    assert charge.charge_id == "chg-1"
    assert charge.price_cr == 3.5
    assert charge.no_charge is False


@pytest.mark.asyncio
async def test_missing_price_is_a_no_charge_success(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"no_charge": True})

    _install(monkeypatch, handler)
    charge = await _client().charge(
        tenant_id="t", action_key="chat", interaction_id="i",
    )
    assert charge.no_charge is True
    assert charge.charge_id == ""


@pytest.mark.asyncio
async def test_billable_charge_without_an_id_is_unavailable(monkeypatch) -> None:
    """Nothing could ever settle or release it — better to run uncharged."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"price_cr": 3.0})

    _install(monkeypatch, handler)
    with pytest.raises(ActionChargeUnavailable):
        await _client().charge(
            tenant_id="t", action_key="chat", interaction_id="i",
        )


@pytest.mark.asyncio
async def test_402_becomes_the_u4_insufficient_credits_refusal(
    monkeypatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            402,
            json={
                "error": {
                    "code": "insufficient_credits",
                    "message": "螢火不足",
                    "retryable": False,
                },
            },
        )

    _install(monkeypatch, handler)
    with pytest.raises(ExpectedCloudRefusal) as excinfo:
        await _client().charge(
            tenant_id="t", action_key="chat", interaction_id="i",
        )
    assert excinfo.value.code == INSUFFICIENT_CREDITS_CODE
    assert excinfo.value.reason == "螢火不足"


@pytest.mark.asyncio
async def test_the_quoted_price_binds_the_charge(monkeypatch) -> None:
    """C1: the charge names the number Core displayed, so the User service can
    refuse rather than silently bill a price the player never saw."""
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"charge_id": "chg-1", "price_cr": 3.5})

    _install(monkeypatch, handler)
    await _client().charge(
        tenant_id="t",
        action_key="chat",
        interaction_id="i",
        expected_price_cr=3.5,
    )

    assert seen["body"]["expected_price_cr"] == 3.5


@pytest.mark.asyncio
async def test_no_quote_means_no_binding_field(monkeypatch) -> None:
    """"Core quoted nothing" must not be sent as "Core quoted free"."""
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"charge_id": "chg-1"})

    _install(monkeypatch, handler)
    await _client().charge(tenant_id="t", action_key="chat", interaction_id="i")

    assert "expected_price_cr" not in seen["body"]


@pytest.mark.asyncio
async def test_managed_character_origin_rides_the_charge(monkeypatch) -> None:
    """EC7: the origin card slug is forwarded as its own snake_case field."""
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"charge_id": "chg-1"})

    _install(monkeypatch, handler)
    await _client().charge(
        tenant_id="t",
        action_key="chat",
        interaction_id="i",
        character_origin="official-yumi",
    )

    assert seen["body"]["character_origin"] == "official-yumi"


@pytest.mark.asyncio
async def test_no_origin_means_no_character_origin_field(monkeypatch) -> None:
    """A non-managed character must not put a key on the wire at all."""
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"charge_id": "chg-1"})

    _install(monkeypatch, handler)
    await _client().charge(tenant_id="t", action_key="chat", interaction_id="i")

    assert "character_origin" not in seen["body"]


@pytest.mark.asyncio
async def test_409_becomes_a_price_change_carrying_the_new_number(
    monkeypatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409, json={"error": "price_changed", "current_price_cr": 4.25},
        )

    _install(monkeypatch, handler)
    with pytest.raises(ActionPriceChanged) as excinfo:
        await _client().charge(
            tenant_id="t",
            action_key="chat",
            interaction_id="i",
            expected_price_cr=3.5,
        )

    assert excinfo.value.current_price_cr == 4.25
    assert excinfo.value.expected_price_cr == 3.5
    assert excinfo.value.action_key == "chat"


@pytest.mark.asyncio
async def test_409_also_reads_the_error_envelope_shape(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "price_changed",
                    "message": "價格已更新",
                    "current_price_cr": "5",
                },
            },
        )

    _install(monkeypatch, handler)
    with pytest.raises(ActionPriceChanged) as excinfo:
        await _client().charge(
            tenant_id="t", action_key="chat", interaction_id="i",
        )

    assert excinfo.value.current_price_cr == 5.0
    assert excinfo.value.reason == "價格已更新"


@pytest.mark.asyncio
async def test_a_409_for_anything_else_stays_the_fail_soft_outage(
    monkeypatch,
) -> None:
    """No policy for it ⇒ do not tell the player their price moved."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error": {"code": "duplicate"}})

    _install(monkeypatch, handler)
    with pytest.raises(ActionChargeUnavailable):
        await _client().charge(
            tenant_id="t", action_key="chat", interaction_id="i",
        )


@pytest.mark.asyncio
async def test_server_error_is_unavailable_not_a_refusal(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="nope")

    _install(monkeypatch, handler)
    with pytest.raises(ActionChargeUnavailable):
        await _client().charge(
            tenant_id="t", action_key="chat", interaction_id="i",
        )


@pytest.mark.asyncio
async def test_transport_failure_is_unavailable(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    _install(monkeypatch, handler)
    with pytest.raises(ActionChargeUnavailable):
        await _client().charge(
            tenant_id="t", action_key="chat", interaction_id="i",
        )


@pytest.mark.asyncio
async def test_settle_and_release_hit_the_lifecycle_paths(monkeypatch) -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={})

    _install(monkeypatch, handler)
    client = _client()
    await client.settle("chg-1")
    await client.release("chg-2")
    assert seen == [
        "/internal/v1/credits/actions/chg-1/settle",
        "/internal/v1/credits/actions/chg-2/release",
    ]


@pytest.mark.asyncio
async def test_a_probed_release_asks_the_ledger_to_decide(monkeypatch) -> None:
    """The one release Core cannot answer for itself.

    A handle rebuilt from job params after a restart has no usage record, so
    Core hands the settle-or-release verdict to the ledger's own
    ``covered_probe_at``. The answer names which way it went.
    """
    requests: list[tuple[httpx.URL, bytes]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url, request.content))
        return httpx.Response(200, json={"closed": "settled"})

    _install(monkeypatch, handler)

    await _client().release("chg-1", settle_if_probed=True)

    assert requests == [
        (
            httpx.URL(
                "https://user.example/internal/v1/credits/actions/chg-1/release"
                "?settle_if_probed=true",
            ),
            b"{}",
        ),
    ]


@pytest.mark.asyncio
async def test_an_ordinary_release_sends_no_probe_flag(monkeypatch) -> None:
    # Byte-identical to the pre-flag request: a live handle already knows the
    # answer, so asking would only invite the ledger to overrule it.
    requests: list[tuple[httpx.URL, bytes]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url, request.content))
        return httpx.Response(200, json={})

    _install(monkeypatch, handler)

    await _client().release("chg-1")

    assert requests == [
        (
            httpx.URL(
                "https://user.example/internal/v1/credits/actions/chg-1/release",
            ),
            b"{}",
        ),
    ]


@pytest.mark.asyncio
async def test_empty_base_url_is_unavailable() -> None:
    client = ActionChargeClient(base_url="", internal_credential=_CREDENTIAL)
    with pytest.raises(ActionChargeUnavailable):
        await client.charge(
            tenant_id="t", action_key="chat", interaction_id="i",
        )


@pytest.mark.asyncio
async def test_a_dropped_connection_is_retried_once_with_the_same_body(
    monkeypatch,
) -> None:
    """A timeout says nothing about whether the far side committed.

    Giving up on the first transport failure would debit the player for a
    charge Core never learned about — money gone until the stale-reservation
    sweeper — and then bill the whole turn again per call. The endpoint is
    idempotent on the Core-owned interaction id, so replaying is the safe move.
    """
    bodies: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            raise httpx.ReadTimeout("boom", request=request)
        return httpx.Response(200, json={"charge_id": "chg-9", "price_cr": 3})

    _install(monkeypatch, handler)

    charge = await _client().charge(
        tenant_id="tenant-1",
        action_key="chat",
        interaction_id="int-1",
    )

    assert charge.charge_id == "chg-9"
    assert len(bodies) == 2
    assert bodies[0] == bodies[1]


@pytest.mark.asyncio
async def test_a_persistent_transport_failure_still_gives_up(monkeypatch) -> None:
    attempts: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ConnectError("down", request=request)

    _install(monkeypatch, handler)

    with pytest.raises(ActionChargeUnavailable):
        await _client().charge(
            tenant_id="tenant-1", action_key="chat", interaction_id="int-1",
        )

    assert len(attempts) == 2
