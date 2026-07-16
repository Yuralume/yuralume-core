"""TDD for ``InMemoryToolRegistry`` hot-swap methods.

``unregister`` / ``replace`` back the runtime provider-settings sync of
the ``web_search`` tool. ``register``'s duplicate-name raise must stay
intact — only the new methods are permitted to overwrite.
"""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from kokoro_link.domain.value_objects.tool_call import ToolResult
from kokoro_link.infrastructure.tools.registry import InMemoryToolRegistry


class _Tool:
    def __init__(self, name: str, marker: str = "") -> None:
        self.name = name
        self.description = ""
        self.parameters_schema: Mapping[str, Any] = {}
        self.marker = marker

    async def invoke(self, ctx: Any) -> ToolResult:  # pragma: no cover
        return ToolResult.success(output_text=self.marker)


def test_register_still_raises_on_duplicate() -> None:
    registry = InMemoryToolRegistry([_Tool("web_search")])
    with pytest.raises(ValueError):
        registry.register(_Tool("web_search"))


def test_unregister_removes_tool() -> None:
    registry = InMemoryToolRegistry([_Tool("web_search")])
    registry.unregister("web_search")
    assert registry.get("web_search") is None


def test_unregister_unknown_is_noop() -> None:
    registry = InMemoryToolRegistry([_Tool("web_fetch")])
    registry.unregister("web_search")  # no raise
    assert registry.get("web_fetch") is not None


def test_replace_overwrites_existing() -> None:
    registry = InMemoryToolRegistry([_Tool("web_search", marker="old")])
    registry.replace(_Tool("web_search", marker="new"))
    tool = registry.get("web_search")
    assert tool is not None
    assert tool.marker == "new"  # type: ignore[attr-defined]


def test_replace_adds_when_absent() -> None:
    registry = InMemoryToolRegistry([])
    registry.replace(_Tool("web_search"))
    assert registry.get("web_search") is not None


def test_replace_rejects_empty_name() -> None:
    registry = InMemoryToolRegistry([])
    with pytest.raises(ValueError):
        registry.replace(_Tool(""))
