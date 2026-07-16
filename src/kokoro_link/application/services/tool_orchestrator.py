"""Tool-call dispatch, permission check, and audit log.

The orchestrator is the *only* entry point for executing a
``ToolCall`` produced by the chat model or the proactive decider. It
enforces three invariants:

1. **Permission** — only tools listed in ``character.allowed_tools``
   may run. An unauthorized call is logged as ``STATUS_DENIED`` and
   the caller gets a ``ToolResult.failure`` so the model can apologise
   to the user instead of pretending it ran.
2. **Audit** — every attempt writes a ``ToolInvocation`` row (pending
   → success / failed / denied). The operator UI reads from here to
   answer "why did Yuki send me a picture of a dog".
3. **Isolation** — tool exceptions never bubble out. A crashing
   adapter becomes a ``failure`` result so the chat loop can still
   produce a human reply.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from kokoro_link.contracts.tool import (
    ToolContext,
    ToolInvocationRepositoryPort,
    ToolRegistryPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.tool_invocation import ToolInvocation
from kokoro_link.domain.value_objects.tool_call import ToolCall, ToolResult

_LOGGER = logging.getLogger(__name__)


class ToolOrchestrator:
    def __init__(
        self,
        *,
        registry: ToolRegistryPort,
        invocation_repository: ToolInvocationRepositoryPort,
    ) -> None:
        self._registry = registry
        self._invocations = invocation_repository

    async def execute(
        self,
        *,
        character: Character,
        call: ToolCall,
        conversation_id: str | None = None,
        recent_dialogue: str = "",
        user_attachment_urls: tuple[str, ...] = (),
    ) -> tuple[ToolInvocation, ToolResult]:
        """Run a tool call end-to-end. Returns ``(audit_row, result)``.

        The caller uses ``audit_row.id`` as a correlation key when
        replying with ``tool`` messages in the chat transcript.
        """
        now = datetime.now(timezone.utc)
        invocation = ToolInvocation.pending(
            character_id=character.id,
            conversation_id=conversation_id,
            tool_name=call.name,
            arguments=call.arguments,
            started_at=now,
        )
        try:
            await self._invocations.add(invocation)
        except Exception:
            # Losing the audit row would hide the call from the
            # operator UI but shouldn't block execution — log and
            # proceed so the user still gets their reply.
            _LOGGER.exception(
                "failed to persist pending tool invocation %s", invocation.id,
            )

        if call.name not in set(character.allowed_tools):
            reason = f"tool {call.name!r} not in character.allowed_tools"
            denied = invocation.deny(reason)
            await self._persist(denied)
            return denied, ToolResult.failure(reason)

        tool = self._registry.get(call.name)
        if tool is None:
            reason = f"tool {call.name!r} is not registered"
            denied = invocation.deny(reason)
            await self._persist(denied)
            return denied, ToolResult.failure(reason)

        ctx = ToolContext(
            character=character,
            arguments=call.arguments,
            conversation_id=conversation_id,
            recent_dialogue=recent_dialogue,
            user_attachment_urls=tuple(user_attachment_urls or ()),
        )
        try:
            result = await tool.invoke(ctx)
        except Exception as exc:  # noqa: BLE001 — adapter isolation is the point
            _LOGGER.exception("tool %s crashed", call.name)
            result = ToolResult.failure(f"tool crashed: {exc}")

        completed = invocation.complete(result)
        await self._persist(completed)
        return completed, result

    async def _persist(self, invocation: ToolInvocation) -> None:
        try:
            await self._invocations.save(invocation)
        except Exception:
            _LOGGER.exception(
                "failed to persist tool invocation %s status=%s",
                invocation.id, invocation.status,
            )
