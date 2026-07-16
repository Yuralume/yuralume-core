"""Tool catalogue route.

Exposes the set of tools the deployment currently has wired so the
frontend can render a checkbox list in the character settings tab.
This is a *catalogue* endpoint — not an invocation entry point. Tool
calls only happen via the chat path (``ChatService`` tool cycle) or
the proactive path (future T5).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from kokoro_link.api.dependencies import get_container
from kokoro_link.bootstrap.container import ServiceContainer

router = APIRouter(tags=["tools"])


class ToolDescriptor(BaseModel):
    name: str
    description: str
    parameters_schema: dict[str, Any]


@router.get("/tools", response_model=list[ToolDescriptor])
async def list_tools(
    container: ServiceContainer = Depends(get_container),
) -> list[ToolDescriptor]:
    return [
        ToolDescriptor(
            name=tool.name,
            description=tool.description,
            parameters_schema=dict(tool.parameters_schema),
        )
        for tool in container.tool_registry.all()
    ]
