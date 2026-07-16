"""External prompt template loader.

Prompts live as plain-text files under ``data/prompts/`` so they can be
edited, reviewed and overridden in operations without touching code.
The loader keeps the templating engine minimal on purpose — Python-side
conditional logic (which sections to include based on context shape) is
expected to stay in Python; templates only own *wording* and *variable
substitution*.

See ``docs/PROMPT_TEMPLATE_GUIDE.md`` for the migration playbook.
"""

from kokoro_link.infrastructure.prompts.loader import (
    PromptLoader,
    PromptPackOverlayStatus,
    PromptTemplateNotFoundError,
    PromptVariableMissingError,
    get_default_loader,
    reset_default_loader_for_tests,
)

__all__ = [
    "PromptLoader",
    "PromptPackOverlayStatus",
    "PromptTemplateNotFoundError",
    "PromptVariableMissingError",
    "get_default_loader",
    "reset_default_loader_for_tests",
]
