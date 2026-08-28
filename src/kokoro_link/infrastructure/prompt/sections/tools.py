"""Tool-rail sections: what the character may call, and what came back.

Both blocks are rendered by ``tools_block`` / ``tool_outcomes_block``,
which the proactive and background surfaces share — this module only
binds them to the chat prompt's context and order.
"""

from kokoro_link.infrastructure.prompt.sections.context import (
    PromptSectionContext,
)
from kokoro_link.infrastructure.prompt.sections.registry import (
    PromptSection,
    section,
)
from kokoro_link.infrastructure.prompt.tool_outcomes_block import (
    render_tool_outcomes_block,
)
from kokoro_link.infrastructure.prompt.tools_block import render_tools_block


def _tools(ctx: PromptSectionContext) -> list[str]:
    return render_tools_block(
        list(ctx.tools.available_tools),
        forced_tool_name=ctx.tools.forced_tool_name,
    )


def _tool_outcomes(ctx: PromptSectionContext) -> list[str]:
    return render_tool_outcomes_block(list(ctx.tools.tool_outcomes))


SECTIONS: tuple[PromptSection, ...] = (
    section("tools", _tools),
    section("tool_outcomes", _tool_outcomes),
)
