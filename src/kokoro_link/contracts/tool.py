"""Tool-use ports.

``ToolPort`` is the unit of extensibility: each real tool (ComfyUI
image, web search, weather lookup, etc.) is a single adapter class
under ``infrastructure/tools/<name>/``. The orchestrator never
imports a concrete tool — it goes through the registry.

``ToolRegistryPort`` decouples the orchestrator from per-character
permission logic: the registry returns only the tools a given
character is allowed to invoke.

``ToolInvocationRepositoryPort`` is the audit-log store. We keep the
repository *separate* from proactive / memory repos because the scope
is different (one row per tool run, not per decision).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.tool_invocation import ToolInvocation
from kokoro_link.domain.value_objects.tool_call import ToolResult


@dataclass(frozen=True, slots=True)
class ToolContext:
    """What a tool adapter gets to see when invoked.

    Concrete tools pick what they need. We pass the full ``Character``
    so tools like ``ComfyImageTool`` can read ``appearance`` /
    ``image_urls`` / ``state.emotion`` without extra lookups.

    ``arguments`` is whatever the LLM produced, already JSON-parsed;
    adapters validate their own schema.
    """

    character: Character
    arguments: Mapping[str, Any]
    conversation_id: str | None = None
    recent_dialogue: str = ""
    """Optional snippet of the last few chat turns, already formatted
    as ``role: text`` lines. Tools that generate visual / media content
    (e.g. ``ComfyImageTool``) use this to resolve scene pronouns and
    implicit references ("那樣的感覺", "剛剛講的那個地方") that only
    make sense in context. Empty when the caller didn't plumb any —
    tools must still work without it."""

    user_attachment_urls: tuple[str, ...] = ()
    """Images the user attached to the *current* turn, already resolved
    to LLM-fetchable form (data: or absolute URL). Visual tools forward
    these to their own vision-capable rewriter so a "幫我換上這件衣服"
    + photo request can extract outfit / scene cues from the picture
    instead of relying on a guess from the chat text alone."""


class ToolPort(Protocol):
    """One concrete tool (ComfyUI image, etc.).

    Implementations should be *stateless per call* — any heavy clients
    (HTTP sessions, websocket connections) are owned by the adapter
    and reused, but no per-invocation state leaks between calls.
    """

    name: str
    description: str
    """Short Chinese sentence the prompt builder embeds so the model
    knows when to pick this tool. Keep under ~40 chars — the full
    tool list is injected into every turn's prompt."""

    parameters_schema: Mapping[str, Any]
    """JSON-schema-lite object describing ``arguments``. The prompt
    builder renders this so the model sees field names + purposes."""

    async def invoke(self, ctx: ToolContext) -> ToolResult: ...


class ToolRegistryPort(Protocol):
    def all(self) -> list[ToolPort]:
        """Every registered tool (for the admin UI + schema dumps)."""

    def get(self, name: str) -> ToolPort | None:
        """Look up a tool by name, regardless of character permissions."""

    def list_for_character(self, character: Character) -> list[ToolPort]:
        """Only the tools this character is allowed to invoke."""


class ToolInvocationRepositoryPort(Protocol):
    async def add(self, invocation: ToolInvocation) -> ToolInvocation: ...

    async def save(self, invocation: ToolInvocation) -> ToolInvocation:
        """Upsert — used to write the completed status after ``add``."""

    async def list_for_character(
        self,
        character_id: str,
        *,
        limit: int = 50,
    ) -> list[ToolInvocation]: ...

    async def delete_for_character(self, character_id: str) -> int: ...
