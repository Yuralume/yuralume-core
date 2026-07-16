"""Shared prompt policy for character knowledge boundaries.

The actual prompt text now lives in
``data/prompts/shared/role_boundary.txt`` so it can be edited and
reviewed without touching code. This module is a thin facade that
preserves the historical ``render_role_knowledge_boundary_lines()``
shape for the chat / proactive / schedule / feed callers that already
splice it into their ``sections: list[str]`` builders.
"""

from __future__ import annotations

from kokoro_link.infrastructure.prompts import get_default_loader

_TEMPLATE_NAME = "shared/role_boundary"


def render_role_knowledge_boundary_lines() -> list[str]:
    """Return a reusable Chinese prompt block for role-scope honesty."""
    return get_default_loader().render_lines(_TEMPLATE_NAME)
