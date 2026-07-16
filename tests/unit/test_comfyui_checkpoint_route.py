"""Tests for GET /system/comfyui/checkpoints (Phase 2, CORE_ENV_TO_ADMIN_CONFIG).

The endpoint powers the ComfyUI checkpoint dropdown in the provider admin
form. It must fail soft: an unreachable / malformed ComfyUI degrades to
``available=False`` so the UI falls back to plain-text entry rather than
blocking the whole provider form.
"""

from __future__ import annotations

import pytest

from kokoro_link.api.routes.system import list_comfyui_checkpoints


@pytest.mark.asyncio
async def test_checkpoints_blank_server_is_unavailable() -> None:
    result = await list_comfyui_checkpoints(server="")
    assert result.available is False
    assert result.checkpoints == []
    assert "server" in result.error.lower()


@pytest.mark.asyncio
async def test_checkpoints_success(monkeypatch) -> None:
    async def fake_list(self):  # noqa: ANN001
        return ["a.safetensors", "b.safetensors"]

    from kokoro_link.infrastructure.tools.comfyui.client import AsyncComfyUiClient

    monkeypatch.setattr(AsyncComfyUiClient, "list_checkpoints", fake_list)

    result = await list_comfyui_checkpoints(server="http://127.0.0.1:8188")
    assert result.available is True
    assert result.checkpoints == ["a.safetensors", "b.safetensors"]
    assert result.error == ""


@pytest.mark.asyncio
async def test_checkpoints_unreachable_falls_back(monkeypatch) -> None:
    async def boom(self):  # noqa: ANN001
        raise RuntimeError("connection refused")

    from kokoro_link.infrastructure.tools.comfyui.client import AsyncComfyUiClient

    monkeypatch.setattr(AsyncComfyUiClient, "list_checkpoints", boom)

    result = await list_comfyui_checkpoints(server="http://127.0.0.1:9999")
    assert result.available is False
    assert result.checkpoints == []
    assert "connection refused" in result.error
