"""Hosted mode owns ``web_search``; DB provider rows must not touch it.

In cloud mode the container mounts a Gateway-routed ``web_search`` and Core
holds no provider keys by design. A stray ``search`` connection row — a legacy
env seed, or a database carried over from a self-host install that was
converted to hosted — must not be able to replace that tool with a direct,
unbilled, unrouted provider call, nor to unregister it.
"""

from __future__ import annotations

import pytest

from kokoro_link.infrastructure.provider_settings.runtime_sync import _sync_search_tool
from kokoro_link.infrastructure.tools.registry import InMemoryToolRegistry
from kokoro_link.infrastructure.tools.websearch import (
    DuckDuckGoSearchClient,
    WebSearchTool,
)


class _StubService:
    """A provider-connection service that would mount DuckDuckGo if consulted."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def list_connections(self):
        self.calls.append("list_connections")
        return [_Row()]

    async def list_enabled_runtime(self, *, capability: str):
        self.calls.append(f"list_enabled_runtime:{capability}")
        return [_Row()]

    async def get_decrypted_secret(self, connection_id: str):
        self.calls.append("get_decrypted_secret")
        return {}

    async def record_runtime_status(self, connection_id: str, *, error: str | None):
        self.calls.append("record_runtime_status")


class _Row:
    id = "row-1"
    provider = "duckduckgo"
    capabilities = ("search",)
    config: dict[str, object] = {}
    created_at = None
    updated_at = None


class _Container:
    def __init__(self, *, cloud_mode: bool) -> None:
        self.provider_connection_service = _StubService()
        self.tool_registry = InMemoryToolRegistry(
            [WebSearchTool(client=_SentinelClient())],
        )
        self.cloud_mode = cloud_mode


class _SentinelClient:
    """Stands in for the Gateway-routed client the container mounted."""

    async def search(self, *, query: str, max_results: int):  # pragma: no cover
        raise AssertionError("not called in this test")


@pytest.mark.asyncio
async def test_cloud_mode_leaves_the_gateway_routed_tool_alone() -> None:
    container = _Container(cloud_mode=True)
    mounted = container.tool_registry.get("web_search")

    await _sync_search_tool(container)

    # Same object: not replaced, not unregistered, and the DB was never read.
    assert container.tool_registry.get("web_search") is mounted
    assert container.provider_connection_service.calls == []


@pytest.mark.asyncio
async def test_self_host_still_syncs_the_tool_from_provider_rows() -> None:
    container = _Container(cloud_mode=False)
    mounted = container.tool_registry.get("web_search")

    await _sync_search_tool(container)

    replaced = container.tool_registry.get("web_search")
    assert replaced is not mounted
    assert isinstance(replaced._client, DuckDuckGoSearchClient)
