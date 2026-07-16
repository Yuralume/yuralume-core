"""Tool-use value objects.

A ``ToolCall`` is the character's *intent* to invoke a tool — produced
by the chat model as JSON, parsed and validated at the application
boundary. A ``ToolResult`` is the structured return value after the
orchestrator runs the tool; it carries both human-readable text (fed
back into the next LLM turn) and zero-or-more ``ToolAttachment`` items
(images / audio / files that need to be delivered to the user outside
the text channel).

Keeping these as frozen domain VOs — not Pydantic DTOs — means the
application layer can reason about tool results without caring which
transport carried them in (chat JSON, proactive decider output, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class ToolCall:
    """The character asked to run ``name`` with ``arguments``.

    ``call_id`` is optional because text-based tool protocols (our
    chosen transport) don't always produce one — we generate a UUID at
    orchestration time when missing.
    """

    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    call_id: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("ToolCall.name must be non-empty")
        object.__setattr__(self, "name", self.name.strip())


@dataclass(frozen=True, slots=True)
class ToolAttachment:
    """A non-text payload emitted by a tool invocation.

    ``kind`` is one of ``"image"`` / ``"audio"`` / ``"file"`` — kept as
    a free-form string so new types (video, 3D model) can be added
    without touching the domain layer.

    ``url`` is the **public-facing** URL (served by FastAPI's static
    mount). Tools should write files to ``uploads/...`` and return the
    corresponding ``/uploads/...`` URL so the frontend and messaging
    adapters can reference it without re-uploading.
    """

    kind: str
    url: str
    mime_type: str = "application/octet-stream"
    caption: str | None = None

    def __post_init__(self) -> None:
        if not self.kind:
            raise ValueError("ToolAttachment.kind must be non-empty")
        if not self.url:
            raise ValueError("ToolAttachment.url must be non-empty")


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The structured outcome of a tool invocation.

    - ``ok=True`` means the tool returned usefully; ``output_text`` is
      fed back into the next LLM turn so the model can incorporate the
      result into its final reply (e.g. "this is the image I just
      painted of me in the rain").
    - ``ok=False`` signals a tool-level error the orchestrator should
      log and surface to the model as a failure note — the model is
      still asked to respond (usually apologising to the user) rather
      than silently swallowing the problem.
    """

    ok: bool
    output_text: str = ""
    attachments: tuple[ToolAttachment, ...] = ()
    error: str | None = None

    @classmethod
    def success(
        cls,
        output_text: str = "",
        *,
        attachments: tuple[ToolAttachment, ...] | list[ToolAttachment] | None = None,
    ) -> "ToolResult":
        return cls(
            ok=True,
            output_text=output_text,
            attachments=tuple(attachments or ()),
        )

    @classmethod
    def failure(cls, error: str) -> "ToolResult":
        return cls(ok=False, error=error)
