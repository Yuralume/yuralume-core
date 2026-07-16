"""Fake tools for tests + the ``fake`` provider local dev setup.

``EchoTool`` — trivial, returns whatever input it received. Lets us
exercise the orchestrator / chat-loop plumbing without hitting any
real external service.

``FakeImageTool`` — mimics ``ComfyImageTool`` enough to verify the
attachment plumbing (tool result carries an image URL, chat prompt
re-ingest works) without needing a live ComfyUI server.
"""

from __future__ import annotations

from typing import Any, Mapping

from kokoro_link.contracts.tool import ToolContext, ToolPort
from kokoro_link.domain.value_objects.tool_call import (
    ToolAttachment,
    ToolResult,
)


class EchoTool(ToolPort):
    name: str = "echo"
    description: str = "把傳入的文字原封不動回傳，用來測試工具鏈。"
    parameters_schema: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "要回聲的文字"},
        },
        "required": ["text"],
    }

    async def invoke(self, ctx: ToolContext) -> ToolResult:
        text = str(ctx.arguments.get("text", ""))
        return ToolResult.success(output_text=f"echo: {text}")


class FakeImageTool(ToolPort):
    """Deterministic stand-in for the real image tool.

    Returns a fixed URL; the test harness can set up a static file at
    that path, or the callers that don't actually render the image
    just need to verify the attachment flowed through.
    """

    name: str = "fake_image"
    description: str = "產生一張角色圖片（測試用 stub）。"
    parameters_schema: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "scene": {"type": "string", "description": "想表現的情境"},
        },
    }

    def __init__(self, url: str = "/uploads/stub/fake.png") -> None:
        self._url = url

    async def invoke(self, ctx: ToolContext) -> ToolResult:
        scene = str(ctx.arguments.get("scene") or "normal")
        return ToolResult.success(
            output_text=f"已產生一張『{scene}』的圖。",
            attachments=[
                ToolAttachment(
                    kind="image", url=self._url, mime_type="image/png",
                    caption=scene,
                ),
            ],
        )
