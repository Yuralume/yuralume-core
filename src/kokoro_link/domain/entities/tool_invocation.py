"""Tool invocation audit entity.

One row per tool call attempted — regardless of outcome. Records:

- what tool was asked for
- the arguments the character suggested
- the result / error
- how long it took

Used by the operator UI to debug "why did Yuki send me a blurry
picture?" and to bill / rate-limit expensive tools (e.g. ComfyUI
runs that take 30-60s of GPU time each).

Separate from ``ProactiveAttempt`` because the scopes don't overlap:
a single proactive push may or may not invoke a tool, and a chat turn
may invoke multiple tools in sequence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from kokoro_link.domain.value_objects.tool_call import ToolResult


STATUS_PENDING = "pending"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_DENIED = "denied"
"""Permission check said the character isn't allowed to call this tool."""


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    id: str
    character_id: str
    conversation_id: str | None
    tool_name: str
    arguments: Mapping[str, Any]
    status: str
    output_text: str = ""
    error: str | None = None
    attachment_urls: tuple[str, ...] = ()
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None

    @classmethod
    def pending(
        cls,
        *,
        character_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        conversation_id: str | None = None,
        started_at: datetime | None = None,
    ) -> "ToolInvocation":
        return cls(
            id=str(uuid4()),
            character_id=character_id,
            conversation_id=conversation_id,
            tool_name=tool_name,
            arguments=dict(arguments),
            status=STATUS_PENDING,
            started_at=started_at or datetime.now(timezone.utc),
        )

    def complete(self, result: ToolResult, *, now: datetime | None = None) -> "ToolInvocation":
        from dataclasses import replace

        return replace(
            self,
            status=STATUS_SUCCESS if result.ok else STATUS_FAILED,
            output_text=result.output_text,
            error=result.error,
            attachment_urls=tuple(a.url for a in result.attachments),
            finished_at=now or datetime.now(timezone.utc),
        )

    def deny(self, reason: str, *, now: datetime | None = None) -> "ToolInvocation":
        from dataclasses import replace

        return replace(
            self,
            status=STATUS_DENIED,
            error=reason,
            finished_at=now or datetime.now(timezone.utc),
        )
