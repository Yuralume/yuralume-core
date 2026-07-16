"""In-process ``ToolRegistryPort`` — dict-backed, per-character filter.

Constructed at container wiring time with every tool the deployment
knows about. Per-character filtering happens on ``list_for_character``
using ``character.allowed_tools``; unknown names (stale references to
a tool we removed) are silently ignored to avoid crashing the chat
loop on config drift.
"""

from __future__ import annotations

from kokoro_link.contracts.tool import ToolPort, ToolRegistryPort
from kokoro_link.domain.entities.character import Character


class InMemoryToolRegistry(ToolRegistryPort):
    def __init__(self, tools: list[ToolPort] | None = None) -> None:
        self._tools: dict[str, ToolPort] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: ToolPort) -> None:
        if not tool.name:
            raise ValueError("Tool must have a non-empty name")
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """Remove a tool by name; no-op if it isn't registered.

        Used by runtime provider-settings sync to hot-detach a tool
        (e.g. ``web_search`` when the operator disables every search
        provider) without a restart. Silent on unknown names so a
        double-disable can't raise."""
        self._tools.pop(name, None)

    def replace(self, tool: ToolPort) -> None:
        """Atomically swap a tool in by name.

        Unlike ``register`` (which raises on a duplicate name), this
        overwrites any existing entry — the hot-swap path when the
        operator switches the active search provider. Building the new
        tool fully before this call keeps the registry from ever holding
        a half-wired ``web_search``."""
        if not tool.name:
            raise ValueError("Tool must have a non-empty name")
        self._tools[tool.name] = tool

    def all(self) -> list[ToolPort]:
        return list(self._tools.values())

    def get(self, name: str) -> ToolPort | None:
        return self._tools.get(name)

    def list_for_character(self, character: Character) -> list[ToolPort]:
        allowed = set(character.allowed_tools)
        return [self._tools[n] for n in character.allowed_tools if n in self._tools] if allowed else []
